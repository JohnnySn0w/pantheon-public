#!/usr/bin/env python3
"""Run one isolated Claude ping/pong turn to refresh subscription quota data.

The probe is intentionally rare and fail-closed.  A known provider-cap message
in the temporary TUI is authoritative only for the negative result.  A positive
result requires a fresh atomic status-line observation written after the probe
submission.  Pane prose is never used to declare health or completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pwd
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from tools.claude_statusline_capture import SCHEMA as STATUSLINE_SCHEMA


PROBE_SCHEMA = "pantheon-claude-quota-probe/v1"
PROMPT = "Reply with exactly PONG and no other text."
PONG_LINE = re.compile(r"(?m)^[^A-Za-z0-9\r\n]{0,4}PONG\s*$")
READY_PROMPT_LINE = re.compile(
    r"(?m)^[ \t\u00a0]*❯(?:[ \t\u00a0]+[^\r\n]*)?[ \t\u00a0]*$"
)
CAP_SENTINELS = (
    re.compile(
        r"(?mi)^[^A-Za-z0-9\r\n]{0,4}"
        r"(you(?:'|’)ve hit your (?:session|weekly|opus) limit"
        r"\s*·\s*resets\s+\S[^\r\n]*)\s*$"
    ),
)


class ClaudeQuotaProbeError(RuntimeError):
    """The one-turn probe could not establish trustworthy quota evidence."""


@dataclass(frozen=True)
class ProbeResult:
    schema: str
    status: Literal["observed", "capped"]
    run_id: str
    account: str
    persona: str
    session_id: str
    tmux_target: str
    submitted_at: str
    completed_at: str
    prompt_sha256: str
    statusline_path: str
    statusline_sha256: str | None
    cap_sentinel: str | None
    pong_observed: bool


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> Mapping[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _fresh_statusline(
    path: Path,
    *,
    submitted_at: datetime,
    baseline_sha256: str | None = None,
    expected_session_id: str | None = None,
) -> Mapping[str, Any] | None:
    current_sha256 = _sha256_file(path)
    if current_sha256 is None or current_sha256 == baseline_sha256:
        return None
    payload = _read_json(path)
    if payload is None or payload.get("schema") != STATUSLINE_SCHEMA:
        return None
    observed_at = _parse_utc(payload.get("observed_at"))
    if observed_at is None or observed_at < submitted_at:
        return None
    if expected_session_id is not None and payload.get("session_id") != expected_session_id:
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, Mapping) or not rate_limits:
        return None
    return payload


def _cap_sentinel(text: str) -> str | None:
    for pattern in CAP_SENTINELS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _ready_for_submission(text: str) -> bool:
    """Return whether Claude has rendered its interactive input frame."""

    return READY_PROMPT_LINE.search(text) is not None


def probe_session_id(account: str, persona: str, run_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pantheon:{account}:{persona}:{run_id}"))


def probe_tmux_target(persona: str, run_id: str) -> str:
    session = f"pantheon-{persona}-quota-{run_id}"[:96]
    return f"{session}:probe.0"


def probe_result_path(state_root: Path, persona: str, run_id: str) -> Path:
    return (
        state_root.resolve()
        / persona
        / "acceptance"
        / run_id
        / "primary-quota-probe"
        / "result.json"
    )


def read_probe_result(path: Path) -> ProbeResult | None:
    payload = _read_json(path)
    if payload is None or payload.get("schema") != PROBE_SCHEMA:
        return None
    try:
        result = ProbeResult(**payload)
    except (TypeError, ValueError):
        return None
    if result.status not in {"observed", "capped"}:
        return None
    return result


def _atomic_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ClaudeQuotaProbeError("probe evidence path must not use symlinks")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ClaudeQuotaProbeError("could not persist probe evidence") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    command = tuple(argv)
    try:
        return runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeQuotaProbeError(
            f"probe command timed out: {command[0]}"
        ) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise ClaudeQuotaProbeError(
            f"probe command could not run: {command[0]}"
        ) from exc


def _require_identity(account: str) -> Path:
    try:
        record = pwd.getpwnam(account)
    except KeyError as exc:
        raise ClaudeQuotaProbeError(f"unknown Unix account: {account}") from exc
    current = pwd.getpwuid(os.geteuid()).pw_name
    if current != account:
        raise ClaudeQuotaProbeError(
            f"probe must run as {account}; observed effective account {current}"
        )
    return Path(record.pw_dir).resolve()


def run_probe(
    *,
    account: str,
    persona: str,
    executable: Path,
    repo_root: Path,
    state_root: Path,
    run_id: str,
    timeout_seconds: float = 90.0,
    runner: CommandRunner = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    enforce_identity: bool = True,
) -> ProbeResult:
    """Run one scratch interactive turn and return only structural probe evidence."""

    if not re.fullmatch(r"[0-9TZ-]{8,40}", run_id):
        raise ClaudeQuotaProbeError("run_id must be a bounded UTC token")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", persona):
        raise ClaudeQuotaProbeError("persona must be a bounded identifier")
    if not math.isfinite(timeout_seconds) or not 10.0 <= timeout_seconds <= 300.0:
        raise ClaudeQuotaProbeError("timeout must be between 10 and 300 seconds")
    account_home = _require_identity(account) if enforce_identity else Path.home().resolve()
    executable = executable.resolve()
    repo_root = repo_root.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ClaudeQuotaProbeError("configured Claude executable is not executable")
    capture_helper = (repo_root / "tools" / "claude_statusline_capture.py").resolve()
    submit_helper = (repo_root / "tools" / "send_keys_submit.sh").resolve()
    for helper in (capture_helper, submit_helper):
        if not helper.is_file() or not helper.is_relative_to(repo_root):
            raise ClaudeQuotaProbeError(f"probe helper unavailable: {helper.name}")

    resolved_state_root = state_root.resolve()
    if enforce_identity and not resolved_state_root.is_relative_to(account_home):
        raise ClaudeQuotaProbeError("probe state root must be inside the target account home")
    persona_state = resolved_state_root / persona
    statusline_path = persona_state / "quota" / "claude-statusline.json"
    acceptance_dir = persona_state / "acceptance" / run_id / "primary-quota-probe"
    settings_path = acceptance_dir / "settings.json"
    result_path = probe_result_path(state_root, persona, run_id)
    if statusline_path.is_symlink() or acceptance_dir.is_symlink():
        raise ClaudeQuotaProbeError("probe state paths must not use symlinks")
    command = shlex.join(
        (
            "/usr/bin/python3",
            str(capture_helper),
            "--output",
            str(statusline_path),
        )
    )
    _atomic_json(
        settings_path,
        {"statusLine": {"type": "command", "command": command}},
    )
    baseline_statusline_sha256: str | None = None

    session = probe_tmux_target(persona, run_id).split(":", 1)[0]
    tmux_target = probe_tmux_target(persona, run_id)
    expected_session_id = probe_session_id(account, persona, run_id)
    launch = _run(
        runner,
        (
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "probe",
            "-c",
            str(account_home),
            shlex.join(
                (
                    str(executable),
                    "--session-id",
                    expected_session_id,
                    "--settings",
                    str(settings_path),
                )
            ),
        ),
    )
    if launch.returncode != 0:
        detail = launch.stderr.strip() or launch.stdout.strip() or "tmux launch failed"
        raise ClaudeQuotaProbeError(detail)

    submitted_at: datetime | None = None
    cap: str | None = None
    observed: Mapping[str, Any] | None = None
    pong_observed = False
    try:
        startup_deadline = monotonic() + min(20.0, timeout_seconds / 3)
        while monotonic() < startup_deadline:
            capture = _run(
                runner,
                ("tmux", "capture-pane", "-p", "-S", "-120", "-t", tmux_target),
            )
            if capture.returncode == 0:
                cap = _cap_sentinel(capture.stdout)
                if cap:
                    submitted_at = now()
                    break
                if PONG_LINE.search(capture.stdout):
                    raise ClaudeQuotaProbeError(
                        "temporary Claude TUI contains an ambiguous pre-submit PONG"
                    )
                if not _ready_for_submission(capture.stdout):
                    sleep(0.25)
                    continue
                # Claude may render the status line during TUI startup.  Take
                # the baseline only after the pane is ready and immediately
                # before submission so startup bytes cannot satisfy the turn.
                baseline_statusline_sha256 = _sha256_file(statusline_path)
                submission_boundary = now()
                submit = _run(
                    runner,
                    (
                        str(submit_helper),
                        "--harness",
                        "claude-code",
                        tmux_target,
                        PROMPT,
                    ),
                    timeout=30.0,
                )
                if submit.returncode == 0:
                    submitted_at = submission_boundary
                    break
            sleep(0.25)
        if submitted_at is None:
            raise ClaudeQuotaProbeError("temporary Claude TUI never accepted the probe")

        deadline = monotonic() + timeout_seconds
        while cap is None and (observed is None or not pong_observed) and monotonic() < deadline:
            capture = _run(
                runner,
                ("tmux", "capture-pane", "-p", "-S", "-200", "-t", tmux_target),
            )
            if capture.returncode == 0:
                cap = _cap_sentinel(capture.stdout)
                pong_observed = pong_observed or bool(PONG_LINE.search(capture.stdout))
            candidate = _fresh_statusline(
                statusline_path,
                submitted_at=submitted_at,
                baseline_sha256=baseline_statusline_sha256,
                expected_session_id=expected_session_id,
            )
            observed = candidate if pong_observed else None
            if cap is None and observed is None:
                sleep(0.25)
        if cap is None and observed is None:
            raise ClaudeQuotaProbeError(
                "probe timed out without a cap sentinel or fresh structured quota"
            )

        completed_at = now()
        result = ProbeResult(
            schema=PROBE_SCHEMA,
            status="capped" if cap is not None else "observed",
            run_id=run_id,
            account=account,
            persona=persona,
            session_id=expected_session_id,
            tmux_target=tmux_target,
            submitted_at=_iso(submitted_at),
            completed_at=_iso(completed_at),
            prompt_sha256=hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
            statusline_path=str(statusline_path),
            statusline_sha256=_sha256_file(statusline_path) if observed is not None else None,
            cap_sentinel=cap,
            pong_observed=pong_observed,
        )
        _atomic_json(result_path, result.__dict__)
        return result
    finally:
        # Teardown cannot turn a typed fail-closed probe refusal into an
        # unhandled persona-consumer failure or mask its original cause.
        try:
            _run(runner, ("tmux", "kill-session", "-t", session))
        except ClaudeQuotaProbeError:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = run_probe(
            account=args.account,
            persona=args.persona,
            executable=args.executable,
            repo_root=args.repo_root,
            state_root=args.state_root,
            run_id=args.run_id,
            timeout_seconds=args.timeout_seconds,
        )
    except ClaudeQuotaProbeError as exc:
        print(f"REFUSE: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result.__dict__, sort_keys=True))
    return 4 if result.status == "capped" else 0


if __name__ == "__main__":
    raise SystemExit(main())
