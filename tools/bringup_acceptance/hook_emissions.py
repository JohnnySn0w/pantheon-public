"""tools/bringup_acceptance/hook_emissions.py — hook-emissions acceptance module.

T-2556 phase 6 / T-2562. Proves every hook actually WIRED in a target's real
``~/.claude/settings.json`` fires and emits its documented contract, not just
that it is registered. "Wired" is necessary, not sufficient — a hook can be
present in settings.json and silently broken (syntax error, a swapped-in
no-op, a dependency that moved); this module is what tells the difference.

Ground-truth corrections made against the T-2562 brief (D11 — the brief's
hook list was written from memory, this module was built from the actual
deployed ``/home/claude/.claude/settings.json``):

* The SubagentStop hook is ``tools/adaptive_finish_detect.py``, not
  ``adaptive_finish_digest.py`` (the digest script is a separate weekly-timer
  *consumer* of the detector's log; it is never itself hook-wired).
* The real settings.json also wires a third-party ``hook-handler.js``
  (Claude-Code-Agent-Monitor plugin) on every event, and a Pantheon-authored
  ``snapshot-settings.sh`` on PreToolUse that the brief's list omitted.
  ``hook-handler.js`` is treated as OUT-OF-SCOPE-NAMED (mirrors inventory.py's
  own Q4 host-hygiene posture: named, never silently dropped, never a gap —
  there is no Pantheon emission contract to assert against a third-party
  plugin). ``snapshot-settings.sh`` IS proven (wired + Pantheon-authored =
  must-prove, team-lead Q1 ruling) — proving it surfaced a second finding:
  it has NO canonical anywhere in pantheon-config (see
  ``SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING``), unlike every sibling
  hook.

Module granularity. ``AcceptanceModule`` is one ``id``/``tier`` per instance,
but T-2556 wants five module_ids (one per hook EVENT — see
``inventory.EVENT_MODULE_ID``). :func:`build_hook_emissions_modules` returns
one :class:`HookEmissionsModule` per event.

No-sampling / no-rollup. Within one event, EVERY wired hook command is its
own path_id (mirrors ``inventory.discover_hooks()``'s own no-sampling design).
A hook that is wired but does not emit its documented proof under a crafted
synthetic event is a :class:`~tools.bringup_acceptance.types.HardStop` naming
THAT hook specifically — never absorbed into a generic "PreToolUse failed"
message. Two other hooks proving fine in the same event never hides the third
one being silent.

Zero real mutation, by construction — no restore.py Baseline needed. Every
hook this module knows how to prove resolves its persistent state via
``Path.home()`` / ``os.path.expanduser("~")`` / an explicit env-var override
(``PANTHEON_STATE_DIR`` etc. — grepped from each hook's own source, not
assumed). Every synthetic invocation therefore runs with ``HOME`` (and, where
available, the hook's own state-dir env var) redirected to a fresh
``tempfile.mkdtemp()`` scratch tree, so nothing this module does ever reads or
writes the real target account's real HOME, real state dirs, or real shared
docs. ``restore()`` is consequently just "rmtree the scratch tree, assert it's
gone" — there is no real-state category to revert in the first place. The one
exception is ``backlog-surface.sh`` (see below), which is deliberately
NEVER-mutated instead of sandboxed.

``prove(ctx)`` independently re-reads ``<account_home>/.claude/settings.json``
and re-runs the already-landed, pure ``inventory.discover_hooks()`` rather
than trusting the caller's ``TargetInventory`` — per the
``AcceptanceModule.prove`` contract ("re-observes ground truth"). The account
home root has a ``PANTHEON_BRINGUP_ACCOUNT_HOME`` env override so the test
suite never depends on the real, host-specific ``/home/claude/.claude/hooks/*``
deployed copies (unlike the sandboxed-HOME used to *invoke* each hook, which
always uses a fresh disposable tempdir, in production too — this override
only controls where prove() looks for the settings.json it is reading).

``backlog-surface.sh`` (SessionStart) is the one hook this module cannot fully
sandbox: its ``BACKLOG`` path is hardcoded to
``/home/claude/pantheon-config/BACKLOG.md`` (tools/hooks/backlog-surface.sh:15)
— NOT ``$HOME``-relative like every sibling SessionStart hook — so there is no
override seam, and forcing its "has open items" branch off-prod would mean
temporarily overwriting a real, concurrently-shared multi-agent document. This
module proves it read-only/contract-only (exit 0, output shape) and surfaces
the hardcoded path as a portability finding rather than either forcing the
mutation or silently skipping the branch (T-2556's own daemon-bringup purpose
depends on this exact hook working for accounts other than "claude").
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.inventory import EVENT_MODULE_ID, discover_hooks
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    HardStop,
    ModuleDeclaration,
    ModuleEvidence,
    MatrixRow,
    RestoreEvidence,
    RunContext,
    Tier,
    TargetInventory,
)


_ACCOUNT_HOME_ENV = "PANTHEON_BRINGUP_ACCOUNT_HOME"

# Third-party / harness-native hooks with no Pantheon emission contract to
# assert against — named, never a gap, mirrors inventory.py's Q4 posture.
_OUT_OF_SCOPE_BASENAMES: dict[str, str] = {
    "hook-handler.js": "third-party plugin (Claude-Code-Agent-Monitor), no Pantheon emission contract",
}

_SCRIPT_EXT_RE = re.compile(r"\.(py|sh|js)$")


# ---------------------------------------------------------------------------
# Ground-truth helpers
# ---------------------------------------------------------------------------


def _account_home(account: str) -> Path:
    """Where to look for ``<account>``'s real settings.json.

    Overridable so the test suite never depends on the real, host-specific
    ``/home/claude/...`` tree. Production (unset override) reads the real
    account home — this is the ground-truth read, distinct from the always-
    sandboxed HOME each hook is *invoked* under (see module docstring).
    """
    override = os.environ.get(_ACCOUNT_HOME_ENV)
    return Path(override) if override else Path(f"/home/{account}")


def _settings_json_path(account: str) -> Path:
    return _account_home(account) / ".claude" / "settings.json"


def _basename_of(command: str) -> str:
    """The script basename a wired command invokes, tolerant of interpreter
    prefixes (``python3 <path>``) and trailing args (``node "<path>" Foo``).

    Picks the first token ending in a known script extension — the leading
    interpreter name (``python3``, ``node``, ``bash``) never matches, and
    trailing bare-word args (``PreToolUse``) never match either.
    """
    tokens = shlex.split(command) if command.strip() else []
    for token in tokens:
        if _SCRIPT_EXT_RE.search(token):
            return Path(token).name
    return Path(tokens[-1]).name if tokens else command


def _sandboxed_env(home: Path, **extra: str) -> dict[str, str]:
    """A copy of the real environment with HOME (and any extra overrides)
    redirected into a disposable scratch tree — never the real account's."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _run_hook(
    command: str, *, stdin: Mapping[str, Any], env: Mapping[str, str], timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    """Invoke a wired hook command exactly as Claude Code does: the literal
    command string through the shell, stdin-JSON in, stdout/exit code out."""
    return subprocess.run(
        command,
        shell=True,
        input=json.dumps(stdin),
        capture_output=True,
        text=True,
        env=dict(env),
        timeout=timeout,
    )


