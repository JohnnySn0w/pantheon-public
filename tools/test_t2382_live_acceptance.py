"""Fixture-only guard tests for the T-2382 live acceptance recorder.

The five approved live-leg method names remain present as provider-gated
wrappers, but this module never touches live providers, tmux sessions, systemd
accounts, or paid APIs.  Behavioral coverage uses local fixtures and fake
command responses against the recorder's current observation/finalization
interfaces.
"""

from __future__ import annotations

import json
import hashlib
import os
import pwd
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools import mailbox as pantheon_mailbox
from tools.claude_quota_probe import (
    PROBE_SCHEMA as CLAUDE_QUOTA_PROBE_SCHEMA,
    PROMPT as CLAUDE_QUOTA_PROBE_PROMPT,
    probe_session_id as claude_probe_session_id,
    probe_tmux_target as claude_probe_tmux_target,
)
from tools.claude_statusline_capture import SCHEMA as CLAUDE_STATUSLINE_SCHEMA
from tools.t2382_live_acceptance import (
    LEG_BATCH,
    LEG_CLAUDE,
    LEG_CODEX,
    LEG_FORCED_CAP,
    LEG_SWAP,
    SOURCE_HASH_PATHS,
    SYSTEMD_PROPERTIES,
    ZERO_HASH,
    AcceptanceRefused,
    AcceptanceSkipped,
    TraceRecorder,
    finalize_trace,
    initialize_trace,
    install_quota_overlay,
    consume_correlated_probe,
    observe_batch_result,
    observe_deployed_source,
    observe_forced_cap_phase,
    observe_mailbox_phase,
    observe_quota,
    observe_quota_overlay,
    observe_planned_preparing,
    observe_primary_quota_probe,
    observe_runtime_mailbox_result,
    observe_runtime_state,
    observe_service_status,
    observe_swap_phase,
    parse_leg,
    require_live_gates,
    remove_quota_overlay,
    _source_manifest_hash,
    _event_hash,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class EntrypointTest(unittest.TestCase):
    def test_cli_runs_as_a_script_from_its_shipped_wrapper_path(self) -> None:
        completed = subprocess.run(
            (
                "/usr/bin/python3",
                str(REPO_ROOT / "tools" / "t2382_live_acceptance.py"),
                "--help",
            ),
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("quota-overlay", completed.stdout)
        self.assertIn("consume-probe", completed.stdout)


class FakeRunner:
    def __init__(
        self,
        responses: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]],
        *,
        tmux_sequences: Mapping[str, Sequence[str]] | None = None,
        tmux_server_pid: int = 123,
    ) -> None:
        self.responses = dict(responses)
        self.tmux_sequences = {
            target: list(outputs) for target, outputs in (tmux_sequences or {}).items()
        }
        self.tmux_server_pid = tmux_server_pid

    def __call__(
        self, command: tuple[str, ...] | list[str]
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        if key and key[0] in {"tmux", "runuser"}:
            if "#{pid}" in key:
                return subprocess.CompletedProcess(
                    key, 0, f"{self.tmux_server_pid}\n", ""
                )
            try:
                target = key[key.index("-t") + 1]
            except (ValueError, IndexError):
                target = ""
            outputs = self.tmux_sequences.get(target)
            if outputs:
                stdout = outputs.pop(0)
                return subprocess.CompletedProcess(key, 0, stdout, "")
        if key not in self.responses:
            return subprocess.CompletedProcess(key, 1, "", f"unexpected command: {key}")
        return self.responses[key]


class T2382LiveAcceptanceFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"
        self.account = pwd.getpwuid(os.geteuid()).pw_name

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_bytes(self, relative: str, raw: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def _rewrite_trace(
        self,
        *,
        leg: str,
        run_id: str,
        mutate: Any,
    ) -> None:
        path = self.evidence / run_id / leg / "trace.jsonl"
        documents = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        previous_hash = ZERO_HASH
        for document in documents:
            mutate(document)
            document["previous_hash"] = previous_hash
            document.pop("event_hash", None)
            document["event_hash"] = _event_hash(document)
            previous_hash = document["event_hash"]
        path.write_text(
            "".join(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
                for document in documents
            ),
            encoding="utf-8",
        )

    def _write_json(self, relative: str, value: Mapping[str, object]) -> Path:
        return self._write(relative, json.dumps(value, sort_keys=True))

    def _repo(self) -> tuple[Path, str]:
        repo = self.root / f"repo-{len(list(self.root.glob('repo-*')))}"
        subprocess.run(("git", "init", str(repo)), check=True, capture_output=True)
        subprocess.run(
            ("git", "-C", str(repo), "checkout", "-b", "agent/T-2382"),
            check=True,
            capture_output=True,
        )
        unit_body = (
            "[Service]\n"
            "Type=simple\n"
            "User=recovery\n"
            "WorkingDirectory=/fixture/repo\n"
            "ExecStart=/fixture/repo/tools/persona_bringup.py consume --persona recovery\n"
            "Restart=always\n"
        )
        for relative in SOURCE_HASH_PATHS:
            text = unit_body if relative.endswith(".service") else f"{relative}\n"
            self._write_repo(repo, relative, text)
        subprocess.run(("git", "-C", str(repo), "add", "."), check=True, capture_output=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "seed",
            ),
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.repo = repo
        return repo, head

    def _write_repo(self, repo: Path, relative: str, text: str) -> Path:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _runner(
        self,
        repo: Path,
        installed_unit: Path,
        *,
        service_result: str = "success",
        service_status: str = "0",
        exec_code: str = "1",
        exit_tick: str = "10",
        account: str | None = None,
        service_started_at: datetime = NOW,
        tmux_sequences: Mapping[str, Sequence[str]] | None = None,
        tmux_server_pid: int = 123,
    ) -> FakeRunner:
        account = account or self.account
        unit = "pantheon-persona-consume@recovery.service"
        service_exited_at = service_started_at + timedelta(seconds=1)
        service_values = {
            "Result": service_result,
            "ExecMainStatus": service_status,
            "ExecMainCode": exec_code,
            "MainPID": "0",
            "ExecMainPID": "4321",
            "InvocationID": "fixture-invocation",
            "FragmentPath": str(installed_unit.resolve()),
            "ExecStart": f"{repo.resolve()}/tools/persona_bringup.py consume --persona recovery",
            "WorkingDirectory": str(repo.resolve()),
            "ExecMainStartTimestamp": service_started_at.strftime(
                "%a %Y-%m-%d %H:%M:%S UTC"
            ),
            "ExecMainExitTimestamp": service_exited_at.strftime(
                "%a %Y-%m-%d %H:%M:%S UTC"
            ),
            "ExecMainStartTimestampMonotonic": "1",
            "ExecMainExitTimestampMonotonic": exit_tick,
            "ActiveState": "inactive",
            "SubState": "dead",
        }
        systemctl = [
            "systemctl",
            "--user",
            f"--machine={account}@.host",
            "show",
            unit,
        ]
        systemctl.extend(f"--property={name}" for name in SYSTEMD_PROPERTIES)
        systemctl.append("--no-pager")
        return FakeRunner(
            {
                (
                    "git",
                    "-C",
                    str(repo.resolve()),
                    "rev-parse",
                    "--show-toplevel",
                ): subprocess.CompletedProcess(
                    ("git",), 0, str(repo.resolve()) + "\n", ""
                ),
                (
                    "git",
                    "-C",
                    str(repo.resolve()),
                    "rev-parse",
                    "--verify",
                    "HEAD",
                ): subprocess.CompletedProcess(
                    ("git",), 0, self.reviewed + "\n", ""
                ),
                (
                    "git",
                    "-C",
                    str(repo.resolve()),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ): subprocess.CompletedProcess(("git",), 0, "", ""),
                tuple(systemctl): subprocess.CompletedProcess(
                    ("systemctl",),
                    0,
                    "".join(f"{key}={service_values[key]}\n" for key in SYSTEMD_PROPERTIES),
                    "",
                ),
            },
            tmux_sequences=tmux_sequences,
            tmux_server_pid=tmux_server_pid,
        )

    def _recorder(self, leg: str, run_id: str) -> TraceRecorder:
        recorder = TraceRecorder(self.evidence, leg=leg, run_id=run_id)
        initialize_trace(recorder, operator="unit-test", purpose=leg)
        return recorder

    def _deployed(
        self,
        recorder: TraceRecorder,
        repo: Path,
        reviewed: str,
        runner: FakeRunner,
    ) -> Path:
        source_unit = repo / "tools/systemd/pantheon-persona-consume@.service"
        installed = self._write(
            f"installed/{recorder.leg}-{recorder.root.parent.name}.service",
            source_unit.read_text(encoding="utf-8"),
        )
        for key, value in list(runner.responses.items()):
            if key and key[0] == "systemctl":
                values = dict(
                    line.split("=", 1)
                    for line in value.stdout.splitlines()
                    if "=" in line
                )
                values["FragmentPath"] = str(installed.resolve())
                values["WorkingDirectory"] = str(repo.resolve())
                runner.responses[key] = subprocess.CompletedProcess(
                    key,
                    value.returncode,
                    "".join(
                        f"{name}={values.get(name, '')}\n" for name in SYSTEMD_PROPERTIES
                    ),
                    value.stderr,
                )
        observe_deployed_source(
            recorder,
            deployed_repo=repo,
            reviewed_revision=reviewed,
            installed_unit=installed,
            source_unit_relative="tools/systemd/pantheon-persona-consume@.service",
            unit="pantheon-persona-consume@recovery.service",
            runner=runner,
        )
        observe_service_status(
            recorder,
            account=self.account,
            unit="pantheon-persona-consume@recovery.service",
            deployed_repo=repo,
            installed_unit=installed,
            persona="recovery",
            runner=runner,
        )
        return installed

    def _proc_root(
        self,
        entries: Mapping[int, Sequence[str]],
        *,
        executable: Path | None = None,
        start_times: Mapping[int, datetime] | None = None,
        cgroups: Mapping[int, str] | None = None,
    ) -> Path:
        proc_root = self.root / f"proc-{len(list(self.root.glob('proc-*')))}"
        exe = executable or self._write(f"bin/exe-{len(list(self.root.glob('bin/exe-*')))}", "exe")
        cwd = self.root / "cwd"
        cwd.mkdir(exist_ok=True)
        proc_root.mkdir(parents=True, exist_ok=True)
        boot = NOW - timedelta(days=1)
        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        (proc_root / "stat").write_text(
            f"cpu  0 0 0 0 0 0 0 0 0 0\nbtime {int(boot.timestamp())}\n",
            encoding="utf-8",
        )
        for pid, argv in entries.items():
            pid_dir = proc_root / str(pid)
            pid_dir.mkdir(parents=True)
            (pid_dir / "cmdline").write_bytes(
                b"\0".join(item.encode("utf-8") for item in argv) + b"\0"
            )
            (pid_dir / "exe").symlink_to(exe)
            (pid_dir / "cwd").symlink_to(cwd)
            (pid_dir / "cgroup").write_text(
                (cgroups or {}).get(
                    pid, "0::/user.slice/pantheon-runtime-tmux.scope\n"
                ),
                encoding="utf-8",
            )
            started = (start_times or {}).get(pid, NOW + timedelta(seconds=1))
            start_ticks = int((started - boot).total_seconds() * ticks_per_second)
            (pid_dir / "stat").write_text(
                f"{pid} (provider) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 {start_ticks} 0\n",
                encoding="utf-8",
            )
        return proc_root

    def _runtime_state(
        self,
        name: str,
        *,
        persona: str = "recovery",
        provider: str = "codex",
        tmux_target: str = "pantheon-recovery:harness",
        executable: Path | None = None,
        missing_provenance: bool = False,
        revision: str | None = None,
        source_hashes: Mapping[str, str] | None = None,
        manifest_hash: str | None = None,
        runtime_start_boundary_at: datetime = NOW,
        runtime_start_observed_at: datetime = NOW + timedelta(seconds=1),
    ) -> Path:
        exe = executable or self._write(f"runtime/{name}-exe", "exe")
        payload: dict[str, object] = {
            "schema": "pantheon-runtime-state/v1",
            "persona": persona,
            "harness": provider,
            "profile": "interactive",
            "executable": str(exe.resolve()),
            "tmux_target": tmux_target,
            "runtime_scope_unit": "pantheon-runtime-tmux.scope",
            "started_at": _iso(runtime_start_observed_at),
        }
        if not missing_provenance:
            repo = getattr(self, "repo")
            hashes = dict(
                source_hashes
                or {
                    relative: self._sha256(repo / relative)
                    for relative in SOURCE_HASH_PATHS
                }
            )
            payload.update(
                {
                    "controller_source_revision": revision or self.reviewed,
                    "controller_source_hashes": hashes,
                    "controller_source_manifest_sha256": (
                        manifest_hash or _source_manifest_hash(hashes)
                    ),
                    "runtime_start_boundary_at": _iso(runtime_start_boundary_at),
                    "runtime_start_observed_at": _iso(runtime_start_observed_at),
                }
            )
        return self._write_json(
            f"runtime/{name}.json",
            payload,
        )

    def _transition_state(self, name: str, **overrides: object) -> Path:
        payload: dict[str, object] = {
            "schema": "pantheon-runtime-transition/v1",
            "persona": "recovery",
            "trigger": "planned-quota",
            "phase": "completed",
            "outgoing_profile": "codex-interactive",
            "incoming_profile": "claude-interactive",
            "outgoing_harness": "codex",
            "incoming_harness": "claude",
            "runtime_swapped": True,
            "successor_read_ack": True,
            "successor_read_evidence": "read-ack",
            "handoff_or_graveyard": "handoff",
        }
        payload.update(overrides)
        return self._write_json(f"transition/{name}.json", payload)

    def _controller_evidence(
        self,
        name: str,
        *,
        transition_id: str,
        message_id: str,
        correlation_id: str,
        capped_post_breaker: bool = False,
    ) -> Path:
        records: list[dict[str, object]] = [
            {
                "schema": "pantheon-runtime-controller-evidence/v1",
                "transition_id": transition_id,
                "event": "breaker_opened",
                "capped_profile": "codex-interactive",
                "outgoing_harness": "codex",
            },
        ]
        if capped_post_breaker:
            records.append(
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "runtime_submission_attempt",
                    "profile": "codex-interactive",
                    "harness": "codex",
                    "message_id": message_id,
                    "correlation_id": correlation_id,
                }
            )
        records.extend(
            [
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "forced_evidence_durable",
                },
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "runtime_replaced",
                },
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "runtime_submission_attempt",
                    "profile": "claude-interactive",
                    "harness": "claude",
                    "message_id": message_id,
                    "correlation_id": correlation_id,
                },
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "runtime_submission_accepted",
                    "profile": "claude-interactive",
                    "harness": "claude",
                    "message_id": message_id,
                    "correlation_id": correlation_id,
                },
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "transition_id": transition_id,
                    "event": "successor_read_acknowledged",
                },
            ]
        )
        return self._write(
            f"controller/{name}.jsonl",
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        )

    def _quota_json(
        self,
        relative: str,
        *,
        provider: str,
        source: str,
        observed_at: str,
        remaining_percent: float,
        meter: str = "subscription",
    ) -> Path:
        reset = int((NOW + timedelta(days=2)).timestamp())
        if provider == "codex" and source == "app-server":
            payload: dict[str, object] = {
                "observed_at": observed_at,
                "rateLimits": {
                    "secondary": {
                        "remainingPercent": remaining_percent,
                        "windowDurationMins": 10080,
                        "resetsAt": reset,
                    }
                },
                "meter": meter,
            }
        else:
            payload = {
                "observed_at": observed_at,
                "limits": {
                    "weekly": {
                        "remaining_percent": remaining_percent,
                        "resets_at": _iso(NOW + timedelta(days=2)),
                    }
                },
                "meter": meter,
            }
        return self._write_json(relative, payload)

    def _mail_record(
        self,
        mailbox: Path,
        folder: str,
        message_id: str,
        correlation_id: str,
        claim_id: str | None = None,
    ) -> Path:
        if folder == "claim":
            path = mailbox / ".inflight" / str(claim_id) / message_id
        else:
            path = mailbox / folder / message_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"id": message_id, "payload": {"correlation_id": correlation_id}}),
            encoding="utf-8",
        )
        return path

    def _mail_sequence(
        self,
        recorder: TraceRecorder,
        mailbox: Path,
        *,
        message_id: str,
        correlation_id: str,
        claim_correlation_id: str | None = None,
    ) -> None:
        new = self._mail_record(mailbox, "new", message_id, correlation_id)
        observe_mailbox_phase(
            recorder,
            phase="mailbox_new",
            mailbox_root=mailbox,
            message_id=message_id,
            correlation_id=correlation_id,
        )
        claim = mailbox / ".inflight" / "claim" / message_id
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_bytes(new.read_bytes())
        new.unlink()
        observe_mailbox_phase(
            recorder,
            phase="runner_claim",
            mailbox_root=mailbox,
            message_id=message_id,
            claim_id="claim",
            correlation_id=claim_correlation_id or correlation_id,
        )
        cur = mailbox / "cur" / message_id
        cur.parent.mkdir(parents=True, exist_ok=True)
        cur.write_bytes(claim.read_bytes())
        claim.unlink()
        observe_mailbox_phase(
            recorder,
            phase="mailbox_cur",
            mailbox_root=mailbox,
            message_id=message_id,
            correlation_id=correlation_id,
        )

    def _batch_files(
        self,
        *,
        provider: str = "claude",
        profile: str = "batch-sdk",
        meter: str = "sdk-credit",
        mode: str = "batch",
        argv: Sequence[str] | None = None,
        provider_result: Mapping[str, object] | None = None,
        provider_billing: Mapping[str, object] | None = None,
    ) -> tuple[Path, Path]:
        index = len(list((self.root / "batch").glob("*"))) if (self.root / "batch").exists() else 0
        command_argv = self._claude_batch_argv() if argv is None else tuple(argv)
        result_payload = {
            "status": "completed",
            "returncode": 0,
            "harness": "claude",
            "command_argv": list(command_argv),
            "provider_billing": dict(
                {
                    "provider": "claude",
                    "api_key_source": "ANTHROPIC_API_KEY",
                    "total_cost_usd": 0.001,
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "source_events": ["system/init", "result"],
                }
                if provider_billing is None
                else provider_billing
            ),
        }
        if provider_result is not None:
            result_payload = dict(provider_result)
        manifest = self._write_json(
            f"batch/{index}-manifest.json",
            {
                "harness_runtime": {
                    "harnesses": {
                        "claude": {
                            "default_profile": "interactive-subscription",
                            "profiles": {
                                "interactive-subscription": {"mode": "interactive"},
                                "batch-sdk": {
                                    "mode": "batch",
                                    "meter": "sdk-credit",
                                    "executable": "/usr/bin/claude",
                                },
                            },
                        }
                    }
                }
            },
        )
        result = self._write_json(
            f"batch/{manifest.stem}-result.json",
            {
                "binding": {
                    "selected_harness": provider,
                    "launch": {
                        "profile": profile,
                        "meter": meter,
                        "mode": mode,
                        "executable": "/usr/bin/claude",
                    },
                },
                "result": dict(
                    result_payload
                ),
            },
        )
        return result, manifest

    def _claude_batch_argv(self) -> tuple[str, ...]:
        return (
            "/usr/bin/claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        )

    def _interactive_trace(
        self,
        *,
        leg_name: str,
        provider: str,
        quota_source: str,
        executable_name: str,
        argv_tail: Sequence[str],
        run_id: str,
        runtime_state_kwargs: Mapping[str, object] | None = None,
        process_started_at: datetime | None = None,
        service_started_at: datetime = NOW,
    ) -> None:
        repo, self.reviewed = self._repo()
        exe = self._write(f"bin/{run_id}-{executable_name}", "exe")
        runtime_state = self._runtime_state(
            f"{run_id}-{provider}",
            provider=provider,
            executable=exe,
            **dict(runtime_state_kwargs or {}),
        )
        proc_root = self._proc_root(
            {123: (str(exe.resolve()), *argv_tail)},
            executable=exe,
            start_times={123: process_started_at} if process_started_at else None,
        )
        runner = self._runner(
            repo,
            self.root / f"installed/unused-{run_id}.service",
            service_started_at=service_started_at,
            tmux_sequences={"pantheon-recovery:harness": ["%7\t123\t0\tprovider\n"]},
        )
        recorder = self._recorder(leg_name, run_id)
        self._deployed(recorder, repo, self.reviewed, runner)
        observe_runtime_state(
            recorder,
            account=self.account,
            provider=provider,
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=runtime_state,
            proc_root=proc_root,
            runner=runner,
        )
        quota = self._quota_json(
            f"quota/{run_id}-{provider}.json",
            provider=provider,
            source=quota_source,
            observed_at=_iso(NOW),
            remaining_percent=50.0,
        )
        observe_quota(
            recorder,
            provider=provider,
            quota_source=quota_source,
            profile="interactive",
            meter="subscription",
            quota_state_path=quota,
            now=NOW,
        )
        self._mail_sequence(
            recorder,
            self.root / f"mailbox-{run_id}",
            message_id=f"msg-{run_id}",
            correlation_id=f"corr-{run_id}",
        )

    def _finalize_valid_interactive(
        self,
        *,
        leg_name: str,
        provider: str,
        quota_source: str,
        executable_name: str,
        argv_tail: Sequence[str],
        run_id: str,
    ) -> None:
        self._interactive_trace(
            leg_name=leg_name,
            provider=provider,
            quota_source=quota_source,
            executable_name=executable_name,
            argv_tail=argv_tail,
            run_id=run_id,
        )
        result = finalize_trace(
            leg_name=leg_name,
            reviewed_revision=self.reviewed,
            evidence_root=self.evidence,
            run_id=run_id,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.leg, leg_name)

    def _finalize_valid_codex(self, run_id: str) -> None:
        self._finalize_valid_interactive(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id=run_id,
        )

    def _finalize_valid_claude(self, run_id: str) -> None:
        self._finalize_valid_interactive(
            leg_name=LEG_CLAUDE,
            provider="claude",
            quota_source="statusline",
            executable_name="claude",
            argv_tail=("--permission-mode", "default", "interactive"),
            run_id=run_id,
        )

    def _finalize_valid_swap(self, run_id: str) -> None:
        self._swap_trace(run_id)
        result = finalize_trace(
            leg_name=LEG_SWAP,
            reviewed_revision=self.reviewed,
            evidence_root=self.evidence,
            run_id=run_id,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.leg, LEG_SWAP)

    def _finalize_valid_forced_cap(self, run_id: str) -> None:
        self._forced_cap_trace(run_id)
        result = finalize_trace(
            leg_name=LEG_FORCED_CAP,
            reviewed_revision=self.reviewed,
            evidence_root=self.evidence,
            run_id=run_id,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.leg, LEG_FORCED_CAP)

    def _finalize_valid_batch(self, run_id: str) -> None:
        self._batch_trace(run_id)
        result = finalize_trace(
            leg_name=LEG_BATCH,
            reviewed_revision=self.reviewed,
            evidence_root=self.evidence,
            run_id=run_id,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.leg, LEG_BATCH)

    def test_acceptance_overlay_never_mutates_canonical_provider_state(self) -> None:
        run_id = "20260803T001000Z"
        state_dir = self.root / "overlay-state"
        canonical = self._write_json(
            "overlay-state/quota/claude-statusline.json",
            {
                "observed_at": _iso(NOW),
                "limits": {
                    "weekly": {
                        "remaining_percent": 74.0,
                        "resets_at": _iso(NOW + timedelta(days=2)),
                    }
                },
            },
        )
        original = canonical.read_bytes()
        overlay = state_dir / "acceptance" / run_id / "quota-overlay.json"
        receipt = install_quota_overlay(
            state_dir=state_dir,
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
            resets_at=NOW + timedelta(seconds=2),
            now=NOW,
        )
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)
        self.assertEqual(canonical.read_bytes(), original)

        recorder = self._recorder(LEG_SWAP, run_id)
        observe_quota_overlay(
            recorder,
            phase="quota_overlay_installed",
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
        )
        quota = observe_quota(
            recorder,
            provider="claude",
            quota_source="acceptance-overlay",
            profile="interactive",
            meter="subscription",
            quota_state_path=overlay,
            now=NOW,
        )
        self.assertTrue(quota.payload["fixture"])
        self.assertEqual(quota.payload["quota_source"], "acceptance-overlay")

        remove_quota_overlay(
            state_dir=state_dir,
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
            expected_sha256=str(receipt["overlay_sha256"]),
            now=NOW + timedelta(seconds=1),
        )
        observe_quota_overlay(
            recorder,
            phase="quota_overlay_removed",
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
        )
        self.assertFalse(overlay.exists())
        self.assertEqual(canonical.read_bytes(), original)

    def test_acceptance_overlay_rejects_timezone_naive_timestamps(self) -> None:
        run_id = "20260803T001000Z"
        state_dir = self.root / "overlay-naive"
        canonical = self._write_json(
            "overlay-naive/quota/claude-statusline.json",
            {"observed_at": _iso(NOW), "limits": {}},
        )
        overlay = state_dir / "acceptance" / run_id / "quota-overlay.json"

        with self.assertRaisesRegex(AcceptanceRefused, "must include a timezone"):
            install_quota_overlay(
                state_dir=state_dir,
                overlay_path=overlay,
                canonical_path=canonical,
                run_id=run_id,
                resets_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
                now=NOW,
            )

        self.assertFalse(overlay.exists())

    def test_correlated_probe_processes_only_target_from_mixed_batch(self) -> None:
        run_id = "20260803T001001Z"
        mailbox = self.root / "probe-mailbox"
        pantheon_mailbox.provision(mailbox)
        self._mail_record(mailbox, "new", "a-other", "corr-other-a")
        self._mail_record(mailbox, "new", "b-target", "corr-target")
        self._mail_record(mailbox, "new", "c-other", "corr-other-c")
        gate = self.root / "probe-state" / "acceptance" / run_id / "probe"
        outcome: dict[str, Any] = {}

        def _run() -> None:
            outcome.update(
                consume_correlated_probe(
                    inbox=mailbox,
                    message_id="b-target",
                    correlation_id="corr-target",
                    run_id=run_id,
                    gate_path=gate,
                    timeout_seconds=5.0,
                )
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        ready = gate / "ready.json"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not ready.is_file():
            time.sleep(0.02)
        self.assertTrue(ready.is_file(), "probe did not expose its live claim")
        claims = tuple((mailbox / ".inflight").glob("*/b-target"))
        self.assertEqual(len(claims), 1)
        (gate / "release.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "message_id": "b-target",
                    "correlation_id": "corr-target",
                }
            ),
            encoding="utf-8",
        )
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["stats"]["processed"], ["b-target"])
        self.assertTrue((mailbox / "cur" / "b-target").is_file())
        for other in ("a-other", "c-other"):
            self.assertTrue((mailbox / "new" / other).is_file())
            self.assertFalse((mailbox / "cur" / other).exists())
            self.assertFalse(tuple((mailbox / ".inflight").glob(f"*/{other}")))
        attempts = mailbox / ".attempts"
        self.assertFalse((attempts / "a-other").exists())
        self.assertFalse((attempts / "c-other").exists())

    def test_correlated_probe_timeout_is_explicit_and_requeues_target(self) -> None:
        run_id = "20260803T001002Z"
        mailbox = self.root / "probe-timeout-mailbox"
        pantheon_mailbox.provision(mailbox)
        self._mail_record(mailbox, "new", "target", "corr-target")
        result = consume_correlated_probe(
            inbox=mailbox,
            message_id="target",
            correlation_id="corr-target",
            run_id=run_id,
            gate_path=self.root / "probe-timeout" / "acceptance" / run_id / "probe",
            timeout_seconds=1.0,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "timeout")
        self.assertTrue((mailbox / "new" / "target").is_file())
        self.assertFalse((mailbox / "cur" / "target").exists())
        self.assertFalse((mailbox / ".attempts" / "target").exists())

    def _finalize_live_if_gated(self, leg_name: str) -> None:
        leg = parse_leg(leg_name)
        try:
            require_live_gates(leg, os.environ)
        except AcceptanceSkipped as exc:
            self.skipTest(str(exc))
        reviewed = os.environ.get("T2382_LIVE_REVIEWED_REVISION")
        if not reviewed:
            self.skipTest("missing T2382_LIVE_REVIEWED_REVISION for gated live finalize")
        run_id = os.environ.get("T2382_LIVE_RUN_ID")
        if not run_id:
            self.skipTest("missing T2382_LIVE_RUN_ID for gated live finalize")
        evidence_root = Path(
            os.environ.get(
                "T2382_LIVE_EVIDENCE_ROOT",
                "/home/codex/.local/state/pantheon/t2382-live-proof",
            )
        )
        result = finalize_trace(
            leg_name=leg.name,
            reviewed_revision=reviewed,
            evidence_root=evidence_root,
            run_id=run_id,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.leg, leg.name)

    def test_live_codex_interactive_tmux_quota_and_ack(self) -> None:
        self._finalize_live_if_gated(LEG_CODEX)

    def test_live_claude_interactive_tmux_quota_and_ack(self) -> None:
        self._finalize_live_if_gated(LEG_CLAUDE)

    def test_live_same_unit_planned_harness_swap_preserves_persona_and_tmux(self) -> None:
        self._finalize_live_if_gated(LEG_SWAP)

    def test_live_forced_cap_requeues_and_bootstraps_from_graveyard_receipt(self) -> None:
        self._finalize_live_if_gated(LEG_FORCED_CAP)

    def test_live_explicit_claude_batch_sdk_is_nondefault_and_separately_metered(self) -> None:
        self._finalize_live_if_gated(LEG_BATCH)

    def test_fixture_refuses_cross_account_systemd_binding_and_incomplete_structural_exit(self) -> None:
        self._finalize_valid_codex("20260803T000000Z")

        repo, self.reviewed = self._repo()
        installed = self._write(
            "installed/wrong-account.service",
            (repo / "tools/systemd/pantheon-persona-consume@.service").read_text(
                encoding="utf-8"
            ),
        )
        wrong_runner = self._runner(
            repo,
            installed,
            account="definitely-not-current",
        )
        wrong_key = next(key for key in wrong_runner.responses if key[0] == "systemctl")
        wrong_stdout = wrong_runner.responses[wrong_key].stdout.replace(
            f"WorkingDirectory={repo.resolve()}",
            f"WorkingDirectory={self.root / 'other-account-repo'}",
        )
        wrong_runner.responses[wrong_key] = subprocess.CompletedProcess(
            wrong_key, 0, wrong_stdout, ""
        )
        with self.assertRaisesRegex(AcceptanceRefused, "WorkingDirectory"):
            observe_service_status(
                self._recorder(LEG_CODEX, "20260803T000016Z"),
                account="definitely-not-current",
                unit="pantheon-persona-consume@recovery.service",
                deployed_repo=repo,
                installed_unit=installed,
                persona="recovery",
                runner=wrong_runner,
            )

        recorder = self._recorder(LEG_CODEX, "20260803T000001Z")
        incomplete_runner = self._runner(repo, installed, exec_code="0")
        self._deployed(recorder, repo, self.reviewed, incomplete_runner)
        with self.assertRaisesRegex(AcceptanceRefused, "structurally exited"):
            finalize_trace(
                leg_name=LEG_CODEX,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000001Z",
            )

    def test_fixture_refuses_stale_quota_and_mismatched_mailbox_correlation(self) -> None:
        self._finalize_valid_claude("20260803T000002Z")

        stale = self._quota_json(
            "quota/stale.json",
            provider="claude",
            source="statusline",
            observed_at=_iso(NOW - timedelta(minutes=20)),
            remaining_percent=50.0,
        )
        with self.assertRaisesRegex(AcceptanceRefused, "not fresh"):
            observe_quota(
                self._recorder(LEG_CLAUDE, "20260803T000003Z"),
                provider="claude",
                quota_source="statusline",
                profile="interactive",
                meter="subscription",
                quota_state_path=stale,
                now=NOW,
            )

        repo, self.reviewed = self._repo()
        exe = self._write("bin/claude", "exe")
        runtime_state = self._runtime_state("claude", provider="claude", executable=exe)
        proc_root = self._proc_root(
            {123: (str(exe.resolve()), "--permission-mode", "default", "interactive")},
            executable=exe,
        )
        runner = self._runner(
            repo,
            self.root / "installed/unused.service",
            tmux_sequences={
                "pantheon-recovery:harness": ["%7\t123\t0\tcodex\n"],
            },
        )
        recorder = self._recorder(LEG_CLAUDE, "20260803T000004Z")
        self._deployed(recorder, repo, self.reviewed, runner)
        observe_runtime_state(
            recorder,
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=runtime_state,
            proc_root=proc_root,
            runner=runner,
        )
        quota = self._quota_json(
            "quota/fresh-claude.json",
            provider="claude",
            source="statusline",
            observed_at=_iso(NOW),
            remaining_percent=50.0,
        )
        observe_quota(
            recorder,
            provider="claude",
            quota_source="statusline",
            profile="interactive",
            meter="subscription",
            quota_state_path=quota,
            now=NOW,
        )
        self._mail_sequence(
            recorder,
            self.root / "mailbox-correlation",
            message_id="msg",
            correlation_id="corr-a",
            claim_correlation_id="corr-b",
        )
        with self.assertRaisesRegex(AcceptanceRefused, "correlation identity mismatch"):
            finalize_trace(
                leg_name=LEG_CLAUDE,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000004Z",
            )

    def test_runtime_accepts_tmux_spawn_pane_when_server_scope_matches(self) -> None:
        repo, self.reviewed = self._repo()
        exe = self._write("bin/scoped-provider", "exe")
        runtime_state = self._runtime_state(
            "scoped-provider", provider="codex", executable=exe
        )
        proc_root = self._proc_root(
            {
                123: (str(exe.resolve()), "interactive"),
                456: ("tmux", "server"),
            },
            executable=exe,
            cgroups={
                123: "0::/user.slice/tmux-spawn-deadbeef.scope\n",
                456: "0::/user.slice/pantheon-runtime-tmux.scope\n",
            },
        )
        runner = self._runner(
            repo,
            self.root / "installed/unused-scoped.service",
            tmux_sequences={
                "pantheon-recovery:harness": ["%7\t123\t0\tcodex\n"]
            },
            tmux_server_pid=456,
        )
        recorder = self._recorder(LEG_CODEX, "20260804T000001Z")
        self._deployed(recorder, repo, self.reviewed, runner)
        event = observe_runtime_state(
            recorder,
            account=self.account,
            provider="codex",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=runtime_state,
            proc_root=proc_root,
            runner=runner,
        )
        self.assertEqual(event.payload["server_pid"], 456)
        self.assertIn("tmux-spawn-deadbeef.scope", event.payload["cgroup"][0])

    def test_claude_runtime_requires_exact_explicit_default_permission_mode(self) -> None:
        repo, self.reviewed = self._repo()
        exe = self._write("bin/permission-mode-claude", "exe")
        runtime_state = self._runtime_state(
            "permission-mode-claude", provider="claude", executable=exe
        )
        cases = {
            "missing": (("interactive",), "exactly one explicit"),
            "duplicate": (
                (
                    "--permission-mode",
                    "default",
                    "--permission-mode",
                    "default",
                ),
                "exactly one explicit",
            ),
            "widened": (
                ("--permission-mode", "bypassPermissions"),
                "not explicit default",
            ),
        }
        for index, (label, (tail, error)) in enumerate(cases.items(), start=30):
            with self.subTest(label=label):
                proc_root = self._proc_root(
                    {123: (str(exe.resolve()), *tail)}, executable=exe
                )
                runner = self._runner(
                    repo,
                    self.root / f"installed/unused-permission-{label}.service",
                    tmux_sequences={
                        "pantheon-recovery:harness": ["%7\t123\t0\tclaude\n"]
                    },
                )
                with self.assertRaisesRegex(AcceptanceRefused, error):
                    observe_runtime_state(
                        self._recorder(LEG_CLAUDE, f"20260804T0000{index}Z"),
                        account=self.account,
                        provider="claude",
                        persona="recovery",
                        unit="pantheon-persona-consume@recovery.service",
                        tmux_target="pantheon-recovery:harness",
                        runtime_state_path=runtime_state,
                        proc_root=proc_root,
                        runner=runner,
                    )

        proc_root = self._proc_root(
            {
                123: (
                    str(exe.resolve()),
                    "--permission-mode",
                    "default",
                    "interactive",
                )
            },
            executable=exe,
        )
        runner = self._runner(
            repo,
            self.root / "installed/unused-permission-default.service",
            tmux_sequences={"pantheon-recovery:harness": ["%7\t123\t0\tclaude\n"]},
        )
        event = observe_runtime_state(
            self._recorder(LEG_CLAUDE, "20260804T000040Z"),
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=runtime_state,
            proc_root=proc_root,
            runner=runner,
        )
        self.assertEqual(
            event.payload["argv"].count("--permission-mode"), 1
        )

    def test_runtime_refuses_server_cgroup_scope_mismatch(self) -> None:
        repo, self.reviewed = self._repo()
        exe = self._write("bin/unscoped-provider", "exe")
        runtime_state = self._runtime_state(
            "unscoped-provider", provider="codex", executable=exe
        )
        proc_root = self._proc_root(
            {
                123: (str(exe.resolve()), "interactive"),
                456: ("tmux", "server"),
            },
            executable=exe,
            cgroups={
                123: "0::/user.slice/tmux-spawn-deadbeef.scope\n",
                456: "0::/user.slice/external.service\n",
            },
        )
        runner = self._runner(
            repo,
            self.root / "installed/unused-unscoped.service",
            tmux_sequences={
                "pantheon-recovery:harness": ["%7\t123\t0\tcodex\n"]
            },
            tmux_server_pid=456,
        )
        recorder = self._recorder(LEG_CODEX, "20260804T000002Z")
        self._deployed(recorder, repo, self.reviewed, runner)
        with self.assertRaisesRegex(AcceptanceRefused, "server cgroup"):
            observe_runtime_state(
                recorder,
                account=self.account,
                provider="codex",
                persona="recovery",
                unit="pantheon-persona-consume@recovery.service",
                tmux_target="pantheon-recovery:harness",
                runtime_state_path=runtime_state,
                proc_root=proc_root,
                runner=runner,
            )

    def test_finalizer_requires_complete_runtime_server_scope_evidence(self) -> None:
        cases = {
            "missing-server-pid": lambda payload: payload.pop("server_pid", None),
            "missing-runtime-scope": lambda payload: payload["runtime_state"].pop(
                "runtime_scope_unit", None
            ),
            "mismatched-server-cgroup": lambda payload: payload.__setitem__(
                "server_cgroup", ["0::/user.slice/external.service"]
            ),
        }
        for index, (label, mutation) in enumerate(cases.items(), start=10):
            with self.subTest(label=label):
                run_id = f"20260804T0000{index}Z"
                self._interactive_trace(
                    leg_name=LEG_CODEX,
                    provider="codex",
                    quota_source="app-server",
                    executable_name="codex",
                    argv_tail=("interactive",),
                    run_id=run_id,
                )

                def mutate(document: dict[str, Any]) -> None:
                    if document.get("phase") == "runtime_state":
                        mutation(document["payload"])

                self._rewrite_trace(leg=LEG_CODEX, run_id=run_id, mutate=mutate)
                with self.assertRaises(AcceptanceRefused):
                    finalize_trace(
                        leg_name=LEG_CODEX,
                        reviewed_revision=self.reviewed,
                        evidence_root=self.evidence,
                        run_id=run_id,
                    )

    def test_swap_finalizer_consumes_nested_server_scope_evidence(self) -> None:
        run_id = "20260804T000020Z"
        self._swap_trace(run_id)

        def mutate(document: dict[str, Any]) -> None:
            if document.get("phase") == "swap_before":
                document["payload"]["runtime"]["server_cgroup"] = [
                    "0::/user.slice/external.service"
                ]

        self._rewrite_trace(leg=LEG_SWAP, run_id=run_id, mutate=mutate)
        with self.assertRaisesRegex(AcceptanceRefused, "server cgroup"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id=run_id,
            )

    def test_fixture_refuses_missing_or_mismatched_runtime_provenance(self) -> None:
        self._finalize_valid_codex("20260803T000018Z")

        self._interactive_trace(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id="20260803T000019Z",
            runtime_state_kwargs={"missing_provenance": True},
        )
        with self.assertRaisesRegex(AcceptanceRefused, "source revision"):
            finalize_trace(
                leg_name=LEG_CODEX,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000019Z",
            )

        self._interactive_trace(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id="20260803T000020Z",
            runtime_state_kwargs={"revision": "2" * 40},
        )
        with self.assertRaisesRegex(AcceptanceRefused, "source revision mismatch"):
            finalize_trace(
                leg_name=LEG_CODEX,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000020Z",
            )

        source_hashes = {
            relative: self._sha256(self.repo / relative)
            for relative in SOURCE_HASH_PATHS
        }
        source_hashes["tools/runtime_controller.py"] = "3" * 64
        self._interactive_trace(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id="20260803T000021Z",
            runtime_state_kwargs={"source_hashes": source_hashes},
        )
        with self.assertRaisesRegex(AcceptanceRefused, "source hash set mismatch"):
            finalize_trace(
                leg_name=LEG_CODEX,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000021Z",
            )

    def test_fixture_refuses_stale_predeploy_runtime_process(self) -> None:
        self._interactive_trace(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id="20260803T000022Z",
            process_started_at=NOW - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(AcceptanceRefused, "predates controller start"):
            finalize_trace(
                leg_name=LEG_CODEX,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000022Z",
            )

    def test_fixture_accepts_reused_pane_after_later_service_wake(self) -> None:
        self._interactive_trace(
            leg_name=LEG_CODEX,
            provider="codex",
            quota_source="app-server",
            executable_name="codex",
            argv_tail=("interactive",),
            run_id="20260803T000023Z",
            process_started_at=NOW + timedelta(seconds=1),
            service_started_at=NOW + timedelta(minutes=5),
        )

        result = finalize_trace(
            leg_name=LEG_CODEX,
            reviewed_revision=self.reviewed,
            evidence_root=self.evidence,
            run_id="20260803T000023Z",
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.runtime.running_source_revision, self.reviewed)

    def _swap_trace(
        self,
        run_id: str,
        *,
        synthetic_label: bool = False,
        transition_mismatch: bool = False,
        duplicate_pid: bool = False,
        omit_removal_event: bool = False,
        recovery_fixture: bool = False,
        fixture_remaining: float = 5.0,
        runtime_result_persona: str = "recovery",
        probe_account: str | None = None,
        probe_tmux_target: str | None = None,
        recovery_observed_at: datetime | None = None,
        duplicate_runtime_ack: bool = False,
        duplicate_runtime_reply: bool = False,
    ) -> None:
        repo, self.reviewed = self._repo()
        claude = self._write("bin/swap-claude", "exe")
        codex = self._write("bin/swap-codex", "exe")
        pids = (301, 301, 302) if duplicate_pid else (301, 302, 303)
        if duplicate_pid:
            proc_entries: Mapping[int, Sequence[str]] = {
                301: (
                    str(claude.resolve()),
                    str(codex.resolve()),
                    "--permission-mode",
                    "default",
                    "interactive",
                ),
                302: (
                    str(claude.resolve()),
                    "--permission-mode",
                    "default",
                    "interactive",
                ),
            }
        else:
            proc_entries = {
                pids[0]: (str(claude.resolve()), "--permission-mode", "default", "interactive"),
                pids[1]: (str(codex.resolve()), "interactive"),
                pids[2]: (str(claude.resolve()), "--permission-mode", "default", "interactive"),
            }
        proc_entries = {**proc_entries, 999: ("tmux", "server")}
        proc_root = self._proc_root(proc_entries)
        runner = self._runner(
            repo,
            self.root / "installed/unused-swap.service",
            tmux_sequences={
                "pantheon-recovery:harness": [
                    f"%7\t{pids[0]}\t0\tclaude\n",
                    f"%7\t{pids[1]}\t0\tcodex\n",
                    f"%7\t{pids[2]}\t0\tclaude\n",
                ]
            },
            tmux_server_pid=999,
        )
        recorder = self._recorder(LEG_SWAP, run_id)
        self._deployed(recorder, repo, self.reviewed, runner)
        before_state = self._runtime_state("swap-before", provider="claude", executable=claude)
        after_state = self._runtime_state("swap-after", provider="codex", executable=codex)
        restore_state = self._runtime_state("swap-restore", provider="claude", executable=claude)
        first_phase = "swap_synthetic" if synthetic_label else "swap_before"
        observe_swap_phase(
            recorder,
            phase=first_phase,  # type: ignore[arg-type]
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=before_state,
            proc_root=proc_root,
            runner=runner,
        )

        state_dir = self.root / "state" / run_id
        canonical = self._write_json(
            f"state/{run_id}/quota/claude-statusline.json",
            {
                "observed_at": _iso(NOW),
                "limits": {
                    "weekly": {
                        "remaining_percent": 75.0,
                        "resets_at": _iso(NOW + timedelta(days=2)),
                    }
                },
            },
        )
        overlay = state_dir / "acceptance" / run_id / "quota-overlay.json"
        receipt = install_quota_overlay(
            state_dir=state_dir,
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
            resets_at=NOW + timedelta(seconds=1),
            now=NOW,
        )
        if fixture_remaining != 5.0:
            envelope = json.loads(overlay.read_text(encoding="utf-8"))
            envelope["payload"]["limits"]["weekly"][
                "remaining_percent"
            ] = fixture_remaining
            overlay.write_text(json.dumps(envelope), encoding="utf-8")
            overlay.chmod(0o600)
            receipt["overlay_sha256"] = hashlib.sha256(overlay.read_bytes()).hexdigest()
        observe_quota_overlay(
            recorder,
            phase="quota_overlay_installed",
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
        )
        observe_quota(
            recorder,
            provider="claude",
            quota_source="acceptance-overlay",
            profile="interactive",
            meter="subscription",
            quota_state_path=overlay,
            now=NOW,
        )
        preparing = self._transition_state(
            "swap-preparing",
            phase="preparing",
            outgoing_harness="claude",
            incoming_harness="codex",
            outgoing_profile="claude-interactive",
            incoming_profile="codex-interactive",
            handoff_requested=True,
            quota_evidence={
                "source": "acceptance-overlay",
                "fixture": "true",
                "run_id": run_id,
                "overlay_sha256": receipt["overlay_sha256"],
            },
        )
        observe_planned_preparing(recorder, transition_state_path=preparing)
        remove_quota_overlay(
            state_dir=state_dir,
            overlay_path=overlay,
            canonical_path=canonical,
            run_id=run_id,
            expected_sha256=str(receipt["overlay_sha256"]),
            now=NOW + timedelta(seconds=1),
        )
        if not omit_removal_event:
            observe_quota_overlay(
                recorder,
                phase="quota_overlay_removed",
                overlay_path=overlay,
                canonical_path=canonical,
                run_id=run_id,
            )
        planned = self._transition_state(
            "swap-planned",
            outgoing_harness="claude",
            incoming_harness="claude" if transition_mismatch else "codex",
            outgoing_profile="claude-interactive",
            incoming_profile="codex-interactive",
        )
        probe_submitted_at = NOW + timedelta(seconds=1)
        probe_observed_at = NOW + timedelta(seconds=2)
        probe_completed_at = NOW + timedelta(seconds=3)
        canonical.write_text(
            json.dumps(
                {
                    "schema": CLAUDE_STATUSLINE_SCHEMA,
                    "observed_at": _iso(probe_observed_at),
                    "session_id": claude_probe_session_id(
                        self.account, "recovery", run_id
                    ),
                    "rate_limits": {
                        "seven_day": {
                            "used_percentage": 25.0,
                            "resets_at": int((NOW + timedelta(days=2)).timestamp()),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        canonical_hash = hashlib.sha256(canonical.read_bytes()).hexdigest()
        probe_result = self._write_json(
            f"state/{run_id}/acceptance/{run_id}/primary-quota-probe/result.json",
            {
                "schema": CLAUDE_QUOTA_PROBE_SCHEMA,
                "status": "observed",
                "run_id": run_id,
                "account": probe_account or self.account,
                "persona": "recovery",
                "session_id": claude_probe_session_id(self.account, "recovery", run_id),
                "tmux_target": probe_tmux_target
                or claude_probe_tmux_target("recovery", run_id),
                "submitted_at": _iso(probe_submitted_at),
                "completed_at": _iso(probe_completed_at),
                "prompt_sha256": hashlib.sha256(
                    CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")
                ).hexdigest(),
                "statusline_path": str(canonical.resolve()),
                "statusline_sha256": canonical_hash,
                "cap_sentinel": None,
                "pong_observed": True,
            },
        )
        reset = self._transition_state(
            "swap-reset",
            trigger="quota-reset",
            outgoing_harness="codex",
            incoming_harness="claude",
            outgoing_profile="codex-interactive",
            incoming_profile="claude-interactive",
            fallback_active=False,
            quota_evidence={
                "probe_source": "statusline",
                "probe_authoritative_limit": "false",
                "probe_freshness": "fresh",
                "probe_observed_at": _iso(
                    recovery_observed_at or probe_observed_at
                ),
                "reset_passed_at": _iso(NOW + timedelta(seconds=1)),
                "probe_weekly_remaining_percent": "75.0",
                "probe_path": str(canonical.resolve()),
                "probe_sha256": canonical_hash,
                **({"probe_fixture": "true"} if recovery_fixture else {}),
            },
        )
        observe_swap_phase(
            recorder,
            phase="swap_after",
            account=self.account,
            provider="codex",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=after_state,
            transition_state_path=planned,
            proc_root=proc_root,
            runner=runner,
        )
        source_mailbox = self.root / "mailbox" / run_id / "recovery"
        reply_mailbox = self.root / "mailbox" / run_id / "codex"
        pantheon_mailbox.provision(source_mailbox)
        pantheon_mailbox.provision(reply_mailbox)
        correlation_id = f"t2382-{run_id}"
        expected_message = f"T-2382-PONG:{run_id}"
        message_id = pantheon_mailbox.send(
            source_mailbox,
            {
                "correlation_id": correlation_id,
                "task": "Return the acceptance nonce through the reply mailbox.",
            },
            "codex",
            subject=f"T-2382 runtime roundtrip {run_id}",
        )
        controller_evidence = self._write(
            f"state/{run_id}/controller-evidence.jsonl",
            json.dumps(
                {
                    "schema": "pantheon-runtime-controller-evidence/v1",
                    "event": "runtime_submission_accepted",
                    "persona": "recovery",
                    "recorded_at": _iso(datetime.now(timezone.utc) - timedelta(seconds=1)),
                    "harness": "codex",
                    "profile": "codex-interactive",
                    "message_id": message_id,
                    "correlation_id": correlation_id,
                    "returncode": 0,
                }
            )
            + "\n",
        )
        (source_mailbox / "new" / message_id).rename(
            source_mailbox / "cur" / message_id
        )
        ack_record = json.dumps(
            {
                "id": message_id,
                "ts_ns": time.time_ns(),
                "label": None,
                "pid": os.getpid(),
                "uid": os.geteuid(),
            }
        )
        (source_mailbox / ".acks.jsonl").write_text(
            ack_record + "\n" + (ack_record + "\n" if duplicate_runtime_ack else ""),
            encoding="utf-8",
        )
        reply_payload = {
            "kind": "persona_result",
            "correlation_id": correlation_id,
            "persona": runtime_result_persona,
            "role": "recovery",
            "harness": "codex",
            "status": "completed",
            "message": expected_message,
            "session_id": f"fixture-{run_id}",
        }
        pantheon_mailbox.send(
            reply_mailbox,
            reply_payload,
            "recovery",
            subject=f"result:{correlation_id}",
        )
        if duplicate_runtime_reply:
            pantheon_mailbox.send(
                reply_mailbox,
                reply_payload,
                "recovery",
                subject=f"result:{correlation_id}",
                dedupe_pending=False,
            )
        observe_runtime_mailbox_result(
            recorder,
            source_mailbox=source_mailbox,
            reply_mailbox=reply_mailbox,
            message_id=message_id,
            correlation_id=correlation_id,
            account=self.account,
            persona="recovery",
            harness="codex",
            expected_message=expected_message,
            controller_evidence_path=controller_evidence,
        )
        observe_primary_quota_probe(
            recorder,
            result_path=probe_result,
            canonical_path=canonical,
        )
        observe_swap_phase(
            recorder,
            phase="swap_restore",
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=restore_state,
            transition_state_path=reset,
            proc_root=proc_root,
            runner=runner,
        )

    def test_fixture_refuses_synthetic_swap_labels_transition_mismatch_and_duplicate_pid(self) -> None:
        self._finalize_valid_swap("20260803T000005Z")

        self._swap_trace("20260803T000004Z", synthetic_label=True)
        with self.assertRaisesRegex(AcceptanceRefused, "missing required trace phase: swap_before"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000004Z",
            )

        self._swap_trace("20260803T000006Z", transition_mismatch=True)
        with self.assertRaisesRegex(AcceptanceRefused, "planned swap transition mismatch"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000006Z",
            )

        self._swap_trace("20260803T000007Z", duplicate_pid=True)
        with self.assertRaisesRegex(AcceptanceRefused, "replace each runtime process"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000007Z",
            )

        self._swap_trace("20260803T001003Z", omit_removal_event=True)
        with self.assertRaisesRegex(
            AcceptanceRefused, "missing required trace phase: quota_overlay_removed"
        ):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001003Z",
            )

        self._swap_trace("20260803T001004Z", recovery_fixture=True)
        with self.assertRaisesRegex(AcceptanceRefused, "still carries fixture"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001004Z",
            )

        self._swap_trace("20260803T001005Z", fixture_remaining=4.0)
        with self.assertRaisesRegex(AcceptanceRefused, "fixed 5% reserve"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001005Z",
            )

        with self.assertRaisesRegex(
            AcceptanceRefused, "persona_result content mismatch"
        ):
            self._swap_trace(
                "20260803T001006Z", runtime_result_persona="forged-recovery"
            )

        with self.assertRaisesRegex(AcceptanceRefused, "acknowledgement"):
            self._swap_trace(
                "20260803T001007Z", duplicate_runtime_ack=True
            )
        with self.assertRaisesRegex(AcceptanceRefused, "one correlated persona_result"):
            self._swap_trace(
                "20260803T001008Z", duplicate_runtime_reply=True
            )

        self._swap_trace(
            "20260803T001009Z",
            recovery_observed_at=NOW + timedelta(seconds=30),
        )
        with self.assertRaisesRegex(AcceptanceRefused, "timestamp differs"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001009Z",
            )

        self._swap_trace("20260803T001010Z", probe_account="wrong-account")
        with self.assertRaisesRegex(AcceptanceRefused, "account identity mismatch"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001010Z",
            )

        self._swap_trace("20260803T001011Z", probe_tmux_target="wrong:probe.0")
        with self.assertRaisesRegex(AcceptanceRefused, "tmux_target identity mismatch"):
            finalize_trace(
                leg_name=LEG_SWAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T001011Z",
            )

    def _forced_cap_trace(
        self,
        run_id: str,
        *,
        message_correlation_mismatch: bool = False,
        retired_pid_live: bool = False,
        receipt_mismatch: bool = False,
        capped_post_breaker_ledger: bool = False,
    ) -> None:
        repo, self.reviewed = self._repo()
        codex = self._write("bin/forced-codex", "exe")
        claude = self._write("bin/forced-claude", "exe")
        proc_root = self._proc_root(
            {
                401: (str(codex.resolve()), "interactive"),
                402: (str(claude.resolve()), "--permission-mode", "default", "interactive"),
                999: ("tmux", "server"),
            }
        )
        runner = self._runner(
            repo,
            self.root / "installed/unused-forced.service",
            tmux_sequences={
                "pantheon-recovery:harness": [
                    "%7\t401\t0\tcodex\n",
                    "%7\t402\t0\tclaude\n",
                    "%7\t402\t0\tclaude\n",
                ]
            },
            tmux_server_pid=999,
        )
        recorder = self._recorder(LEG_FORCED_CAP, run_id)
        self._deployed(recorder, repo, self.reviewed, runner)
        mailbox = self.root / f"forced-mailbox-{run_id}"
        message_id = "cap-msg"
        correlation_id = "corr-a"
        transition_id = f"transition-{run_id}"
        new = self._mail_record(mailbox, "new", message_id, correlation_id)
        observe_mailbox_phase(
            recorder,
            phase="mailbox_new",
            mailbox_root=mailbox,
            message_id=message_id,
            correlation_id=correlation_id,
        )
        original_claim = mailbox / ".inflight" / "original" / message_id
        original_claim.parent.mkdir(parents=True, exist_ok=True)
        original_claim.write_bytes(new.read_bytes())
        new.unlink()
        observe_mailbox_phase(
            recorder,
            phase="runner_claim",
            mailbox_root=mailbox,
            message_id=message_id,
            claim_id="original",
            correlation_id=correlation_id,
        )
        before_state = self._runtime_state("forced-before", provider="codex", executable=codex)
        cap_state = self._runtime_state("forced-cap", provider="claude", executable=claude)
        transition_cap = self._transition_state(
            "forced-cap",
            transition_id=transition_id,
            trigger="forced-cap",
            phase="awaiting-successor-read",
            breaker_open=True,
        )
        transition_ack = self._transition_state(
            "forced-ack",
            transition_id=transition_id,
            trigger="forced-cap",
            phase="completed",
            breaker_open=True,
            successor_read_ack=True,
            successor_read_evidence="graveyard-read",
        )
        controller_evidence = self._controller_evidence(
            run_id,
            transition_id=transition_id,
            message_id=message_id,
            correlation_id=correlation_id,
            capped_post_breaker=capped_post_breaker_ledger,
        )
        observe_forced_cap_phase(
            recorder,
            phase="forced_before",
            message_id=message_id,
            correlation_id=correlation_id,
            account=self.account,
            provider="codex",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=before_state,
            proc_root=proc_root,
            runner=runner,
        )
        if not retired_pid_live:
            for child in (proc_root / "401").iterdir():
                child.unlink()
            (proc_root / "401").rmdir()
        observe_forced_cap_phase(
            recorder,
            phase="forced_cap",
            message_id=message_id,
            correlation_id=correlation_id,
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=cap_state,
            transition_state_path=transition_cap,
            retired_pid=401,
            proc_root=proc_root,
            runner=runner,
        )
        original_claim.unlink()
        requeue_corr = "corr-b" if message_correlation_mismatch else correlation_id
        self._mail_record(mailbox, "new", message_id, requeue_corr)
        observe_forced_cap_phase(
            recorder,
            phase="message_requeued",
            message_id=message_id,
            correlation_id=requeue_corr,
            mailbox_root=mailbox,
        )
        graveyard = self._write_json(
            f"graveyard/{run_id}.json",
            {"id": message_id, "payload": {"correlation_id": correlation_id}},
        )
        receipt_corr = "corr-b" if receipt_mismatch else correlation_id
        receipt = self._write_json(
            f"receipt/{run_id}.json",
            {
                "schema": "pantheon-forced-failover/v1",
                "message_id": message_id,
                "correlation_id": receipt_corr,
                "transition_id": transition_id,
                "graveyard_entry": str(graveyard.resolve()),
            },
        )
        observe_forced_cap_phase(
            recorder,
            phase="graveyard_receipt",
            message_id=message_id,
            correlation_id=correlation_id,
            receipt_path=receipt,
        )
        observe_forced_cap_phase(
            recorder,
            phase="receipt_read_ack",
            message_id=message_id,
            correlation_id=correlation_id,
            transition_state_path=transition_ack,
        )
        observe_forced_cap_phase(
            recorder,
            phase="fallback_retry",
            message_id=message_id,
            correlation_id=correlation_id,
            account=self.account,
            provider="claude",
            persona="recovery",
            unit="pantheon-persona-consume@recovery.service",
            tmux_target="pantheon-recovery:harness",
            runtime_state_path=cap_state,
            controller_evidence_path=controller_evidence,
            proc_root=proc_root,
            runner=runner,
        )
        retry_claim = mailbox / ".inflight" / "retry" / message_id
        retry_claim.parent.mkdir(parents=True, exist_ok=True)
        retry_claim.write_bytes((mailbox / "new" / message_id).read_bytes())
        (mailbox / "new" / message_id).unlink()
        observe_mailbox_phase(
            recorder,
            phase="runner_claim",
            mailbox_root=mailbox,
            message_id=message_id,
            claim_id="retry",
            correlation_id=correlation_id,
        )
        cur = mailbox / "cur" / message_id
        cur.parent.mkdir(parents=True, exist_ok=True)
        cur.write_bytes(retry_claim.read_bytes())
        retry_claim.unlink()
        observe_mailbox_phase(
            recorder,
            phase="mailbox_cur",
            mailbox_root=mailbox,
            message_id=message_id,
            correlation_id=correlation_id,
        )

    def test_fixture_refuses_forced_cap_correlation_retired_pid_and_receipt_mismatch(self) -> None:
        self._finalize_valid_forced_cap("20260803T000008Z")

        self._forced_cap_trace("20260803T000009Z", message_correlation_mismatch=True)
        with self.assertRaisesRegex(AcceptanceRefused, "message/correlation identity changed"):
            finalize_trace(
                leg_name=LEG_FORCED_CAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000009Z",
            )

        self._forced_cap_trace("20260803T000010Z", retired_pid_live=True)
        with self.assertRaisesRegex(AcceptanceRefused, "structurally retired"):
            finalize_trace(
                leg_name=LEG_FORCED_CAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000010Z",
            )

        self._forced_cap_trace("20260803T000011Z", receipt_mismatch=True)
        with self.assertRaisesRegex(AcceptanceRefused, "receipt correlation_id mismatch"):
            finalize_trace(
                leg_name=LEG_FORCED_CAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000011Z",
            )

        self._forced_cap_trace(
            "20260803T000017Z",
            capped_post_breaker_ledger=True,
        )
        with self.assertRaisesRegex(AcceptanceRefused, "post-breaker submission"):
            finalize_trace(
                leg_name=LEG_FORCED_CAP,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000017Z",
            )

    def _batch_trace(self, run_id: str, **batch_kwargs: Any) -> None:
        repo, self.reviewed = self._repo()
        runner = self._runner(repo, self.root / "installed/unused-batch.service")
        recorder = self._recorder(LEG_BATCH, run_id)
        self._deployed(recorder, repo, self.reviewed, runner)
        result, manifest = self._batch_files(**batch_kwargs)
        observe_batch_result(
            recorder,
            result_path=result,
            manifest_path=manifest,
        )

    def test_fixture_refuses_batch_label_spoof_missing_argv_and_empty_outcomes(self) -> None:
        self._finalize_valid_batch("20260803T000012Z")

        argv = self._claude_batch_argv()
        self._batch_trace("20260803T000013Z", provider="codex", argv=argv)
        with self.assertRaisesRegex(AcceptanceRefused, "batch leg must be Claude"):
            finalize_trace(
                leg_name=LEG_BATCH,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000013Z",
            )

        result, manifest = self._batch_files(provider_result={"status": "completed"})
        with self.assertRaisesRegex(AcceptanceRefused, "lacks command argv"):
            observe_batch_result(
                self._recorder(LEG_BATCH, "20260803T000014Z"),
                result_path=result,
                manifest_path=manifest,
            )

        outer_argv = ("/repo/tools/persona_bringup.py", "run", "--profile", "batch-sdk")
        self._batch_trace("20260803T000022Z", argv=outer_argv)
        with self.assertRaisesRegex(AcceptanceRefused, "executable mismatch"):
            finalize_trace(
                leg_name=LEG_BATCH,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000022Z",
            )

        self._batch_trace(
            "20260803T000015Z",
            argv=argv,
            provider_result={"command_argv": list(argv)},
        )
        with self.assertRaisesRegex(AcceptanceRefused, "structurally completed"):
            finalize_trace(
                leg_name=LEG_BATCH,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000015Z",
            )

        self._batch_trace(
            "20260803T000016Z",
            argv=argv,
            provider_result={
                "status": "completed",
                "returncode": 0,
                "harness": "claude",
                "command_argv": list(argv),
                "quota_state": {
                    "meter": "sdk-credit",
                    "source": "anthropic-sdk",
                },
            },
        )
        with self.assertRaisesRegex(AcceptanceRefused, "provider billing"):
            finalize_trace(
                leg_name=LEG_BATCH,
                reviewed_revision=self.reviewed,
                evidence_root=self.evidence,
                run_id="20260803T000016Z",
            )

    def test_fixture_refuses_batch_missing_or_wrong_api_key_source(self) -> None:
        argv = self._claude_batch_argv()
        for run_id, source in (
            ("20260803T000018Z", None),
            ("20260803T000019Z", "CLAUDE_CODE_OAUTH"),
        ):
            self._batch_trace(
                run_id,
                argv=argv,
                provider_billing={
                    "provider": "claude",
                    "api_key_source": source,
                    "total_cost_usd": 0.001,
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "source_events": ["system/init", "result"],
                },
            )
            with self.assertRaisesRegex(AcceptanceRefused, "ANTHROPIC_API_KEY"):
                finalize_trace(
                    leg_name=LEG_BATCH,
                    reviewed_revision=self.reviewed,
                    evidence_root=self.evidence,
                    run_id=run_id,
                )

    def test_fixture_refuses_batch_non_positive_non_finite_or_missing_cost(self) -> None:
        argv = self._claude_batch_argv()
        for run_id, cost, message in (
            ("20260803T000020Z", 0.0, "not positive"),
            ("20260803T000021Z", None, "lacks total_cost_usd"),
            ("20260803T000022Z", float("nan"), "not positive"),
            ("20260803T000023Z", float("inf"), "not positive"),
        ):
            billing = {
                "provider": "claude",
                "api_key_source": "ANTHROPIC_API_KEY",
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "source_events": ["system/init", "result"],
            }
            if cost is not None:
                billing["total_cost_usd"] = cost
            self._batch_trace(run_id, argv=argv, provider_billing=billing)
            with self.assertRaisesRegex(AcceptanceRefused, message):
                finalize_trace(
                    leg_name=LEG_BATCH,
                    reviewed_revision=self.reviewed,
                    evidence_root=self.evidence,
                    run_id=run_id,
                )


if __name__ == "__main__":
    unittest.main()
