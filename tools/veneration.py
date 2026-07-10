"""tools/veneration.py — the resume-the-dead recovery harness (T-0766).

When an agent boots blank or its predecessor was cut off (forced/api-health restart, crash,
OOM, handoff cut mid-write), the dead session is not gone — its full transcript lives in its
jsonl on disk, and `claude --resume` reloads the entire context even though the process is
dead. Veneration resurrects that dead session UNDER the-ancestor (channel-less), prompts it
to write a fresh HANDOFF, harvests it, and buries the resumed session.

This is the FALLBACK tier: most recovery is just reading the latest graveyard handoff
(tools/graveyard.py). Veneration is for the deaths the graveyard can't cover — a handoff
that was never written or cut off.

SAFETY — why it runs under the-ancestor with channels DISABLED: the resume subprocess is a
fresh `claude` with NO --channels / no telegram plugin, so it holds no bot token and cannot
hijack a live agent's bridge (the second-claude-steals-the-token landmine). This is the
whole reason Veneration runs as the-ancestor, never on an agent's own bot account.

MUTEX — the-ancestor is a single shared substrate, so only ONE veneration runs at a time,
guarded by the shared flock lease (tools/_lease.py): non-blocking acquire (a daemon backs
off rather than stalling its poll cycle), implicit release on process exit (a crashed child
frees the lease — daemon-safe), and stale-takeover for a wedged-but-alive holder.

Flow: acquire lease → run ONE headless channel-less `claude --resume <session-id> --print
--safe-mode --permission-mode bypassPermissions <harvest-prompt>` (resumes the dead context,
writes the HANDOFF, self-exits) → confirm the HANDOFF was actually written → release lease.
The T-0766 supervised first-fire started from an interactive tmux flow (launch → wait-ready
sentinel → send-keys inject → mtime-ack → kill) but that path fights three TUI-only gates an
unattended one-shot can't clear — the `--dangerously-skip-permissions` consent screen, a
readiness sentinel the bypass-mode footer doesn't match, and the target account's SessionStart
hooks injecting competing nudges. Headless `--print` clears all three and removes the
wait/inject/kill machinery entirely. Interactive questioning of the dead session is a v2 —
when it's actually built it can re-introduce a TUI path; we don't carry one speculatively.

CAVEAT: a session that was api-health POISONED (400ing on request shape) may inherit the
poison on resume and 400 too — so Veneration reliably covers crashed/cut-off/OOM
(healthy-but-died), less certainly the poisoned case.

The orchestration is decoupled from its side effects via an injected `Ops` so it is fully
unit-testable without tmux/claude; the live first-fire is a SUPERVISED step (every
restart-stack change gets one), gated on the operator.
"""
from __future__ import annotations

import os
import pwd
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:  # tools.-first so discover-from-root and bare-run resolve one module (no dup-import)
    from tools._lease import Lease
except ImportError:
    from _lease import Lease

DEFAULT_HARVEST_TIMEOUT = 300  # headless resume (reload large transcript) + write a HANDOFF
DEFAULT_LEASE_TTL = 900        # a resume + single handoff write is minutes (T-0766 §E)
_ANCESTOR_LEASE = "/home/the-ancestor/.veneration/veneration.lock"

# SECURITY: agent + dead_jsonl come from a SHARED mailbox payload (any peer can write it)
# and flow into a subprocess that runs as the-ancestor. Validate them to a strict charset
# BEFORE they reach any command construction — this is the primary defense against shell/
# arg injection (e.g. a crafted dead_jsonl smuggling `--channels`, which would defeat the
# channel-less safety invariant, or arbitrary command execution as the-ancestor).
_AGENT_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_JSONL_RE = re.compile(r"/[A-Za-z0-9/._-]+\.jsonl\Z")


def validate_request(agent: str, dead_jsonl: str) -> str | None:
    """Return None if (agent, dead_jsonl) are safe to use, else a reason string. Strict
    allowlist: no shell metacharacters, no whitespace, no quotes, no path traversal."""
    if not agent or not _AGENT_RE.match(agent):
        return f"invalid agent name {agent!r} (must match {_AGENT_RE.pattern})"
    if not dead_jsonl or ".." in dead_jsonl or not _JSONL_RE.match(dead_jsonl):
        return f"invalid dead_jsonl {dead_jsonl!r} (must be an absolute .jsonl path, safe charset)"
    return None


def harvest_prompt(handoff_target: str) -> str:
    """The single canned prompt injected into the resurrected session. Deliberately narrow:
    write the handoff, then stop — no other action (the session is dead context, not live)."""
    return (
        f"You have been resurrected ONLY to harvest your handoff for your successor. "
        f"Write {handoff_target} now, summarizing your current state, in-flight work, and "
        f"the immediate next actions for the next session. When the file is written, say "
        f"'handoff written' and STOP. Do not take any other action, send any message, or "
        f"use any channel — you are a resumed dead session, not a live agent."
    )


class Result:
    """Outcome of a resurrection attempt. status ∈ {ok, skipped, failed}."""
    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:
        return f"Result({self.status!r}, {self.detail!r})"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --------------------------------------------------------------------------- Ops seam
