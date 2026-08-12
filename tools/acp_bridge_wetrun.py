#!/usr/bin/env python3
"""tools/acp_bridge_wetrun.py — T-1633 empirical gate for tools/acp_bridge.py.

Rung-driver convention (mirrors the T-1630 spike harness rung{1,2,3}.py):
talks to the REAL claude-code-acp wrapper over stdio. NOT run in CI, NOT
imported by tools/test_acp_bridge.py, and NOT collected by pytest (this
filename doesn't match test_*.py / *_test.py). Run this by hand against a
real, OAuth-authed `~/.claude` credential store before any Unit-2 pilot.

Package pin (HO disposition, T-1633 dispatch — record exactly): the wrapper
is the zed-industries `claude-code-acp` npm package at version 0.16.2 (wraps
Claude Agent SDK 0.2.44, node20); runtime command `claude-code-acp`. This
driver assumes a PINNED install of that exact version, not `@latest`.

Why this file exists (D11 — third-party surface verification): the engine's
session/load wire-shape ({sessionId, cwd}) is UNVERIFIED against the
authoritative ACP spec — tools/acp_bridge.py's any-error-falls-back-to-
session/new design makes the engine correct regardless of the true shape,
but "correct via fallback" is not the same as "session/load actually does
what we assume it does." This driver is the live-surface gate that closes
that gap: run it once with a fresh session (no state file) to exercise
session/new, then run it again (state file now holds a sessionId) to
exercise session/load for real and confirm whether it succeeds or falls
back, against the real wrapper — not a fake-server mirror of our own
assumptions (D11's "real-surface validation is the gate, not fake-server
tests").

Usage:
    python3 tools/acp_bridge_wetrun.py --agent orchestrator --cwd /home/claude/pantheon-config \\
        --inbox /home/claude/.pantheon-mailbox/orchestrator/inbox [--wrapper-cmd claude-code-acp] \\
        [--state-path ~/.local/state/pantheon/acp-bridge-orchestrator.json] [--poll-interval 5] [--serve]

By default runs ONE drain pass (run_once()) and exits. --serve loops via
AcpBridge.serve() instead (bounded by --max-iterations if given).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # bare-import the SUT
import acp_bridge  # noqa: E402


# ------------------------------------------------------------ fs-caps rung
class _RecordingTransport:
    """T-1831 fs-caps rung support: a thin recording wrapper around a real
    AcpTransport. Captures the ACTUAL `initialize` params AcpBridge.start()
    sends (never a hardcoded copy of the caps dict — that's the whole point:
    a hardcoded copy could drift from what the bridge really advertises and
    the rung would stop meaning anything) and which fs handlers actually
    fired during the canary turn, via the same register_handler() surface
    AcpTransport and FakeAcpTransport both expose."""

    def __init__(self, inner):
        self._inner = inner
        self.initialize_params = None
        self.fired: set[str] = set()

    def request(self, method: str, params=None, timeout: float = 60.0):
        if method == "initialize":
            self.initialize_params = params
        return self._inner.request(method, params, timeout)

    def notify(self, method: str, params=None) -> None:
        self._inner.notify(method, params)

    def register_handler(self, method: str, handler) -> None:
        def _instrumented(p):
            result = handler(p)
            self.fired.add(method)
            return result

        self._inner.register_handler(method, _instrumented)

    def close(self) -> None:
        self._inner.close()


def _exercise_read(bridge: "acp_bridge.AcpBridge", rec: _RecordingTransport, cwd: str) -> bool:
    """Canary turn: ask the inner agent to read a temp file via its ACP
    fs tool. PASS requires BOTH the readTextFile handler firing AND the
    turn reaching a terminal stopReason — a capability advertised but never
    exercisable must fail loud, not silently."""
    canary_path = os.path.join(cwd, f".acp-fs-caps-canary-read-{os.getpid()}.txt")
    magic = f"XYZZY-T1831-READ-{os.getpid()}"
    with open(canary_path, "w", encoding="utf-8") as fh:
        fh.write(magic)
    try:
        result = bridge.transport.request(
            "session/prompt",
            {
                "sessionId": bridge.session_id,
                "prompt": [{
                    "type": "text",
                    "text": (
                        f"Use your file-read tool to read the file at {canary_path} "
                        "and reply with exactly its contents, nothing else."
                    ),
                }],
            },
            timeout=bridge.prompt_timeout,
        )
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        fired = "fs/read_text_file" in rec.fired
        if not fired:
            print("  readTextFile: handler never fired for the canary read turn")
        if stop_reason != "end_turn":
            print(f"  readTextFile: turn did not reach end_turn (stopReason={stop_reason!r})")
        return fired and stop_reason == "end_turn"
    finally:
        try:
            os.remove(canary_path)
        except OSError:
            pass


def _exercise_write(bridge: "acp_bridge.AcpBridge", rec: _RecordingTransport, cwd: str) -> bool:
    """Canary turn: ask the inner agent to write a temp file via its ACP fs
    tool. PASS requires the writeTextFile handler firing, the turn reaching
    a terminal stopReason, AND the canary content actually landing on disk —
    the strongest available evidence that advertised == implemented."""
    canary_path = os.path.join(cwd, f".acp-fs-caps-canary-write-{os.getpid()}.txt")
    magic = f"XYZZY-T1831-WRITE-{os.getpid()}"
    try:
        os.remove(canary_path)
    except OSError:
        pass
    try:
        result = bridge.transport.request(
            "session/prompt",
            {
                "sessionId": bridge.session_id,
                "prompt": [{
                    "type": "text",
                    "text": (
                        f"Use your file-write tool to write exactly '{magic}' "
                        f"(no quotes, no extra text) to the file at {canary_path}, "
                        "creating it if needed. Then reply with the single word done."
                    ),
                }],
            },
            timeout=bridge.prompt_timeout,
        )
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        fired = "fs/write_text_file" in rec.fired
        on_disk_ok = False
        if os.path.exists(canary_path):
            with open(canary_path, "r", encoding="utf-8") as fh:
                on_disk_ok = magic in fh.read()
        if not fired:
            print("  writeTextFile: handler never fired for the canary write turn")
        if stop_reason != "end_turn":
            print(f"  writeTextFile: turn did not reach end_turn (stopReason={stop_reason!r})")
        if not on_disk_ok:
            print(f"  writeTextFile: canary content not found on disk at {canary_path}")
        return fired and stop_reason == "end_turn" and on_disk_ok
    finally:
        try:
            os.remove(canary_path)
        except OSError:
            pass


def run_fs_caps_rung(*, agent, cwd, inbox, wrapper_cmd, state_path, prompt_timeout) -> bool:
    """T-1831 wetrun rung: advertised-implies-implemented for fs caps.

    Reads the caps the bridge ADVERTISES straight out of the real
    `initialize` params it sends (via _RecordingTransport — never a
    hardcoded copy), then for each advertised-true fs cap drives an
    end-to-end canary turn and asserts the corresponding handler actually
    fired and the turn completed. A capability advertised but never
    exercisable is a rung FAIL naming that capability — the class-killer
    this ticket exists to add: advertise-without-handler can no longer ship
    dark, because nothing gates a deploy on this rung passing."""
    real_transport = acp_bridge.AcpTransport(wrapper_cmd)
    rec = _RecordingTransport(real_transport)
    bridge = acp_bridge.AcpBridge(
        agent=agent,
        cwd=cwd,
        inbox=inbox,
        wrapper_cmd=wrapper_cmd,
        state_path=state_path,
        transport=rec,
        prompt_timeout=prompt_timeout,
    )
    try:
        bridge.start()
        caps = (rec.initialize_params or {}).get("clientCapabilities", {}).get("fs", {})
        advertised = [name for name, on in caps.items() if on]
        print(f"advertised fs caps (from the real initialize params): {advertised}")
        if not advertised:
            print("RUNG fs-caps: FAIL -- no fs caps advertised true, nothing to exercise")
            return False

        exercisers = {"readTextFile": _exercise_read, "writeTextFile": _exercise_write}
        results = {}
        for cap_name, exercise in exercisers.items():
            if caps.get(cap_name):
                results[cap_name] = exercise(bridge, rec, cwd)

        print("=== RUNG fs-caps matrix ===")
        ok = True
        for cap_name, passed in results.items():
            print(f"  {cap_name}: {'PASS' if passed else 'FAIL'}")
            if not passed:
                ok = False
        if ok:
            print("RUNG fs-caps: PASS -- every advertised-true fs cap fired its handler end-to-end")
        else:
            failed = [name for name, passed in results.items() if not passed]
            print(f"RUNG fs-caps: FAIL -- advertised but not exercisable: {failed}")
        return ok
    finally:
        bridge.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, help="agent name (state file + logging label)")
    parser.add_argument("--cwd", required=True, help="the agent's REAL working directory")
    parser.add_argument("--inbox", required=True, help="mailbox inbox dir (tools/mailbox.py layout)")
    parser.add_argument("--wrapper-cmd", nargs="+", default=None,
                         help="override the wrapper command (default: ['claude-code-acp'])")
    parser.add_argument("--state-path", default=None,
                         help="override the session-state file path")
    parser.add_argument("--prompt-timeout", type=float, default=300.0)
    parser.add_argument("--serve", action="store_true",
                         help="loop via AcpBridge.serve() instead of a single run_once()")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument(
        "--rung", choices=["fs-caps"], default=None,
        help="run an empirical wetrun rung instead of the normal drain/serve flow "
             "(T-1831; 'fs-caps' = advertised-implies-implemented gate for fs caps)",
    )
    args = parser.parse_args(argv)

    if args.rung == "fs-caps":
        ok = run_fs_caps_rung(
            agent=args.agent,
            cwd=args.cwd,
            inbox=args.inbox,
            wrapper_cmd=args.wrapper_cmd,
            state_path=args.state_path,
            prompt_timeout=args.prompt_timeout,
        )
        return 0 if ok else 1

    bridge = acp_bridge.AcpBridge(
        agent=args.agent,
        cwd=args.cwd,
        inbox=args.inbox,
        wrapper_cmd=args.wrapper_cmd,
        state_path=args.state_path,
        prompt_timeout=args.prompt_timeout,
    )

    try:
        if args.serve:
            bridge.serve(poll_interval=args.poll_interval, max_iterations=args.max_iterations)
            print("serve() loop ended (max-iterations reached).")
        else:
            result = bridge.run_once()
            print(json.dumps(result, indent=2, sort_keys=True))
            print(f"session_id={bridge.session_id} state_path={bridge.state_path}")
    finally:
        bridge.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