def _nonce(label: str) -> str:
    return f"t2562-probe-{label}-{uuid.uuid4().hex[:8]}"


def _parse_deny_payload(stdout: str, *, hook_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise HardStop(f"{hook_name}: wired but produced no parseable emission: {stdout!r}") from exc
    decision = (payload.get("hookSpecificOutput") or {}).get("permissionDecision")
    if decision != "deny":
        raise HardStop(f"{hook_name}: wired but did not DENY under the crafted trigger: {payload!r}")
    return payload


# ---------------------------------------------------------------------------
# Per-hook provers. Each: (wired command string, a scratch dir dedicated to
# this hook) -> a JSON-safe proof detail, or raises HardStop naming ITSELF
# (never a generic event-level failure).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mcp-list-guard live-poller stand-in (T-4412)
#
# The hook decides "poller live" from /proc/<pid>/cmdline of bot.pid's pid. A
# freshly Popen()ed stand-in is NOT necessarily readable there yet: Popen returns
# as soon as the child's exec has closed the errpipe, which the kernel does BEFORE
# it publishes the new argv (mm->arg_start/arg_end are set late in the ELF load),
# so /proc/<pid>/cmdline reads EMPTY for a window -- measured at up to ~37 ms
# under a pinned-CPU load, and stretched wherever exec-time page-ins stall (a
# disk-bound CI host). If the hook starts inside that window its fail-open
# own_poller_live() reads "", concludes "no live poller", and the hook silently
# ALLOWS: empty stdout, rc 0. That is a harness race, not a hook defect -- so the
# prover waits (bounded) for the same substrings the hook greps for before it
# runs the hook, and any emission that still isn't a deny carries enough state
# (hook stderr, the stand-in's /proc view, bot.pid) to explain itself.
# ---------------------------------------------------------------------------

# The substrings own_poller_live() (tools/hooks/mcp-list-guard.py) greps for in
# the poller's /proc cmdline. Mirrored, not imported: the hook resolves HOME at
# import time and this module never imports hook scripts.
_POLLER_CMDLINE_MARKERS: tuple[str, ...] = ("bun", "server.ts")
_STAND_IN_READY_TIMEOUT_S = 10.0
_STAND_IN_POLL_INTERVAL_S = 0.005
_PROC_STATUS_KEYS = ("Name", "State", "Pid", "PPid")


def _read_proc_cmdline(pid: int) -> str | None:
    """``/proc/<pid>/cmdline`` exactly as own_poller_live() reads it (NULs folded
    to spaces, undecodable bytes replaced); None when unreadable (process gone)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _read_proc_status(pid: int) -> str:
    """The identifying lines of ``/proc/<pid>/status`` (Name/State/Pid/PPid) on one line."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<unreadable: {type(exc).__name__}>"
    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.split(":", 1)[0] in _PROC_STATUS_KEYS]
    return "; ".join(lines)


