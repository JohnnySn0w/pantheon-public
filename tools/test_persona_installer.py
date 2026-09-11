"""Tests for the ORG-049 manifest-driven persona installer."""

from __future__ import annotations

import atexit
import dataclasses
import base64
import functools
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tools import codex_telegram_bridge
from tools import runtime_controller as rc
from tools import t2611_recovery_persona_apply as persona_apply
from tools.agent_role_map import instance_binding, resolve_instance_role
from tools.persona_bringup import (
    load_binding,
    quota_check_personas,
    quota_check_supported,
)
from tools.persona_installer import (
    MAX_UNIT_FIRE_TIMEOUT_SECONDS,
    MANAGED_MARKER,
    ApplyReceipt,
    ActorContext,
    CodexStandingInstructionsReference,
    EXECUTION_CONTEXT_ALL_IN_ONE,
    EXECUTION_CONTEXT_STAGE_PREFIX,
    EXECUTION_CONTEXT_STAGED_ACTIVATE,
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    Manifest,
    ManifestError,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PersonaInstaller,
    PreflightResult,
    UnitFirePollSnapshot,
    LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS,
    LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS,
    _exec_status_within_success_policy,
    _legacy_convergence_service_in_flight,
    _timer_enablement_absence_admissible_classifications,
    _unit_fire_snapshot_has_failed,
    SimulatedHostBackend,
    STALE_UNIT_FAILURE_RESET_PROPERTIES,
    _all_plan_capabilities,
    _parse_stale_unit_failure_reset_show,
    _parse_systemctl_timer_show,
    codex_standing_instructions_reference,
    _finite_next_elapse_basis,
    _read_verified_standing_instructions,
    _snapshot,
    _snapshot_fingerprint,
    _symlink_snapshot,
    CLAUDE_MCP_SERVERS_SCHEMA,
    parse_claude_mcp_block,
    validate_claude_mcp_block,
    _claude_mcp_expected,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
R10_ORCHESTRATOR_CODEX_COMMIT = "f803cddfe27d4d1b9b5fd9c547ef33de0a8186c6"
R10_ORCHESTRATOR_CODEX_SHA256 = "36f0d5eb9ff0bcd6396bd82e658fa68fd49d46b1d7415186943a357a865cc688"
TEST_INVOCATION_ID = "a" * 32


class LocalHostStageBackend(LocalHostBackend):
    def __init__(self) -> None:
        super().__init__(command_timeout=1, timed_fire_timeout=1)
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[tuple[tuple[str, ...], float | None]] = []
        self.stale_unit_show_sequence: list[str] = []
        self.stale_unit_show_count = 0
        self.service_show_sequence: list[str] = []
        self.service_show_count = 0
        self.mcp_add_calls: list[tuple[list[str], str]] = []
        self.mcp_remove_calls: list[tuple[str, str, str]] = []
        # T-3612: fake time seams for ALL unit-fire polling (disabled-first-
        # fire capture/completion/cleanup loops AND the legacy-convergence
        # settle loop) -- lifted from LegacyConvergedTimerBackend, which
        # previously defined this seam only for itself (T-3211/T-3614).
        # Every LocalHostStageBackend instance now gets it for free, so
        # tests that drive unit-fire polling through the base class
        # directly no longer real-sleep. _unit_fire_sleep advances a fake
        # clock instead of blocking; sleep_calls records every call (a
        # zero-length list is itself the fast-path assertion).
        self.sleep_calls: list[float] = []
        self._fake_clock = 0.0

    def _unit_fire_sleep(self, seconds: float) -> None:  # type: ignore[override]
        self.sleep_calls.append(seconds)
        self._fake_clock += seconds

    def _unit_fire_monotonic(self) -> float:  # type: ignore[override]
        return self._fake_clock

    def _run_claude_mcp(self, args, *, home, phase):  # type: ignore[override]
        # T-3109: simulates the `claude mcp add-json` CLI by mutating the
        # fixture ~/.claude.json in place (mirrors ClaudeMcpServerCase's
        # _CapturingClaudeMcpBackend), instead of shelling out to the real
        # `claude` binary -- unreachable before this ticket closed the
        # codex-telegram archive-read gap that always short-circuited
        # stage_disabled/activate_staged upstream of any MCP registration
        # operation, so this fixture-level gap was never exercised.
        self.mcp_add_calls.append((list(args), home))
        name = args[3]
        payload = json.loads(args[4])
        path = Path(home) / ".claude.json"
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        document.setdefault("mcpServers", {})[name] = payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(["claude"], 0, "added", "")

    def _run_claude_mcp_remove(self, name, *, home, scope):  # type: ignore[override]
        self.mcp_remove_calls.append((name, home, scope))
        path = Path(home) / ".claude.json"
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        servers = document.get("mcpServers", {})
        if isinstance(servers, dict):
            servers.pop(name, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(["claude"], 0, "removed", "")

    def _run(self, argv, *, timeout=None):  # type: ignore[override]
        command = tuple(str(item) for item in argv)
        self.commands.append(command)
        self.timeouts.append((command, timeout))
        if command[:4] == ("systemctl", "--user", "show", "codex-telegram-poller.service"):
            return subprocess.CompletedProcess(command, 0, "ActiveState=inactive\nMainPID=0\n", "")
        if (
            command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.timer")
            or command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@recovery.timer")
        ):
            properties = set(command[4:])
            if "NextElapseUSecRealtime" in properties or "NextElapseUSecMonotonic" in properties:
                return subprocess.CompletedProcess(command, 0, "ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=Sat 2026-08-15 04:00:00 UTC\nNextElapseUSecMonotonic=12345\n", "")
            return subprocess.CompletedProcess(command, 0, "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", "")
        if (
            command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
            or command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@recovery.service")
        ):
            properties = set(command[4:])
            if "UnitFileState" in properties:
                return subprocess.CompletedProcess(command, 0, "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n", "")
            if "LoadState" in properties:
                if self.stale_unit_show_sequence:
                    index = min(self.stale_unit_show_count, len(self.stale_unit_show_sequence) - 1)
                    self.stale_unit_show_count += 1
                    return subprocess.CompletedProcess(command, 0, self.stale_unit_show_sequence[index], "")
                self.stale_unit_show_count += 1
                return subprocess.CompletedProcess(command, 0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\nJob=\n", "")
            if "InvocationID" in properties and "ActiveState" in properties:
                if self.service_show_sequence:
                    index = min(self.service_show_count, len(self.service_show_sequence) - 1)
                    self.service_show_count += 1
                    return subprocess.CompletedProcess(command, 0, self.service_show_sequence[index], "")
                self.service_show_count += 1
                if self.service_show_count == 1:
                    return subprocess.CompletedProcess(command, 0, "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n", "")
                if self.service_show_count == 2:
                    return subprocess.CompletedProcess(command, 0, f"InvocationID={TEST_INVOCATION_ID}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n", "")
                return subprocess.CompletedProcess(command, 0, "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n", "")
            if "InvocationID" in properties:
                return subprocess.CompletedProcess(command, 0, f"InvocationID={TEST_INVOCATION_ID}\n", "")
            if "ActiveState" in properties and "Result" in properties and "ExecMainStatus" in properties:
                return subprocess.CompletedProcess(command, 0, "ActiveState=inactive\nResult=success\nExecMainStatus=0\n", "")
        if command[:3] == ("systemctl", "--user", "show-environment"):
            return subprocess.CompletedProcess(command, 0, "PATH=/usr/bin\n", "")
        if command[:3] == ("systemctl", "--user", "is-enabled"):
            return subprocess.CompletedProcess(command, 1, "disabled\n", "")
        if command[:4] == ("systemctl", "--user", "enable", "--now"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("systemctl", "--user", "start"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("systemctl", "--user", "cat"):
            return subprocess.CompletedProcess(command, 0, "[Unit]\nDescription=test\n", "")
        if len(command) >= 4 and command[0] == "git" and command[2] == "cat-file":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def preflight(self, operation) -> PreflightResult:  # type: ignore[override]
        if operation.kind == "harness-check":
            return PreflightResult(True, "harness", f"{operation.target} test harness available")
        if operation.provenance.get("install_root") != "/":
            operation = dataclasses.replace(
                operation,
                provenance={**operation.provenance, "install_root": "/"},
            )
        return super().preflight(operation)

    def _current_account_only(self, account: str) -> PreflightResult:
        return PreflightResult(True, "current-account", f"test account {account}")

    def _identity_ready(self, operation) -> PreflightResult:  # type: ignore[override]
        return PreflightResult(True, "identity", f"test identity {operation.owner}")

    def _manager_ready(self) -> PreflightResult:
        return PreflightResult(True, "active", "test user manager reachable")


class LegacyConvergedTimerBackend(LocalHostStageBackend):
    """Reports the consume timer as an already-enabled, live-converged
    legacy install (T-3087 recovery-analog): `is-enabled` succeeds, the timer
    is active+waiting+finite-next-elapse, the service is inactive+success+
    exit-0. Individual invariants can be broken via the constructor to
    exercise the reject paths.

    Non-stateful by default (unlike LocalHostStageBackend's own
    service_show_sequence counter, which models the disabled-first-fire
    dance's temporal sequence): a legacy-converged probe re-queries the
    SAME steady state every call, so a counter that advances per-call would
    make the second preflight pass (e.g. activate_staged's own
    _preflight_apply re-running gate 1) see a different, non-converged
    snapshot than the first and spuriously reject (T-3087 DECISIONS).

    T-3211: ``service_sample_sequence`` is an OPT-IN escape hatch for
    exercising the legacy-convergence probe's in-flight settle loop
    specifically -- it models the single probe CALL's own re-sample
    sequence (service goes activating -> active -> inactive within one
    ``_legacy_convergence_evidence`` invocation), which is a different axis
    from the steady-state-across-separate-preflight-passes concern the
    class docstring above describes. Left ``None`` (the default for every
    existing caller), it behaves exactly as before: a constant value
    returned on every call, forever.

    ``timer_tracks_service_settle`` is a second, independent opt-in that
    couples the TIMER sample to whichever SERVICE sample was most recently
    returned, reproducing recovery's live capture (T-3211 ticket log, "MID-FIRE
    TIMER CAPTURE"): while the service is in flight the timer reads
    ``SubState=running`` / ``NextElapseUSecMonotonic=infinity`` (fails both
    the waiting check and ``_finite_next_elapse_basis``); once the service
    has settled the timer reads ``SubState=waiting`` with a finite monotonic
    elapse. This exists to prove the probe samples the timer only AFTER the
    service settles -- if the code sampled the timer first (or reused a
    pre-settle sample), a test built on this coupling would see the
    in-flight (rejecting) timer shape and fail.
    """

    def __init__(
        self,
        *,
        persona: str = "orchestrator",
        service_result: str = "success",
        service_exec_main_status: str = "0",
        service_success_exit_status: str = "3 4 5",
        service_active_state: str = "inactive",
        next_elapse_finite: bool = True,
        timer_active_state: str = "active",
        timer_sub_state: str = "waiting",
        service_sample_sequence: "list[dict[str, str]] | None" = None,
        timer_tracks_service_settle: bool = False,
    ) -> None:
        super().__init__()
        self._persona = persona
        self._service_result = service_result
        self._service_exec_main_status = service_exec_main_status
        self._service_success_exit_status = service_success_exit_status
        self._service_active_state = service_active_state
        self._next_elapse_finite = next_elapse_finite
        self._timer_active_state = timer_active_state
        self._timer_sub_state = timer_sub_state
        self._service_sample_sequence = (
            list(service_sample_sequence) if service_sample_sequence is not None else None
        )
        self._service_call_index = 0
        self._timer_tracks_service_settle = timer_tracks_service_settle
        self._service_in_flight = False
        # T-3612: the fake-clock seam this class relied on (T-3211/T-3614)
        # now lives on LocalHostStageBackend, our parent -- self.sleep_calls
        # / self._fake_clock / _unit_fire_sleep / _unit_fire_monotonic are
        # inherited unchanged. Nothing left to define here.

    def _run(self, argv, *, timeout=None):  # type: ignore[override]
        command = tuple(str(item) for item in argv)
        override = self._legacy_override(command)
        if override is not None:
            self.commands.append(command)
            self.timeouts.append((command, timeout))
            return override
        return super()._run(argv, timeout=timeout)

    def _next_service_sample(self) -> dict[str, str]:
        default = {
            "InvocationID": "",
            "ActiveState": self._service_active_state,
            "Result": self._service_result,
            "ExecMainStatus": self._service_exec_main_status,
            "SuccessExitStatus": self._service_success_exit_status,
            "Job": "",
        }
        if self._service_sample_sequence is None:
            sample = default
        else:
            index = min(self._service_call_index, len(self._service_sample_sequence) - 1)
            self._service_call_index += 1
            sample = {**default, **self._service_sample_sequence[index]}
        self._service_in_flight = _legacy_convergence_service_in_flight(sample)
        return sample

    def _legacy_override(self, command):
        unit_prefix = ("systemctl", "--user", "show", f"pantheon-persona-consume@{self._persona}.timer")
        service_prefix = ("systemctl", "--user", "show", f"pantheon-persona-consume@{self._persona}.service")
        # T-3109: narrowed to the consume timer unit specifically -- a
        # blanket match on any is-enabled call also caught the unrelated
        # codex-telegram.disabled-first gate's own is-enabled probe of
        # codex-telegram-poller.service once that gate became reachable
        # (previously short-circuited upstream by the archive-read guard
        # this ticket retired), incorrectly reporting it "enabled" too.
        if command[:4] == ("systemctl", "--user", "is-enabled", f"pantheon-persona-consume@{self._persona}.timer"):
            return subprocess.CompletedProcess(command, 0, "enabled\n", "")
        if command[:4] == unit_prefix:
            properties = set(command[4:])
            if "NextElapseUSecRealtime" in properties or "NextElapseUSecMonotonic" in properties:
                if self._timer_tracks_service_settle:
                    if self._service_in_flight:
                        sub_state = "running"
                        elapse = "NextElapseUSecRealtime=\nNextElapseUSecMonotonic=infinity\n"
                    else:
                        sub_state = "waiting"
                        elapse = "NextElapseUSecRealtime=Sat 2026-08-15 04:00:00 UTC\nNextElapseUSecMonotonic=12345\n"
                else:
                    sub_state = self._timer_sub_state
                    elapse = (
                        "NextElapseUSecRealtime=Sat 2026-08-15 04:00:00 UTC\nNextElapseUSecMonotonic=12345\n"
                        if self._next_elapse_finite
                        else "NextElapseUSecRealtime=n/a\nNextElapseUSecMonotonic=0\n"
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"ActiveState={self._timer_active_state}\nSubState={sub_state}\n{elapse}",
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                (
                    "LoadState=loaded\nUnitFileState=enabled\n"
                    f"ActiveState={self._timer_active_state}\nSubState={self._timer_sub_state}\n"
                ),
                "",
            )
        if command[:4] == service_prefix:
            properties = set(command[4:])
            # T-3614: check the stale-unit-failure-reset shape (LoadState
            # present) BEFORE the InvocationID+ActiveState hybrid check --
            # both real callers' property lists contain InvocationID AND
            # ActiveState, so checking the hybrid condition first routed
            # the stale-reset query to the wrong (SuccessExitStatus-
            # bearing, LoadState-less) shape. Mirrors the disambiguation
            # order LocalHostStageBackend._run already uses above.
            if "LoadState" in properties:
                sample = self._next_service_sample()
                return subprocess.CompletedProcess(
                    command,
                    0,
                    (
                        "LoadState=not-found\n"
                        f"ActiveState={sample['ActiveState']}\nSubState=dead\n"
                        f"Result={sample['Result']}\nExecMainStatus={sample['ExecMainStatus']}\n"
                        f"InvocationID={sample['InvocationID']}\nJob={sample['Job']}\n"
                    ),
                    "",
                )
            if "InvocationID" in properties and "ActiveState" in properties:
                sample = self._next_service_sample()
                return subprocess.CompletedProcess(
                    command,
                    0,
                    (
                        f"InvocationID={sample['InvocationID']}\n"
                        f"ActiveState={sample['ActiveState']}\nSubState=dead\n"
                        f"Result={sample['Result']}\nExecMainStatus={sample['ExecMainStatus']}\n"
                        f"SuccessExitStatus={sample['SuccessExitStatus']}\nJob={sample['Job']}\n"
                    ),
                    "",
                )
        return None


def _manifest_dict(*, second_persona: bool = False) -> dict:
    roles: dict[str, dict] = {
        "recovery": {
            "persona": "recovery",
            "account": "claude-fallback",
            "harness": "claude-code",
        }
    }
    if second_persona:
        roles["peer-daemon-cairn"] = {
            "persona": "cairn",
            "account": "codex",
            "harness": "codex",
        }
    return {
        "manifest_version": 2,
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test-rev"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": {
                "hub": "/mailbox",
                "group": "pantheon-mailbox",
                "probe_peer": "orchestrator",
            },
            "systemd": {
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
            },
            "policy": {"bundle": ["rulebook/", "sop/", "adr/"]},
            "security": {
                "credential_mode": "references-only",
                "prohibit_auth_copy": True,
            },
            "lifecycle": {
                "require_disabled_first_fire": True,
                "require_mailbox_send_receive": True,
                "require_finite_next_elapse": True,
                "require_observed_timed_fire": True,
            },
            "provenance": {"binding_source": "instance.yaml"},
        },
        "instance": {"id": "test-instance", "harness_default": "claude-code"},
        "roles": roles,
    }


_CODEX_CONFIG_FIXTURE_REPO_URL = "https://github.com/example-org/codex-config.git"
_codex_config_fixture_root: str | None = None


def _ensure_codex_config_fixture() -> str:
    """Lazily clone codex-config into a per-process /tmp dir on first use
    (memoized — one clone serves every ``InstallerCase`` method that needs
    it) so codex-telegram-bridge tests can read the pinned source content
    without needing UID=codex's cross-partition /home/codex/codex-config
    access (T-3109; retires the T-3563 slice A / T-3547 skip guards this
    replaced). Lazy rather than a ``setUpModule`` eager clone so a clone
    failure (e.g. no network) only fails the tests that opted in via
    ``_with_codex_config_fixture``, not all 153 tests in this module.

    Full clone, not ``--depth=1``: the pinned revision
    (``codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_REVISION``) is often
    behind the default branch's current HEAD, so a shallow clone of only
    the latest commit does not contain it — verified empirically
    2026-09-10 (``git rev-parse <pin>^{commit}`` fails against a
    ``--depth=1`` clone of that date's HEAD). A full clone is still small
    (~1.3MB) and fast (<1s).

    T-3613: if ``CODEX_CONFIG_READ_PAT`` is set (the CI runner's Actions
    secret, a fine-grained read-only PAT on example-org/codex-config), the
    clone URL is built as ``https://x-access-token:<PAT>@github.com/...``
    (GitHub's documented PAT-over-HTTPS-basic-auth shape) instead of the
    unauth URL that resolves via ``gh auth git-credential`` on dev seats.
    With the PAT wired, a clone failure is a real infrastructure problem —
    raise ``RuntimeError`` (fail loudly) rather than degrading to SKIP. The
    PAT-unset path is unchanged from T-3109: SKIP on clone failure, since
    that's still legitimate for unauth CI and a fresh dev clone without
    ``gh auth`` set up yet."""
    global _codex_config_fixture_root
    if _codex_config_fixture_root is None:
        target = tempfile.mkdtemp(prefix="pantheon-codex-config-fixture-")
        # Belt-and-braces cleanup (also removed in tearDownModule; rmtree here
        # keeps the tempfile-hygiene lint's scope-local cleanup-signal check
        # happy and covers the case where the module tear-down never runs, e.g.
        # if the process exits before this fixture is used again).
        atexit.register(shutil.rmtree, target, ignore_errors=True)
        pat = os.environ.get("CODEX_CONFIG_READ_PAT")
        clone_url = (
            f"https://x-access-token:{pat}@github.com/example-org/codex-config.git"
            if pat
            else _CODEX_CONFIG_FIXTURE_REPO_URL
        )
        result = subprocess.run(
            ("git", "clone", clone_url, target),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout).strip()
            if pat:
                # Belt-and-braces: current git versions don't echo credentials
                # in `fatal:` text, but never surface the raw token in a
                # RuntimeError message / CI log regardless of git's behavior.
                error_text = error_text.replace(pat, "<redacted>")
                raise RuntimeError(
                    "T-3613 codex-config fixture clone failed despite "
                    f"CODEX_CONFIG_READ_PAT being set: {error_text}"
                )
            # T-3109: codex-config is a PRIVATE repo — a seat without an
            # authenticated gh CLI, a cross-repo PAT in the git credential
            # helper, or CODEX_CONFIG_READ_PAT hits "could not read Username
            # for 'https://github.com'". Degrade to SKIP rather than FAIL:
            # this is still best-effort on an unauth seat (unauth CI without
            # the T-3613 secret wired, or a fresh dev clone before `gh auth`
            # is set up) — the pre-T-3109 behaviour was skip-when-unreachable
            # and this preserves that for the unauth case.
            raise unittest.SkipTest(
                f"T-3109 codex-config fixture clone unreachable: {error_text}"
            )
        _codex_config_fixture_root = target
    return _codex_config_fixture_root


def tearDownModule() -> None:
    global _codex_config_fixture_root
    if _codex_config_fixture_root is not None:
        shutil.rmtree(_codex_config_fixture_root, ignore_errors=True)
        _codex_config_fixture_root = None


def _with_codex_config_fixture(test_method):
    """Replaces the retired ``@unittest.skipUnless(_codex_home_reachable(),
    ...)`` guard (T-3109). Activates a ``Manifest.load`` patch scoped to a
    single test method's execution: any load of the real
    ``REPO_ROOT / "instance.yaml"`` has its
    ``installer["codex_telegram_bridge"]["source"]["source_root"]``
    (a plain mutable dict nested inside the frozen ``Manifest`` dataclass —
    safe to rewrite in place, never reassigning the frozen field itself)
    rewritten to the /tmp clone from ``_ensure_codex_config_fixture``
    instead of the production ``/home/codex/codex-config`` path.

    Scoped per-test rather than class- or module-wide so tests that assert
    on the literal prod path in a *planned* payload (e.g.
    ``test_orchestrator_installer_plan_is_disabled_first_fire_and_references_
    only``, which never opts into this decorator) keep exercising the real
    value — only call sites that need the archive to actually be
    *readable* opt in."""

    @functools.wraps(test_method)
    def wrapper(self, *args, **kwargs):
        fixture_root = _ensure_codex_config_fixture()
        original_load = Manifest.load

        def _patched_load(path: Path, _original=original_load, _root=fixture_root) -> Manifest:
            manifest = _original(path)
            if path == REPO_ROOT / "instance.yaml":
                bridge = manifest.installer.get("codex_telegram_bridge")
                if isinstance(bridge, dict):
                    source = bridge.get("source")
                    if isinstance(source, dict) and isinstance(source.get("source_root"), str):
                        source["source_root"] = _root
            return manifest

        with patch.object(Manifest, "load", staticmethod(_patched_load)):
            return test_method(self, *args, **kwargs)

    return wrapper


class CodexConfigFixturePatCase(unittest.TestCase):
    """T-3613: ``_ensure_codex_config_fixture``'s ``CODEX_CONFIG_READ_PAT``
    branch — clone-URL shape, RuntimeError-vs-SkipTest error-type selection,
    and the token-leak guard. Lives near the helper (not inside
    ``InstallerCase``, which is ~4000 lines and consumes this fixture only
    indirectly via ``@_with_codex_config_fixture``) so a reviewer checking
    this helper's own behavior doesn't have to search the whole file.

    Mocks ``subprocess.run`` — no real network clone. Real ``tempfile.mkdtemp``
    is used (cheap; the existing ``atexit.register`` cleanup in the function
    under test already handles hygiene on both the pass and fail paths, so no
    extra per-test cleanup is needed here). setUp/tearDown save+restore the
    module's memoized ``_codex_config_fixture_root`` global so this class
    doesn't collide with the ~48 ``@_with_codex_config_fixture``-decorated
    tests elsewhere in this module that populate it for real."""

    def setUp(self) -> None:
        module = sys.modules[__name__]
        self._saved_fixture_root = module._codex_config_fixture_root
        module._codex_config_fixture_root = None
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("CODEX_CONFIG_READ_PAT", None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        module = sys.modules[__name__]
        module._codex_config_fixture_root = self._saved_fixture_root

    def test_pat_set_success_builds_authenticated_clone_url(self) -> None:
        os.environ["CODEX_CONFIG_READ_PAT"] = "test-pat-value-abc123"
        with patch("tools.test_persona_installer.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            result = _ensure_codex_config_fixture()
        clone_url = mock_run.call_args[0][0][2]
        self.assertEqual(
            clone_url,
            "https://x-access-token:test-pat-value-abc123@github.com/example-org/codex-config.git",
        )
        self.assertTrue(result)

    def test_pat_unset_success_uses_unauth_url(self) -> None:
        with patch("tools.test_persona_installer.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            _ensure_codex_config_fixture()
        clone_url = mock_run.call_args[0][0][2]
        self.assertEqual(clone_url, _CODEX_CONFIG_FIXTURE_REPO_URL)

    def test_pat_unset_failure_raises_skiptest(self) -> None:
        with patch("tools.test_persona_installer.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                returncode=1, stdout="", stderr="could not read Username for 'https://github.com'"
            )
            with self.assertRaises(unittest.SkipTest):
                _ensure_codex_config_fixture()

    def test_pat_set_failure_raises_runtimeerror(self) -> None:
        os.environ["CODEX_CONFIG_READ_PAT"] = "test-pat-value-abc123"
        with patch("tools.test_persona_installer.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                returncode=1, stdout="", stderr="fatal: repository not found"
            )
            with self.assertRaises(RuntimeError):
                _ensure_codex_config_fixture()

    def test_pat_set_failure_does_not_leak_token(self) -> None:
        pat = "ghp_topsecrettoken1234567890"
        os.environ["CODEX_CONFIG_READ_PAT"] = pat
        with patch("tools.test_persona_installer.subprocess.run") as mock_run:
            # Worst case: git's fatal text echoes the failed URL verbatim,
            # which would embed the token if we didn't scrub it.
            mock_run.return_value = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"fatal: could not clone https://x-access-token:{pat}@github.com/example-org/codex-config.git",
            )
            with self.assertRaises(RuntimeError) as ctx:
                _ensure_codex_config_fixture()
        message = str(ctx.exception)
        self.assertNotIn(pat, message)
        self.assertIn("<redacted>", message)


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.write_manifest(_manifest_dict(second_persona=True))
        self.manifest = Manifest.load(self.manifest_path)
        self.installer = PersonaInstaller(REPO_ROOT)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, value: dict) -> None:
        self.manifest_path.write_text(
            yaml.safe_dump(value, sort_keys=False),
            encoding="utf-8",
        )

    def enable_codex_telegram_bridge_manifest(self) -> None:
        data = _manifest_dict(second_persona=True)
        data["persona_installer"]["codex_telegram_bridge"] = {
            "schema": "pantheon-codex-telegram-bridge/v1",
            "source": {
                "repo": "codex-config",
                "revision": codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_REVISION,
                "paths": list(codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_PATHS),
                "sha256": dict(codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_SHA256),
            },
            "operator_chat_id": 456474197,
            "personas": {
                "specialist": {
                    "account": "claude-research",
                    "claude_receiver_topology": "runtime-owned",
                    "claude_receiver_unit": "claude-telegram-research.service",
                    "tmux_session": "claude-research-bot",
                    "tmux_pane": "0.0",
                    "codex_receiver_unit": "codex-telegram-poller.service",
                    "credential_env": ".claude/channels/telegram/.env",
                    "state_dir": ".config/codex-telegram",
                    "release_root": ".local/lib/codex-telegram",
                    "mcp_name": "telegram",
                },
                "recovery": {
                    "account": "claude-fallback",
                    "claude_receiver_topology": "runtime-owned",
                    "claude_receiver_unit": "claude-telegram-fallback.service",
                    "tmux_session": "claude-telegram-bot-fallback",
                    "tmux_pane": "0.0",
                    "codex_receiver_unit": "codex-telegram-poller.service",
                    "credential_env": ".claude/channels/telegram/.env",
                    "state_dir": ".config/codex-telegram",
                    "release_root": ".local/lib/codex-telegram",
                    "mcp_name": "telegram",
                    "watchdog_units": [
                        "recovery-telegram-child-watchdog.timer",
                        "recovery-telegram-child-watchdog.service",
                    ],
                },
            },
        }
        self.write_manifest(data)
        self.manifest = Manifest.load(self.manifest_path)

    def codex_telegram_mcp_operation(self):
        self.enable_codex_telegram_bridge_manifest()
        plan = self.plan("recovery", "codex")
        return self.operation(plan, "codex-telegram.mcp-registration")

    def additive_mcp_registration(self, operation) -> str:
        name = operation.desired["name"]
        return operation.desired["expected"] + f"""
[mcp_servers.{name}.tools.reply]
approval_mode = "approve"

[mcp_servers.{name}.tools.react]
approval_mode = "approve"

[mcp_servers.{name}.tools.edit_message]
approval_mode = "approve"

[mcp_servers.{name}.tools.download_attachment]
approval_mode = "approve"
"""

    def test_manifest_from_bytes_matches_load_and_never_reopens_path(self) -> None:
        raw = self.manifest_path.read_bytes()
        parsed = Manifest.from_bytes(self.manifest_path, raw)
        self.assertEqual(parsed, Manifest.load(self.manifest_path))

        missing_path = self.root / "does-not-exist.yaml"
        self.assertEqual(
            Manifest.from_bytes(missing_path, raw),
            parsed.__class__(
                path=missing_path,
                version=parsed.version,
                instance_id=parsed.instance_id,
                installer=parsed.installer,
                roles=parsed.roles,
            ),
        )

        for invalid in (b"\xff", b"not: [valid"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, invalid)

        self.manifest_path.write_bytes(b"\xff")
        with self.assertRaises(ManifestError):
            Manifest.load(self.manifest_path)

    def plan(self, persona: str = "recovery", harness: str | None = None):
        return self.installer.plan(
            self.manifest,
            persona,
            harness_override=harness,
            root=self.root / "projection",
        )

    def actor(self, plan, *, all_capabilities: bool = True) -> ActorContext:
        capabilities = frozenset(
            operation.provenance["required_capability"]
            for operation in plan.operations
            if operation.provenance.get("required_capability")
        )
        capabilities |= frozenset(
            action.required_capability for action in plan.operator_actions
        )
        if not all_capabilities:
            capabilities = frozenset()
        return ActorContext(
            uid=1000,
            persona="cairn",
            role="installer",
            instance=plan.instance,
            capabilities=capabilities,
        )

    def apply(self, plan, backend=None):
        backend = backend or SimulatedHostBackend()
        return (
            backend,
            self.installer.apply(plan, plan.digest, backend, self.actor(plan)),
        )

    def operation(self, plan, operation_id: str):
        return next(item for item in plan.operations if item.operation_id == operation_id)

    def orchestrator_local_stage_fixture(self, name: str):
        root = self.root / name
        account_home = root / "home" / "claude"
        account_home.mkdir(parents=True)
        (account_home / ".local" / "lib").mkdir(parents=True)
        (account_home / "CLAUDE.md").write_text("Orchestrator brief fixture\n", encoding="utf-8")
        for mailbox_persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (root / "home" / "the-ancestor" / "mailbox" / mailbox_persona / subdir).mkdir(parents=True)
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=root,
        )
        archive = self.operation(plan, "codex-telegram.source-archive")
        source = codex_telegram_bridge.CodexTelegramSource(
            "codex-config",
            archive.desired["revision"],
            tuple(json.loads(archive.desired["paths"])),
            dict(json.loads(archive.desired["sha256"])),
            owner_account=archive.desired.get("source_account") or None,
            source_root=Path(archive.desired["source_root"]),
        )
        codex_telegram_bridge.create_verified_source_archive(
            Path(archive.desired["source_root"]),
            Path(archive.desired["archive_path"]),
            source,
        )
        return root, plan, LocalHostStageBackend()

    def orchestrator_timer_gate_fixture_root(self, name: str):
        """T-3087: like orchestrator_local_stage_fixture but skips the
        codex-telegram source-archive creation. Only needed for tests that
        exercise the connective.timer-enablement-absent / connective.unit-
        fire gates in isolation (preflight() / execute() called directly,
        or a stage_disabled() run expected to refuse at operation index 0
        before any other prefix operation's preflight runs) -- callers of
        this fixture don't need @_with_codex_config_fixture (T-3109) since
        the archive step it deliberately skips is the only thing that
        needs the codex-config content to be reachable."""
        root = self.root / name
        account_home = root / "home" / "claude"
        account_home.mkdir(parents=True)
        (account_home / ".local" / "lib").mkdir(parents=True)
        (account_home / "CLAUDE.md").write_text("Orchestrator brief fixture\n", encoding="utf-8")
        for mailbox_persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (root / "home" / "the-ancestor" / "mailbox" / mailbox_persona / subdir).mkdir(parents=True)
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=root,
        )
        return root, plan

    def orchestrator_stage_disabled(self, name: str, stamp: str):
        root, plan, backend = self.orchestrator_local_stage_fixture(name)
        exit_code, summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=root,
        )
        self.assertEqual(exit_code, 0, summary)
        return root, plan, backend, summary

    def consume_timer_link_paths(self, root: Path, persona: str = "orchestrator") -> tuple[Path, Path]:
        unit_dir = root / "home" / "claude" / ".config" / "systemd" / "user"
        return (
            unit_dir / "timers.target.wants" / f"pantheon-persona-consume@{persona}.timer",
            unit_dir / "pantheon-persona-consume@.timer",
        )

    def write_consume_timer_link(
        self,
        root: Path,
        target: str | Path,
        *,
        persona: str = "orchestrator",
    ) -> tuple[Path, Path]:
        link, template = self.consume_timer_link_paths(root, persona)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(str(target))
        return link, template

    def timer_enable_owned_mutation(
        self,
        root: Path,
        *,
        actual_readlink: str,
        persona: str = "orchestrator",
        changed: bool = True,
        owned: bool = True,
        activation_intent: bool = False,
    ) -> MutationRecord:
        link, template = self.consume_timer_link_paths(root, persona)
        resolved = Path(actual_readlink)
        if not resolved.is_absolute():
            resolved = (link.parent / actual_readlink).resolve(strict=False)
        return MutationRecord(
            "connective.timer-enable",
            "timer-enable",
            f"pantheon-persona-consume@{persona}.timer",
            "claude",
            changed,
            {
                "state": "disabled",
                "activation_intent": str(activation_intent).lower(),
                "owned": str(owned).lower(),
                "enablement_link_state": "absent",
                "enablement_link_path": str(link),
                "canonical_template_path": str(template),
                "canonical_template_path_sha256": hashlib.sha256(str(template).encode("utf-8")).hexdigest(),
                "enablement_link_actual_readlink": actual_readlink,
                "enablement_link_actual_readlink_sha256": hashlib.sha256(actual_readlink.encode("utf-8")).hexdigest(),
                "enablement_link_resolved_target": str(resolved),
                "enablement_link_resolved_target_sha256": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
            },
            "timer-enabled",
        )

    def rewrite_staged_checkpoint_timer_mutation(
        self,
        root: Path,
        stamp: str,
        mutation: MutationRecord,
        *,
        phase: str = "failed",
    ) -> None:
        checkpoint = persona_apply._checkpoint_path_from_home(root / "home" / "claude", "orchestrator", stamp)
        raw = json.loads(checkpoint.read_text(encoding="utf-8"))
        receipt = raw["receipt"]
        receipt["phase"] = phase
        receipt["mutations"] = [
            mutation.to_dict() if hasattr(mutation, "to_dict") else dataclasses.asdict(mutation)
            if item["operation_id"] == "connective.timer-enable"
            else item
            for item in receipt["mutations"]
        ]
        checkpoint.write_text(json.dumps(raw, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def activation_suffix_ids(self, plan) -> set[str]:
        return {
            operation.operation_id
            for operation in plan.operations[self.installer._activation_index(plan) :]
        }

    def activation_suffix_events(self, backend: LocalHostStageBackend, plan) -> set[str]:
        return set(backend.events) & self.activation_suffix_ids(plan)

    def stale_unit_clean_show(self) -> str:
        return "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\nJob=\n"

    def stale_unit_failed_show(
        self,
        *,
        load_state: str = "not-found",
        active_state: str = "failed",
        sub_state: str = "failed",
        result: str = "exit-code",
        exec_main_status: str = "1",
        invocation_id: str = TEST_INVOCATION_ID,
        job: str = "",
    ) -> str:
        return (
            f"LoadState={load_state}\n"
            f"ActiveState={active_state}\n"
            f"SubState={sub_state}\n"
            f"Result={result}\n"
            f"ExecMainStatus={exec_main_status}\n"
            f"InvocationID={invocation_id}\n"
            f"Job={job}\n"
        )

    def activation_commands(self, backend: LocalHostStageBackend, start_index: int) -> list[tuple[str, ...]]:
        return backend.commands[start_index:]

    def assert_no_later_activation_systemctl(self, commands: list[tuple[str, ...]]) -> None:
        forbidden = [
            command
            for command in commands
            if command[:3] == ("systemctl", "--user", "start")
            or command[:4] == ("systemctl", "--user", "enable", "--now")
            or command == ("systemctl", "--user", "daemon-reload")
        ]
        self.assertEqual(forbidden, [], commands)

    def checkpoint_source_mutation(self, root: Path, plan, stamp: str):
        checkpoint = (
            root
            / "home"
            / "claude"
            / ".local"
            / "state"
            / "pantheon"
            / "personas"
            / "orchestrator"
            / f"staged-cutover-{stamp}.json"
        )
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        receipt = ApplyReceipt.from_dict(payload["receipt"])
        mutation = next(item for item in receipt.mutations if item.operation_id == "codex-telegram.source-archive")
        return checkpoint, mutation

    def write_invocation_viability_receipt(
        self,
        plan,
        *,
        invocation_id: str = TEST_INVOCATION_ID,
        selected_runtime: str | None = None,
        status: str = "selected",
        receiver_ok: bool = True,
        provider_status: str | None = None,
        state_root: Path | None = None,
    ) -> dict:
        unit_fire = self.operation(plan, "connective.unit-fire")
        allowed = json.loads(unit_fire.desired["allowed_runtime_refs"])
        selected = selected_runtime or allowed[-1]
        selected_harness, _, selected_profile = selected.partition("/")
        if not provider_status:
            provider_status = "provider-completed" if selected_harness == "claude" else "not-applicable"
        attempts = []
        for index, runtime in enumerate(allowed, start=1):
            harness, _, profile = runtime.partition("/")
            if runtime == selected and status == "selected":
                attempts.append(
                    {
                        "sequence": index,
                        "runtime": runtime,
                        "harness": harness,
                        "profile": profile,
                        "status": "selected",
                        "failure_class": None,
                        "cause": "runtime-ready",
                        "readiness": {
                            "event": "runtime_started",
                            "provider_completion_status": provider_status,
                        },
                        "teardown": {},
                        "receiver": (
                            {
                                "schema": "pantheon-codex-telegram-handoff/v1",
                                "ok": True,
                                "direction": "claude-to-codex",
                                "phase": "post",
                                "operations": json.loads(unit_fire.desired["receiver_operations_by_runtime"]).get(runtime, []),
                            }
                            if receiver_ok
                            else {}
                        ),
                        "cooldown": {"opened": "false"},
                        "source_provenance": {
                            "revision": "test-rev",
                            "source_manifest_sha256": "1" * 64,
                            "runtime_start_boundary_at": "2026-08-15T00:00:00Z",
                            "runtime_start_observed_at": "2026-08-15T00:00:01Z",
                        },
                        "precheck_source_provenance": {},
                    }
                )
            else:
                attempts.append(
                    {
                        "sequence": index,
                        "runtime": runtime,
                        "harness": harness,
                        "profile": profile,
                        "status": "failed",
                        "failure_class": "operational-readiness",
                        "cause": "runtime_input_not_ready",
                        "readiness": {"event": "runtime_input_not_ready"},
                        "teardown": {"runtime_state_cleared": "true", "receiver_absent_proven": "true"},
                        "receiver": {},
                        "cooldown": {"opened": "true", "until": "2026-08-15T00:15:00Z"},
                        "source_provenance": {
                            "revision": "test-rev",
                            "source_manifest_sha256": "1" * 64,
                            "runtime_start_boundary_at": "2026-08-15T00:00:00Z",
                            "runtime_start_observed_at": "2026-08-15T00:00:00Z",
                        },
                        "precheck_source_provenance": {},
                    }
                )
        receipt = {
            "schema": "pantheon-runtime-initial-viability/v1",
            "persona": plan.persona,
            "status": status,
            "selected_runtime": selected if status == "selected" else None,
            "selected_harness": selected_harness if status == "selected" else None,
            "selected_profile": selected_profile if status == "selected" else None,
            "manifest_order": allowed,
            "attempt_limit": len(allowed),
            "attempts": attempts,
            "recorded_at": "2026-08-15T00:00:02Z",
        }
        state_root = state_root or Path(unit_fire.desired["persona_state_root"])
        payload = {
            "schema": "pantheon-runtime-initial-viability-invocation/v1",
            "persona": plan.persona,
            "invocation_id": invocation_id,
            "initial_viability": receipt,
        }
        path = (
            state_root
            / plan.persona
            / "runtime-controller"
            / "initial-viability-invocations"
            / f"{invocation_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return payload

    def prepare_mailbox_probe_dirs(self, plan) -> None:
        mailbox_probe = self.operation(plan, "connective.mailbox-probe")
        Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
        Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)

    def activate_orchestrator_from_stage(self, name: str):
        root, plan, backend = self.orchestrator_local_stage_fixture(name)
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIn(staged.status, {"applied", "noop"}, staged.errors)
        self.assertIsNotNone(staged.receipt, staged.errors)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        self.prepare_mailbox_probe_dirs(plan)
        activated = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        return root, plan, backend, activated

    def test_versioned_manifest_preserves_roles_consumers(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        self.assertGreaterEqual(manifest.version, 2)
        self.assertEqual(resolve_instance_role("recovery"), "recovery")
        self.assertEqual(instance_binding("recovery")["account"], "claude-fallback")
        runtime = load_binding(REPO_ROOT / "instance.yaml", "recovery")
        self.assertEqual(runtime.role, "recovery")
        self.assertEqual(runtime.account, "claude-fallback")
        from tools import context_fill_monitor, restart_drain, ticket

        self.assertEqual(
            context_fill_monitor.AGENT_CONFIG["recovery"]["sudo_user"],
            "claude-fallback",
        )
        self.assertEqual(
            restart_drain.DRAIN_AGENT_CONFIG["recovery"]["restart_service"],
            # T-2684 (reverted T-3082, 2026-08-23): recovery is NOT control-
            # plane-supervised pre-T-3026 — restart_service stays the RAW
            # bespoke unit (T-3026 will re-derive to the consume unit once
            # the interactive-session cutover lands).
            "claude-telegram-fallback.service",
        )
        self.assertIn("recovery", ticket._valid_target_values())
        self.assertIn("codex", ticket._valid_target_values())

    def test_quota_cadence_manifest_default_and_allowlist(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        quota = manifest.installer["quota_check"]
        self.assertEqual(quota["cadence_minutes"], 30)
        self.assertGreater(quota["randomized_delay_seconds"], 0)
        self.assertEqual(
            quota_check_personas(REPO_ROOT / "instance.yaml"),
            frozenset({"specialist", "recovery"}),
        )
        orchestrator = load_binding(REPO_ROOT / "instance.yaml", "orchestrator")
        supported, reason = quota_check_supported(
            orchestrator,
            manifest_path=REPO_ROOT / "instance.yaml",
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "persona-not-allowlisted")

    def test_quota_systemd_units_disabled_first_and_randomized(self) -> None:
        plan = self.plan()
        quota_timer = self.operation(plan, "quota.timer-unit")
        self.assertIn("OnUnitInactiveSec=30min", quota_timer.desired["content"])
        self.assertIn("RandomizedDelaySec=300s", quota_timer.desired["content"])
        self.assertNotIn("timer-enable", [item.kind for item in plan.operations if item.operation_id.startswith("quota.")])

        raw = _manifest_dict()
        raw["persona_installer"]["quota_check"] = {
            "cadence_minutes": 17,
            "randomized_delay_seconds": 45,
            "personas": ["specialist", "recovery"],
            "service_template": "tools/systemd/pantheon-persona-quota@.service",
            "timer_template": "tools/systemd/pantheon-persona-quota@.timer",
        }
        self.write_manifest(raw)
        changed = self.installer.plan(
            Manifest.load(self.manifest_path),
            "recovery",
            root=self.root / "projection",
        )
        rendered = self.operation(changed, "quota.timer-unit").desired["content"]
        self.assertIn("OnUnitInactiveSec=17min", rendered)
        self.assertIn("RandomizedDelaySec=45s", rendered)

    def test_orchestrator_installer_plan_is_disabled_first_fire_and_references_only(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=self.root / "projection",
        )
        ids = [operation.operation_id for operation in plan.operations]
        self.assertEqual(plan.selected_harness, "claude")
        self.assertIn("codex-telegram.unit-install", ids)
        self.assertIn("codex-telegram.disabled-first", ids)
        self.assertIn("codex-standing-instructions.install", ids)
        self.assertLess(ids.index("codex-telegram.unit-install"), ids.index("codex-telegram.disabled-first"))
        self.assertLess(ids.index("codex-standing-instructions.source-check"), ids.index("codex-standing-instructions.install"))
        unit = self.operation(plan, "codex-telegram.unit-install")
        disabled = self.operation(plan, "codex-telegram.disabled-first")
        self.assertEqual(disabled.desired["active"], "inactive")
        self.assertEqual(disabled.desired["enabled"], "disabled")
        self.assertEqual(disabled.desired["environment_file"], "forbidden")
        payload = json.dumps(plan.to_dict(), sort_keys=True)
        self.assertIn("/home/codex/codex-config", payload)
        self.assertIn(".claude/channels/telegram/.env", payload)
        self.assertIn(".codex/AGENTS.md", payload)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", payload)
        self.assertNotIn("EnvironmentFile", unit.desired["content"])

    def test_installer_brief_check_uses_resolved_persona_brief_ref_in_rooted_fixture(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        root = self.root / "projection"
        brief_path = root / "home" / "claude" / "CLAUDE.md"
        brief_path.parent.mkdir(parents=True)
        brief_path.write_text("canonical Orchestrator brief\n", encoding="utf-8")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=root,
        )
        brief = self.operation(plan, "persona.brief")
        self.assertEqual(Path(brief.target), brief_path)
        self.assertEqual(brief.desired["source"], "account-home-ref")

        backend = LocalHostBackend()
        preflight = backend.preflight(brief)
        self.assertTrue(preflight.ok, preflight.detail)
        self.assertEqual(preflight.detail, "persona brief present")

        brief_path.unlink()
        missing = backend.preflight(brief)
        self.assertFalse(missing.ok)
        self.assertEqual(missing.detail, "persona brief missing")

    def test_orchestrator_codex_standing_instructions_resolve_ho_identity_from_manifest_reference(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=self.root / "projection",
        )
        source_check = self.operation(plan, "codex-standing-instructions.source-check")
        install = self.operation(plan, "codex-standing-instructions.install")
        target_check = self.operation(plan, "codex-standing-instructions.target-check")
        override_check = self.operation(plan, "codex-standing-instructions.override-check")
        self.assertEqual(source_check.desired["identity"], "orchestrator")
        self.assertEqual(source_check.desired["source"], "pinned-rules/orchestrator-codex.md")
        self.assertEqual(
            source_check.desired["source_commit"],
            R10_ORCHESTRATOR_CODEX_COMMIT,
        )
        self.assertEqual(
            source_check.desired["source_sha256"],
            R10_ORCHESTRATOR_CODEX_SHA256,
        )
        self.assertEqual(Path(install.target).name, "AGENTS.md")
        self.assertEqual(Path(install.target).parent.name, ".codex")
        self.assertEqual(target_check.desired["sha256"], source_check.desired["source_sha256"])
        self.assertEqual(override_check.desired["state"], "absent-or-empty")

        forbidden = CodexStandingInstructionsReference(
            persona="orchestrator",
            account="claude",
            source_repo="pantheon-config",
            source=Path("/home/codex/codex-config/AGENTS.md"),
            source_commit=R10_ORCHESTRATOR_CODEX_COMMIT,
            source_sha256="0" * 64,
            target=Path("/home/claude/.codex/AGENTS.md"),
            override_target=Path("/home/claude/.codex/AGENTS.override.md"),
            override_must_be_absent=True,
            forbidden_source_paths=(Path("/home/codex/codex-config/AGENTS.md"),),
            identity="orchestrator",
            canonical_source_of_truth="/home/claude/CLAUDE.md",
        )
        with self.assertRaises(ManifestError):
            _read_verified_standing_instructions(forbidden, repo_root=REPO_ROOT)

    def test_orchestrator_codex_standing_instructions_pin_cairn_r10_object_and_sha(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        reference = codex_standing_instructions_reference(manifest, "orchestrator")
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.source, Path("pinned-rules/orchestrator-codex.md"))
        self.assertEqual(reference.source_commit, R10_ORCHESTRATOR_CODEX_COMMIT)
        self.assertEqual(reference.source_sha256, R10_ORCHESTRATOR_CODEX_SHA256)

        cat_file = subprocess.run(
            ("git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{reference.source_commit}^{{commit}}"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(cat_file.returncode, 0, cat_file.stderr)
        ancestor = subprocess.run(
            ("git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", reference.source_commit, "HEAD"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ancestor.returncode, 0, ancestor.stderr)
        source_at_commit = subprocess.run(
            ("git", "-C", str(REPO_ROOT), "show", f"{reference.source_commit}:{reference.source}"),
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(hashlib.sha256(source_at_commit).hexdigest(), reference.source_sha256)
        worktree = (REPO_ROOT / reference.source).read_bytes()
        self.assertEqual(hashlib.sha256(worktree).hexdigest(), reference.source_sha256)
        self.assertIn(b"mcp__telegram__reply", worktree)

    def test_persona_brief_ref_rejects_absolute_traversal_and_out_of_account_paths(self) -> None:
        for value in (
            "/home/claude/CLAUDE.md",
            "../CLAUDE.md",
            "rules/../../CLAUDE.md",
            "../codex/AGENTS.md",
            "",
        ):
            with self.subTest(value=value):
                data = _manifest_dict(second_persona=True)
                data["roles"]["recovery"]["persona_brief_ref"] = value
                self.write_manifest(data)
                with self.assertRaises(ManifestError):
                    Manifest.load(self.manifest_path).binding("recovery")
                with self.assertRaises(ValueError):
                    load_binding(self.manifest_path, "recovery")

    def test_plan_marks_operator_required_and_privileged_steps(self) -> None:
        plan = self.plan()
        privileges = {item.privilege for item in plan.operations}
        self.assertEqual(
            privileges,
            {"unprivileged", "privileged-helper", "operator-required"},
        )
        self.assertTrue(plan.operator_actions)
        self.assertTrue(
            all(
                item.provenance.get("required_capability")
                for item in plan.operations
                if item.privilege != "unprivileged"
            )
        )

    def test_manifest_relative_paths_cannot_escape_declared_roots(self) -> None:
        mutations = (
            ("paths", "unit_dir", "/etc/systemd/user"),
            ("paths", "state_dir", "../../operator-state"),
            ("systemd", "service_template", "../foreign.service"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                raw = _manifest_dict()
                raw["persona_installer"][section][key] = value
                self.write_manifest(raw)
                manifest = Manifest.load(self.manifest_path)
                with self.assertRaises(ManifestError):
                    self.installer.plan(
                        manifest,
                        "recovery",
                        root=self.root / "projection",
                    )
        raw = _manifest_dict()
        raw["persona_installer"]["policy"]["bundle"] = ["/etc/"]
        self.write_manifest(raw)
        with self.assertRaises(ManifestError):
            self.installer.plan(
                Manifest.load(self.manifest_path),
                "recovery",
                root=self.root / "projection",
            )

    def test_apply_refuses_unavailable_privilege(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan, all_capabilities=False),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})
        self.assertEqual(backend.events, [])

    def test_apply_refuses_backend_unavailable_before_mutation(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(unavailable_kinds=("manager-check",))
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})
        self.assertEqual(backend.events, [])

    def test_plan_digest_mismatch_refuses_before_mutation(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        report = self.installer.apply(
            plan,
            "0" * 64,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})

    def test_unknown_existing_file_fails_closed(self) -> None:
        plan = self.plan()
        target = self.operation(plan, "connective.service-unit").target
        backend = SimulatedHostBackend()
        backend.seed_file(target, "operator-owned file\n")
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(
            json.loads(json.dumps(backend.entries[target])),
            backend.entries[target],
        )
        self.assertEqual(backend.events, [])

    def test_apply_failure_rolls_back_only_owned_unchanged_paths(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(fail_operation="connective.daemon-reload")
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertGreater(report.changed, 0)
        self.assertEqual(backend.entries, {})
        self.assertTrue(all("restored" in item for item in report.rollback))

    def test_rollback_refuses_post_apply_drift(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertIsNotNone(applied.receipt)
        target = self.operation(plan, "connective.service-unit").target
        backend.drift(target, "operator changed this after apply\n")
        report = self.installer.rollback(
            applied.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertIn("drifted", report.errors[0])

    def test_user_systemd_first_fire_precedes_timer_enable(self) -> None:
        plan = self.plan()
        backend, report = self.apply(plan)
        self.assertEqual(report.status, "applied")
        self.assertLess(
            backend.events.index("harness.direct-fire"),
            backend.events.index("connective.timer-enable"),
        )
        self.assertLess(
            backend.events.index("connective.unit-fire"),
            backend.events.index("connective.timer-enable"),
        )
        self.assertLess(
            backend.events.index("connective.mailbox-probe"),
            backend.events.index("connective.timer-enable"),
        )

    def test_timer_next_elapse_checked_after_oneshot_inactive(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(finite_next_elapse=False)
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("timer observation incomplete", report.errors[0])
        self.assertFalse(backend.timer_enabled)

    def test_finite_next_elapse_basis_accepts_realtime_and_monotonic(self) -> None:
        cases = (
            (
                {
                    "NextElapseUSecRealtime": "Sat 2026-08-02 14:40:00 UTC",
                    "NextElapseUSecMonotonic": "infinity",
                },
                "realtime",
            ),
            (
                {
                    "NextElapseUSecRealtime": "",
                    "NextElapseUSecMonotonic": " 2min 5s ",
                },
                "monotonic",
            ),
            (
                {
                    "NextElapseUSecRealtime": "tomorrow",
                    "NextElapseUSecMonotonic": "5min",
                },
                "realtime",
            ),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(_finite_next_elapse_basis(values), expected)

    def test_finite_next_elapse_basis_rejects_systemd_sentinels(self) -> None:
        invalid_values = ("", "0", "n/a", "N/A", "infinity", " InFiNiTy ")
        for realtime in invalid_values:
            for monotonic in invalid_values:
                with self.subTest(realtime=realtime, monotonic=monotonic):
                    self.assertIsNone(
                        _finite_next_elapse_basis(
                            {
                                "NextElapseUSecRealtime": realtime,
                                "NextElapseUSecMonotonic": monotonic,
                            }
                        )
                    )

    def test_timer_acceptance_requires_first_fire_invocation_receipt(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(finite_next_elapse=False)
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("timer observation incomplete", report.errors[0])
        self.assertFalse(backend.timer_enabled)

    def test_mailbox_acl_send_and_receive_are_both_verified(self) -> None:
        plan = self.plan()
        for send, receive in ((False, True), (True, False)):
            with self.subTest(send=send, receive=receive):
                backend = SimulatedHostBackend(
                    mailbox_send=send,
                    mailbox_receive=receive,
                )
                report = self.installer.apply(
                    plan,
                    plan.digest,
                    backend,
                    self.actor(plan),
                )
                self.assertEqual(report.status, "failed")
                self.assertIn("mailbox ACL direction failed", report.errors[0])

    def test_secret_values_and_auth_file_copy_are_rejected(self) -> None:
        for key in ("value", "auth_file", "copy_from", "template"):
            with self.subTest(key=key):
                raw = _manifest_dict()
                raw["roles"]["recovery"]["credential_refs"] = [
                    {
                        "id": "github/recovery",
                        "kind": "deploy-key",
                        "provider": "host-file",
                        "owner": "claude-fallback",
                        "scope": ["repo:read"],
                        "rotation": {"strategy": "replace", "revoke": "remove old"},
                        "direct_use_justification": "provider cannot mint leases",
                        key: "forbidden",
                    }
                ]
                self.write_manifest(raw)
                manifest = Manifest.load(self.manifest_path)
                with self.assertRaises(ManifestError):
                    manifest.binding("recovery")

    def test_credential_ref_requires_provider_scope_and_rotation_plan(self) -> None:
        raw = _manifest_dict()
        valid = {
            "id": "github/recovery",
            "kind": "deploy-key",
            "provider": "host-file",
            "owner": "claude-fallback",
            "scope": ["repo:read"],
            "rotation": {"strategy": "replace", "revoke": "remove old grant"},
            "location": "/run/user/1001/pantheon/github-recovery",
            "direct_use_justification": "provider cannot mint leases",
        }
        raw["roles"]["recovery"]["credential_refs"] = [valid]
        self.write_manifest(raw)
        binding = Manifest.load(self.manifest_path).binding("recovery")
        self.assertEqual(binding.credential_refs[0].reference_id, "github/recovery")
        for missing in ("provider", "scope", "rotation"):
            with self.subTest(missing=missing):
                broken = _manifest_dict()
                item = dict(valid)
                item.pop(missing)
                broken["roles"]["recovery"]["credential_refs"] = [item]
                self.write_manifest(broken)
                with self.assertRaises(ManifestError):
                    Manifest.load(self.manifest_path).binding("recovery")

    def test_harness_switch_preserves_persona_role_and_owner(self) -> None:
        normal = self.plan()
        switched = self.plan(harness="codex")
        self.assertEqual(
            (normal.persona, normal.role, normal.account),
            (switched.persona, switched.role, switched.account),
        )
        self.assertEqual(normal.configured_harness, "claude")
        self.assertEqual(switched.selected_harness, "codex")

    def test_verify_reports_all_layers_and_provenance(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertEqual(applied.status, "applied")
        report = self.installer.verify(plan, backend)
        self.assertEqual(report.status, "verified")
        self.assertEqual([item.layer for item in report.layers], [0, 1, 2, 3, 4])
        self.assertTrue(all(item.ok for item in report.layers))
        self.assertEqual(report.provenance["persona"], "recovery")
        self.assertEqual(report.provenance["instance"], "test-instance")

    def test_converged_enabled_all_in_one_apply_returns_noop(self) -> None:
        plan = self.plan()
        backend, first = self.apply(plan)
        second = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(first.status, "applied")
        self.assertEqual(second.status, "noop")
        self.assertEqual(second.changed, 0)
        self.assertIsNotNone(second.receipt)
        assert second.receipt is not None
        self.assertNotIn(
            "connective.timer-enablement-absent",
            {item.operation_id for item in second.receipt.mutations},
        )
        self.assertNotIn(
            "connective.timer-enablement-absent",
            {item.operation_id for item in second.receipt.evidence},
        )

    @_with_codex_config_fixture
    def test_all_in_one_fresh_timer_enable_issues_enable_command(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r30-all-in-one-fresh")
        for subdir in ("tmp", "new", "cur"):
            (root / "home" / "the-ancestor" / "mailbox" / "recovery" / subdir).mkdir(parents=True, exist_ok=True)
        self.write_invocation_viability_receipt(plan)
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        self.assertIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-persona-consume@orchestrator.timer"),
            backend.commands,
        )
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutation = next(item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable")
        link, _template = self.consume_timer_link_paths(root)
        self.assertTrue(timer_mutation.changed)
        self.assertEqual(timer_mutation.before.get("owned"), "true")
        self.assertEqual(timer_mutation.before.get("enablement_link_path"), str(link))
        self.assertNotIn(
            "connective.timer-enablement-absent",
            {item.operation_id for item in report.receipt.mutations},
        )

    @_with_codex_config_fixture
    def test_staged_fresh_timer_enable_issues_enable_command_and_records_exact_link(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r30-staged-fresh")
        for subdir in ("tmp", "new", "cur"):
            (root / "home" / "the-ancestor" / "mailbox" / "recovery" / subdir).mkdir(parents=True, exist_ok=True)
        self.write_invocation_viability_receipt(plan)
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        before_commands = len(backend.commands)
        report = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "applied", report.errors)
        self.assertIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-persona-consume@orchestrator.timer"),
            backend.commands[before_commands:],
        )
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutation = next(item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable")
        link, _template = self.consume_timer_link_paths(root)
        self.assertTrue(timer_mutation.changed)
        self.assertEqual(timer_mutation.before.get("owned"), "true")
        self.assertEqual(timer_mutation.before.get("enablement_link_path"), str(link))
        self.assertTrue(timer_mutation.before.get("enablement_link_actual_readlink"))

    def test_timer_enable_context_is_explicit_immutable_and_not_backend_ambient(self) -> None:
        self.assertEqual(EXECUTION_CONTEXT_ALL_IN_ONE.workflow, "all-in-one")
        self.assertEqual(EXECUTION_CONTEXT_STAGE_PREFIX.workflow, "stage-prefix")
        self.assertEqual(EXECUTION_CONTEXT_STAGED_ACTIVATE.workflow, "staged-activate")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            EXECUTION_CONTEXT_ALL_IN_ONE.workflow = "staged-activate"  # type: ignore[misc]
        plan = self.plan()
        operation = self.operation(plan, "connective.timer-enable")
        backend = SimulatedHostBackend()
        with self.assertRaises(TypeError):
            backend.execute(operation)  # type: ignore[call-arg]

        class ContextRecordingBackend(SimulatedHostBackend):
            def __init__(self) -> None:
                super().__init__()
                self.timer_contexts: list[ExecutionContext] = []

            def execute(self, operation, context):  # type: ignore[override]
                if operation.operation_id == "connective.timer-enable":
                    self.timer_contexts.append(context)
                return super().execute(operation, context)

        all_backend = ContextRecordingBackend()
        all_report = self.installer.apply(plan, plan.digest, all_backend, self.actor(plan))
        self.assertEqual(all_report.status, "applied", all_report.errors)
        self.assertEqual(all_backend.timer_contexts, [EXECUTION_CONTEXT_ALL_IN_ONE])

        staged_backend = ContextRecordingBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, staged_backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        assert staged.receipt is not None
        activated = self.installer.activate_staged(plan, plan.digest, staged.receipt, staged_backend, self.actor(plan))
        self.assertEqual(activated.status, "applied", activated.errors)
        self.assertEqual(staged_backend.timer_contexts, [EXECUTION_CONTEXT_STAGED_ACTIVATE])

    def test_successful_rollback_disables_owned_timer_and_restores_files(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertTrue(backend.timer_enabled)
        report = self.installer.rollback(
            applied.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "rolled-back")
        self.assertFalse(backend.timer_enabled)
        self.assertEqual(backend.entries, {})

    def test_staged_receipt_precedes_activation_and_recovers_crash(self) -> None:
        class CrashAfterTimerEnable(SimulatedHostBackend):
            def execute(self, operation, context):
                result = super().execute(operation, context)
                if operation.kind == "timer-enable":
                    raise SystemExit("simulated process crash")
                return result

        plan = self.plan()
        backend = CrashAfterTimerEnable()
        with self.assertRaisesRegex(SystemExit, "simulated process crash"):
            self.installer.apply(
                plan,
                plan.digest,
                backend,
                self.actor(plan),
            )
        self.assertTrue(backend.timer_enabled)
        self.assertLess(
            backend.events.index("receipt:staged"),
            backend.events.index("connective.timer-enable"),
        )
        raw_receipt = next(iter(backend.receipts.values()))
        self.assertEqual(raw_receipt["phase"], "staged")
        receipt = ApplyReceipt.from_dict(raw_receipt)
        staged_targets = {
            mutation.target
            for mutation in receipt.mutations
            if mutation.changed and mutation.kind != "timer-enable"
        }
        rollback = self.installer.rollback(receipt, backend, self.actor(plan))
        self.assertEqual(rollback.status, "rolled-back")
        self.assertTrue(backend.timer_enabled)
        self.assertTrue(all(target not in backend.entries for target in staged_targets))
        self.assertIn(self.operation(plan, "connective.service-unit").target, backend.entries)

    def test_failed_receipt_retains_partial_timer_enable_intent(self) -> None:
        class PartialTimerFailure(SimulatedHostBackend):
            suppress_first_timer_rollback = True

            def execute(self, operation, context):
                result = super().execute(operation, context)
                if operation.kind == "timer-enable":
                    raise OperationFailed(
                        "enable returned failure after side effect",
                        evidence={
                            "enable_command_issued": "true",
                            "enable_returncode": "1",
                        },
                    )
                return result

            def can_rollback(self, mutation):
                if (
                    mutation.kind == "timer-enable"
                    and self.suppress_first_timer_rollback
                ):
                    self.suppress_first_timer_rollback = False
                    return PreflightResult(
                        False,
                        "injected",
                        "first automatic timer rollback unavailable",
                    )
                return super().can_rollback(mutation)

        plan = self.plan()
        backend = PartialTimerFailure()
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertTrue(backend.timer_enabled)
        self.assertEqual(report.receipt.phase, "failed")
        self.assertIn(
            "connective.timer-enable",
            {item.operation_id for item in report.receipt.mutations},
        )
        recovered = self.installer.rollback(
            report.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(recovered.status, "rolled-back")
        self.assertFalse(backend.timer_enabled)
        self.assertEqual(backend.entries, {})

    def test_stage_disabled_apply_cannot_start_runtime_or_receiver(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        plan = self.installer.plan(
            manifest,
            "orchestrator",
            harness_override="claude",
            root=self.root / "projection",
        )
        backend = SimulatedHostBackend()
        report = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIn(report.status, {"applied", "noop"})
        activation_kinds = {
            "daemon-reload",
            "direct-fire",
            "unit-fire",
            "mailbox-probe",
            "timer-enable",
            "timer-observe",
        }
        executed = [
            operation
            for operation in plan.operations
            if operation.operation_id in backend.events
        ]
        self.assertTrue(executed)
        self.assertTrue(all(operation.kind not in activation_kinds for operation in executed))
        self.assertIn("codex-telegram.disabled-first", backend.events)
        self.assertIn("codex-standing-instructions.override-check", backend.events)
        self.assertNotIn("codex-telegram.daemon-reload", backend.events)
        self.assertNotIn("connective.unit-fire", backend.events)

    @_with_codex_config_fixture
    def test_activation_suffix_starts_with_stale_unit_failure_reset_before_unit_installs(self) -> None:
        _root, plan, _backend = self.orchestrator_local_stage_fixture("r27-order")
        activation = plan.operations[self.installer._activation_index(plan) :]
        self.assertGreater(len(activation), 1)
        self.assertEqual(activation[0].operation_id, "connective.stale-unit-failure-reset")
        self.assertEqual(activation[0].kind, "stale-unit-failure-reset")
        self.assertEqual(activation[0].target, "pantheon-persona-consume@orchestrator.service")
        activation_ids = [operation.operation_id for operation in activation]
        for later in (
            "codex-telegram.daemon-reload",
            "connective.service-unit",
            "connective.timer-unit",
            "quota.service-unit",
            "quota.timer-unit",
            "connective.daemon-reload",
            "harness.direct-fire",
            "connective.unit-fire",
            "connective.timer-enable",
        ):
            if later in activation_ids:
                self.assertLess(
                    activation_ids.index("connective.stale-unit-failure-reset"),
                    activation_ids.index(later),
                    activation_ids,
                )

    @_with_codex_config_fixture
    def test_stage_disabled_does_not_execute_stale_unit_failure_reset_or_consume_unit_install(self) -> None:
        _root, plan, backend = self.orchestrator_local_stage_fixture("r27-stage-boundary")
        report = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIn(report.status, {"applied", "noop"}, report.errors)
        self.assertNotIn("connective.stale-unit-failure-reset", backend.events)
        self.assertNotIn("connective.service-unit", backend.events)
        self.assertNotIn("quota.service-unit", backend.events)
        self.assertFalse(
            any(
                command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
                and "-p" in command
                and "LoadState" in command
                for command in backend.commands
            ),
            backend.commands,
        )

    @_with_codex_config_fixture
    def test_activate_staged_clean_not_found_inactive_noops_then_installs_once(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r27-clean")
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt, staged.errors)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        self.prepare_mailbox_probe_dirs(plan)
        activation_start = len(backend.commands)
        activated = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        self.assertIsNotNone(activated.receipt)
        assert activated.receipt is not None
        stale = next(item for item in activated.receipt.mutations if item.operation_id == "connective.stale-unit-failure-reset")
        self.assertFalse(stale.changed)
        self.assertEqual(stale.before["state"], "clean-noop")
        self.assertTrue((root / "home" / "claude" / ".config" / "systemd" / "user" / "pantheon-persona-consume@.service").is_file())
        self.assertEqual(
            sum(1 for item in activated.receipt.mutations if item.operation_id == "connective.service-unit"),
            1,
        )
        commands = self.activation_commands(backend, activation_start)
        stale_show_index = next(
            index
            for index, command in enumerate(commands)
            if command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
            and "LoadState" in command
        )
        start_index = next(index for index, command in enumerate(commands) if command[:3] == ("systemctl", "--user", "start"))
        self.assertLess(stale_show_index, start_index, commands)
        self.assertNotIn(("systemctl", "--user", "reset-failed", "pantheon-persona-consume@orchestrator.service"), commands)

    @_with_codex_config_fixture
    def test_activate_staged_resets_not_found_failed_stale_invocation_before_any_activation_mutation(self) -> None:
        _root, plan, backend = self.orchestrator_local_stage_fixture("r27-reset")
        backend.stale_unit_show_sequence = [self.stale_unit_failed_show(), self.stale_unit_clean_show()]
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt, staged.errors)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        self.prepare_mailbox_probe_dirs(plan)
        activation_start = len(backend.commands)
        activated = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        commands = self.activation_commands(backend, activation_start)
        reset_index = commands.index(("systemctl", "--user", "reset-failed", "pantheon-persona-consume@orchestrator.service"))
        later_mutations = [
            index
            for index, command in enumerate(commands)
            if command == ("systemctl", "--user", "daemon-reload")
            or command[:3] == ("systemctl", "--user", "start")
            or command[:4] == ("systemctl", "--user", "enable", "--now")
        ]
        self.assertTrue(later_mutations, commands)
        self.assertTrue(all(reset_index < index for index in later_mutations), commands)
        evidence = backend.events["connective.stale-unit-failure-reset"].evidence
        self.assertEqual(evidence["classification"], "reset")
        self.assertEqual(evidence["pre_result"], "exit-code")
        self.assertEqual(evidence["post_state"], "clean-noop")

    @_with_codex_config_fixture
    def test_stale_unit_failure_reset_refuses_loaded_failed_before_activation_mutation(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r27-loaded")
        backend.stale_unit_show_sequence = [
            self.stale_unit_failed_show(load_state="loaded"),
        ]
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt, staged.errors)
        assert staged.receipt is not None
        activation_start = len(backend.commands)
        report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIn("pre-existing loaded service", report.errors[0])
        commands = self.activation_commands(backend, activation_start)
        self.assert_no_later_activation_systemctl(commands)
        self.assertFalse((root / "home" / "claude" / ".config" / "systemd" / "user" / "pantheon-persona-consume@.service").exists())

    @_with_codex_config_fixture
    def test_stale_unit_failure_reset_refuses_active_or_nonempty_job_before_activation_mutation(self) -> None:
        cases = {
            "active": self.stale_unit_failed_show(active_state="active", sub_state="running", result="success", exec_main_status="0"),
            "activating": self.stale_unit_failed_show(active_state="activating", sub_state="start", result="success", exec_main_status="0"),
            "job": self.stale_unit_failed_show(active_state="inactive", sub_state="dead", result="success", exec_main_status="0", invocation_id="", job="42"),
        }
        for label, stale_show in cases.items():
            with self.subTest(label=label):
                _root, plan, backend = self.orchestrator_local_stage_fixture(f"r27-active-{label}")
                backend.stale_unit_show_sequence = [stale_show]
                staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
                self.assertIsNotNone(staged.receipt, staged.errors)
                assert staged.receipt is not None
                activation_start = len(backend.commands)
                report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
                self.assertEqual(report.status, "failed")
                self.assert_no_later_activation_systemctl(self.activation_commands(backend, activation_start))

    @_with_codex_config_fixture
    def test_stale_unit_failure_reset_refuses_malformed_or_ambiguous_show_before_activation_mutation(self) -> None:
        cases = {
            "missing": "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\n",
            "duplicate": self.stale_unit_clean_show() + "Job=\n",
            "unexpected": self.stale_unit_clean_show() + "MainPID=0\n",
            "bad-invocation": self.stale_unit_failed_show(invocation_id="A" * 32),
            "future-result": self.stale_unit_failed_show(result="timeout"),
            "signed-status": self.stale_unit_failed_show(exec_main_status="+1"),
            "zero-status-stale": self.stale_unit_failed_show(exec_main_status="0"),
            "padded-load-state": self.stale_unit_clean_show().replace(
                "LoadState=not-found\n",
                "LoadState= not-found\n",
            ),
            "padded-invocation": self.stale_unit_failed_show(
                invocation_id=f"{TEST_INVOCATION_ID} "
            ),
            "padded-job": self.stale_unit_clean_show().replace("Job=\n", "Job= \n"),
            "padded-exec-main-status": self.stale_unit_failed_show(
                exec_main_status="1 "
            ),
        }
        for label, stale_show in cases.items():
            with self.subTest(label=label):
                _root, plan, backend = self.orchestrator_local_stage_fixture(f"r27-malformed-{label}")
                backend.stale_unit_show_sequence = [stale_show]
                staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
                self.assertIsNotNone(staged.receipt, staged.errors)
                assert staged.receipt is not None
                activation_start = len(backend.commands)
                report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
                self.assertEqual(report.status, "failed")
                commands = self.activation_commands(backend, activation_start)
                self.assertNotIn(
                    ("systemctl", "--user", "reset-failed", "pantheon-persona-consume@orchestrator.service"),
                    commands,
                )
                self.assert_no_later_activation_systemctl(commands)

    @_with_codex_config_fixture
    def test_stale_unit_failure_reset_failure_or_residual_stale_fields_aborts_before_activation_mutation(self) -> None:
        class ResetFailureBackend(LocalHostStageBackend):
            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                if command == ("systemctl", "--user", "reset-failed", "pantheon-persona-consume@orchestrator.service"):
                    self.commands.append(command)
                    self.timeouts.append((command, timeout))
                    return subprocess.CompletedProcess(command, 1, "", "reset refused\n")
                return super()._run(argv, timeout=timeout)

        cases = {
            "reset-failed": ResetFailureBackend(),
            "residual": LocalHostStageBackend(),
        }
        cases["reset-failed"].stale_unit_show_sequence = [self.stale_unit_failed_show()]
        cases["residual"].stale_unit_show_sequence = [self.stale_unit_failed_show(), self.stale_unit_failed_show()]
        for label, backend in cases.items():
            with self.subTest(label=label):
                _root, plan, _unused = self.orchestrator_local_stage_fixture(f"r27-residual-{label}")
                staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
                self.assertIsNotNone(staged.receipt, staged.errors)
                assert staged.receipt is not None
                activation_start = len(backend.commands)
                report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
                self.assertEqual(report.status, "failed")
                commands = self.activation_commands(backend, activation_start)
                self.assertIn(("systemctl", "--user", "reset-failed", "pantheon-persona-consume@orchestrator.service"), commands)
                self.assert_no_later_activation_systemctl(commands)

    @_with_codex_config_fixture
    def test_stale_unit_failure_reset_receipt_is_evidence_only_not_rollback_intent(self) -> None:
        _root, plan, backend = self.orchestrator_local_stage_fixture("r27-receipt")
        backend.stale_unit_show_sequence = [self.stale_unit_failed_show(), self.stale_unit_clean_show()]
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt, staged.errors)
        assert staged.receipt is not None
        self.assertNotIn(
            "connective.stale-unit-failure-reset",
            {item.operation_id for item in staged.receipt.mutations},
        )
        self.write_invocation_viability_receipt(plan)
        self.prepare_mailbox_probe_dirs(plan)
        activated = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        self.assertIsNotNone(activated.receipt)
        assert activated.receipt is not None
        stale_mutations = [
            item for item in activated.receipt.mutations if item.operation_id == "connective.stale-unit-failure-reset"
        ]
        self.assertEqual(len(stale_mutations), 1)
        stale = stale_mutations[0]
        self.assertFalse(stale.changed)
        self.assertEqual(stale.before["state"], "reset-required")
        self.assertEqual(stale.after_fingerprint, "evidence-only")
        self.assertNotIn("failed", stale.after_fingerprint)
        stale_evidence = [
            item for item in activated.receipt.evidence if item.operation_id == "connective.stale-unit-failure-reset"
        ]
        self.assertEqual(len(stale_evidence), 1)
        self.assertEqual(stale_evidence[0].evidence["pre_state"], "reset-required")
        self.assertEqual(stale_evidence[0].evidence["post_state"], "clean-noop")

    def test_cutover_activation_requires_matching_staged_receipt(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        stale = dataclasses.replace(staged.receipt, plan_digest="0" * 64)
        refused = self.installer.activate_staged(
            plan,
            plan.digest,
            stale,
            backend,
            self.actor(plan),
        )
        self.assertEqual(refused.status, "refused")
        self.assertIn("receipt plan digest mismatch", refused.errors)
        self.assertNotIn("connective.unit-fire", backend.events)

    def test_activation_failure_rollback_removes_staged_disabled_artifacts(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(fail_operation="connective.unit-fire")
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        report = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        self.assertIn("rollback:connective.service-unit", backend.events)
        self.assertEqual(backend.entries, {})

    def test_pre_activation_rollback_staged_uses_checkpoint_without_manifest(self) -> None:
        backend = SimulatedHostBackend()
        exit_code, summary = persona_apply.stage_disabled(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            "stamp-r5",
            backend=backend,
            root=self.root / "projection",
        )
        self.assertEqual(exit_code, 0, summary)
        account_home = self.root / "projection" / "home" / "claude-fallback"
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "recovery",
            "stamp-r5",
            account_home,
            backend=backend,
        )
        self.assertEqual(rollback_code, 0, rollback_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=rolled-back", rollback_summary)
        self.assertEqual(backend.entries, {})

    def test_staged_noop_drift_blocks_check_and_activation_before_suffix(self) -> None:
        backend = SimulatedHostBackend()
        stamp = "stamp-r6-drift"
        for _ in range(2):
            exit_code, summary = persona_apply.stage_disabled(
                self.manifest_path,
                "recovery",
                "claude",
                REPO_ROOT,
                stamp,
                backend=backend,
                root=self.root / "projection",
            )
            self.assertEqual(exit_code, 0, summary)
        plan = self.installer.plan(
            Manifest.load(self.manifest_path),
            "recovery",
            harness_override="claude",
            root=self.root / "projection",
        )
        target_operation = self.operation(plan, "persona.binding")
        target = target_operation.target
        suffix_ids = {
            operation.operation_id
            for operation in plan.operations[self.installer._activation_index(plan) :]
        }
        suffix_count = sum(1 for event in backend.events if event in suffix_ids)
        backend.drift(
            target,
            f"# {MANAGED_MARKER}\nmanaged but no longer desired\n",
        )
        ok, message = persona_apply.check_staged(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection",
        )
        self.assertFalse(ok, message)
        self.assertIn(f"{target_operation.operation_id}: staged invariant failed", message)
        activate_code, activate_summary = persona_apply.activate_staged(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection",
        )
        self.assertNotEqual(activate_code, 0, activate_summary)
        self.assertIn(f"{target_operation.operation_id}: staged invariant failed", activate_summary)
        self.assertEqual(
            suffix_count,
            sum(1 for event in backend.events if event in suffix_ids),
        )

    def test_staged_noop_delete_blocks_check_and_activation_before_suffix(self) -> None:
        backend = SimulatedHostBackend()
        stamp = "stamp-r6-delete"
        for _ in range(2):
            exit_code, summary = persona_apply.stage_disabled(
                self.manifest_path,
                "recovery",
                "claude",
                REPO_ROOT,
                stamp,
                backend=backend,
                root=self.root / "projection",
            )
            self.assertEqual(exit_code, 0, summary)
        plan = self.installer.plan(
            Manifest.load(self.manifest_path),
            "recovery",
            harness_override="claude",
            root=self.root / "projection",
        )
        target_operation = self.operation(plan, "persona.binding")
        target = target_operation.target
        suffix_ids = {
            operation.operation_id
            for operation in plan.operations[self.installer._activation_index(plan) :]
        }
        suffix_count = sum(1 for event in backend.events if event in suffix_ids)
        backend.entries.pop(target, None)
        ok, message = persona_apply.check_staged(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection",
        )
        self.assertFalse(ok, message)
        self.assertIn(f"{target_operation.operation_id}: staged invariant failed", message)
        activate_code, activate_summary = persona_apply.activate_staged(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection",
        )
        self.assertNotEqual(activate_code, 0, activate_summary)
        self.assertIn(f"{target_operation.operation_id}: staged invariant failed", activate_summary)
        self.assertEqual(
            suffix_count,
            sum(1 for event in backend.events if event in suffix_ids),
        )

    def test_orchestrator_simulated_double_stage_noop_source_pointer_check_staged_green(self) -> None:
        backend = SimulatedHostBackend()
        stamp = "stamp-r14-sim-noop"
        for _ in range(2):
            exit_code, summary = persona_apply.stage_disabled(
                REPO_ROOT / "instance.yaml",
                "orchestrator",
                "claude",
                REPO_ROOT,
                stamp,
                backend=backend,
                root=self.root / "projection",
            )
            self.assertEqual(exit_code, 0, summary)
        plan = self.installer.plan(
            Manifest.load(REPO_ROOT / "instance.yaml"),
            "orchestrator",
            harness_override="claude",
            root=self.root / "projection",
        )
        account_home = self.root / "projection" / "home" / "claude"
        checkpoint = (
            account_home
            / ".local"
            / "state"
            / "pantheon"
            / "personas"
            / "orchestrator"
            / f"staged-cutover-{stamp}.json"
        )
        receipt = ApplyReceipt.from_dict(json.loads(checkpoint.read_text(encoding="utf-8"))["receipt"])
        source_mutation = next(item for item in receipt.mutations if item.operation_id == "codex-telegram.source-archive")
        source_operation = self.operation(plan, "codex-telegram.source-archive")
        self.assertEqual(source_mutation.target, source_operation.target)
        self.assertFalse(source_mutation.changed)
        ok, message = persona_apply.check_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection",
        )
        self.assertTrue(ok, message)

        suffix_ids = self.activation_suffix_ids(plan)
        suffix_count = sum(1 for event in backend.events if event in suffix_ids)
        for mode in ("drift", "delete"):
            with self.subTest(mode=mode):
                mutated_backend = SimulatedHostBackend()
                mutated_stamp = f"stamp-r14-sim-{mode}"
                for _ in range(2):
                    exit_code, summary = persona_apply.stage_disabled(
                        REPO_ROOT / "instance.yaml",
                        "orchestrator",
                        "claude",
                        REPO_ROOT,
                        mutated_stamp,
                        backend=mutated_backend,
                        root=self.root / f"projection-{mode}",
                    )
                    self.assertEqual(exit_code, 0, summary)
                mutated_plan = self.installer.plan(
                    Manifest.load(REPO_ROOT / "instance.yaml"),
                    "orchestrator",
                    harness_override="claude",
                    root=self.root / f"projection-{mode}",
                )
                mutated_operation = self.operation(mutated_plan, "codex-telegram.source-archive")
                mutated_target = mutated_operation.target
                mutated_suffix_ids = self.activation_suffix_ids(mutated_plan)
                mutated_suffix_count = sum(1 for event in mutated_backend.events if event in mutated_suffix_ids)
                if mode == "drift":
                    mutated_backend.drift(
                        mutated_target,
                        f"# {MANAGED_MARKER}\nmanaged but no longer desired\n",
                    )
                else:
                    mutated_backend.entries.pop(mutated_target, None)
                ok, message = persona_apply.check_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    mutated_stamp,
                    backend=mutated_backend,
                    root=self.root / f"projection-{mode}",
                )
                self.assertFalse(ok, message)
                self.assertIn(f"{mutated_operation.operation_id}: staged invariant failed", message)
                activate_code, activate_summary = persona_apply.activate_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    mutated_stamp,
                    backend=mutated_backend,
                    root=self.root / f"projection-{mode}",
                )
                self.assertNotEqual(activate_code, 0, activate_summary)
                self.assertIn(f"{mutated_operation.operation_id}: staged invariant failed", activate_summary)
                self.assertEqual(
                    mutated_suffix_count,
                    sum(1 for event in mutated_backend.events if event in mutated_suffix_ids),
                )
        self.assertEqual(suffix_count, sum(1 for event in backend.events if event in suffix_ids))

    @_with_codex_config_fixture
    def test_orchestrator_real_stage_disabled_check_staged_accepts_release_pointer_mutation_target(self) -> None:
        root, plan, backend, _summary = self.orchestrator_stage_disabled("r14-parity", "stamp-r14-parity")
        archive = self.operation(plan, "codex-telegram.source-archive")
        binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator", root=root)
        self.assertEqual(Path(archive.target), binding.current_release)
        self.assertEqual(Path(archive.desired["archive_path"]), root / "tmp" / f"codex-telegram-orchestrator-{archive.desired['revision']}.tar")
        self.assertNotEqual(Path(archive.target), Path(archive.desired["archive_path"]))
        self.assertTrue(Path(archive.desired["archive_path"]).is_file())

        ok, message = persona_apply.check_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r14-parity",
            backend=backend,
            root=root,
        )
        self.assertTrue(ok, message)
        _checkpoint, mutation = self.checkpoint_source_mutation(root, plan, "stamp-r14-parity")
        self.assertEqual(mutation.target, archive.target)
        self.assertEqual(mutation.target, str(binding.current_release))
        self.assertNotEqual(mutation.target, archive.desired["archive_path"])
        self.assertEqual(mutation.after_fingerprint, _snapshot_fingerprint(_symlink_snapshot(binding.current_release)))

    @_with_codex_config_fixture
    def test_orchestrator_staged_source_archive_input_or_release_pointer_drift_fails_before_activation(self) -> None:
        for mode in ("pointer-drift", "pointer-delete", "archive-tamper", "archive-delete"):
            with self.subTest(mode=mode):
                stamp = f"stamp-r14-{mode}"
                root, plan, backend, _summary = self.orchestrator_stage_disabled(mode, stamp)
                archive = self.operation(plan, "codex-telegram.source-archive")
                current = Path(archive.target)
                archive_path = Path(archive.desired["archive_path"])
                suffix_before = self.activation_suffix_events(backend, plan)
                if mode == "pointer-drift":
                    drift_release = current.parent / "external-drift"
                    drift_release.mkdir(parents=True)
                    current.unlink()
                    current.symlink_to(drift_release)
                elif mode == "pointer-delete":
                    current.unlink()
                elif mode == "archive-tamper":
                    archive_path.write_bytes(b"tampered source archive")
                else:
                    archive_path.unlink()

                ok, message = persona_apply.check_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    stamp,
                    backend=backend,
                    root=root,
                )
                self.assertFalse(ok, message)
                self.assertIn("codex-telegram.source-archive", message)
                activate_code, activate_summary = persona_apply.activate_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    stamp,
                    backend=backend,
                    root=root,
                )
                self.assertNotEqual(activate_code, 0, activate_summary)
                self.assertIn("codex-telegram.source-archive", activate_summary)
                self.assertEqual(suffix_before, self.activation_suffix_events(backend, plan))

    @_with_codex_config_fixture
    def test_orchestrator_rollback_staged_restores_release_pointer_without_treating_archive_as_mutation(self) -> None:
        root, plan, _backend = self.orchestrator_local_stage_fixture("r14-rollback")
        archive = self.operation(plan, "codex-telegram.source-archive")
        current = Path(archive.target)
        old_release = current.parent / "old-release"
        old_release.mkdir(parents=True)
        current.symlink_to(old_release)
        before_archive = Path(archive.desired["archive_path"]).read_bytes()
        backend = LocalHostStageBackend()
        exit_code, summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r14-rollback",
            backend=backend,
            root=root,
        )
        self.assertEqual(exit_code, 0, summary)
        self.assertNotEqual(os.readlink(current), str(old_release))
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r14-rollback",
            root / "home" / "claude",
            backend=backend,
        )
        self.assertEqual(rollback_code, 0, rollback_summary)
        self.assertEqual(os.readlink(current), str(old_release))
        self.assertEqual(Path(archive.desired["archive_path"]).read_bytes(), before_archive)

        drift_root, drift_plan, drift_backend, _summary = self.orchestrator_stage_disabled("r14-rollback-drift", "stamp-r14-rollback-drift")
        drift_archive = self.operation(drift_plan, "codex-telegram.source-archive")
        drift_current = Path(drift_archive.target)
        external = drift_current.parent / "external-release"
        external.mkdir(parents=True)
        drift_current.unlink()
        drift_current.symlink_to(external)
        refused_code, refused_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r14-rollback-drift",
            drift_root / "home" / "claude",
            backend=drift_backend,
        )
        self.assertNotEqual(refused_code, 0, refused_summary)
        self.assertIn("release pointer changed after apply", refused_summary)
        self.assertEqual(os.readlink(drift_current), str(external))

    @_with_codex_config_fixture
    def test_orchestrator_installer_preflight_requires_target_account_rw_for_each_mailbox_root_before_activation(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r21-mailbox-roots")
        root_checks = [
            operation
            for operation in plan.operations
            if operation.kind == "mailbox-writable-root-check"
        ]
        self.assertEqual(
            [operation.target for operation in root_checks],
            [
                str(root / "home" / "the-ancestor" / "mailbox" / "orchestrator"),
                str(root / "home" / "the-ancestor" / "mailbox" / "codex"),
            ],
        )
        self.assertTrue(all(operation.desired["harness"] == "codex" for operation in root_checks))
        activation_index = self.installer._activation_index(plan)
        first_root_check = min(plan.operations.index(operation) for operation in root_checks)
        self.assertLess(first_root_check, activation_index)

        (root / "home" / "the-ancestor" / "mailbox" / "codex" / "cur").rmdir()
        report = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "refused")
        self.assertIn("mailbox-writable-root.codex.1", "; ".join(report.errors))
        self.assertFalse(self.activation_suffix_events(backend, plan))

    @_with_codex_config_fixture
    def test_unit_fire_captures_invocation_id_while_oneshot_is_active_before_it_turns_inactive(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r22-capture")
        self.write_invocation_viability_receipt(plan)
        unit_fire = self.operation(plan, "connective.unit-fire")
        mutation, evidence = backend.execute(unit_fire, EXECUTION_CONTEXT_STAGED_ACTIVATE)
        self.assertFalse(mutation.changed)
        self.assertTrue(evidence.ok, evidence.detail)
        self.assertEqual(evidence.evidence["pre_enqueue_active_state"], "inactive")
        self.assertEqual(evidence.evidence["capture_active_state"], "activating")
        self.assertEqual(evidence.evidence["invocation_id"], TEST_INVOCATION_ID)
        self.assertEqual(evidence.evidence["post_exit_invocation_id_state"], "empty")
        self.assertEqual(
            [command for command in backend.commands if command[:3] == ("systemctl", "--user", "start")],
            [("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")],
        )

    @_with_codex_config_fixture
    def test_unit_fire_validates_immutable_receipt_after_captured_invocation_completes(self) -> None:
        _root, _plan, backend, activated = self.activate_orchestrator_from_stage("r22-receipt")
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        unit = backend.events["connective.unit-fire"].evidence
        self.assertEqual(unit["invocation_id"], TEST_INVOCATION_ID)
        self.assertTrue(unit["initial_viability_receipt_sha256"])
        self.assertEqual(unit["active_state"], "inactive")
        self.assertEqual(unit["result"], "success")
        self.assertEqual(unit["exec_main_status"], "0")

    @_with_codex_config_fixture
    def test_unit_fire_rejects_missing_malformed_or_changed_active_invocation_id_without_second_start(self) -> None:
        _root, plan, _backend = self.orchestrator_local_stage_fixture("r22-bad-id")
        unit_fire = self.operation(plan, "connective.unit-fire")
        self.write_invocation_viability_receipt(plan)
        cases = {
            "pre-existing-active": [
                f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
            ],
            "pending-job": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=99\n",
            ],
            "missing-until-inactive": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
            "uppercase": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                f"InvocationID={'A' * 32}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
            "path-like": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                "InvocationID=../../not-an-id\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
            "changed": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
                f"InvocationID={'b' * 32}\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
            "failed-before-capture": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                "InvocationID=\nActiveState=failed\nSubState=failed\nResult=failed\nExecMainStatus=1\nJob=\n",
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
            "nonzero-before-capture": [
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                "InvocationID=\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=7\nJob=1\n",
                "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            ],
        }
        for label, sequence in cases.items():
            with self.subTest(label=label):
                backend = LocalHostStageBackend()
                backend.service_show_sequence = list(sequence)
                with self.assertRaises(OperationFailed):
                    backend.execute(unit_fire, EXECUTION_CONTEXT_STAGED_ACTIVATE)
                starts = [command for command in backend.commands if command[:3] == ("systemctl", "--user", "start")]
                if label in {"pre-existing-active", "pending-job"}:
                    self.assertEqual(starts, [], backend.commands)
                    self.assertFalse(any(command[:3] == ("systemctl", "--user", "stop") for command in backend.commands))
                else:
                    self.assertEqual(
                        starts,
                        [("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")],
                        backend.commands,
                    )
                    self.assertTrue(any(command[:3] == ("systemctl", "--user", "stop") for command in backend.commands))

        class CaptureTimeoutBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "TIMEOUT",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                if (
                    command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
                    and self.service_show_count == 1
                ):
                    self.commands.append(command)
                    self.timeouts.append((command, timeout))
                    self.service_show_count += 1
                    raise subprocess.TimeoutExpired(command, timeout)
                return super()._run(argv, timeout=timeout)

        timeout_backend = CaptureTimeoutBackend()
        with self.assertRaises(OperationFailed) as raised:
            timeout_backend.execute(unit_fire, EXECUTION_CONTEXT_STAGED_ACTIVATE)
        self.assertIn("state-unavailable", str(raised.exception))
        self.assertEqual(
            [
                command
                for command in timeout_backend.commands
                if command == ("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")
            ],
            [("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")],
        )
        self.assertEqual(
            [
                command
                for command in timeout_backend.commands
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service")
            ],
            [("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service")],
        )

    @_with_codex_config_fixture
    def test_unit_fire_timeout_stops_and_proves_consume_inactive_before_compensation(self) -> None:
        class TimeoutBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.now = 1000.0
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
                    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

            def _unit_fire_monotonic(self) -> float:  # type: ignore[override]
                return self.now

            def _unit_fire_sleep(self, seconds: float) -> None:  # type: ignore[override]
                self.now = 1258.0

        _root, plan, _backend = self.orchestrator_local_stage_fixture("r22-timeout")
        self.write_invocation_viability_receipt(plan)
        backend = TimeoutBackend()
        backend.now = 1317.9
        remaining = backend._remaining_timeout_seconds(1318.0)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 0.1)
        backend.now = 1000.0
        with self.assertRaises(OperationFailed) as raised:
            backend.execute(
                self.operation(plan, "connective.unit-fire"),
                EXECUTION_CONTEXT_STAGED_ACTIVATE,
            )
        self.assertIn("completion-timeout", str(raised.exception))
        self.assertTrue(any(command[:3] == ("systemctl", "--user", "stop") for command in backend.commands))

    @_with_codex_config_fixture
    def test_unit_fire_failure_records_bounded_systemd_invocation_diagnostics(self) -> None:
        class MalformedBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "InvocationID=BAD-RAW-INVOCATION\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

        class MalformedShowBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "InvocationID=BAD=RAW\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

        class FailedCleanupBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "InvocationID=BAD-RAW-INVOCATION\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                    "InvocationID=\nActiveState=failed\nSubState=failed\nResult=failed\nExecMainStatus=1\nJob=\n",
                ]

        class CaptureTimeoutReceiptBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                if (
                    command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
                    and self.service_show_count == 1
                ):
                    self.commands.append(command)
                    self.timeouts.append((command, timeout))
                    self.service_show_count += 1
                    raise subprocess.TimeoutExpired(command, timeout)
                return super()._run(argv, timeout=timeout)

        class CleanupStopTimeoutBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    "InvocationID=BAD-RAW-INVOCATION\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
                ]

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service"):
                    self.commands.append(command)
                    self.timeouts.append((command, timeout))
                    raise subprocess.TimeoutExpired(command, timeout)
                return super()._run(argv, timeout=timeout)

        cases = [
            (MalformedBackend(), "malformed-invocation-id", "capture", "inactive", "BAD-RAW-INVOCATION"),
            (MalformedShowBackend(), "state-unavailable", "capture", "inactive", "BAD=RAW"),
            (FailedCleanupBackend(), "unsafe-cleanup", "cleanup", "failed", "BAD-RAW-INVOCATION"),
            (CaptureTimeoutReceiptBackend(), "state-unavailable", "capture", "inactive", "TimeoutExpired"),
            (CleanupStopTimeoutBackend(), "unsafe-cleanup", "cleanup", "stop-timeout", "BAD-RAW-INVOCATION"),
        ]
        for index, (failing_backend, classification, phase, cleanup_state, raw_forbidden) in enumerate(cases):
            with self.subTest(classification=classification):
                _root, plan, stage_backend = self.orchestrator_local_stage_fixture(f"r22-diagnostics-{index}")
                staged = self.installer.stage_disabled(plan, plan.digest, stage_backend, self.actor(plan))
                self.assertIsNotNone(staged.receipt, staged.errors)
                assert staged.receipt is not None
                self.write_invocation_viability_receipt(plan)
                self.prepare_mailbox_probe_dirs(plan)
                activated = self.installer.activate_staged(
                    plan,
                    plan.digest,
                    staged.receipt,
                    failing_backend,
                    self.actor(plan),
                )
                self.assertEqual(activated.status, "failed")
                self.assertIsNotNone(activated.receipt)
                assert activated.receipt is not None
                failed = [
                    item
                    for item in activated.receipt.evidence
                    if item.operation_id == "connective.unit-fire" and not item.ok
                ]
                self.assertEqual(len(failed), 1)
                diagnostics = failed[0].evidence
                self.assertEqual(diagnostics["classification"], classification)
                self.assertEqual(diagnostics["phase"], phase)
                self.assertEqual(diagnostics["cleanup_state"], cleanup_state)
                if classification == "malformed-invocation-id":
                    self.assertTrue(diagnostics["invocation_id_sha256"])
                serialized = json.dumps(activated.receipt.to_dict(), sort_keys=True)
                self.assertNotIn(raw_forbidden, serialized)
                self.assertEqual(
                    sum(
                        1
                        for command in failing_backend.commands
                        if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service")
                    ),
                    1,
                    failing_backend.commands,
                )

    @_with_codex_config_fixture
    def test_orchestrator_activation_live_shape_post_exit_invocation_id_empty_still_accepts_captured_receipt(self) -> None:
        _root, _plan, backend, activated = self.activate_orchestrator_from_stage("r22-live-empty")
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        unit = backend.events["connective.unit-fire"].evidence
        timer = backend.events["connective.timer-observe"].evidence
        self.assertEqual(unit["post_exit_invocation_id_state"], "empty")
        self.assertEqual(timer["post_exit_invocation_id_state"], "empty")
        self.assertEqual(timer["unit_fire_invocation_id"], TEST_INVOCATION_ID)

    @_with_codex_config_fixture
    def test_timer_observe_accepts_completed_first_fire_with_empty_post_exit_invocation_id_without_second_consume(self) -> None:
        _root, _plan, backend, activated = self.activate_orchestrator_from_stage("r22-timer-empty")
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        timer = backend.events["connective.timer-observe"].evidence
        self.assertEqual(timer["second_consume_absent"], "true")
        self.assertEqual(timer["post_exit_invocation_id_state"], "empty")
        self.assertEqual(
            sum(1 for command in backend.commands if command[:3] == ("systemctl", "--user", "start")),
            1,
        )

        root, plan, bad_backend = self.orchestrator_local_stage_fixture("r22-timer-bad")
        bad_backend.service_show_sequence = [
            "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            f"InvocationID={TEST_INVOCATION_ID}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
            "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
            "InvocationID=BAD\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
        ]
        staged = self.installer.stage_disabled(plan, plan.digest, bad_backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        self.prepare_mailbox_probe_dirs(plan)
        bad = self.installer.activate_staged(plan, plan.digest, staged.receipt, bad_backend, self.actor(plan))
        self.assertEqual(bad.status, "failed")
        self.assertIn("consume service InvocationID changed after timer arm", bad.errors[0])

    @_with_codex_config_fixture
    def test_activate_staged_unit_fire_uses_runtime_viability_timeout_not_default_command_timeout(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r19-timeout")
        unit_fire = self.operation(plan, "connective.unit-fire")
        self.assertEqual(unit_fire.desired["unit_fire_timeout_seconds"], "318")
        self.assertEqual(unit_fire.desired["unit_fire_timeout_candidate_count"], "2")
        self.assertEqual(unit_fire.desired["unit_fire_timeout_post_start_count"], "2")
        self.assertTrue(plan.validate_digest())
        service_template = (REPO_ROOT / "tools" / "systemd" / "pantheon-persona-consume@.service").read_text(encoding="utf-8")
        self.assertIn("TimeoutStartSec=30min", service_template)
        self.assertLess(MAX_UNIT_FIRE_TIMEOUT_SECONDS, 30 * 60)

        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIn(staged.status, {"applied", "noop"}, staged.errors)
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        mailbox_probe = self.operation(plan, "connective.mailbox-probe")
        Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
        Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)
        self.write_invocation_viability_receipt(plan)
        activated = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        start_timeouts = [
            timeout
            for command, timeout in backend.timeouts
            if command == ("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")
        ]
        self.assertEqual(len(start_timeouts), 1)
        self.assertGreater(start_timeouts[0], 257)
        self.assertLessEqual(start_timeouts[0], 258)

        bad_cases = {
            "missing-seconds": {"drop": ("unit_fire_timeout_seconds",)},
            "zero-seconds": {"unit_fire_timeout_seconds": "0"},
            "malformed-seconds": {"unit_fire_timeout_seconds": "not-an-int"},
            "signed-seconds": {"unit_fire_timeout_seconds": "+318"},
            "float-seconds": {"unit_fire_timeout_seconds": "318.0"},
            "whitespace-seconds": {"unit_fire_timeout_seconds": "318 "},
            "leading-zero-seconds": {"unit_fire_timeout_seconds": "0318"},
            "boolean-seconds": {"unit_fire_timeout_seconds": True},
            "excessive-seconds": {"unit_fire_timeout_seconds": "1800"},
            "missing-candidate-count": {"drop": ("unit_fire_timeout_candidate_count",)},
            "zero-candidate-count": {"unit_fire_timeout_candidate_count": "0"},
            "malformed-candidate-count": {"unit_fire_timeout_candidate_count": "two"},
            "leading-zero-candidate-count": {"unit_fire_timeout_candidate_count": "02"},
            "missing-post-start-count": {"drop": ("unit_fire_timeout_post_start_count",)},
            "negative-post-start-count": {"unit_fire_timeout_post_start_count": "-1"},
            "malformed-post-start-count": {"unit_fire_timeout_post_start_count": "2.0"},
            "missing-formula": {"drop": ("unit_fire_timeout_formula",)},
            "bogus-formula": {"unit_fire_timeout_formula": "seconds"},
            "seconds-count-mismatch": {"unit_fire_timeout_candidate_count": "1"},
            "missing-state-root": {"drop": ("persona_state_root",)},
            "escaped-state-root": {"persona_state_root": str(root / "outside-state")},
            "missing-allowed-refs": {"drop": ("allowed_runtime_refs",)},
            "malformed-allowed-refs": {"allowed_runtime_refs": "[not-json]"},
            "duplicate-allowed-refs": {"allowed_runtime_refs": "[\"codex/interactive-chatgpt\",\"codex/interactive-chatgpt\"]"},
            "missing-invocation-requirement": {"drop": ("require_invocation_receipt",)},
            "malformed-invocation-requirement": {"require_invocation_receipt": "yes"},
            "missing-invocation-schema": {"drop": ("invocation_receipt_schema",)},
            "bad-invocation-schema": {"invocation_receipt_schema": "v0"},
            "missing-receiver-required": {"drop": ("receiver_receipt_required",)},
            "bad-receiver-required": {"receiver_receipt_required": "1"},
            "missing-receiver-map": {"drop": ("receiver_operations_by_runtime",)},
            "undeclared-receiver-map": {"receiver_operations_by_runtime": "{\"synthetic/other\":[]}"},
        }
        for label, bad in bad_cases.items():
            with self.subTest(label=label):
                bad_backend = LocalHostStageBackend()
                desired = dict(unit_fire.desired)
                for key in bad.get("drop", ()):
                    desired.pop(key, None)
                for key, value in bad.items():
                    if key != "drop":
                        desired[key] = value  # type: ignore[assignment]
                bad_operation = dataclasses.replace(
                    unit_fire,
                    desired=desired,
                )
                with self.assertRaises(OperationFailed):
                    bad_backend.execute(bad_operation, EXECUTION_CONTEXT_STAGED_ACTIVATE)
                self.assertFalse(
                    any(
                        command[:3] == ("systemctl", "--user", "is-enabled")
                        for command in bad_backend.commands
                    ),
                    bad_backend.commands,
                )
                self.assertFalse(
                    any(
                        command == ("systemctl", "--user", "start", "pantheon-persona-consume@orchestrator.service")
                        or command == ("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")
                        for command in bad_backend.commands
                    ),
                    bad_backend.commands,
                )

    @_with_codex_config_fixture
    def test_activation_accepts_first_unit_fire_viability_and_arms_timer_without_second_consume(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r20-activation")
        stamp = "stamp-r20-activation"
        stage_code, stage_summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=root,
        )
        self.assertEqual(stage_code, 0, stage_summary)
        self.write_invocation_viability_receipt(plan)
        mailbox_probe = self.operation(plan, "connective.mailbox-probe")
        Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
        Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)
        activate_code, activate_summary = persona_apply.activate_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=root,
        )
        self.assertEqual(activate_code, 0, activate_summary)
        unit = backend.events["connective.unit-fire"].evidence
        timer = backend.events["connective.timer-observe"].evidence
        self.assertEqual(unit["invocation_id"], TEST_INVOCATION_ID)
        self.assertEqual(timer["unit_fire_invocation_id"], TEST_INVOCATION_ID)
        self.assertEqual(timer["first_fire_invocation_observed"], "true")
        self.assertEqual(timer["second_consume_absent"], "true")
        self.assertEqual(timer["next_elapse_finite"], "true")
        self.assertEqual(
            sum(
                1
                for command in backend.commands
                if command == ("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")
            ),
            1,
        )

    @_with_codex_config_fixture
    def test_activation_rejects_missing_or_mismatched_invocation_viability_receipt(self) -> None:
        cases = {
            "missing": {},
            "undeclared-selected": {"selected_runtime": "synthetic/other"},
            "missing-receiver": {"receiver_ok": False},
        }
        for name, kwargs in cases.items():
            with self.subTest(name=name):
                root, plan, backend = self.orchestrator_local_stage_fixture(f"r20-{name}")
                staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
                self.assertIn(staged.status, {"applied", "noop"}, staged.errors)
                self.assertIsNotNone(staged.receipt, staged.errors)
                assert staged.receipt is not None
                if kwargs:
                    self.write_invocation_viability_receipt(plan, **kwargs)
                mailbox_probe = self.operation(plan, "connective.mailbox-probe")
                Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
                Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)
                activated = self.installer.activate_staged(
                    plan,
                    plan.digest,
                    staged.receipt,
                    backend,
                    self.actor(plan),
                )
                self.assertEqual(activated.status, "failed")
                self.assertIn("RuntimeError", activated.errors[0])
                self.assertTrue(
                    any(
                        command == ("systemctl", "--user", "start", "pantheon-persona-consume@orchestrator.service")
                        or command == ("systemctl", "--user", "start", "--no-block", "pantheon-persona-consume@orchestrator.service")
                        for command in backend.commands
                    ),
                    backend.commands,
                )
                self.assertNotIn("connective.timer-enable", backend.events)

    @_with_codex_config_fixture
    def test_timer_enable_arms_finite_next_elapse_without_immediate_second_fire(self) -> None:
        class SecondConsumeBackend(LocalHostStageBackend):
            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                if (
                    command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service")
                    and "InvocationID" in set(command[4:])
                    and any(item == ("systemctl", "--user", "enable", "--now", "pantheon-persona-consume@orchestrator.timer") for item in self.commands)
                ):
                    self.commands.append(command)
                    self.timeouts.append((command, timeout))
                    return subprocess.CompletedProcess(command, 0, f"InvocationID={'b' * 32}\nActiveState=inactive\nResult=success\nExecMainStatus=0\n", "")
                return super()._run(argv, timeout=timeout)

        root, plan, _unused = self.orchestrator_local_stage_fixture("r20-second-fire")
        backend = SecondConsumeBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        mailbox_probe = self.operation(plan, "connective.mailbox-probe")
        Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
        Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)
        activated = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(activated.status, "failed")
        self.assertIn("consume service InvocationID changed after timer arm", activated.errors[0])

    @_with_codex_config_fixture
    def test_orchestrator_activation_race_first_selected_second_exhausted_preserves_acceptance(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r20-race")
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        self.write_invocation_viability_receipt(plan)
        latest = (
            Path(self.operation(plan, "connective.unit-fire").desired["persona_state_root"])
            / "orchestrator"
            / "runtime-controller"
            / "initial-viability.json"
        )
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(
            json.dumps(
                {
                    "schema": "pantheon-runtime-initial-viability/v1",
                    "persona": "orchestrator",
                    "status": "exhausted",
                    "selected_runtime": None,
                    "selected_harness": None,
                    "selected_profile": None,
                    "manifest_order": ["claude/primary", "codex/interactive-chatgpt"],
                    "attempt_limit": 2,
                    "attempts": [],
                    "recorded_at": "2026-08-15T00:00:05Z",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        mailbox_probe = self.operation(plan, "connective.mailbox-probe")
        Path(mailbox_probe.desired["receive_tmp"]).mkdir(parents=True, exist_ok=True)
        Path(mailbox_probe.desired["send_tmp"]).mkdir(parents=True, exist_ok=True)
        activated = self.installer.activate_staged(
            plan,
            plan.digest,
            staged.receipt,
            backend,
            self.actor(plan),
        )
        self.assertIn(activated.status, {"applied", "noop"}, activated.errors)
        self.assertEqual(backend.events["connective.unit-fire"].evidence["invocation_id"], TEST_INVOCATION_ID)

    def test_consume_timer_template_uses_activation_relative_first_elapse(self) -> None:
        template = (REPO_ROOT / "tools" / "systemd" / "pantheon-persona-consume@.timer").read_text(encoding="utf-8")
        self.assertIn("OnActiveSec=2min", template)
        self.assertNotIn("OnBootSec=", template)
        plan = self.plan()
        timer = self.operation(plan, "connective.timer-unit")
        self.assertIn("OnActiveSec=2min", timer.desired["content"])
        self.assertNotIn("OnBootSec=", timer.desired["content"])

    def test_activate_staged_timeout_compensation_then_rollback_staged_is_idempotent_for_restored_timer(self) -> None:
        backend = SimulatedHostBackend(fail_operation="connective.unit-fire")
        stamp = "stamp-r19-timeout"
        exit_code, summary = persona_apply.stage_disabled(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection-r19-timeout",
        )
        self.assertEqual(exit_code, 0, summary)
        activate_code, activate_summary = persona_apply.activate_staged(
            self.manifest_path,
            "recovery",
            "claude",
            REPO_ROOT,
            stamp,
            backend=backend,
            root=self.root / "projection-r19-timeout",
        )
        self.assertNotEqual(activate_code, 0, activate_summary)
        self.assertIn("ACTIVATE_STATUS=failed", activate_summary)
        self.assertFalse(backend.timer_enabled)
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "recovery",
            stamp,
            self.root / "projection-r19-timeout" / "home" / "claude-fallback",
            backend=backend,
        )
        self.assertEqual(rollback_code, 0, rollback_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=noop", rollback_summary)
        self.assertIn("RESTORED=0", rollback_summary)

        owned = MutationRecord(
            "owned.file",
            "atomic-write",
            "/owned",
            "claude-fallback",
            True,
            {"state": "absent"},
            _snapshot_fingerprint({"state": "file", "content_b64": "", "fingerprint": "after", "mode": "0600"}),
        )
        skipped = MutationRecord(
            "already.restored",
            "atomic-write",
            "/already",
            "claude-fallback",
            True,
            {"state": "absent"},
            "different-after",
        )
        mixed_plan = self.plan()
        mixed_backend = SimulatedHostBackend()
        mixed_backend.entries["/owned"] = {"state": "file", "content_b64": "", "fingerprint": "after", "mode": "0600"}
        mixed_receipt = ApplyReceipt(
            schema="pantheon-persona-install-receipt/v1",
            receipt_id="mixed-r19",
            phase="failed",
            plan_digest=mixed_plan.digest,
            instance=mixed_plan.instance,
            persona=mixed_plan.persona,
            role=mixed_plan.role,
            account=mixed_plan.account,
            actor={
                "uid": str(os.geteuid()),
                "persona": mixed_plan.persona,
                "role": mixed_plan.role,
                "instance": mixed_plan.instance,
                "capabilities": ",".join(sorted(_all_plan_capabilities(mixed_plan))),
            },
            mutations=(skipped, owned),
            evidence=(),
            created_at="2026-08-15T00:00:00Z",
        )
        mixed = self.installer.rollback(mixed_receipt, mixed_backend, self.actor(mixed_plan))
        self.assertEqual(mixed.status, "rolled-back", mixed)
        self.assertEqual(mixed.restored, 1)
        self.assertNotIn("/owned", mixed_backend.entries)

    def test_rollback_staged_timer_enable_missing_unit_is_not_applied_but_wrong_enabled_state_still_refuses(self) -> None:
        class TimerStateBackend(LocalHostStageBackend):
            def __init__(self, *, stdout: str, returncode: int = 0, stderr: str = "") -> None:
                super().__init__()
                self.stdout = stdout
                self.returncode = returncode
                self.stderr = stderr

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:3] == ("systemctl", "--user", "show"):
                    return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)
                return super()._run(argv, timeout=timeout)

        def mutation(before: str) -> MutationRecord:
            return MutationRecord(
                "connective.timer-enable",
                "timer-enable",
                "pantheon-persona-consume@orchestrator.timer",
                "claude",
                True,
                {"state": before},
                "timer-enabled",
            )

        cases = (
            ("before-disabled-loaded-disabled", "disabled", "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", True, "not-applied"),
            ("before-disabled-not-found", "disabled", "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n", True, "not-applied"),
            ("before-disabled-not-found-active", "disabled", "LoadState=not-found\nUnitFileState=not-found\nActiveState=active\nSubState=running\n", True, "owned"),
            ("before-disabled-loaded-enabled", "disabled", "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=waiting\n", True, "owned"),
            ("before-enabled-loaded-enabled", "enabled", "LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\n", True, "not-applied"),
            ("before-enabled-loaded-disabled", "enabled", "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", False, "drifted"),
            ("before-enabled-not-found", "enabled", "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n", False, "drifted"),
            ("masked", "disabled", "LoadState=loaded\nUnitFileState=masked\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("static", "disabled", "LoadState=loaded\nUnitFileState=static\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("generated", "disabled", "LoadState=loaded\nUnitFileState=generated\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("unknown", "disabled", "LoadState=unknown\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("malformed", "disabled", "LoadState loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("duplicate", "disabled", "LoadState=loaded\nLoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", False, "unavailable"),
            ("missing-key", "disabled", "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\n", False, "unavailable"),
        )
        for label, before, stdout, ok, classification in cases:
            with self.subTest(label=label):
                result = TimerStateBackend(stdout=stdout).can_rollback(mutation(before))
                self.assertEqual(result.ok, ok, result)
                self.assertEqual(result.classification, classification, result)
        bus = TimerStateBackend(stdout="", returncode=1, stderr="Failed to connect to bus")
        result = bus.can_rollback(mutation("disabled"))
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "unavailable")
        with self.assertRaises(OperationFailed):
            _parse_systemctl_timer_show("LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\nUnexpected=value\n")

    @_with_codex_config_fixture
    def test_timer_enablement_absence_check_is_first_staged_prefix_operation(self) -> None:
        _root, plan, _backend = self.orchestrator_local_stage_fixture("r29-first-prefix")
        first = plan.operations[0]
        self.assertEqual(first.operation_id, "connective.timer-enablement-absent")
        self.assertEqual(first.kind, "timer-enablement-absence-check")
        self.assertEqual(first.execution_scope, "staged-cutover")
        self.assertLess(
            plan.operations.index(first),
            plan.operations.index(self.operation(plan, "connective.service-unit")),
        )
        self.assertLess(
            plan.operations.index(first),
            plan.operations.index(self.operation(plan, "connective.timer-unit")),
        )
        self.assertTrue(
            all(
                operation.execution_scope == "all"
                for operation in plan.operations
                if operation.operation_id != "connective.timer-enablement-absent"
            )
        )

    def test_all_in_one_receipt_neither_claims_nor_requires_staged_only_mutation(self) -> None:
        plan = self.plan()
        backend, report = self.apply(plan)
        self.assertEqual(report.status, "applied", report.errors)
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        self.assertNotIn(
            "connective.timer-enablement-absent",
            {item.operation_id for item in report.receipt.mutations},
        )
        self.assertNotIn(
            "connective.timer-enablement-absent",
            {item.operation_id for item in report.receipt.evidence},
        )

    @_with_codex_config_fixture
    def test_stage_disabled_refuses_dangling_consume_timer_enablement_before_checkpoint(self) -> None:
        root, _plan, backend = self.orchestrator_local_stage_fixture("r29-stage-dangling")
        _link_path, template = self.consume_timer_link_paths(root)
        self.write_consume_timer_link(root, template)
        code, summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-stage-dangling",
            backend=backend,
            root=root,
        )
        self.assertNotEqual(code, 0, summary)
        self.assertIn("STAGE_STATUS=refused", summary)
        # T-3109: this reaches the codex-telegram-bridge-gated preflight
        # pass for the first time (previously short-circuited upstream by
        # the archive-read guard this ticket retired). Empirically verified
        # (standalone repro against unmodified persona_installer.py): the
        # generic consume-timer show() fake this fixture already uses for
        # OTHER tests reports active+waiting+finite-elapse, so
        # "connective.timer-enablement-absent" legitimately classifies the
        # link-at-template shape as T-3087's converged-legacy (a read-only
        # evidence-gathering PASS, not a refusal) and seeds connective.unit-
        # fire/connective.timer-observe as that classification's evidence --
        # the actual refusal comes from "connective.timer-enable" detecting
        # the link already exists. assertEqual(backend.events, {}) was
        # never actually exercised before (same archive-read short-circuit)
        # and doesn't hold given that legitimate by-design admission; the
        # invariant this test cares about -- no enablement MUTATION landed
        # before the refusal -- is what's asserted below instead.
        self.assertNotIn("connective.timer-enable", backend.events)
        checkpoint = persona_apply._checkpoint_path_from_home(root / "home" / "claude", "orchestrator", "stamp-r29-stage-dangling")
        self.assertFalse(checkpoint.exists())

    @_with_codex_config_fixture
    def test_stage_disabled_refuses_preexisting_canonical_enabled_absolute_link_before_mutation(self) -> None:
        root, _plan, backend = self.orchestrator_local_stage_fixture("r29-stage-canonical-enabled")
        link, template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))

        class EnabledBackend(LocalHostStageBackend):
            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:4] == ("systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.timer"):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=waiting\n",
                        "",
                    )
                return super()._run(argv, timeout=timeout)

        enabled_backend = EnabledBackend()
        code, summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-stage-canonical-enabled",
            backend=enabled_backend,
            root=root,
        )
        self.assertNotEqual(code, 0, summary)
        self.assertIn("STAGE_STATUS=refused", summary)
        self.assertEqual(enabled_backend.events, {})
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(template))

    @_with_codex_config_fixture
    def test_activate_staged_refuses_dangling_consume_timer_enablement_before_template_install(self) -> None:
        root, _plan, backend, _summary = self.orchestrator_stage_disabled("r29-activate-dangling", "stamp-r29-activate-dangling")
        _link_path, template = self.consume_timer_link_paths(root)
        self.write_consume_timer_link(root, template)
        before_commands = len(backend.commands)
        ok, message = persona_apply.check_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-activate-dangling",
            backend=backend,
            root=root,
        )
        self.assertFalse(ok, message)
        code, summary = persona_apply.activate_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-activate-dangling",
            backend=backend,
            root=root,
        )
        self.assertNotEqual(code, 0, summary)
        self.assertIn("ACTIVATE_STATUS=refused", summary)
        new_commands = backend.commands[before_commands:]
        self.assertNotIn(("systemctl", "--user", "daemon-reload"), new_commands)
        self.assertNotIn(("systemctl", "--user", "start", "pantheon-persona-consume@orchestrator.service"), new_commands)

    @_with_codex_config_fixture
    def test_activate_staged_refuses_post_checkpoint_canonical_enabled_link_before_template_install(self) -> None:
        root, _plan, backend, _summary = self.orchestrator_stage_disabled("r29-activate-canonical-enabled", "stamp-r29-activate-canonical-enabled")
        link, template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        before_commands = len(backend.commands)
        code, summary = persona_apply.activate_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-activate-canonical-enabled",
            backend=backend,
            root=root,
        )
        self.assertNotEqual(code, 0, summary)
        self.assertIn("ACTIVATE_STATUS=refused", summary)
        self.assertEqual(os.readlink(link), str(template))
        self.assertNotIn(("systemctl", "--user", "daemon-reload"), backend.commands[before_commands:])

    def test_activate_staged_refuses_timer_enable_boundary_canonical_enabled_link_without_rollback_authority(self) -> None:
        plan = self.plan()

        class BoundaryEnabledBackend(SimulatedHostBackend):
            def execute(self, operation, context):  # type: ignore[override]
                if operation.operation_id == "connective.timer-enable" and context.workflow == "staged-activate":
                    link = operation.desired["enablement_link_path"]
                    canonical = operation.desired["canonical_template_path"]
                    self.timer_enabled = True
                    self.entries[link] = {
                        "state": "symlink",
                        "target": canonical,
                        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    }
                return super().execute(operation, context)

        backend = BoundaryEnabledBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        assert staged.receipt is not None
        report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutations = [item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable"]
        self.assertTrue(all(not item.changed for item in timer_mutations), timer_mutations)
        self.assertTrue(all(item.before.get("owned") != "true" for item in timer_mutations), timer_mutations)
        self.assertTrue(
            all(item.evidence.get("enable_command_issued") != "true" for item in report.receipt.evidence),
            report.receipt.evidence,
        )
        link = self.operation(plan, "connective.timer-enable").desired["enablement_link_path"]
        self.assertIn(link, backend.entries)
        self.assertNotIn("rollback:connective.timer-enable", backend.events)

    def test_activate_staged_refuses_timer_enable_boundary_wrong_link_without_rollback_authority(self) -> None:
        plan = self.plan()

        class BoundaryWrongLinkBackend(SimulatedHostBackend):
            def execute(self, operation, context):  # type: ignore[override]
                if operation.operation_id == "connective.timer-enable" and context.workflow == "staged-activate":
                    link = operation.desired["enablement_link_path"]
                    self.timer_enabled = True
                    self.entries[link] = {
                        "state": "symlink",
                        "target": "/tmp/wrong.timer",
                        "fingerprint": hashlib.sha256(b"/tmp/wrong.timer").hexdigest(),
                    }
                return super().execute(operation, context)

        backend = BoundaryWrongLinkBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        assert staged.receipt is not None
        report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutations = [item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable"]
        self.assertTrue(all(not item.changed for item in timer_mutations), timer_mutations)
        self.assertTrue(all(item.before.get("owned") != "true" for item in timer_mutations), timer_mutations)
        self.assertTrue(
            all(item.evidence.get("enable_command_issued") != "true" for item in report.receipt.evidence),
            report.receipt.evidence,
        )
        link = self.operation(plan, "connective.timer-enable").desired["enablement_link_path"]
        self.assertEqual(backend.entries[link]["target"], "/tmp/wrong.timer")
        self.assertNotIn("rollback:connective.timer-enable", backend.events)

    # -- T-3087: legacy-enabled consume timer convergence admit -------------
    #
    # orchestrator_timer_gate_fixture_root is used (not orchestrator_local_stage_fixture)
    # for every test below that only needs the timer-enablement gates in
    # isolation -- it's a leaner fixture that skips the codex-telegram
    # source-archive step entirely (see that fixture's docstring). The one
    # full CLI E2E test (stage_disabled -> activate_staged, which DOES need
    # the full staged prefix incl. the archive) uses orchestrator_local_stage_
    # fixture directly and opts into @_with_codex_config_fixture (T-3109).

    # T-3109: this full E2E half (stage_disabled -> activate_staged) reaches
    # persona_installer.py's verify_operation() dispatch for the first time
    # -- previously short-circuited upstream by the archive-read guard this
    # ticket retired.
    #
    # T-3611 (RESOLVED): verify_operation's "timer-enablement-absence-check"
    # branch used to treat classification == "absent" as the ONLY ok=True
    # case, never recognizing "converged-legacy" (the SAME classification
    # its own preflight() accepts as a pass) -- activate_staged's
    # _validate_staged_receipt re-verify always refused a legitimate
    # legacy-converged stage with "staged invariant failed: existing
    # canonical enablement matches converged shape". Fixed by threading the
    # STAGED RECEIPT's own evidence for this operation_id into
    # verify_operation (`staged_evidence` kwarg, both HostBackend
    # implementations): "converged-legacy" is now additionally admissible
    # ONLY when that staged evidence proves the enablement link already
    # existed -- and was already accepted via the live-convergence probe --
    # at STAGE time (`pre_enablement_link_state` != "absent", staged
    # evidence `ok`). A staged evidence showing "absent" (the dangling/
    # TOCTOU shape exercised by test_activate_staged_refuses_dangling_
    # consume_timer_enablement_before_template_install, immediately below)
    # is deliberately NOT widened -- see
    # _timer_enablement_absence_admissible_classifications's docstring for
    # the full rationale. See also test_timer_enablement_absence_
    # admissible_classifications_helper_boundaries and the two
    # verify_operation-direct regression tests just below this test for the
    # isolated (non-E2E) coverage of that fix.
    #
    # T-3614 (RESOLVED): with T-3611 resolved, this test progressed past the
    # timer-enablement-absence-check gate and into activate_staged's
    # activation loop, hitting a SEPARATE bug: OperationFailed "stale unit
    # show output contains unexpected keys" from
    # _parse_stale_unit_failure_reset_show (stale-unit-failure-reset
    # operation kind). Root cause was the TEST FIXTURE, not production code:
    # LegacyConvergedTimerBackend._legacy_override routed ANY service-show
    # query containing InvocationID+ActiveState to the T-3106 hybrid
    # unit-fire shape (SuccessExitStatus, no LoadState) -- including the
    # stale-unit-reset check's own query, which also contains
    # InvocationID+ActiveState but expects LoadState instead of
    # SuccessExitStatus. Confirmed empirically that real systemctl `show
    # --property=<list>` never emits properties outside the requested list,
    # so the parser's strictness was correct and stayed untouched; fixed by
    # disambiguating on "LoadState in properties" first in the fake (see
    # test_legacy_converged_timer_backend_stale_unit_reset_show_parses_
    # cleanly for the isolated unit-level coverage of that routing fix).
    @_with_codex_config_fixture
    def test_activate_staged_admits_legacy_enabled_timer_with_converged_live_shape(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-converged")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")

        probe_backend = LegacyConvergedTimerBackend()
        result = probe_backend.preflight(operation)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.classification, "converged-legacy")
        self.assertEqual(
            probe_backend.events["connective.timer-observe"].evidence.get("next_elapse_finite"),
            "true",
        )
        self.assertEqual(probe_backend.events["connective.unit-fire"].evidence.get("result"), "success")

        e2e_root, e2e_plan, _unused = self.orchestrator_local_stage_fixture("r38-legacy-converged-e2e")
        self.write_consume_timer_link(e2e_root, str(self.consume_timer_link_paths(e2e_root)[1]))
        self.prepare_mailbox_probe_dirs(e2e_plan)
        e2e_backend = LegacyConvergedTimerBackend()
        stamp = "stamp-r38-legacy-converged"
        stage_code, stage_summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=e2e_backend,
            root=e2e_root,
        )
        self.assertEqual(stage_code, 0, stage_summary)
        self.assertTrue(
            any(f"STAGE_STATUS={status}" in stage_summary for status in ("applied", "noop")),
            stage_summary,
        )
        activate_code, activate_summary = persona_apply.activate_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=e2e_backend,
            root=e2e_root,
        )
        self.assertEqual(activate_code, 0, activate_summary)
        self.assertTrue(
            any(f"ACTIVATE_STATUS={status}" in activate_summary for status in ("applied", "noop")),
            activate_summary,
        )

    # -- T-3614: LegacyConvergedTimerBackend's stale-unit-reset show shape --
    #
    # Isolates Bug 2 (see the comment block above) at unit-test speed,
    # without needing the full activate_staged E2E path: the fake's
    # _legacy_override service_prefix branch routed ANY show query
    # containing InvocationID+ActiveState to the T-3106 hybrid unit-fire
    # shape (SuccessExitStatus, no LoadState) -- including the
    # stale-unit-failure-reset check's own query (STALE_UNIT_FAILURE_RESET_
    # PROPERTIES: LoadState/ActiveState/SubState/Result/ExecMainStatus/
    # InvocationID/Job), which also contains InvocationID+ActiveState.
    # Real systemctl `show --property=<list>` never emits properties
    # outside the requested list (verified empirically on this host, with
    # and without --all), so the fake's over-broad routing -- not the
    # parser's strictness -- was the fault.
    def test_legacy_converged_timer_backend_stale_unit_reset_show_parses_cleanly(self) -> None:
        backend = LegacyConvergedTimerBackend()
        argv = ["systemctl", "--user", "show", "pantheon-persona-consume@orchestrator.service", "--all"]
        for item in STALE_UNIT_FAILURE_RESET_PROPERTIES:
            argv.extend(("-p", item))
        result = backend._run(argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = _parse_stale_unit_failure_reset_show(result.stdout)
        self.assertEqual(snapshot.load_state, "not-found")
        self.assertEqual(snapshot.active_state, "inactive")
        self.assertEqual(snapshot.sub_state, "dead")
        self.assertEqual(snapshot.result, "success")
        self.assertEqual(snapshot.exec_main_status, "0")
        self.assertEqual(snapshot.invocation_id, "")
        self.assertEqual(snapshot.job, "")

    # -- T-3611: verify_operation's staged_evidence-conditioned admission --
    #
    # These four tests isolate the T-3611 fix from T-3614's stale-unit-
    # failure-reset fix (see the comment block above
    # test_activate_staged_admits_legacy_enabled_timer_with_converged_live_
    # shape): the fix lives entirely in
    # _timer_enablement_absence_admissible_classifications and its two
    # verify_operation call sites, none of which touch activate_staged's
    # activation loop (where T-3614's stale-unit-failure-reset fix lives),
    # so these exercise and pass independently of it.

    def test_timer_enablement_absence_admissible_classifications_helper_boundaries(self) -> None:
        guard_id, guard_layer, guard_kind = "connective.timer-enablement-absent", 1, "timer-enablement-absence-check"
        self.assertEqual(
            _timer_enablement_absence_admissible_classifications(None),
            frozenset({"absent"}),
        )
        no_evidence_fields = OperationEvidence(
            guard_id, guard_layer, guard_kind, True, "no link-state evidence recorded", {},
        )
        self.assertEqual(
            _timer_enablement_absence_admissible_classifications(no_evidence_fields),
            frozenset({"absent"}),
        )
        absent_at_stage = OperationEvidence(
            guard_id, guard_layer, guard_kind, True, "consume timer enablement link is absent",
            {"pre_enablement_link_state": "absent"},
        )
        self.assertEqual(
            _timer_enablement_absence_admissible_classifications(absent_at_stage),
            frozenset({"absent"}),
        )
        # ok=False at stage time must not widen even if the link was present --
        # a failed staged evidence record is not trustworthy proof of convergence.
        present_at_stage_not_ok = OperationEvidence(
            guard_id, guard_layer, guard_kind, False, "consume timer enablement link already exists",
            {"pre_enablement_link_state": "symlink"},
        )
        self.assertEqual(
            _timer_enablement_absence_admissible_classifications(present_at_stage_not_ok),
            frozenset({"absent"}),
        )
        present_at_stage_ok = OperationEvidence(
            guard_id, guard_layer, guard_kind, True, "existing canonical enablement matches converged shape",
            {"pre_enablement_link_state": "symlink"},
        )
        self.assertEqual(
            _timer_enablement_absence_admissible_classifications(present_at_stage_ok),
            frozenset({"absent", "converged-legacy"}),
        )

    def test_verify_operation_admits_converged_legacy_when_staged_evidence_proves_preexisting_link(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-verify-staged-legit")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend()

        # T-3611 ask (c): default (no staged_evidence -- the verify()/live
        # post-apply path) stays strict, unchanged from before this fix.
        verified_default = backend.verify_operation(operation)
        self.assertFalse(verified_default.ok, verified_default)

        # T-3611 ask (a): staged evidence proving the link already existed
        # (and was accepted) at stage time widens admission to include the
        # live-reprobed "converged-legacy" classification.
        staged_evidence = OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            True,
            "existing canonical enablement matches converged shape",
            {"pre_enablement_link_state": "symlink"},
        )
        verified_widened = backend.verify_operation(operation, staged_evidence=staged_evidence)
        self.assertTrue(verified_widened.ok, verified_widened)

    def test_verify_operation_refuses_converged_legacy_when_staged_evidence_shows_link_absent_at_stage_time(self) -> None:
        # T-3611 ask (b): same live-converged shape as the admit test above,
        # but the staged evidence records the link as ABSENT at stage time
        # -- the dangling/TOCTOU shape (something enabled the timer AFTER
        # staging, before this re-verify). Must stay refused even though a
        # staged_evidence argument is present.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-verify-staged-dangling")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend()
        staged_evidence = OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            True,
            "consume timer enablement link is absent",
            {"pre_enablement_link_state": "absent"},
        )
        verified = backend.verify_operation(operation, staged_evidence=staged_evidence)
        self.assertFalse(verified.ok, verified)

    @_with_codex_config_fixture
    def test_check_staged_admits_legacy_converged_timer_enablement_staged_before_stage_disabled(self) -> None:
        # T-3611 ask (a), at the integration level this bug actually
        # manifested at (_validate_staged_receipt via check_staged) --
        # isolated from T-3614's activate_staged-only Bug 2 since
        # check_staged never runs the activation loop.
        e2e_root, e2e_plan, _unused = self.orchestrator_local_stage_fixture("r38-check-staged-legit")
        self.write_consume_timer_link(e2e_root, str(self.consume_timer_link_paths(e2e_root)[1]))
        self.prepare_mailbox_probe_dirs(e2e_plan)
        e2e_backend = LegacyConvergedTimerBackend()
        stamp = "stamp-r38-check-staged-legit"
        stage_code, stage_summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=e2e_backend,
            root=e2e_root,
        )
        self.assertEqual(stage_code, 0, stage_summary)
        ok, message = persona_apply.check_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            stamp,
            backend=e2e_backend,
            root=e2e_root,
        )
        self.assertTrue(ok, message)

    def test_activate_staged_rejects_non_canonical_link(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-non-canonical")
        self.write_consume_timer_link(root, "/tmp/not-the-canonical-template.timer")
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend()
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_activate_staged_rejects_service_result_failed(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-service-failed")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(service_result="exit-code", service_exec_main_status="1")
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_activate_staged_admits_success_result_with_nonzero_successexitstatus_code(self) -> None:
        # T-3105: a consume service may map benign skip codes to success via
        # SuccessExitStatus (recovery's pantheon-persona-consume@recovery.service is
        # configured SuccessExitStatus=3 4 5 and exits 5 on an idle cycle).
        # systemd has already applied that policy, so Result==success IS the
        # exit-vs-policy match -- the legacy probe must admit it rather than
        # reject on a hardcoded ExecMainStatus=="0". This is the accept-
        # counterpart of the reject-on-real-failure test above (which keeps
        # Result!=success rejecting). Mutation guard: with the old
        # `ExecMainStatus != "0"` invariant this input is rejected as conflicting.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-admit-successexitstatus-nonzero")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(service_result="success", service_exec_main_status="5")
        result = backend.preflight(operation)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.classification, "converged-legacy")
        self.assertEqual(backend.events["connective.unit-fire"].evidence.get("result"), "success")
        self.assertEqual(backend.events["connective.unit-fire"].evidence.get("exec_main_status"), "5")
        # T-3107: the legacy evidence must carry the success policy so the shared
        # hybrid verify() acceptance gate admits this exit-5 on the re-verify path
        # too (not just the preflight probe). Without it the gate defaults policy
        # to "" and rejects exit 5.
        self.assertEqual(
            backend.events["connective.unit-fire"].evidence.get("success_exit_status"), "3 4 5"
        )

    def test_activate_staged_rejects_reset_failed_masked_out_of_policy_exit(self) -> None:
        # T-3106: `systemctl reset-failed` clears Result to success but leaves
        # ExecMainStatus stale, so a genuinely-failed service (exit 1, NOT in the
        # unit's SuccessExitStatus={3,4,5}) can read Result=success at a point-in-
        # time cutover probe. Trusting Result alone (T-3105) admitted it; the hybrid
        # exit-status invariant re-derives membership and REJECTS it (1 not in
        # {0,3,4,5}). Mutation guard: with T-3105's Result-only gate this is admitted.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-reset-masked")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(
            service_result="success",
            service_exec_main_status="1",
            service_success_exit_status="3 4 5",
        )
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_activate_staged_rejects_nonzero_exit_absent_from_success_policy(self) -> None:
        # T-3106: a non-zero exit is only in policy if the unit actually lists it in
        # SuccessExitStatus. Exit 5 with an EMPTY policy is a real failure -> reject
        # (guards against admitting on any non-zero exit regardless of policy).
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-no-policy")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(
            service_result="success",
            service_exec_main_status="5",
            service_success_exit_status="",
        )
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_exec_status_within_success_policy(self) -> None:
        # T-3106: pure exit-vs-policy helper shared by the legacy-convergence probe
        # and the active first-fire apply path.
        f = _exec_status_within_success_policy
        self.assertTrue(f("0", ""))            # exit 0 is always in policy
        self.assertTrue(f("0", "3 4 5"))
        self.assertTrue(f("5", "3 4 5"))       # recovery: mapped-success non-zero exit
        self.assertTrue(f("3", "3 4 5"))
        self.assertTrue(f("5", "3 4 SIGTERM 5"))  # signal tokens skipped, 5 matched
        self.assertFalse(f("1", "3 4 5"))      # reset-masked out-of-policy failure
        self.assertFalse(f("5", ""))           # non-zero exit, no policy
        self.assertFalse(f("", "3 4 5"))       # missing exec status
        self.assertFalse(f("garbage", "3 4 5"))

    def test_unit_fire_snapshot_has_failed_honors_success_policy(self) -> None:
        # T-3107: the unit-fire "has failed" gate (the earliest fresh-install gate,
        # runs before the disabled-first-fire completion / timer-observe gates) now
        # uses the shared success-policy helper instead of hardcoding
        # ExecMainStatus in {"", "0"}. So a fresh consume service that exits an
        # in-policy non-zero code (recovery: 5 in SuccessExitStatus=3 4 5) on its idle
        # first fire is NOT treated as failed, while an out-of-policy exit still is.
        # Mutation guard: revert the exec clause to `not in {"", "0"}` and the
        # exit-5-in-policy case flips to failed.
        def snap(*, result="success", active="inactive", exec_status="0", policy="3 4 5"):
            return UnitFirePollSnapshot(
                active_state=active, substate="dead", result=result,
                exec_main_status=exec_status, success_exit_status=policy,
                invocation_id="", invocation_id_valid=False, job="",
                stdout_length=0, stdout_sha256="",
            )
        # in policy -> NOT failed
        self.assertFalse(_unit_fire_snapshot_has_failed(snap(exec_status="0")))
        self.assertFalse(_unit_fire_snapshot_has_failed(snap(exec_status="5", policy="3 4 5")))
        self.assertFalse(_unit_fire_snapshot_has_failed(snap(exec_status="")))  # no exit yet
        # out of policy / real failure -> failed
        self.assertTrue(_unit_fire_snapshot_has_failed(snap(exec_status="1", policy="3 4 5")))
        self.assertTrue(_unit_fire_snapshot_has_failed(snap(exec_status="5", policy="")))
        self.assertTrue(_unit_fire_snapshot_has_failed(snap(result="exit-code", exec_status="1")))
        self.assertTrue(_unit_fire_snapshot_has_failed(snap(active="failed")))

    def test_activate_staged_rejects_infinite_next_elapse(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-infinite-elapse")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(next_elapse_finite=False)
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_activate_staged_rejects_inactive_dead_timer(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-reject-inactive-dead")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(timer_active_state="inactive", timer_sub_state="dead")
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertEqual(backend.events, {})

    def test_execute_timer_enablement_absence_check_admits_legacy_via_probe(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-execute-gate4")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend()
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_STAGE_PREFIX)
        self.assertTrue(evidence.ok, evidence.detail)
        self.assertEqual(evidence.evidence.get("historical_boundary_absent"), "true")
        self.assertFalse(mutation.changed)
        self.assertIn("connective.unit-fire", backend.events)
        self.assertIn("connective.timer-observe", backend.events)

    def test_legacy_convergence_evidence_returns_none_when_desired_timer_missing(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-missing-timer")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        degenerate = dataclasses.replace(
            operation,
            desired={key: value for key, value in operation.desired.items() if key != "timer"},
        )
        backend = LegacyConvergedTimerBackend()
        self.assertIsNone(backend._legacy_convergence_evidence(degenerate))

    def test_legacy_convergence_evidence_falls_back_to_pwd_home_for_gate_shapes_without_link_paths(self) -> None:
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-pwd-fallback")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        unit_fire_operation = self.operation(plan, "connective.unit-fire")
        backend = LegacyConvergedTimerBackend()
        fake_pwd_entry = SimpleNamespace(pw_dir=str(root / "home" / "claude"))
        with patch("tools.persona_installer.pwd.getpwnam", return_value=fake_pwd_entry):
            evidence = backend._legacy_convergence_evidence(unit_fire_operation)
        self.assertIsNotNone(evidence)
        unit_fire_evidence, timer_observe_evidence = evidence
        self.assertEqual(unit_fire_evidence["result"], "success")
        self.assertEqual(timer_observe_evidence["next_elapse_finite"], "true")

    def test_legacy_convergence_evidence_admits_service_that_settles_within_resample_window(self) -> None:
        # T-3211: recovery's 11:02:27Z abort -- the probe sampled the consume
        # SERVICE mid-fire (activating -> active) and rejected on a single
        # point-in-time read even though the seat was, in fact, converged.
        # Sequence: active (Job set) -> active (Job set) -> settled inactive/
        # success/exit-5/policy "3 4 5" (recovery's exact idle-exit shape).
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-settle-admits")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(
            service_sample_sequence=[
                {"ActiveState": "active", "Job": "9724960"},
                {"ActiveState": "active", "Job": "9724960"},
                {
                    "ActiveState": "inactive",
                    "Result": "success",
                    "ExecMainStatus": "5",
                    "SuccessExitStatus": "3 4 5",
                    "Job": "",
                },
            ],
        )
        evidence = backend._legacy_convergence_evidence(operation)
        self.assertIsNotNone(evidence)
        unit_fire_evidence, timer_observe_evidence = evidence
        self.assertEqual(unit_fire_evidence["result"], "success")
        self.assertEqual(unit_fire_evidence["exec_main_status"], "5")
        self.assertEqual(timer_observe_evidence["next_elapse_finite"], "true")
        self.assertEqual(len(backend.sleep_calls), 2)
        self.assertTrue(
            all(seconds == LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS for seconds in backend.sleep_calls)
        )

    def test_legacy_convergence_evidence_rejects_service_in_flight_for_whole_window(self) -> None:
        # T-3211 mutation guard: a service that NEVER settles must still be
        # rejected, and the poll loop must be bounded (window / interval
        # polls), not infinite.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-settle-never")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(service_active_state="active")
        evidence = backend._legacy_convergence_evidence(operation)
        self.assertIsNone(evidence)
        expected_polls = int(
            LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS / LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS
        )
        self.assertEqual(len(backend.sleep_calls), expected_polls)

    def test_legacy_convergence_evidence_admits_inactive_on_first_sample_with_zero_resamples(self) -> None:
        # T-3211: the fast path (today's behavior) must cost nothing extra --
        # a service that is already settled on the FIRST sample triggers no
        # resample and no sleep at all.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-settle-fastpath")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend()
        evidence = backend._legacy_convergence_evidence(operation)
        self.assertIsNotNone(evidence)
        self.assertEqual(backend.sleep_calls, [])

    def test_local_host_stage_backend_unit_fire_clock_is_fake_not_real(self) -> None:
        # T-3612: the fake-clock seam (T-3211/T-3614) must live on the
        # SHARED LocalHostStageBackend base, not only on
        # LegacyConvergedTimerBackend -- every test that instantiates the
        # base directly and drives unit-fire polling gets it for free. A
        # real _unit_fire_sleep would block the calling thread for the
        # requested duration; the fake one must record the call and advance
        # _unit_fire_monotonic() by exactly that amount without blocking.
        backend = LocalHostStageBackend()
        self.assertEqual(backend.sleep_calls, [])
        start = backend._unit_fire_monotonic()
        real_start = time.monotonic()
        backend._unit_fire_sleep(5.0)
        real_elapsed = time.monotonic() - real_start
        self.assertLess(real_elapsed, 1.0, "fake clock must not block the real thread")
        self.assertEqual(backend.sleep_calls, [5.0])
        self.assertEqual(backend._unit_fire_monotonic(), start + 5.0)

    def test_legacy_convergence_evidence_rejects_service_that_settles_failed_within_window(self) -> None:
        # T-3211: settling is not sufficient on its own -- a service that
        # settles to a genuine failure (Result=exit-code, ExecMainStatus=1,
        # not in SuccessExitStatus) within the window must still be rejected,
        # exactly as a same-shape single-sample read is rejected today
        # (T-3106).
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-settle-failed")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(
            service_sample_sequence=[
                {"ActiveState": "active", "Job": "9724960"},
                {
                    "ActiveState": "inactive",
                    "Result": "exit-code",
                    "ExecMainStatus": "1",
                    "SuccessExitStatus": "3 4 5",
                    "Job": "",
                },
            ],
        )
        evidence = backend._legacy_convergence_evidence(operation)
        self.assertIsNone(evidence)
        self.assertEqual(len(backend.sleep_calls), 1)

    def test_legacy_convergence_evidence_admits_timer_nonfinite_in_flight_finite_after_settle(self) -> None:
        # T-3211 (Recovery's "MID-FIRE TIMER CAPTURE"): sampling recovery's live
        # units at 0.2s through a real fire showed the TIMER reading
        # ActiveState=active SubState=running NextElapseUSecMonotonic=infinity
        # while the consume SERVICE was activating, settling to
        # SubState=waiting with a finite monotonic elapse only once the
        # service went inactive. A pre-settle timer sample fails BOTH the
        # waiting check and _finite_next_elapse_basis. This proves the probe
        # samples the timer only AFTER the service settles (the approved
        # service-first-then-timer order): timer_tracks_service_settle=True
        # ties the fixture's timer response to whichever service sample was
        # most recently returned, so a regression back to timer-before-
        # settle would make this test see the in-flight (rejecting) timer
        # shape and fail.
        root, plan = self.orchestrator_timer_gate_fixture_root("r38-legacy-settle-timer-coupled")
        self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        operation = self.operation(plan, "connective.timer-enablement-absent")
        backend = LegacyConvergedTimerBackend(
            timer_tracks_service_settle=True,
            service_sample_sequence=[
                {"ActiveState": "active", "Job": "9724960"},
                {
                    "ActiveState": "inactive",
                    "Result": "success",
                    "ExecMainStatus": "5",
                    "SuccessExitStatus": "3 4 5",
                    "Job": "",
                },
            ],
        )
        evidence = backend._legacy_convergence_evidence(operation)
        self.assertIsNotNone(evidence)
        unit_fire_evidence, timer_observe_evidence = evidence
        self.assertEqual(unit_fire_evidence["result"], "success")
        self.assertEqual(timer_observe_evidence["next_elapse_finite"], "true")
        self.assertEqual(len(backend.sleep_calls), 1)

    @_with_codex_config_fixture
    def test_activate_staged_dangling_timer_enablement_red_green_spans_stage_disabled_then_activate(self) -> None:
        root, plan, backend = self.orchestrator_local_stage_fixture("r29-stage-activate-span")
        link, template = self.consume_timer_link_paths(root)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(str(template))
        blocked, blocked_summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-span-blocked",
            backend=backend,
            root=root,
        )
        self.assertNotEqual(blocked, 0, blocked_summary)
        link.unlink()
        clean, clean_summary = persona_apply.stage_disabled(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-span-clean",
            backend=backend,
            root=root,
        )
        self.assertEqual(clean, 0, clean_summary)
        self.write_consume_timer_link(root, template)
        before_events = self.activation_suffix_events(backend, plan)
        activated, activate_summary = persona_apply.activate_staged(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            "claude",
            REPO_ROOT,
            "stamp-r29-span-clean",
            backend=backend,
            root=root,
        )
        self.assertNotEqual(activated, 0, activate_summary)
        self.assertEqual(before_events, self.activation_suffix_events(backend, plan))
        self.assertTrue(link.is_symlink())

    def test_final_apply_verification_uses_historical_absence_after_timer_enable(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        report = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        guard = self.operation(plan, "connective.timer-enablement-absent")
        verified = backend.verify_operation(guard)
        self.assertTrue(verified.ok, verified)
        self.assertIn("historical", verified.detail)

    def test_staged_receipt_requires_timer_enablement_guard_mutation_and_evidence(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(staged.status, "applied", staged.errors)
        self.assertIsNotNone(staged.receipt)
        assert staged.receipt is not None
        stripped = dataclasses.replace(
            staged.receipt,
            mutations=tuple(
                item
                for item in staged.receipt.mutations
                if item.operation_id != "connective.timer-enablement-absent"
            ),
        )
        refused = self.installer.activate_staged(plan, plan.digest, stripped, backend, self.actor(plan))
        self.assertEqual(refused.status, "refused")
        self.assertTrue(
            any("connective.timer-enablement-absent: staged mutation missing or duplicated" in item for item in refused.errors),
            refused.errors,
        )
        stripped_evidence = dataclasses.replace(
            staged.receipt,
            evidence=tuple(
                item
                for item in staged.receipt.evidence
                if item.operation_id != "connective.timer-enablement-absent"
            ),
        )
        refused_evidence = self.installer.activate_staged(plan, plan.digest, stripped_evidence, backend, self.actor(plan))
        self.assertEqual(refused_evidence.status, "refused")
        self.assertTrue(
            any("connective.timer-enablement-absent: staged evidence missing or duplicated" in item for item in refused_evidence.errors),
            refused_evidence.errors,
        )

    @_with_codex_config_fixture
    def test_timer_enablement_receipt_accepts_absolute_systemd_link_target(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-absolute-link", "stamp-r29-absolute-link")
        link, template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.assertTrue(LocalHostStageBackend().can_rollback(mutation).ok)

    @_with_codex_config_fixture
    def test_timer_enablement_receipt_accepts_safe_relative_link_target_to_canonical_template(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-relative-link", "stamp-r29-relative-link")
        link, _template = self.write_consume_timer_link(root, "../pantheon-persona-consume@.timer")
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.assertTrue(LocalHostStageBackend().can_rollback(mutation).ok)

    @_with_codex_config_fixture
    def test_timer_enablement_receipt_refuses_wrong_absolute_or_relative_target_without_unlink(self) -> None:
        for label, actual in (
            ("wrong-absolute", str(self.root / "foreign.timer")),
            ("wrong-relative", "../foreign.timer"),
        ):
            with self.subTest(label=label):
                root, _plan, _backend, _summary = self.orchestrator_stage_disabled(f"r29-{label}", f"stamp-r29-{label}")
                link, _template = self.write_consume_timer_link(root, actual)
                mutation = self.timer_enable_owned_mutation(root, actual_readlink=actual)
                result = LocalHostStageBackend().can_rollback(mutation)
                self.assertFalse(result.ok)
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), actual)

    @_with_codex_config_fixture
    def test_rollback_staged_stop_not_loaded_converged_when_show_notfound_inactive_dead(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-stop-not-loaded", "stamp-r29-stop-not-loaded")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.rewrite_staged_checkpoint_timer_mutation(root, "stamp-r29-stop-not-loaded", mutation)

        class StopNotLoadedBackend(LocalHostStageBackend):
            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:3] == ("systemctl", "--user", "stop"):
                    return subprocess.CompletedProcess(command, 1, "", "arbitrary stop failure text")
                if command[:3] == ("systemctl", "--user", "show") and command[3] in {
                    "pantheon-persona-consume@orchestrator.timer",
                    "pantheon-persona-consume@orchestrator.service",
                }:
                    return subprocess.CompletedProcess(command, 0, "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                if command[:4] == ("systemctl", "--user", "disable", "--now"):
                    return subprocess.CompletedProcess(command, 1, "", "Unit not loaded")
                if command[:3] == ("systemctl", "--user", "daemon-reload"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ("systemctl", "--user", "reset-failed"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return super()._run(argv, timeout=timeout)

        code, summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r29-stop-not-loaded",
            root / "home" / "claude",
            backend=StopNotLoadedBackend(),
        )
        self.assertEqual(code, 0, summary)
        self.assertFalse(link.exists() or link.is_symlink())

    @_with_codex_config_fixture
    def test_rollback_staged_stop_not_loaded_refuses_active_or_ambiguous_timer_state(self) -> None:
        for label, stdout in (
            ("active", "LoadState=not-found\nUnitFileState=not-found\nActiveState=active\nSubState=running\n"),
            ("ambiguous", "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\n"),
        ):
            with self.subTest(label=label):
                root, _plan, _backend, _summary = self.orchestrator_stage_disabled(f"r29-stop-{label}", f"stamp-r29-stop-{label}")
                link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
                mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
                self.rewrite_staged_checkpoint_timer_mutation(root, f"stamp-r29-stop-{label}", mutation)

                class RefusingBackend(LocalHostStageBackend):
                    def _run(self, argv, *, timeout=None):  # type: ignore[override]
                        command = tuple(str(item) for item in argv)
                        self.commands.append(command)
                        self.timeouts.append((command, timeout))
                        if command[:3] == ("systemctl", "--user", "stop"):
                            return subprocess.CompletedProcess(command, 1, "", "Unit not loaded")
                        if command[:3] == ("systemctl", "--user", "show") and command[3] in {
                            "pantheon-persona-consume@orchestrator.timer",
                            "pantheon-persona-consume@orchestrator.service",
                        }:
                            return subprocess.CompletedProcess(command, 0, stdout, "")
                        return super()._run(argv, timeout=timeout)

                code, summary = persona_apply.rollback_staged(
                    "orchestrator",
                    f"stamp-r29-stop-{label}",
                    root / "home" / "claude",
                    backend=RefusingBackend(),
                )
                self.assertNotEqual(code, 0, summary)
                self.assertTrue(link.is_symlink())

    @_with_codex_config_fixture
    def test_activate_staged_receipt_owned_timer_enablement_cleanup_precedes_template_install(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-owned-order", "stamp-r29-owned-order")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.rewrite_staged_checkpoint_timer_mutation(root, "stamp-r29-owned-order", mutation)

        class OrderBackend(LocalHostStageBackend):
            def rollback_mutation(self, item):  # type: ignore[override]
                if item.operation_id == "connective.timer-unit":
                    self.commands.append(("assert-link-absent-before-template-removal",))
                    if link.exists() or link.is_symlink():
                        raise OperationFailed("enablement link still exists before timer template removal")
                return super().rollback_mutation(item)

        code, summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r29-owned-order",
            root / "home" / "claude",
            backend=OrderBackend(),
        )
        self.assertEqual(code, 0, summary)
        self.assertFalse(link.exists() or link.is_symlink())

    @_with_codex_config_fixture
    def test_rollback_staged_removes_receipt_owned_enablement_link_before_timer_template_removal(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-remove-link", "stamp-r29-remove-link")
        link, _template = self.write_consume_timer_link(root, "../pantheon-persona-consume@.timer")
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.rewrite_staged_checkpoint_timer_mutation(root, "stamp-r29-remove-link", mutation)
        code, summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r29-remove-link",
            root / "home" / "claude",
            backend=LocalHostStageBackend(),
        )
        self.assertEqual(code, 0, summary)
        self.assertFalse(link.exists() or link.is_symlink())

    @_with_codex_config_fixture
    def test_rollback_staged_complete_leaves_no_dangling_consume_timer_reactivation_path(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-no-reactivation", "stamp-r29-no-reactivation")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        mutation = self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link))
        self.rewrite_staged_checkpoint_timer_mutation(root, "stamp-r29-no-reactivation", mutation)
        code, summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r29-no-reactivation",
            root / "home" / "claude",
            backend=LocalHostStageBackend(),
        )
        self.assertEqual(code, 0, summary)
        self.assertFalse(link.exists() or link.is_symlink())

    @_with_codex_config_fixture
    def test_rollback_staged_staged_only_timer_intent_does_not_authorize_link_cleanup(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r29-staged-intent", "stamp-r29-staged-intent")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        mutation = self.timer_enable_owned_mutation(
            root,
            actual_readlink=os.readlink(link),
            changed=False,
            owned=False,
            activation_intent=True,
        )
        self.rewrite_staged_checkpoint_timer_mutation(root, "stamp-r29-staged-intent", mutation, phase="staged")
        code, summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r29-staged-intent",
            root / "home" / "claude",
            backend=LocalHostStageBackend(),
        )
        self.assertEqual(code, 0, summary)
        self.assertTrue(link.is_symlink())

    def test_pre_command_timer_enable_failure_does_not_synthesize_owned_recovery_mutation(self) -> None:
        plan = self.plan()

        class PreCommandConcurrentBackend(SimulatedHostBackend):
            def execute(self, operation, context):  # type: ignore[override]
                if operation.operation_id == "connective.timer-enable":
                    link = operation.desired["enablement_link_path"]
                    canonical = operation.desired["canonical_template_path"]
                    self.timer_enabled = True
                    self.entries[link] = {
                        "state": "symlink",
                        "target": canonical,
                        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    }
                    raise OperationFailed("pre-command failure")
                return super().execute(operation, context)

        backend = PreCommandConcurrentBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutations = [item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable"]
        self.assertEqual(len(timer_mutations), 1)
        self.assertFalse(timer_mutations[0].changed)
        self.assertEqual(timer_mutations[0].before.get("owned"), "false")
        self.assertTrue(backend.timer_enabled)
        self.assertIn(self.operation(plan, "connective.timer-enable").desired["enablement_link_path"], backend.entries)

    def test_issued_timer_enable_partial_failure_synthesizes_owned_recovery_mutation(self) -> None:
        plan = self.plan()

        class IssuedFailureBackend(SimulatedHostBackend):
            def execute(self, operation, context):  # type: ignore[override]
                mutation, evidence = super().execute(operation, context)
                if operation.operation_id == "connective.timer-enable":
                    raise OperationFailed(
                        "post-enable proof failed",
                        evidence={"enable_command_issued": "true", "enable_returncode": "0"},
                    )
                return mutation, evidence

        backend = IssuedFailureBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutations = [item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable"]
        self.assertEqual(len(timer_mutations), 1)
        self.assertTrue(timer_mutations[0].changed)
        self.assertEqual(timer_mutations[0].before.get("owned"), "true")
        self.assertFalse(backend.timer_enabled)
        self.assertNotIn(self.operation(plan, "connective.timer-enable").desired["enablement_link_path"], backend.entries)

    def test_issued_timer_enable_failure_wrong_link_refuses_without_unlink(self) -> None:
        plan = self.plan()

        class ReplacedLinkFailureBackend(SimulatedHostBackend):
            def execute(self, operation, context):  # type: ignore[override]
                mutation, evidence = super().execute(operation, context)
                if operation.operation_id == "connective.timer-enable":
                    link = operation.desired["enablement_link_path"]
                    self.entries[link] = {
                        "state": "symlink",
                        "target": "/tmp/replaced.timer",
                        "fingerprint": hashlib.sha256(b"/tmp/replaced.timer").hexdigest(),
                    }
                    raise OperationFailed(
                        "post-enable proof failed",
                        evidence={"enable_command_issued": "true", "enable_returncode": "0"},
                    )
                return mutation, evidence

        backend = ReplacedLinkFailureBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertIsNotNone(report.receipt)
        assert report.receipt is not None
        timer_mutations = [item for item in report.receipt.mutations if item.operation_id == "connective.timer-enable"]
        self.assertEqual(len(timer_mutations), 1)
        self.assertFalse(timer_mutations[0].changed)
        link = self.operation(plan, "connective.timer-enable").desired["enablement_link_path"]
        self.assertEqual(backend.entries[link]["target"], "/tmp/replaced.timer")

    @_with_codex_config_fixture
    def test_rollback_staged_refuses_removed_but_active_consume_timer(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r28-active-refuse", "stamp-r28-active-refuse")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        self.rewrite_staged_checkpoint_timer_mutation(
            root,
            "stamp-r28-active-refuse",
            self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link)),
        )

        class RemovedActiveBackend(LocalHostStageBackend):
            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.timer":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "LoadState=not-found\nUnitFileState=not-found\nActiveState=active\nSubState=running\n",
                        "",
                    )
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.service":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "LoadState=loaded\nUnitFileState=static\nActiveState=active\nSubState=running\n",
                        "",
                    )
                if command[:3] == ("systemctl", "--user", "stop"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return super()._run(argv, timeout=timeout)

        backend = RemovedActiveBackend()
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r28-active-refuse",
            root / "home" / "claude",
            backend=backend,
        )
        self.assertNotEqual(rollback_code, 0, rollback_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=failed", rollback_summary)
        self.assertNotIn("ROLLBACK_STAGED_STATUS=noop", rollback_summary)
        self.assertIn(("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.timer"), backend.commands)

    @_with_codex_config_fixture
    def test_timer_rollback_stops_missing_file_active_timer_and_proves_dead(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r28-active-success", "stamp-r28-active-success")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        self.rewrite_staged_checkpoint_timer_mutation(
            root,
            "stamp-r28-active-success",
            self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link)),
        )

        class RemovedActiveConvergesBackend(LocalHostStageBackend):
            def __init__(self) -> None:
                super().__init__()
                self.timer_show_count = 0
                self.service_show_count = 0
                self.timer_stopped = False
                self.service_stopped = False

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.timer":
                    self.timer_show_count += 1
                    if not self.timer_stopped:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            "LoadState=not-found\nUnitFileState=not-found\nActiveState=active\nSubState=running\n",
                            "",
                        )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n",
                        "",
                    )
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.service":
                    self.service_show_count += 1
                    if not self.service_stopped:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            "LoadState=loaded\nUnitFileState=static\nActiveState=active\nSubState=running\n",
                            "",
                        )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n",
                        "",
                    )
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.timer"):
                    self.timer_stopped = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service"):
                    self.service_stopped = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:4] == ("systemctl", "--user", "disable", "--now"):
                    return subprocess.CompletedProcess(command, 1, "", "Unit file does not exist")
                if command[:3] == ("systemctl", "--user", "daemon-reload"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ("systemctl", "--user", "reset-failed"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return super()._run(argv, timeout=timeout)

        backend = RemovedActiveConvergesBackend()
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r28-active-success",
            root / "home" / "claude",
            backend=backend,
        )
        self.assertEqual(rollback_code, 0, rollback_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=rolled-back", rollback_summary)
        timer_stop = backend.commands.index(("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.timer"))
        service_stop = backend.commands.index(("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service"))
        disable = backend.commands.index(("systemctl", "--user", "disable", "--now", "pantheon-persona-consume@orchestrator.timer"))
        self.assertLess(timer_stop, service_stop)
        self.assertLess(service_stop, disable)

    @_with_codex_config_fixture
    def test_rollback_staged_timer_teardown_is_idempotent_after_partial_failure(self) -> None:
        root, _plan, _backend, _summary = self.orchestrator_stage_disabled("r28-partial-retry", "stamp-r28-partial-retry")
        link, _template = self.write_consume_timer_link(root, str(self.consume_timer_link_paths(root)[1]))
        self.rewrite_staged_checkpoint_timer_mutation(
            root,
            "stamp-r28-partial-retry",
            self.timer_enable_owned_mutation(root, actual_readlink=os.readlink(link)),
        )

        class PartialFailureBackend(LocalHostStageBackend):
            def __init__(self, *, fail_reload: bool) -> None:
                super().__init__()
                self.fail_reload = fail_reload
                self.timer_active = True
                self.service_active = True

            def _state(self, *, active: bool, unit_file_state: str = "not-found") -> str:
                return (
                    f"LoadState=not-found\nUnitFileState={unit_file_state}\n"
                    f"ActiveState={'active' if active else 'inactive'}\n"
                    f"SubState={'running' if active else 'dead'}\n"
                )

            def _run(self, argv, *, timeout=None):  # type: ignore[override]
                command = tuple(str(item) for item in argv)
                self.commands.append(command)
                self.timeouts.append((command, timeout))
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.timer":
                    return subprocess.CompletedProcess(command, 0, self._state(active=self.timer_active), "")
                if command[:3] == ("systemctl", "--user", "show") and command[3] == "pantheon-persona-consume@orchestrator.service":
                    return subprocess.CompletedProcess(command, 0, self._state(active=self.service_active), "")
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.timer"):
                    self.timer_active = False
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command == ("systemctl", "--user", "stop", "pantheon-persona-consume@orchestrator.service"):
                    self.service_active = False
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:4] == ("systemctl", "--user", "disable", "--now"):
                    return subprocess.CompletedProcess(command, 1, "", "Unit file does not exist")
                if command[:3] == ("systemctl", "--user", "daemon-reload"):
                    if self.fail_reload:
                        return subprocess.CompletedProcess(command, 1, "", "reload failed")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ("systemctl", "--user", "reset-failed"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return super()._run(argv, timeout=timeout)

        first = PartialFailureBackend(fail_reload=True)
        first_code, first_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r28-partial-retry",
            root / "home" / "claude",
            backend=first,
        )
        self.assertNotEqual(first_code, 0, first_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=failed", first_summary)

        retry = PartialFailureBackend(fail_reload=False)
        retry.timer_active = False
        retry.service_active = False
        retry_code, retry_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r28-partial-retry",
            root / "home" / "claude",
            backend=retry,
        )
        self.assertEqual(retry_code, 0, retry_summary)
        self.assertIn("ROLLBACK_STAGED_STATUS=rolled-back", retry_summary)

    def test_same_stamp_failed_activation_rollback_preserves_single_receiver_restart_order(self) -> None:
        script = REPO_ROOT / "tools" / "persona_rollback.sh"

        def run_case(*, helper_ok: bool) -> tuple[int, str, list[str]]:
            sandbox = self.root / ("rollback-ok" if helper_ok else "rollback-fail")
            fakebin = sandbox / "bin"
            home = sandbox / "home" / "claude"
            runtime = sandbox / "run"
            log = sandbox / "events.log"
            fakebin.mkdir(parents=True)
            runtime.mkdir(parents=True)
            for path in (
                home / ".config" / "systemd" / "user" / "claude-telegram.service",
                home / ".local" / "bin" / "orchestrator-bot-supervise.sh",
                home / ".claude" / "settings.json",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("live\n", encoding="utf-8")
                Path(f"{path}.pre-cutover-stamp-r19").write_text("backup\n", encoding="utf-8")
            helper = home / "pantheon-config" / "tools" / "t2611_recovery_persona_apply.py"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

            (fakebin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"LOG={shlex.quote(str(log))}\n"
                "case \"$1\" in\n"
                "  *persona_cutover_resolve.py)\n"
                f"    cat <<'ENV'\n"
                "PCX_PERSONA=orchestrator\n"
                "PCX_ROLE=orchestrator\n"
                "PCX_ACCOUNT=claude\n"
                f"PCX_UID={os.geteuid()}\n"
                f"PCX_HOME={shlex.quote(str(home))}\n"
                f"PCX_XDG_RUNTIME_DIR={shlex.quote(str(runtime))}\n"
                "PCX_DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/test-bus\n"
                "PCX_TMUX_SESSION=claude-telegram-bot\n"
                "PCX_SERVICE=claude-telegram.service\n"
                f"PCX_SERVICE_UNIT_PATH={shlex.quote(str(home / '.config' / 'systemd' / 'user' / 'claude-telegram.service'))}\n"
                f"PCX_SETTINGS_JSON_PATH={shlex.quote(str(home / '.claude' / 'settings.json'))}\n"
                f"PCX_SUPERVISE_WRAPPER_PATH={shlex.quote(str(home / '.local' / 'bin' / 'orchestrator-bot-supervise.sh'))}\n"
                "PCX_CONSUME_TIMER=pantheon-persona-consume@orchestrator.timer\n"
                "PCX_CONSUME_SERVICE=pantheon-persona-consume@orchestrator.service\n"
                f"PCX_APPLY_PATH={shlex.quote(str(fakebin))}:/usr/bin\n"
                "PCX_COMPANION_UNITS=\n"
                "ENV\n"
                "    exit 0 ;;\n"
                "  *t2611_recovery_persona_apply.py)\n"
                "    echo rollback-staged >> \"$LOG\"\n"
                + (
                    "    echo 'ROLLBACK_STAGED_STATUS=noop RESTORED=0 ROLLBACK_ERRORS=[]'\n    exit 0 ;;\n"
                    if helper_ok
                    else "    echo 'ROLLBACK_STAGED_STATUS=refused RESTORED=0 ROLLBACK_ERRORS=[\"drift\"]' >&2\n    exit 1 ;;\n"
                )
                + "  *) exec /usr/bin/python3 \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fakebin / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"LOG={shlex.quote(str(log))}\n"
                f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
                "echo systemctl:$* >> \"$LOG\"\n"
                "if [[ \"$*\" == \"--user show pantheon-runtime-tmux.scope -p ActiveState --value\" ]]; then echo inactive; exit 0; fi\n"
                "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
                "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.service -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
                "if [[ \"$*\" == \"--user is-active --quiet codex-telegram-poller.service\" ]]; then exit 3; fi\n"
                "if [[ \"$*\" == \"--user restart claude-telegram.service\" ]]; then touch \"$TMUX_UP\"; exit 0; fi\n"
                "if [[ \"$*\" == \"--user is-active claude-telegram.service\" ]]; then echo active; exit 0; fi\n"
                "if [[ \"$*\" == \"--user is-enabled claude-telegram.service\" ]]; then echo enabled; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fakebin / "tmux").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"LOG={shlex.quote(str(log))}\n"
                f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
                "echo tmux:$* >> \"$LOG\"\n"
                "if [[ \"${1:-}\" == \"kill-session\" ]]; then rm -f \"$TMUX_UP\"; exit 0; fi\n"
                "if [[ \"${1:-}\" == \"has-session\" ]]; then [[ -f \"$TMUX_UP\" ]] && exit 0 || exit 1; fi\n"
                "if [[ \"${1:-}\" == \"-N\" ]]; then exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fakebin / "pgrep").write_text(
                "#!/usr/bin/env bash\n"
                f"echo pgrep:$* >> {shlex.quote(str(log))}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for executable in fakebin.iterdir():
                executable.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
            proc = subprocess.run(
                ["bash", str(script), "--persona", "orchestrator", "--stamp", "stamp-r19", "--unit-file", str(sandbox / "unit.service"), "--execute"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            events = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return proc.returncode, proc.stdout + proc.stderr, events

        ok_rc, ok_output, ok_events = run_case(helper_ok=True)
        self.assertEqual(ok_rc, 0, ok_output)
        codex_stop = ok_events.index("systemctl:--user stop codex-telegram-poller.service")
        codex_probe = ok_events.index("systemctl:--user is-active --quiet codex-telegram-poller.service")
        checkpoint = ok_events.index("rollback-staged")
        predecessor_enable = ok_events.index("systemctl:--user enable claude-telegram.service")
        predecessor_restart = ok_events.index("systemctl:--user restart claude-telegram.service")
        self.assertLess(codex_stop, codex_probe)
        self.assertLess(codex_probe, checkpoint)
        self.assertLess(checkpoint, predecessor_enable)
        self.assertLess(predecessor_enable, predecessor_restart)
        self.assertIn("confirmed: codex-telegram-poller.service inactive/not-found before bespoke restart", ok_output)

        fail_rc, fail_output, fail_events = run_case(helper_ok=False)
        self.assertNotEqual(fail_rc, 0, fail_output)
        self.assertIn("staged checkpoint rollback failed before bespoke restart", fail_output)
        self.assertIn("rollback-staged", fail_events)
        self.assertNotIn("systemctl:--user enable claude-telegram.service", fail_events)
        self.assertNotIn("systemctl:--user restart claude-telegram.service", fail_events)

    def test_same_stamp_failed_activation_rollback_proves_consume_timer_dead_before_predecessor_restart(self) -> None:
        script = REPO_ROOT / "tools" / "persona_rollback.sh"
        sandbox = self.root / "rollback-r28-order"
        fakebin = sandbox / "bin"
        home = sandbox / "home" / "claude"
        runtime = sandbox / "run"
        log = sandbox / "events.log"
        fakebin.mkdir(parents=True)
        runtime.mkdir(parents=True)
        for path in (
            home / ".config" / "systemd" / "user" / "claude-telegram.service",
            home / ".local" / "bin" / "orchestrator-bot-supervise.sh",
            home / ".claude" / "settings.json",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("live\n", encoding="utf-8")
            Path(f"{path}.pre-cutover-stamp-r28").write_text("backup\n", encoding="utf-8")
        helper = home / "pantheon-config" / "tools" / "t2611_recovery_persona_apply.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        state = sandbox / "state"

        (fakebin / "python3").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            "case \"$1\" in\n"
            "  *persona_cutover_resolve.py)\n"
            "    cat <<'ENV'\n"
            "PCX_PERSONA=orchestrator\n"
            "PCX_ROLE=orchestrator\n"
            "PCX_ACCOUNT=claude\n"
            f"PCX_UID={os.geteuid()}\n"
            f"PCX_HOME={shlex.quote(str(home))}\n"
            f"PCX_XDG_RUNTIME_DIR={shlex.quote(str(runtime))}\n"
            "PCX_DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/test-bus\n"
            "PCX_TMUX_SESSION=claude-telegram-bot\n"
            "PCX_SERVICE=claude-telegram.service\n"
            f"PCX_SERVICE_UNIT_PATH={shlex.quote(str(home / '.config' / 'systemd' / 'user' / 'claude-telegram.service'))}\n"
            f"PCX_SETTINGS_JSON_PATH={shlex.quote(str(home / '.claude' / 'settings.json'))}\n"
            f"PCX_SUPERVISE_WRAPPER_PATH={shlex.quote(str(home / '.local' / 'bin' / 'orchestrator-bot-supervise.sh'))}\n"
            "PCX_CONSUME_TIMER=pantheon-persona-consume@orchestrator.timer\n"
            "PCX_CONSUME_SERVICE=pantheon-persona-consume@orchestrator.service\n"
            f"PCX_APPLY_PATH={shlex.quote(str(fakebin))}:/usr/bin\n"
            "PCX_COMPANION_UNITS=\n"
            "ENV\n"
            "    exit 0 ;;\n"
            "  *t2611_recovery_persona_apply.py)\n"
            "    echo rollback-staged >> \"$LOG\"\n"
            "    touch \"$STATE/after-staged\"\n"
            "    echo 'ROLLBACK_STAGED_STATUS=rolled-back RESTORED=1 ROLLBACK_ERRORS=[]'\n"
            "    exit 0 ;;\n"
            "  *) STATE=" + shlex.quote(str(state)) + " exec /usr/bin/python3 \"$@\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fakebin / "systemctl").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            f"STATE={shlex.quote(str(state))}\n"
            f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
            "mkdir -p \"$STATE\"\n"
            "echo systemctl:$* >> \"$LOG\"\n"
            "if [[ \"$*\" == \"--user show pantheon-runtime-tmux.scope -p ActiveState --value\" ]]; then echo inactive; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active --quiet codex-telegram-poller.service\" ]]; then exit 3; fi\n"
            "if [[ \"$*\" == \"--user stop pantheon-persona-consume@orchestrator.timer\" ]]; then touch \"$STATE/timer-stopped\"; exit 0; fi\n"
            "if [[ \"$*\" == \"--user stop pantheon-persona-consume@orchestrator.service\" ]]; then touch \"$STATE/service-stopped\"; exit 0; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then if [[ -f \"$STATE/after-staged\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; elif [[ -f \"$STATE/timer-stopped\" ]]; then printf 'LoadState=loaded\\nUnitFileState=disabled\\nActiveState=inactive\\nSubState=dead\\n'; else printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=active\\nSubState=running\\n'; fi; exit 0; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.service -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then if [[ -f \"$STATE/after-staged\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; elif [[ -f \"$STATE/service-stopped\" ]]; then printf 'LoadState=loaded\\nUnitFileState=static\\nActiveState=inactive\\nSubState=dead\\n'; else printf 'LoadState=loaded\\nUnitFileState=static\\nActiveState=active\\nSubState=running\\n'; fi; exit 0; fi\n"
            "if [[ \"$*\" == \"--user restart claude-telegram.service\" ]]; then touch \"$TMUX_UP\"; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active claude-telegram.service\" ]]; then echo active; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-enabled claude-telegram.service\" ]]; then echo enabled; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "tmux").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
            "echo tmux:$* >> \"$LOG\"\n"
            "if [[ \"${1:-}\" == \"kill-session\" ]]; then rm -f \"$TMUX_UP\"; exit 0; fi\n"
            "if [[ \"${1:-}\" == \"has-session\" ]]; then [[ -f \"$TMUX_UP\" ]] && exit 0 || exit 1; fi\n"
            "if [[ \"${1:-}\" == \"-N\" ]]; then exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "pgrep").write_text(
            "#!/usr/bin/env bash\n"
            f"echo pgrep:$* >> {shlex.quote(str(log))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        for executable in fakebin.iterdir():
            executable.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
        env["STATE"] = str(state)
        proc = subprocess.run(
            ["bash", str(script), "--persona", "orchestrator", "--stamp", "stamp-r28", "--unit-file", str(sandbox / "unit.service"), "--execute"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        events = log.read_text(encoding="utf-8").splitlines()
        timer_stop = events.index("systemctl:--user stop pantheon-persona-consume@orchestrator.timer")
        service_stop = events.index("systemctl:--user stop pantheon-persona-consume@orchestrator.service")
        staged = events.index("rollback-staged")
        final_timer_proof = max(i for i, event in enumerate(events) if event == "systemctl:--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState")
        final_service_proof = max(i for i, event in enumerate(events) if event == "systemctl:--user show pantheon-persona-consume@orchestrator.service -p LoadState -p UnitFileState -p ActiveState -p SubState")
        predecessor_restart = events.index("systemctl:--user restart claude-telegram.service")
        self.assertLess(timer_stop, service_stop)
        self.assertLess(service_stop, staged)
        self.assertLess(staged, final_timer_proof)
        self.assertLess(staged, final_service_proof)
        self.assertLess(final_timer_proof, predecessor_restart)
        self.assertLess(final_service_proof, predecessor_restart)
        self.assertIn("confirmed: consume link absent and timer/service inactive/dead before bespoke restart", output)

    def test_same_stamp_rollback_shell_tolerates_not_loaded_after_exact_notfound_dead_proof(self) -> None:
        script = REPO_ROOT / "tools" / "persona_rollback.sh"
        sandbox = self.root / "rollback-r29-not-loaded"
        fakebin = sandbox / "bin"
        home = sandbox / "home" / "claude"
        runtime = sandbox / "run"
        log = sandbox / "events.log"
        fakebin.mkdir(parents=True)
        runtime.mkdir(parents=True)
        for path in (
            home / ".config" / "systemd" / "user" / "claude-telegram.service",
            home / ".local" / "bin" / "orchestrator-bot-supervise.sh",
            home / ".claude" / "settings.json",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("live\n", encoding="utf-8")
            Path(f"{path}.pre-cutover-stamp-r29").write_text("backup\n", encoding="utf-8")
        helper = home / "pantheon-config" / "tools" / "t2611_recovery_persona_apply.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        (fakebin / "python3").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            "case \"$1\" in\n"
            "  *persona_cutover_resolve.py)\n"
            "    cat <<'ENV'\n"
            "PCX_PERSONA=orchestrator\n"
            "PCX_ROLE=orchestrator\n"
            "PCX_ACCOUNT=claude\n"
            f"PCX_UID={os.geteuid()}\n"
            f"PCX_HOME={shlex.quote(str(home))}\n"
            f"PCX_XDG_RUNTIME_DIR={shlex.quote(str(runtime))}\n"
            "PCX_DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/test-bus\n"
            "PCX_TMUX_SESSION=claude-telegram-bot\n"
            "PCX_SERVICE=claude-telegram.service\n"
            f"PCX_SERVICE_UNIT_PATH={shlex.quote(str(home / '.config' / 'systemd' / 'user' / 'claude-telegram.service'))}\n"
            f"PCX_SETTINGS_JSON_PATH={shlex.quote(str(home / '.claude' / 'settings.json'))}\n"
            f"PCX_SUPERVISE_WRAPPER_PATH={shlex.quote(str(home / '.local' / 'bin' / 'orchestrator-bot-supervise.sh'))}\n"
            "PCX_CONSUME_TIMER=pantheon-persona-consume@orchestrator.timer\n"
            "PCX_CONSUME_SERVICE=pantheon-persona-consume@orchestrator.service\n"
            f"PCX_APPLY_PATH={shlex.quote(str(fakebin))}:/usr/bin\n"
            "PCX_COMPANION_UNITS=\n"
            "ENV\n"
            "    exit 0 ;;\n"
            "  *t2611_recovery_persona_apply.py)\n"
            "    echo rollback-staged >> \"$LOG\"\n"
            "    echo 'ROLLBACK_STAGED_STATUS=rolled-back RESTORED=1 ROLLBACK_ERRORS=[]'\n"
            "    exit 0 ;;\n"
            "  *) exec /usr/bin/python3 \"$@\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fakebin / "systemctl").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
            "echo systemctl:$* >> \"$LOG\"\n"
            "if [[ \"$*\" == \"--user show pantheon-runtime-tmux.scope -p ActiveState --value\" ]]; then echo inactive; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active --quiet codex-telegram-poller.service\" ]]; then exit 3; fi\n"
            "if [[ \"$*\" == \"--user stop pantheon-persona-consume@orchestrator.timer\" || \"$*\" == \"--user stop pantheon-persona-consume@orchestrator.service\" ]]; then echo 'arbitrary stop failure text' >&2; exit 5; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.service -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
            "if [[ \"$*\" == \"--user restart claude-telegram.service\" ]]; then touch \"$TMUX_UP\"; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active claude-telegram.service\" ]]; then echo active; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-enabled claude-telegram.service\" ]]; then echo enabled; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "tmux").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            f"TMUX_UP={shlex.quote(str(sandbox / 'tmux-up'))}\n"
            "echo tmux:$* >> \"$LOG\"\n"
            "if [[ \"${1:-}\" == \"kill-session\" ]]; then rm -f \"$TMUX_UP\"; exit 0; fi\n"
            "if [[ \"${1:-}\" == \"has-session\" ]]; then [[ -f \"$TMUX_UP\" ]] && exit 0 || exit 1; fi\n"
            "if [[ \"${1:-}\" == \"-N\" ]]; then exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        for executable in fakebin.iterdir():
            executable.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
        proc = subprocess.run(
            ["bash", str(script), "--persona", "orchestrator", "--stamp", "stamp-r29", "--unit-file", str(sandbox / "unit.service"), "--execute"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        events = log.read_text(encoding="utf-8").splitlines()
        self.assertLess(
            events.index("systemctl:--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState"),
            events.index("systemctl:--user restart claude-telegram.service"),
        )

    def test_persona_rollback_shell_blocks_predecessor_restart_until_consume_link_absent_and_units_dead(self) -> None:
        script = REPO_ROOT / "tools" / "persona_rollback.sh"
        sandbox = self.root / "rollback-r29-link-present"
        fakebin = sandbox / "bin"
        home = sandbox / "home" / "claude"
        runtime = sandbox / "run"
        log = sandbox / "events.log"
        fakebin.mkdir(parents=True)
        runtime.mkdir(parents=True)
        link = home / ".config" / "systemd" / "user" / "timers.target.wants" / "pantheon-persona-consume@orchestrator.timer"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(str(home / ".config" / "systemd" / "user" / "pantheon-persona-consume@.timer"))
        for path in (
            home / ".config" / "systemd" / "user" / "claude-telegram.service",
            home / ".local" / "bin" / "orchestrator-bot-supervise.sh",
            home / ".claude" / "settings.json",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("live\n", encoding="utf-8")
            Path(f"{path}.pre-cutover-stamp-r29").write_text("backup\n", encoding="utf-8")
        helper = home / "pantheon-config" / "tools" / "t2611_recovery_persona_apply.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        (fakebin / "python3").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            "case \"$1\" in\n"
            "  *persona_cutover_resolve.py)\n"
            "    cat <<'ENV'\n"
            "PCX_PERSONA=orchestrator\n"
            "PCX_ROLE=orchestrator\n"
            "PCX_ACCOUNT=claude\n"
            f"PCX_UID={os.geteuid()}\n"
            f"PCX_HOME={shlex.quote(str(home))}\n"
            f"PCX_XDG_RUNTIME_DIR={shlex.quote(str(runtime))}\n"
            "PCX_DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/test-bus\n"
            "PCX_TMUX_SESSION=claude-telegram-bot\n"
            "PCX_SERVICE=claude-telegram.service\n"
            f"PCX_SERVICE_UNIT_PATH={shlex.quote(str(home / '.config' / 'systemd' / 'user' / 'claude-telegram.service'))}\n"
            f"PCX_SETTINGS_JSON_PATH={shlex.quote(str(home / '.claude' / 'settings.json'))}\n"
            f"PCX_SUPERVISE_WRAPPER_PATH={shlex.quote(str(home / '.local' / 'bin' / 'orchestrator-bot-supervise.sh'))}\n"
            "PCX_CONSUME_TIMER=pantheon-persona-consume@orchestrator.timer\n"
            "PCX_CONSUME_SERVICE=pantheon-persona-consume@orchestrator.service\n"
            f"PCX_APPLY_PATH={shlex.quote(str(fakebin))}:/usr/bin\n"
            "PCX_COMPANION_UNITS=\n"
            "ENV\n"
            "    exit 0 ;;\n"
            "  *t2611_recovery_persona_apply.py)\n"
            "    echo rollback-staged >> \"$LOG\"\n"
            "    echo 'ROLLBACK_STAGED_STATUS=rolled-back RESTORED=1 ROLLBACK_ERRORS=[]'\n"
            "    exit 0 ;;\n"
            "  *) exec /usr/bin/python3 \"$@\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fakebin / "systemctl").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            "echo systemctl:$* >> \"$LOG\"\n"
            "if [[ \"$*\" == \"--user show pantheon-runtime-tmux.scope -p ActiveState --value\" ]]; then echo inactive; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active --quiet codex-telegram-poller.service\" ]]; then exit 3; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.timer -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
            "if [[ \"$*\" == \"--user show pantheon-persona-consume@orchestrator.service -p LoadState -p UnitFileState -p ActiveState -p SubState\" ]]; then printf 'LoadState=not-found\\nUnitFileState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-active claude-telegram.service\" ]]; then echo active; exit 0; fi\n"
            "if [[ \"$*\" == \"--user is-enabled claude-telegram.service\" ]]; then echo enabled; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "tmux").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"LOG={shlex.quote(str(log))}\n"
            "echo tmux:$* >> \"$LOG\"\n"
            "if [[ \"${1:-}\" == \"kill-session\" || \"${1:-}\" == \"has-session\" ]]; then exit 1; fi\n"
            "if [[ \"${1:-}\" == \"-N\" ]]; then exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        for executable in fakebin.iterdir():
            executable.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
        proc = subprocess.run(
            ["bash", str(script), "--persona", "orchestrator", "--stamp", "stamp-r29", "--unit-file", str(sandbox / "unit.service"), "--execute"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        output = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, output)
        events = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("rollback-staged", events)
        self.assertNotIn("systemctl:--user enable claude-telegram.service", events)
        self.assertNotIn("systemctl:--user restart claude-telegram.service", events)
        self.assertTrue(link.is_symlink())

    @_with_codex_config_fixture
    def test_orchestrator_same_stamp_rollback_after_acceptance_config_mutations_restores_prior_fingerprint(self) -> None:
        from tools.bringup_acceptance import harness_codex_ready as hcr
        from tools.harness_adapter.codex import ensure_project_trusted, ensure_update_check_disabled
        from tools.harness_adapter.types import TmuxTarget

        root, plan, backend, _summary = self.orchestrator_stage_disabled(
            "r25-acceptance-config", "stamp-r25-acceptance-config",
        )
        mcp = self.operation(plan, "codex-telegram.mcp-registration")
        config_path = Path(mcp.target)
        staged_fingerprint = _snapshot_fingerprint(_snapshot(config_path))
        target = TmuxTarget(session="s", window="w", pane="0")
        work_root = root / "home" / "claude" / ".local" / "state" / "pantheon" / "t2953-codex-ready"

        class FakeAdapter:
            def build_interactive_launch(self, _target: TmuxTarget, *, cwd: Path) -> object:
                return SimpleNamespace(cwd=work_root, argv=("/bin/true",), env={})

        fake_binding = SimpleNamespace(
            controller=SimpleNamespace(tmux=target),
            account_home=root / "home" / "claude",
            persona="orchestrator",
            launch=SimpleNamespace(harness="codex"),
            launch_cwd=work_root,
        )

        def mutate_startup(_binding, _adapter, *, replace: bool, context: object) -> rc.RuntimeStart:
            del replace, context
            ensure_project_trusted(config_path.parent, work_root)
            ensure_update_check_disabled(config_path.parent)
            return rc.RuntimeStart(scope_unit="fake.scope", created_scope=True)

        with patch.object(hcr.persona_bringup, "load_binding", return_value=fake_binding), \
             patch.object(hcr.persona_bringup, "build_adapter", return_value=FakeAdapter()), \
             patch.object(rc, "_start_runtime", side_effect=mutate_startup), \
             patch.object(rc, "_verify_started_runtime", return_value=None), \
             patch.object(hcr, "_wait_for_codex_ready", return_value="╭───╮\n›\n"), \
             patch.object(hcr, "_teardown_disposable_tmux_context", return_value=[]):
            hcr.run_codex_ready_bringup(work_root=work_root, account="claude")

        self.assertEqual(_snapshot_fingerprint(_snapshot(config_path)), staged_fingerprint)
        rollback_code, rollback_summary = persona_apply.rollback_staged(
            "orchestrator",
            "stamp-r25-acceptance-config",
            root / "home" / "claude",
            backend=backend,
        )
        self.assertEqual(rollback_code, 0, rollback_summary)

    @_with_codex_config_fixture
    def test_installer_rolls_back_policy_expanded_mcp_config_to_prior_fingerprint(self) -> None:
        _root, plan, _backend = self.orchestrator_local_stage_fixture("mcp-expanded-rollback")
        operation = self.operation(plan, "codex-telegram.mcp-registration")
        self.assertIn("[mcp_servers.telegram.tools.reply]", operation.desired["expected"])
        backend = SimulatedHostBackend()
        prior = '[settings]\napi_key = "possible-secret"\n'
        backend.seed_file(operation.target, prior)
        prior_fingerprint = backend.entries[operation.target]["fingerprint"]

        mutation, _observed = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(mutation.changed)
        self.assertNotEqual(backend.entries[operation.target]["fingerprint"], prior_fingerprint)
        self.assertTrue(backend.can_rollback(mutation).ok)
        backend.rollback_mutation(mutation)

        self.assertEqual(backend.entries[operation.target]["fingerprint"], prior_fingerprint)
        restored = base64.b64decode(backend.entries[operation.target]["content_b64"]).decode("utf-8")
        self.assertEqual(restored, prior)

    @_with_codex_config_fixture
    def test_localhost_stage_preflight_allows_fresh_absent_standing_instruction_target(self) -> None:
        _, plan, backend = self.orchestrator_local_stage_fixture("localhost-stage-fresh")
        target_check = self.operation(plan, "codex-standing-instructions.target-check")
        self.assertFalse(Path(target_check.target).exists())
        preflight = backend.preflight(target_check)
        self.assertTrue(preflight.ok, preflight)
        self.assertEqual(preflight.classification, "active")
        report = self.installer.stage_disabled(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertIn(report.status, {"applied", "noop"}, report.errors)
        self.assertTrue(Path(target_check.target).is_file())
        verified = backend.verify_operation(target_check)
        self.assertTrue(verified.ok, verified)
        self.assertNotIn("connective.unit-fire", backend.events)

    @_with_codex_config_fixture
    def test_localhost_verify_rejects_deleted_or_drifted_standing_instruction_target_after_stage(self) -> None:
        for mode in ("delete", "drift"):
            with self.subTest(mode=mode):
                root, plan, backend = self.orchestrator_local_stage_fixture(f"localhost-stage-{mode}")
                stamp = f"stamp-r6-localhost-{mode}"
                exit_code, summary = persona_apply.stage_disabled(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    stamp,
                    backend=backend,
                    root=root,
                )
                self.assertEqual(exit_code, 0, summary)
                target_check = self.operation(plan, "codex-standing-instructions.target-check")
                target = Path(target_check.target)
                self.assertTrue(target.is_file())
                if mode == "delete":
                    target.unlink()
                else:
                    target.write_text(
                        f"# {MANAGED_MARKER}\ndrifted standing instruction target\n",
                        encoding="utf-8",
                    )
                verified = backend.verify_operation(target_check)
                self.assertFalse(verified.ok, verified)
                suffix_ids = {
                    operation.operation_id
                    for operation in plan.operations[self.installer._activation_index(plan) :]
                }
                suffix_before = set(backend.events) & suffix_ids
                ok, message = persona_apply.check_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    stamp,
                    backend=backend,
                    root=root,
                )
                self.assertFalse(ok, message)
                self.assertIn("codex-standing-instructions.target-check: staged invariant failed", message)
                activate_code, activate_summary = persona_apply.activate_staged(
                    REPO_ROOT / "instance.yaml",
                    "orchestrator",
                    "claude",
                    REPO_ROOT,
                    stamp,
                    backend=backend,
                    root=root,
                )
                self.assertNotEqual(activate_code, 0, activate_summary)
                self.assertIn(
                    "codex-standing-instructions.target-check: staged invariant failed",
                    activate_summary,
                )
                self.assertEqual(suffix_before, set(backend.events) & suffix_ids)

    def test_second_persona_disposable_codex_seat_has_no_persona_branch(self) -> None:
        recovery = self.plan("recovery", "codex")
        cairn = self.plan("cairn")
        self.assertEqual(cairn.selected_harness, "codex")
        self.assertEqual(cairn.account, "codex")
        recovery_service = self.operation(recovery, "connective.service-unit").desired["content"]
        cairn_service = self.operation(cairn, "connective.service-unit").desired["content"]
        recovery_body = "\n".join(recovery_service.splitlines()[2:])
        cairn_body = "\n".join(cairn_service.splitlines()[2:])
        self.assertEqual(recovery_body, cairn_body)
        self.assertNotIn("recovery", cairn_body.lower())

    def test_codex_telegram_mcp_preflight_accepts_additive_tools_policy(self) -> None:
        operation = self.codex_telegram_mcp_operation()
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, self.additive_mcp_registration(operation), "0600")
        result = backend.preflight(operation)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "matching")

    def test_codex_telegram_mcp_simulated_apply_noops_on_additive_registration(self) -> None:
        operation = self.codex_telegram_mcp_operation()
        backend = SimulatedHostBackend()
        original = self.additive_mcp_registration(operation)
        backend.seed_file(operation.target, original, "0600")
        before = dict(backend.entries[operation.target])
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(mutation.changed)
        self.assertTrue(evidence.ok)
        self.assertEqual(backend.entries[operation.target], before)

    def test_codex_telegram_mcp_local_apply_noops_on_additive_registration(self) -> None:
        operation = self.codex_telegram_mcp_operation()
        config = self.root / "local-config.toml"
        original = self.additive_mcp_registration(operation)
        config.write_text(original, encoding="utf-8")
        local_operation = dataclasses.replace(operation, target=str(config))
        mutation, evidence = LocalHostBackend().execute(local_operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(mutation.changed)
        self.assertTrue(evidence.ok)
        self.assertEqual(mutation.before["appended_suffix"], "")
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_codex_telegram_mcp_installer_mismatch_rejects_before_mutation(self) -> None:
        operation = self.codex_telegram_mcp_operation()
        bad = self.additive_mcp_registration(operation).replace('command = "python3"', 'command = "wrong"')
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, bad, "0600")
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        before = dict(backend.entries[operation.target])
        with self.assertRaises(OperationFailed):
            backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertEqual(backend.entries[operation.target], before)

        config = self.root / "local-config-bad.toml"
        config.write_text(bad, encoding="utf-8")
        local_operation = dataclasses.replace(operation, target=str(config))
        with self.assertRaises(OperationFailed):
            LocalHostBackend().execute(local_operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertEqual(config.read_text(encoding="utf-8"), bad)

    def test_fixture_only_proof_cannot_satisfy_live_acceptance(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertEqual(applied.status, "applied")
        report = self.installer.verify(plan, backend)
        self.assertEqual(report.status, "verified")
        self.assertEqual(report.evidence_scope, "fixture")
        self.assertFalse(report.live_acceptance)

    def test_local_backend_refuses_cross_account_before_mutation(self) -> None:
        plan = self.installer.plan(self.manifest, "recovery", root=Path("/"))
        backend = LocalHostBackend()
        foreign = SimpleNamespace(pw_uid=999_999)
        with patch("tools.persona_installer.pwd.getpwnam", return_value=foreign), \
             patch("tools.persona_installer.os.geteuid", return_value=1000), \
             patch.object(backend, "_run", side_effect=AssertionError("cross-account refusal must precede systemctl")) as run:
            report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "refused")
        self.assertIn("target account", report.errors[0])
        run.assert_not_called()

    def test_cross_process_plan_is_deterministic_and_secret_free(self) -> None:
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "persona_installer.py"),
            "plan",
            "--manifest",
            str(self.manifest_path),
            "--persona",
            "recovery",
            "--root",
            str(self.root / "projection"),
        ]
        first = subprocess.run(command, check=True, capture_output=True, text=True)
        second = subprocess.run(command, check=True, capture_output=True, text=True)
        first_json = json.loads(first.stdout)
        second_json = json.loads(second.stdout)
        self.assertEqual(first_json, second_json)
        rendered = json.dumps(first_json).lower()
        self.assertNotIn('"value"', rendered)
        self.assertNotIn("copy_from", rendered)
        self.assertNotIn("auth_file", rendered)


def _claude_mcp_server_cfg(**overrides):
    cfg = {
        "type": "stdio",
        "command": "python3",
        "args": ["/home/suborch2/pantheon-config/tools/mailbox_mcp.py"],
        "env": {},
    }
    cfg.update(overrides)
    return cfg


class _CapturingClaudeMcpBackend(LocalHostBackend):
    """LocalHostBackend that simulates the `claude mcp` CLI by mutating the
    fixture ~/.claude.json in place, capturing the argv + HOME of each call.
    Lets the write/rollback wiring be exercised without the real CLI."""

    def __init__(self, *, fail_add: bool = False) -> None:
        super().__init__(command_timeout=5)
        self.add_calls: list[tuple[list[str], str]] = []
        self.remove_calls: list[tuple[str, str, str]] = []
        self.fail_add = fail_add

    def _run_claude_mcp(self, args, *, home, phase):  # type: ignore[override]
        self.add_calls.append((list(args), home))
        if self.fail_add:
            from tools.persona_installer import OperationFailed as _OF

            raise _OF("simulated claude mcp add-json failure")
        # args = ["add-json", "--scope", scope, name, json]
        name = args[3]
        payload = json.loads(args[4])
        path = Path(home) / ".claude.json"
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        document.setdefault("mcpServers", {})[name] = payload
        path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(["claude"], 0, "added", "")

    def _run_claude_mcp_remove(self, name, *, home, scope):  # type: ignore[override]
        self.remove_calls.append((name, home, scope))
        path = Path(home) / ".claude.json"
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        servers = document.get("mcpServers", {})
        if isinstance(servers, dict):
            servers.pop(name, None)
        path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(["claude"], 0, "removed", "")


class ClaudeMcpServerCase(unittest.TestCase):
    """T-3559: Claude-Code-side (user-scope) MCP server provisioning."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.installer = PersonaInstaller(REPO_ROOT)

    def tearDown(self) -> None:
        self.temp.cleanup()

    # --- helpers -----------------------------------------------------------

    def _write_manifest(self, block) -> Manifest:
        data = _manifest_dict()
        data["roles"]["sub-orchestrator-2"] = {
            "persona": "atlas",
            "account": "suborch2",
            "harness": "claude-code",
        }
        if block is not None:
            data["persona_installer"]["claude_mcp_servers"] = block
        self.manifest_path.write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        return Manifest.load(self.manifest_path)

    def _atlas_block(self, server=None):
        return {
            "schema": CLAUDE_MCP_SERVERS_SCHEMA,
            "personas": {
                "atlas": {
                    "account": "suborch2",
                    "scope": "user",
                    "servers": {"mailbox": server or _claude_mcp_server_cfg()},
                }
            },
        }

    def _ops(self, server=None):
        manifest = self._write_manifest(self._atlas_block(server))
        plan = self.installer.plan(manifest, "atlas", root=self.root / "proj")
        check = next(
            op for op in plan.operations if op.operation_id == "claude-mcp.check.mailbox"
        )
        register = next(
            op for op in plan.operations if op.operation_id == "claude-mcp.register.mailbox"
        )
        return check, register

    def _seed(self, backend, target, servers):
        backend.seed_file(target, json.dumps({"mcpServers": servers}), mode="0600")

    def _live_op(self, op, home_dir):
        """Rebase a planned op onto a fixture HOME with install_root='/' so the
        LocalHostBackend (which activates only root=/ plans) targets the fixture
        ~/.claude.json rather than the real user config."""
        home_dir.mkdir(parents=True, exist_ok=True)
        config_path = home_dir / ".claude.json"
        return dataclasses.replace(
            op,
            target=str(config_path),
            desired={
                **op.desired,
                "home": str(home_dir),
                "config_path": str(config_path),
            },
            provenance={**op.provenance, "install_root": "/"},
        )

    # --- parser / allowlist (ManifestError at plan time) -------------------

    def test_allowlist_rejects_non_allowlisted_server(self) -> None:
        block = {
            "schema": CLAUDE_MCP_SERVERS_SCHEMA,
            "personas": {
                "atlas": {
                    "account": "suborch2",
                    "scope": "user",
                    "servers": {"rogue-server": _claude_mcp_server_cfg()},
                }
            },
        }
        with self.assertRaises(ManifestError) as ctx:
            parse_claude_mcp_block(block)
        self.assertIn("allowlist", str(ctx.exception))

    def test_allowlist_rejects_persona_not_allowlisted_for_server(self) -> None:
        # pantheon-mcp-codex is allowlisted only for orchestrator, not atlas.
        block = {
            "schema": CLAUDE_MCP_SERVERS_SCHEMA,
            "personas": {
                "atlas": {
                    "account": "suborch2",
                    "scope": "user",
                    "servers": {"pantheon-mcp-codex": _claude_mcp_server_cfg()},
                }
            },
        }
        with self.assertRaises(ManifestError):
            parse_claude_mcp_block(block)

    def test_parser_rejects_inline_secret_env_key(self) -> None:
        block = self._atlas_block(
            _claude_mcp_server_cfg(env={"API_KEY": "sk-leaked"})
        )
        with self.assertRaises(ManifestError) as ctx:
            parse_claude_mcp_block(block)
        self.assertIn("credential", str(ctx.exception))

    def test_parser_rejects_non_user_scope(self) -> None:
        block = self._atlas_block()
        block["personas"]["atlas"]["scope"] = "project"
        with self.assertRaises(ManifestError):
            parse_claude_mcp_block(block)

    def test_parser_rejects_non_stdio_type(self) -> None:
        block = self._atlas_block(_claude_mcp_server_cfg(type="http"))
        with self.assertRaises(ManifestError):
            parse_claude_mcp_block(block)

    def test_parser_rejects_unknown_schema(self) -> None:
        block = self._atlas_block()
        block["schema"] = "wrong/v9"
        with self.assertRaises(ManifestError):
            parse_claude_mcp_block(block)

    def test_manifest_load_rejects_bad_block(self) -> None:
        with self.assertRaises(ManifestError):
            self._write_manifest(
                {
                    "schema": CLAUDE_MCP_SERVERS_SCHEMA,
                    "personas": {
                        "atlas": {
                            "account": "suborch2",
                            "scope": "user",
                            "servers": {"rogue": _claude_mcp_server_cfg()},
                        }
                    },
                }
            )

    def test_account_mismatch_raises_at_plan_time(self) -> None:
        block = self._atlas_block()
        block["personas"]["atlas"]["account"] = "wrong-account"
        manifest = self._write_manifest(block)
        with self.assertRaises(ManifestError) as ctx:
            self.installer.plan(manifest, "atlas", root=self.root / "proj")
        self.assertIn("does not match bound account", str(ctx.exception))

    def test_real_instance_manifest_parses(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        parsed = parse_claude_mcp_block(manifest.installer["claude_mcp_servers"])
        self.assertEqual(parsed["orchestrator"]["account"], "claude")
        self.assertIn("pantheon-mcp-codex", parsed["orchestrator"]["servers"])
        self.assertEqual(sorted(parsed["atlas"]["servers"]), ["mailbox"])

    # --- check op (read-only audit) ---------------------------------------

    def test_check_op_classifies_absent(self) -> None:
        check, _ = self._ops()
        backend = SimulatedHostBackend()
        result = backend.preflight(check)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "absent")

    def test_check_op_classifies_matching(self) -> None:
        check, _ = self._ops()
        backend = SimulatedHostBackend()
        self._seed(backend, check.target, {"mailbox": _claude_mcp_server_cfg()})
        result = backend.preflight(check)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "matching")

    def test_check_op_classifies_conflicting(self) -> None:
        check, _ = self._ops()
        backend = SimulatedHostBackend()
        self._seed(
            backend,
            check.target,
            {"mailbox": _claude_mcp_server_cfg(command="/usr/bin/evil")},
        )
        result = backend.preflight(check)
        # Read-only audit stays available; the drift rides the classification.
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "conflicting")

    def test_check_op_never_writes(self) -> None:
        check, _ = self._ops()
        backend = SimulatedHostBackend()
        before_entries = dict(backend.entries)
        mutation, _ = backend.execute(check, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(mutation.changed)
        self.assertEqual(backend.entries, before_entries)

    # --- registration op (write via CLI) ----------------------------------

    def test_register_backfills_empty_surface(self) -> None:
        _, register = self._ops()
        backend = SimulatedHostBackend()
        mutation, evidence = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(mutation.changed)
        self.assertTrue(evidence.ok)
        servers = json.loads(
            base64.b64decode(backend.entries[register.target]["content_b64"])
        )["mcpServers"]
        self.assertIn("mailbox", servers)

    def test_register_is_idempotent(self) -> None:
        _, register = self._ops()
        backend = SimulatedHostBackend()
        first, _ = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(first.changed)
        second, _ = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(second.changed)

    def test_register_fails_closed_on_conflict(self) -> None:
        _, register = self._ops()
        backend = SimulatedHostBackend()
        self._seed(
            backend,
            register.target,
            {"mailbox": _claude_mcp_server_cfg(command="/usr/bin/evil")},
        )
        with self.assertRaises(OperationFailed):
            backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)

    def test_register_preflight_refuses_conflict(self) -> None:
        _, register = self._ops()
        backend = SimulatedHostBackend()
        self._seed(
            backend,
            register.target,
            {"mailbox": _claude_mcp_server_cfg(command="/usr/bin/evil")},
        )
        result = backend.preflight(register)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")

    def test_register_rollback_removes_added_server(self) -> None:
        _, register = self._ops()
        backend = SimulatedHostBackend()
        mutation, _ = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(mutation.changed)
        can = backend.can_rollback(mutation)
        self.assertTrue(can.ok)
        backend.rollback_mutation(mutation)
        servers = json.loads(
            base64.b64decode(backend.entries[register.target]["content_b64"])
        ).get("mcpServers", {})
        self.assertNotIn("mailbox", servers)

    # --- live backend (real ~/.claude.json + shelled-out CLI) --------------

    def test_live_check_reads_fixture_home(self) -> None:
        check, _ = self._ops()
        check = self._live_op(check, self.root / "livehome")
        Path(check.target).write_text(
            json.dumps({"mcpServers": {"mailbox": _claude_mcp_server_cfg()}}),
            encoding="utf-8",
        )
        backend = LocalHostBackend(command_timeout=5)
        result = backend.preflight(check)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "matching")

    def test_live_register_shells_out_add_json_with_home(self) -> None:
        _, register = self._ops()
        register = self._live_op(register, self.root / "livehome")
        target = Path(register.target)
        target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        backend = _CapturingClaudeMcpBackend()
        mutation, evidence = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(mutation.changed)
        self.assertTrue(evidence.ok)
        self.assertEqual(len(backend.add_calls), 1)
        args, home = backend.add_calls[0]
        self.assertEqual(args[0], "add-json")
        self.assertIn("--scope", args)
        self.assertEqual(args[args.index("--scope") + 1], "user")
        self.assertEqual(args[3], "mailbox")
        self.assertEqual(home, register.desired["home"])

    def test_live_register_idempotent_no_shellout(self) -> None:
        _, register = self._ops()
        register = self._live_op(register, self.root / "livehome")
        Path(register.target).write_text(
            json.dumps({"mcpServers": {"mailbox": _claude_mcp_server_cfg()}}),
            encoding="utf-8",
        )
        backend = _CapturingClaudeMcpBackend()
        mutation, _ = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(mutation.changed)
        self.assertEqual(backend.add_calls, [])

    def test_live_register_conflict_no_shellout(self) -> None:
        _, register = self._ops()
        register = self._live_op(register, self.root / "livehome")
        Path(register.target).write_text(
            json.dumps(
                {"mcpServers": {"mailbox": _claude_mcp_server_cfg(command="/evil")}}
            ),
            encoding="utf-8",
        )
        backend = _CapturingClaudeMcpBackend()
        with self.assertRaises(OperationFailed):
            backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertEqual(backend.add_calls, [])

    def test_live_register_rollback_shells_out_remove(self) -> None:
        _, register = self._ops()
        register = self._live_op(register, self.root / "livehome")
        target = Path(register.target)
        target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        backend = _CapturingClaudeMcpBackend()
        mutation, _ = backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)
        backend.rollback_mutation(mutation)
        self.assertEqual(len(backend.remove_calls), 1)
        name, home, scope = backend.remove_calls[0]
        self.assertEqual(name, "mailbox")
        self.assertEqual(scope, "user")
        servers = json.loads(target.read_text(encoding="utf-8")).get("mcpServers", {})
        self.assertNotIn("mailbox", servers)

    def test_live_register_fail_closed_on_add_json_nonzero(self) -> None:
        _, register = self._ops()
        register = self._live_op(register, self.root / "livehome")
        Path(register.target).write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8"
        )
        backend = _CapturingClaudeMcpBackend(fail_add=True)
        with self.assertRaises(OperationFailed):
            backend.execute(register, EXECUTION_CONTEXT_ALL_IN_ONE)


if __name__ == "__main__":
    unittest.main()