class Ops:
    """The side-effecting operations, isolated so resurrect() is pure orchestration and
    fully unit-testable. The default impls wrap tmux / claude / the filesystem; tests
    inject a fake. Each is best-effort and returns a simple signal rather than raising."""

    def handoff_exists_nonempty(self, path: str) -> bool:
        """True if the harvested HANDOFF was actually written (exists + has content). The
        post-harvest witness — a clean subprocess exit is necessary but not sufficient."""
        try:
            return Path(path).stat().st_size > 0
        except (FileNotFoundError, OSError):
            return False

    def harvest(self, dead_jsonl: str, prompt: str, *, claude_bin: str,
                run_as: str | None, timeout: int) -> bool:
        """Run the resume-and-harvest as ONE headless subprocess and return whether it
        exited 0 within `timeout`:

            claude --resume <session-id> --print --safe-mode \\
                   --permission-mode bypassPermissions <prompt>

        Headless `--print` (not an interactive tmux TUI) is what makes an UNATTENDED harvest
        robust — the T-0766 first-fire proved the interactive path fights three TUI-only
        gates that don't exist in --print: the `--dangerously-skip-permissions` consent
        screen, a readiness sentinel that the bypass-mode footer doesn't match, and the
        target account's SessionStart hooks injecting competing nudges. In --print all three
        vanish, and the process self-exits (no tmux session to bury).

        Invariants preserved:
        - channel-less: NO --channels, and --safe-mode disables plugins/MCP entirely, so the
          resumed session holds no bot token and cannot reach any channel (the whole reason
          veneration runs under the-ancestor). --safe-mode also drops the target's hooks +
          CLAUDE.md, so nothing competes with or re-primes the harvest prompt.
        - unattended write: --permission-mode bypassPermissions lands the harvest Write with
          no prompt (headless --print has no one-time consent screen, unlike the TUI flag).
        - project-scoped resume: `--resume` takes a SESSION ID (the .jsonl basename, e.g.
          .../<uuid>.jsonl → <uuid>), resolved against the cwd-hashed project — so the
          process must run in run_as's home, the project dir the dead transcript lives under.

        `dead_jsonl` is pre-validated by validate_request(); every operand is shlex-quoted
        into the `bash -c` body (cd-into-home must happen AS run_as via sudo, not in Recovery's
        own process, which can't enter a 700-mode home)."""
        session_id = Path(dead_jsonl).stem  # <uuid>.jsonl → <uuid>
        if run_as:
            try:
                pw = pwd.getpwnam(run_as)
            except KeyError:
                return False
            home, uid, prefix = pw.pw_dir, pw.pw_uid, ["sudo", "-n", "-u", run_as]
        else:
            home, uid, prefix = os.path.expanduser("~"), os.getuid(), []
        inner = ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}", claude_bin,
                 "--resume", session_id, "--print", "--safe-mode",
                 "--permission-mode", "bypassPermissions", prompt]  # NO --channels
        # cd into run_as's home (project dir for the resume) AS run_as, then exec claude.
        script = f"cd {shlex.quote(home)} && exec {shlex.join(inner)}"
        argv = [*prefix, "bash", "-c", script]
        try:
            return subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


# ----------------------------------------------------------------------- orchestration
def resurrect(agent: str, dead_jsonl: str, handoff_target: str, *,
              ops: Ops | None = None,
              lease: Lease | None = None,
              run_as: str | None = "the-ancestor",
              claude_bin: str = "/home/the-ancestor/.npm-global/bin/claude",
              harvest_timeout: int = DEFAULT_HARVEST_TIMEOUT,
              lease_ttl: int = DEFAULT_LEASE_TTL) -> Result:
    """Resurrect `agent`'s dead session (`dead_jsonl`) under the-ancestor to harvest a fresh
    HANDOFF at `handoff_target`. Returns a Result. Single-holder via the shared lease — if
    another veneration holds it, returns skipped (the daemon backs off, never blocks).

    The harvest is a single headless `claude --print` subprocess (see Ops.harvest) — it
    resumes the dead session, writes the HANDOFF, and self-exits. A clean exit is necessary
    but not sufficient: we also confirm the file was actually written (exists + non-empty)
    before declaring ok.
    """
    # SECURITY: validate BEFORE acquiring the lease or building any command — agent +
    # dead_jsonl are untrusted (shared mailbox), and a crafted value could smuggle
    # `--channels` or arbitrary commands run as the-ancestor (see validate_request).
    bad = validate_request(agent, dead_jsonl)
    if bad:
        return Result("failed", f"rejected: {bad}")

    ops = ops or Ops()
    lease = lease or Lease(_ANCESTOR_LEASE, ttl_secs=lease_ttl, label=f"veneration:{agent}")

    if not lease.acquire():  # non-blocking; reaps a stale holder, else backs off
        return Result("skipped", f"lease held: {lease.read_record()}")

    try:
        if not ops.harvest(dead_jsonl, harvest_prompt(handoff_target),
                           claude_bin=claude_bin, run_as=run_as, timeout=harvest_timeout):
            return Result("failed",
                          f"harvest subprocess failed or timed out ({harvest_timeout}s)")
        if not ops.handoff_exists_nonempty(handoff_target):
            return Result("failed", "harvest exited cleanly but no HANDOFF was written")
        return Result("ok", f"harvested {handoff_target} from {dead_jsonl}")
    finally:
        lease.release()  # also implicit on process exit (daemon-safe)


if __name__ == "__main__":
    # Manual / supervised-first-fire entry. NOT auto-invoked — the live resurrection is a
    # supervised step. Usage: veneration.py <agent> <dead_jsonl> <handoff_target>
    if len(sys.argv) != 4:
        print("usage: veneration.py <agent> <dead_jsonl> <handoff_target>", file=sys.stderr)
        raise SystemExit(64)
    res = resurrect(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"[veneration] {res}")
    raise SystemExit(0 if res.ok else 1)