def _stand_in_snapshot(proc: "subprocess.Popen[bytes]", bot_pid_path: Path) -> str:
    """One-line view of the stand-in and bot.pid as read RIGHT NOW. /proc is read
    before ``poll()`` so a zombie still shows State: Z instead of being reaped away."""
    cmdline = _read_proc_cmdline(proc.pid)
    status = _read_proc_status(proc.pid)
    poll = proc.poll()
    try:
        bot_pid_content: str | None = bot_pid_path.read_text(encoding="utf-8")
    except OSError:
        bot_pid_content = None
    return (
        f"stand_in(pid={proc.pid} poll={poll!r} "
        f"cmdline={'<unreadable>' if cmdline is None else repr(cmdline)} "
        f"status=[{status}] "
        f"bot.pid_exists={bot_pid_path.exists()} bot.pid_content={bot_pid_content!r})"
    )


def _wait_for_stand_in_visible(proc: "subprocess.Popen[bytes]", bot_pid_path: Path) -> float:
    """Block (bounded) until the stand-in's /proc cmdline carries every marker
    own_poller_live() greps for; returns the seconds waited. HardStop -- naming the
    HARNESS, not the hook -- if the stand-in exits or never becomes visible."""
    markers = list(_POLLER_CMDLINE_MARKERS)
    started = time.monotonic()
    deadline = started + _STAND_IN_READY_TIMEOUT_S
    while True:
        cmdline = _read_proc_cmdline(proc.pid)
        if cmdline is not None and all(marker in cmdline for marker in markers):
            return time.monotonic() - started
        waited = time.monotonic() - started
        if proc.poll() is not None:
            raise HardStop(
                f"mcp-list-guard.py prover harness: the live-poller stand-in exited "
                f"(rc={proc.returncode}) after {waited:.3f}s, before its /proc cmdline showed "
                f"{markers}; {_stand_in_snapshot(proc, bot_pid_path)}"
            )
        if time.monotonic() >= deadline:
            raise HardStop(
                f"mcp-list-guard.py prover harness: the live-poller stand-in's /proc cmdline "
                f"never showed {markers} within {waited:.3f}s; {_stand_in_snapshot(proc, bot_pid_path)}"
            )
        time.sleep(_STAND_IN_POLL_INTERVAL_S)


def _emission_diagnostics(
    result: "subprocess.CompletedProcess[str]", *, waited_s: float, before_hook: str, after_hook: str
) -> str:
    """What a non-deny emission needs to explain itself: the hook's rc + stderr
    and the stand-in / bot.pid state at hook start and right after the hook ran."""
    return (
        f"[hook_rc={result.returncode} hook_stderr={result.stderr!r} "
        f"stand_in_visible_after={waited_s:.3f}s; before_hook: {before_hook}; after_hook: {after_hook}]"
    )


def _prove_mcp_list_guard(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    env = _sandboxed_env(home)
    trigger = {"tool_name": "Bash", "tool_input": {"command": "claude -p hi"}}

    # Case A: no live poller -> silent allow (the common, non-blocking path).
    allow_result = _run_hook(command, stdin=trigger, env=env)
    if allow_result.returncode != 0 or allow_result.stdout.strip():
        raise HardStop(
            f"mcp-list-guard.py: expected silent allow with no bot.pid present, got "
            f"exit={allow_result.returncode} stdout={allow_result.stdout!r} "
            f"stderr={allow_result.stderr!r}"
        )

    # Case B: a disposable, self-owned process standing in for a live poller
    # (argv crafted to contain "bun" + "server.ts", the own_poller_live()
    # substrings) -> the deny branch must actually fire and emit.
    bot_pid_path = home / ".claude" / "channels" / "telegram" / "bot.pid"
    bot_pid_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        args=["bun_probe_server.ts", "300"],
        executable="/bin/sleep",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        bot_pid_path.write_text(str(proc.pid), encoding="utf-8")
        # Popen returning does not mean /proc/<pid>/cmdline is populated yet (see
        # the T-4412 note above): wait until the stand-in is what the hook greps for.
        waited_s = _wait_for_stand_in_visible(proc, bot_pid_path)
        before_hook = _stand_in_snapshot(proc, bot_pid_path)
        deny_result = _run_hook(command, stdin=trigger, env=env)
        after_hook = _stand_in_snapshot(proc, bot_pid_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    diagnostics = _emission_diagnostics(
        deny_result, waited_s=waited_s, before_hook=before_hook, after_hook=after_hook
    )
    if deny_result.returncode != 0:
        raise HardStop(
            f"mcp-list-guard.py: exit {deny_result.returncode} on the live-poller case {diagnostics}"
        )
    try:
        payload = _parse_deny_payload(deny_result.stdout, hook_name="mcp-list-guard.py")
    except HardStop as exc:
        raise HardStop(f"{exc} {diagnostics}") from exc
    return {"allow_case": "silent", "deny_case": payload}


def _prove_recall_feedback(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    memory_dir = scratch / "memory"
    memory_dir.mkdir(parents=True)
    nonce = _nonce("recall")
    (memory_dir / f"{nonce}.md").write_text(
        f"---\nname: {nonce}\ndescription: probe memory for {nonce}.py — "
        f"hook-emissions synthetic fixture (T-2562)\nmetadata:\n  type: feedback\n---\n\n"
        "synthetic fixture, not a real memory.\n",
        encoding="utf-8",
    )
    env = _sandboxed_env(home, RECALL_MEMORY_DIR=str(memory_dir))
    result = _run_hook(
        command,
        stdin={
            "tool_name": "Edit",
            "tool_input": {"file_path": f"/tmp/{nonce}.py"},
            "session_id": f"hook-emissions-{nonce}",
        },
        env=env,
    )
    if result.returncode != 0:
        raise HardStop(f"recall-feedback.py: unexpected exit {result.returncode}: {result.stderr!r}")
    if nonce not in result.stdout:
        raise HardStop(
            f"recall-feedback.py: wired but did not surface the nonce fixture memory: {result.stdout!r}"
        )
    return {"nonce": nonce, "stdout": result.stdout}


def _prove_worker_guard(command: str, scratch: Path) -> Mapping[str, Any]:
    env = _sandboxed_env(scratch / "home")
    trigger_input = {"command": "cat ~/.claude/channels/telegram/.env"}

    deny_result = _run_hook(
        command,
        stdin={"agent_type": "implementer", "tool_name": "Bash", "tool_input": trigger_input},
        env=env,
    )
    if deny_result.returncode != 0:
        raise HardStop(f"worker-guard.py: unexpected exit {deny_result.returncode}")
    payload = _parse_deny_payload(deny_result.stdout, hook_name="worker-guard.py")

    # Main-thread exemption: agent_type absent -> silent allow, same trigger.
    allow_result = _run_hook(command, stdin={"tool_name": "Bash", "tool_input": trigger_input}, env=env)
    if allow_result.returncode != 0 or allow_result.stdout.strip():
        raise HardStop(
            f"worker-guard.py: expected silent allow with agent_type absent (HO exemption), got "
            f"exit={allow_result.returncode} stdout={allow_result.stdout!r}"
        )
    return {"deny_case": payload, "allow_case": "silent"}


def _prove_background_pipe_gate(command: str, scratch: Path) -> Mapping[str, Any]:
    env = _sandboxed_env(scratch / "home")
    result = _run_hook(
        command,
        stdin={
            "tool_name": "Bash",
            "tool_input": {"command": "long_running_thing 2>&1 | tail -20", "run_in_background": True},
        },
        env=env,
    )
    if result.returncode != 0:
        raise HardStop(f"background-pipe-gate.py: unexpected exit {result.returncode}")
    payload = _parse_deny_payload(result.stdout, hook_name="background-pipe-gate.py")
    return {"deny_case": payload}


# Second D11 discovery (distinct from the BACKLOG_SURFACE_PORTABILITY_FINDING
# below): unlike every sibling hook, snapshot-settings.sh has NO canonical in
# pantheon-config at all — no git history, no tools/hooks/ entry. It exists
# only as a deployed file at the host-specific ~/.claude/hooks/snapshot-
# settings.sh (verify-hook-integrity.sh's own comment already flags it as
# "local-only, not content-checkable"). It IS wired and Pantheon-authored, so
# per team-lead's Q1 ruling it's still proven here — but it has no audit
# trail or recoverability-from-clone the way every other hook does.
SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING = (
    "tools/hooks/snapshot-settings.sh has NO canonical in pantheon-config (no git "
    "history, not present in tools/hooks/) — it exists only as a deployed file at "
    "~/.claude/hooks/snapshot-settings.sh, unlike every sibling hook. verify-hook-"
    "integrity.sh's own comment already flags it as 'local-only, not content-"
    "checkable'; this also means it has no audit trail / recoverability if lost."
)


def _prove_snapshot_settings(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    nonce = _nonce("snapshot")
    original = json.dumps({"nonce": nonce})
    target.write_text(original, encoding="utf-8")
    env = _sandboxed_env(home)
    result = _run_hook(command, stdin={"tool_input": {"file_path": str(target)}}, env=env)
    if result.returncode != 0:
        raise HardStop(f"snapshot-settings.sh: unexpected exit {result.returncode}")
    backups = sorted(target.parent.glob(f"{target.name}.bak.*"))
    if not backups:
        raise HardStop(
            "snapshot-settings.sh: wired but produced no .bak snapshot of the touched settings.json"
        )
    if backups[-1].read_text(encoding="utf-8") != original:
        raise HardStop("snapshot-settings.sh: backup content did not match the original file bytes")
    return {
        "nonce": nonce,
        "backup": str(backups[-1]),
        "finding": SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING,
    }


def _prove_telegram_reply_gate(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    nonce = _nonce("telegram")
    transcript_path = scratch / "transcript.jsonl"
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": f'<channel source="telegram" chat_id="{nonce}">hello</channel>'}
            ],
        },
    }
    transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    env = _sandboxed_env(home)
    result = _run_hook(
        command,
        stdin={"session_id": f"hook-emissions-{nonce}", "transcript_path": str(transcript_path)},
        env=env,
    )
    if result.returncode != 0:
        raise HardStop(f"telegram-reply-gate.py: unexpected exit {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise HardStop(
            f"telegram-reply-gate.py: wired but did not block an unanswered telegram turn: {result.stdout!r}"
        ) from exc
    if payload.get("decision") != "block":
        raise HardStop(f"telegram-reply-gate.py: unexpected payload: {payload!r}")
    return {"nonce": nonce, "payload": payload}


def _prove_ho_stop_failure(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    state_dir = scratch / "state"
    mirror_path = scratch / "mirror.json"
    nonce_session = _nonce("stopfailure")
    env = _sandboxed_env(
        home,
        PANTHEON_STATE_DIR=str(state_dir),
        PANTHEON_TMP_MIRROR_OVERRIDE=str(mirror_path),
        PANTHEON_WRITTEN_AT_OVERRIDE="1735000000",
    )
    result = _run_hook(
        command,
        stdin={
            "error_type": "rate_limit",
            "error_message": "resets 3:45pm",
            "session_id": nonce_session,
            "transcript_path": f"/tmp/{nonce_session}.jsonl",
        },
        env=env,
    )
    if result.returncode != 0:
        raise HardStop(f"ho_stop_failure.py: unexpected exit {result.returncode}: {result.stderr!r}")
    state_path = state_dir / "ho_limit_hit.json"
    if not state_path.is_file():
        raise HardStop(f"ho_stop_failure.py: wired but wrote no state file at {state_path}")
    written = json.loads(state_path.read_text(encoding="utf-8"))
    if written.get("session_id") != nonce_session:
        raise HardStop(f"ho_stop_failure.py: state file session_id did not correlate: {written!r}")
    return {"nonce_session": nonce_session, "state": written}


def _prove_adaptive_finish_detect(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    state_dir = scratch / "state"
    transcript_path = scratch / "transcript.jsonl"
    nonce = _nonce("subagentstop")
    record = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": f"Investigating {nonce}. Let me"}]},
    }
    transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    env = _sandboxed_env(home, PANTHEON_STATE_DIR=str(state_dir))
    result = _run_hook(
        command,
        stdin={"agent_type": "implementer", "agent_transcript_path": str(transcript_path)},
        env=env,
    )
    if result.returncode != 0:
        raise HardStop(f"adaptive_finish_detect.py: unexpected exit {result.returncode}: {result.stderr!r}")
    log_path = state_dir / "adaptive_finish_log.jsonl"
    if not log_path.is_file():
        raise HardStop(
            f"adaptive_finish_detect.py: wired but logged no hit for a narrate-then-exit turn at {log_path}"
        )
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not any(nonce in entry.get("message_excerpt", "") for entry in lines):
        raise HardStop(f"adaptive_finish_detect.py: log present but no hit correlates to the nonce turn: {lines!r}")
    return {"nonce": nonce, "log_lines": lines}


def _prove_handoff_auto_read(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    readme = home / "pantheon-config" / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    nonce = _nonce("handoff")
    readme.write_text(f"## Read order\n{nonce}\n## Repository convention\nmore\n", encoding="utf-8")
    result = _run_hook(command, stdin={}, env=_sandboxed_env(home))
    if result.returncode != 0:
        raise HardStop(f"handoff-auto-read.sh: unexpected exit {result.returncode}")
    if nonce not in result.stdout:
        raise HardStop(
            f"handoff-auto-read.sh: wired but did not inline the README fixture content: {result.stdout!r}"
        )
    return {"nonce": nonce}


def _prove_verify_hook_integrity(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    canon_dir = home / "pantheon-config" / "tools" / "hooks"
    deployed_dir = home / ".claude" / "hooks"
    canon_dir.mkdir(parents=True, exist_ok=True)
    deployed_dir.mkdir(parents=True, exist_ok=True)
    nonce = _nonce("integrity")
    (canon_dir / "foo.sh").write_text("echo canonical\n", encoding="utf-8")
    (deployed_dir / "foo.sh").write_text(f"echo tampered-{nonce}\n", encoding="utf-8")
    result = _run_hook(command, stdin={}, env=_sandboxed_env(home))
    if result.returncode != 0:
        raise HardStop(f"verify-hook-integrity.sh: unexpected exit {result.returncode}")
    if "DIFFER from canonical" not in result.stdout or "foo.sh" not in result.stdout:
        raise HardStop(
            f"verify-hook-integrity.sh: wired but did not report drift on a tampered deployed hook: "
            f"{result.stdout!r}"
        )
    return {"nonce": nonce}


def _prove_ensure_hooks_active(command: str, scratch: Path) -> Mapping[str, Any]:
    home = scratch / "home"
    repo = home / "probe-repo"
    (repo / ".githooks").mkdir(parents=True, exist_ok=True)
    (repo / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    init = subprocess.run(
        ["git", "init", "-q", str(repo)], capture_output=True, text=True, timeout=10
    )
    if init.returncode != 0:
        raise HardStop(f"ensure-hooks-active.sh: could not stage the probe repo: {init.stderr!r}")
    result = _run_hook(command, stdin={}, env=_sandboxed_env(home))
    if result.returncode != 0:
        raise HardStop(f"ensure-hooks-active.sh: unexpected exit {result.returncode}")
    if "auto-repaired" not in result.stdout or "probe-repo" not in result.stdout:
        raise HardStop(
            f"ensure-hooks-active.sh: wired but did not report an auto-repair for the probe repo: "
            f"{result.stdout!r}"
        )
    check = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "core.hooksPath"],
        capture_output=True, text=True, timeout=10,
    )
    if check.stdout.strip() != ".githooks":
        raise HardStop(
            f"ensure-hooks-active.sh: reported an auto-repair but core.hooksPath is "
            f"{check.stdout!r}, not '.githooks'"
        )
    return {"repo": str(repo)}


# Discovered portability gap (Q3, surfaced to team-lead for a follow-up
# ticket, NOT fixed here — out of this module's file scope).
BACKLOG_SURFACE_PORTABILITY_FINDING = (
    "tools/hooks/backlog-surface.sh:15 hardcodes "
    "BACKLOG=/home/claude/pantheon-config/BACKLOG.md — NOT $HOME-relative like every "
    "sibling SessionStart hook (handoff-auto-read.sh, verify-hook-integrity.sh, "
    "ensure-hooks-active.sh all resolve via $HOME). A new daemon bringing up on a "
    "different account gets this hook silently reading Orchestrator's BACKLOG.md (or nothing, "
    "if unreadable) instead of its own. Genuine portability finding, not a test gap "
    "(already ticketed, not fixed here — see T-2581)."
)

_BACKLOG_LINE_RE = re.compile(r"^Open BACKLOG items: B\d+(,B\d+)*$")


def _prove_backlog_surface(command: str, scratch: Path) -> Mapping[str, Any]:
    # Deliberately NOT sandboxed — see BACKLOG_SURFACE_PORTABILITY_FINDING.
    # The wired path is hardcoded to a real, concurrently-shared doc; this
    # module proves the fail-open CONTRACT against real ground truth without
    # forcing (and therefore risking corrupting) either branch.
    result = _run_hook(command, stdin={}, env=dict(os.environ))
    if result.returncode != 0:
        raise HardStop(f"backlog-surface.sh: unexpected exit {result.returncode}: {result.stderr!r}")
    stdout = result.stdout.strip("\n")
    if stdout and not _BACKLOG_LINE_RE.match(stdout):
        raise HardStop(f"backlog-surface.sh: emitted output not matching its documented shape: {stdout!r}")
    return {
        "forced_branch": False,
        "contract_verified": True,
        "observed_stdout": stdout,
        "portability_finding": BACKLOG_SURFACE_PORTABILITY_FINDING,
    }


_PROVERS: dict[str, Callable[[str, Path], Mapping[str, Any]]] = {
    "mcp-list-guard.py": _prove_mcp_list_guard,
    "recall-feedback.py": _prove_recall_feedback,
    "worker-guard.py": _prove_worker_guard,
    "background-pipe-gate.py": _prove_background_pipe_gate,
    "snapshot-settings.sh": _prove_snapshot_settings,
    "telegram-reply-gate.py": _prove_telegram_reply_gate,
    "ho_stop_failure.py": _prove_ho_stop_failure,
    "adaptive_finish_detect.py": _prove_adaptive_finish_detect,
    "handoff-auto-read.sh": _prove_handoff_auto_read,
    "verify-hook-integrity.sh": _prove_verify_hook_integrity,
    "ensure-hooks-active.sh": _prove_ensure_hooks_active,
    "backlog-surface.sh": _prove_backlog_surface,
}


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class HookEmissionsModule:
    """One instance per hook EVENT — see :func:`build_hook_emissions_modules`."""

    tier: Tier = "optional"

    def __init__(self, *, event: str, module_id: str) -> None:
        self.event = event
        self.id = module_id
        self._scratch_root: Path | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        wired = [e for e in target.entries if e.module_id == self.id and e.status == "applies"]
        if not wired:
            return ModuleDeclaration(
                module_id=self.id,
                tier=self.tier,
                applies=False,
                reason=f"no hooks wired for {self.event} in this target's settings.json",
            )
        return ModuleDeclaration(
            module_id=self.id,
            tier=self.tier,
            applies=True,
            reason=f"{len(wired)} hook(s) wired for {self.event}",
        )

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        settings_path = _settings_json_path(ctx.account)
        settings_text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
        entries = discover_hooks(settings_text, ctx.account)
        wired = [e for e in entries if e.module_id == self.id and e.status == "applies"]

        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        self._scratch_root = Path(tempfile.mkdtemp(prefix=f"bringup-hook-emissions-{self.id}-"))

        detail: dict[str, Any] = {"event": self.event, "wired_count": len(wired), "hooks": {}}

        if not wired:
            # Defensive fallback only: the caller is expected to have already
            # short-circuited on declare().applies == False and never reach
            # here. If it does anyway, "gap" is the honest signal — never a
            # silent green (a module_id has no ModuleEvidenceStatus value for
            # "declared_absent"; that row shape is the ORCHESTRATOR's to build
            # straight from declare(), without ever calling prove()).
            recorder.append("prove", {"result": "no-hooks-wired"})
            detail["result"] = "no-hooks-wired"
            return ModuleEvidence(
                module_id=self.id, status="gap", trace_path=recorder.path, evidence_paths=(), detail=detail
            )

        for entry in wired:
            command = str(entry.path)
            basename = _basename_of(command)

            if basename in _OUT_OF_SCOPE_BASENAMES:
                detail["hooks"][command] = {
                    "basename": basename,
                    "status": "out_of_scope",
                    "reason": _OUT_OF_SCOPE_BASENAMES[basename],
                }
                recorder.append("observe", {"path_id": command, "basename": basename, "status": "out_of_scope"})
                continue

            prover = _PROVERS.get(basename)
            if prover is None:
                raise HardStop(
                    f"{self.id}: wired hook has no registered prover: {basename} ({command!r}). "
                    "An unrecognized wired hook is a named gap, never a silent pass."
                )

            scratch = self._scratch_root / basename
            scratch.mkdir(parents=True, exist_ok=True)
            try:
                proof_detail = prover(command, scratch)
            except HardStop:
                raise
            except Exception as exc:  # noqa: BLE001 — surfaced as a named HardStop, not swallowed
                raise HardStop(
                    f"{self.id}: hook {basename} ({command!r}) raised while proving emission: {exc}"
                ) from exc

            detail["hooks"][command] = {"basename": basename, "status": "proven", "proof": proof_detail}
            recorder.append("observe", {"path_id": command, "basename": basename, "status": "proven"})

        evidence_paths = tuple(
            write_module_evidence(
                RunPaths(root=ctx.evidence_root, run_id=ctx.run_id),
                self.id,
                hook_detail["basename"],
                hook_detail,
            )
            for hook_detail in detail["hooks"].values()
        )
        recorder.append("result", {"status": "proven", "wired_count": len(wired)})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths, detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        removed = str(self._scratch_root) if self._scratch_root is not None else None
        if self._scratch_root is not None and self._scratch_root.exists():
            shutil.rmtree(self._scratch_root)
        if self._scratch_root is not None and self._scratch_root.exists():
            raise HardStop(f"{self.id}: restore residue — scratch root still exists: {self._scratch_root}")
        self._scratch_root = None
        return RestoreEvidence(module_id=self.id, restored=True, detail={"scratch_root_removed": removed})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            f"{evidence.detail.get('event', self.event)}: "
            f"{evidence.detail.get('wired_count', 0)} hook(s) wired, status={evidence.status}"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


def build_hook_emissions_modules() -> tuple[HookEmissionsModule, ...]:
    """One :class:`HookEmissionsModule` per hook event, per T-2556's five module_ids."""
    return tuple(
        HookEmissionsModule(event=event, module_id=module_id)
        for event, module_id in EVENT_MODULE_ID.items()
    )
