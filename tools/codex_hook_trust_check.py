#!/usr/bin/env python3
"""codex_hook_trust_check.py — is this account's codex worker-guard wired AND trusted? (T-4155)

Codex runs a command hook only once it is TRUSTED; an untrusted hook is skipped with
no error (T-3244). So deleting or editing the T-4144 worker-guard entry, or losing its
trusted_hash, silently leaves codex subagents unguarded, and the Claude-side hook
allowlist check never reads ~/.codex/config.toml. This asks codex itself.

Run AS the seat account, read-only. It drives `codex app-server` over stdio JSON-RPC
(initialize, initialized, hooks/list) from the account's home, which is the same view
the seat's own sessions get. Verdict (ORG-099 parity-axis contract, check_method
file_grep):
  stdout "true"   the worker-guard PreToolUse hook is enabled + trusted, its matcher
                  covers both "Bash" and "apply_patch", and every OTHER enabled hook,
                  trusted or not, is one the codex harness snippet declares (T-4160: the
                  control-plane launch bypasses hook trust, so untrusted hooks run).
                  Every Codex runtime also declares the trusted SessionStart/Stop
                  lifecycle pair installed by the launch adapter (T-4990).
                  The shell-box PreToolUse hook (T-5480, installer op
                  harness.codex-shell-box) is declared by codex_shell_box itself on EVERY
                  seat with a codex runtime, not by the snippet. A Claude seat (its codex
                  runtime is only a fallback) declares those lifecycle hooks, the
                  guard, and the shell-box hook.
  stdout "false"  any of that fails. The reasons go to stderr as JSON.
  (no stdout, exit 3)  could not evaluate (app-server, snippet or instance.yaml
                  unreadable, or this account has no harness binding), which parity
                  classifies UNMEASURABLE rather than a false FAIL.

Seat type (T-4763): instance.yaml `roles.<role>.harness` for the role whose `account`
is this one. `codex` is a codex-primary seat, which declares the snippet's hooks;
`claude-code` is a Claude seat with a codex fallback, which declares none. Until
T-4763 the signal was the PRESENCE of ~/codex-config/templates/config.toml.snippet,
which stops meaning anything once the snippet lives in the in-repo harness every
seat clones (ORG-091).

The injection direction (a trusted hook nobody declared) compares against the
codex harness template (harness/codex/templates/config.toml.snippet in the clone
this file runs from, `{{ codex_home }}` bound to this account's home). The
worker-guard command is always expected, so a snippet that predates T-4144 can't
flag the guard itself. The guard is the worker-guard-v2.py hook (T-5695); the v1
worker-guard.py is accepted only as the SOLE guard generation (the rollback, when the
seat's single guard entry is re-pointed back to v1). A v1 hook next to a v2 hook is not
a guard and is flagged as undeclared. The active guard is declared by this file's own
constants, never by the snippet.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import selectors
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# Canonical guard: worker-guard v2 (T-5695). v1 is the ROLLBACK guard only.
GUARD_SUFFIX = "/pantheon-config/tools/hooks/worker-guard-v2.py"
ROLLBACK_GUARD_SUFFIX = "/pantheon-config/tools/hooks/worker-guard.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNIPPET = REPO_ROOT / "harness" / "codex" / "templates" / "config.toml.snippet"
DEFAULT_INSTANCE = REPO_ROOT / "instance.yaml"
# instance.yaml harness value -> does the seat declare the codex snippet's hooks?
_HARNESS_DECLARES_SNIPPET = {"codex": True, "claude-code": False}
GUARDED_TOOLS = ("Bash", "apply_patch")
EXIT_UNMEASURABLE = 3
# The ORG-099 axes run this file from each SEAT's clone. Contract 3 adds the
# T-4990 lifecycle-only verdict; stale clones remain UNMEASURABLE.
CHECK_CONTRACT = 3
# parity_render's file_grep branch reads these stderr strings as "required file
# absent" -> HARD FAIL, whatever the exit code. An UNMEASURABLE reason must never
# carry them (T-4160: an unreadable snippet paged as a missing guard).
_PARITY_FAIL_MARKERS = {"No such file or directory": "ENOENT", "FileNotFoundError": "ENOENT"}


def _parity_safe(text: str) -> str:
    for marker, replacement in _PARITY_FAIL_MARKERS.items():
        text = text.replace(marker, replacement)
    return text


class Unavailable(RuntimeError):
    """The check could not be evaluated (not a verdict)."""


def _codex_bin() -> str:
    home = Path.home()
    path = os.pathsep.join([str(home / ".npm-global" / "bin"), os.environ.get("PATH", ""), "/usr/bin"])
    found = shutil.which("codex", path=path)
    if not found:
        raise Unavailable("codex binary not found for this account")
    return found


def list_hooks(*, codex_bin: str, cwd: Path, timeout: float = 30.0) -> list[dict]:
    """Every hook codex's app-server reports for `cwd`, flattened."""
    try:
        proc = subprocess.Popen([codex_bin, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1, cwd=str(cwd))
    except OSError as exc:
        raise Unavailable(f"could not start codex app-server: {exc}") from exc
    deadline = time.time() + timeout

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def reply(want_id: int) -> dict:
        # T-5872: selectors, not select.select() (ValueError for fd >= 1024).
        with selectors.PollSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            while time.time() < deadline:
                if not sel.select(0.5):
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == want_id:
                    return msg
        raise Unavailable(f"no app-server reply to request {want_id} within {timeout:.0f}s")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "codex-hook-trust-check", "version": "1"}}})
        reply(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {}})
        res = reply(2)
    except (OSError, ValueError) as exc:
        raise Unavailable(f"app-server exchange failed: {exc}") from exc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # T-5872: release the pipes (no ResourceWarning leak)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
    if "error" in res:
        raise Unavailable(f"hooks/list error: {res['error']}")
    hooks: list[dict] = []
    for entry in (res.get("result") or {}).get("data") or []:
        hooks.extend(h for h in entry.get("hooks") or [] if isinstance(h, dict))
    return hooks


def seat_harness(account: str, instance: Path) -> str:
    """This account's primary harness, from instance.yaml `roles.<role>.harness`.

    A role without `harness:` takes `instance.harness_default`. Any doubt (manifest
    unreadable, no role bound to the account, two roles disagreeing, a harness this
    check has no expectation for) is Unavailable, never a guess: guessing "Claude
    seat" on a codex seat would flag its own snippet hooks as injections, and the
    reverse would let a stray hook on a Claude seat pass."""
    try:
        import yaml  # lazy: a missing PyYAML is "could not evaluate", not a crash
        text = instance.read_text(encoding="utf-8")
    except (OSError, ImportError, UnicodeDecodeError) as exc:
        raise Unavailable(f"instance.yaml unreadable: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Unavailable(f"instance.yaml does not parse: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
        raise Unavailable("instance.yaml has no roles mapping")
    instance_block = data.get("instance") if isinstance(data.get("instance"), dict) else {}
    default = instance_block.get("harness_default")
    found = {binding.get("harness", default)
             for binding in data["roles"].values()
             if isinstance(binding, dict) and binding.get("account") == account}
    if not found:
        raise Unavailable(f"no instance.yaml role binds account {account!r}")
    if len(found) > 1:
        raise Unavailable(f"instance.yaml roles disagree on account {account!r}'s harness: "
                          f"{sorted(map(str, found))}")
    harness = found.pop()
    if harness not in _HARNESS_DECLARES_SNIPPET:
        raise Unavailable(f"account {account!r} harness {harness!r} has no trust-check expectation")
    return harness


def snippet_commands(snippet: Path, codex_home: Path) -> set[str]:
    """Hook commands the codex harness template declares, bound to this account."""
    try:
        text = snippet.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unavailable(f"codex harness snippet unreadable: {exc}") from exc
    try:
        parsed = tomllib.loads(text.replace("{{ codex_home }}", str(codex_home)))
    except tomllib.TOMLDecodeError as exc:
        raise Unavailable(f"codex harness snippet does not parse: {exc}") from exc
    commands: set[str] = set()
    for groups in (parsed.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue  # e.g. a [hooks.state] table
        for group in groups:
            for hook in group.get("hooks") or []:
                if isinstance(hook, dict) and hook.get("command"):
                    commands.add(hook["command"])
    return commands


def _matcher_covers(matcher: object, tool: str) -> bool:
    if matcher in (None, "", "*"):
        return True  # no matcher: the hook sees every tool
    try:
        return re.fullmatch(str(matcher), tool) is not None
    except re.error:
        return False


def shell_box_commands(codex_home: Path) -> set[str]:
    """The shell-box hook command, from codex_shell_box (single source, no re-spelled path).

    Lazy import: this file runs as a plain script, where `tools` may not be importable.
    Any failure is Unavailable (UNMEASURABLE), never a false FAIL."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from tools.harness_adapter import codex_shell_box
        return {codex_shell_box.hook_command(codex_home)}
    except Exception as exc:  # noqa: BLE001 - includes ImportError
        raise Unavailable(f"codex_shell_box unavailable: {type(exc).__name__}: {exc}") from exc


def lifecycle_commands(home: Path, account: str) -> tuple[set[str], set[str]]:
    """Accepted SessionStart/Stop commands: seat-clone canonical plus Consultant legacy."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from tools import agent_role_map
        from tools.harness_adapter import codex as codex_adapter
        persona = agent_role_map.account_persona(account)
    except Exception as exc:  # noqa: BLE001
        raise Unavailable(f"persona mapping unavailable: {type(exc).__name__}: {exc}") from exc
    if persona is None:
        raise Unavailable(f"no persona mapping for account {account!r}")
    source = codex_adapter.lifecycle_source_path(home, persona)
    if source is None or not source.is_file():
        raise Unavailable(f"no persona-owned lifecycle source for {persona!r}")
    canonical = codex_adapter.lifecycle_hook_commands(home, source)
    starts, stops = {canonical[0]}, {canonical[1]}
    legacy = codex_adapter.accepted_legacy_lifecycle_commands(home, persona)
    if legacy:
        starts.add(legacy[0])
        stops.add(legacy[1])
    return starts, stops


def evaluate_lifecycle(
    hooks: list[dict], starts: set[str], stops: set[str], *, home: Path | None = None,
) -> list[str]:
    """Problems, or [] when exactly one trusted/live lifecycle pair is reported.

    With ``home`` (the production path), inventory is entrypoint-family based rather
    than accepted-command based.  Thus a wrong source, malformed argument tail, or
    disabled/untrusted duplicate remains evidence under ``--lifecycle-only``.
    """
    problems: list[str] = []
    adapter = None
    if home is not None:
        try:
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))
            from tools.harness_adapter import codex as adapter
        except Exception as exc:  # noqa: BLE001
            problems.append(f"lifecycle family classifier unavailable: {type(exc).__name__}: {exc}")
            return problems
    for event, config_event, commands, matcher in (
        ("sessionStart", "SessionStart", starts, "startup|resume|clear|compact"),
        ("stop", "Stop", stops, None),
    ):
        found = [
            hook for hook in hooks
            if hook.get("eventName") == event
            and (
                hook.get("command") in commands
                if adapter is None
                else adapter.lifecycle_command_in_family(home, config_event, hook.get("command"))
            )
        ]
        if len(found) != 1:
            problems.append(f"expected exactly one {event} lifecycle hook, found {len(found)}")
            continue
        hook = found[0]
        if hook.get("command") not in commands:
            problems.append(
                f"{event} lifecycle hook uses a non-canonical or wrong-source command: "
                f"{hook.get('command')!r}"
            )
            continue
        if hook.get("trustStatus") != "trusted" or not hook.get("enabled", True):
            problems.append(
                f"{event} lifecycle hook not live: trust={hook.get('trustStatus')} "
                f"enabled={hook.get('enabled')}"
            )
        if hook.get("timeoutSec") != 5:
            problems.append(f"{event} lifecycle hook timeout is {hook.get('timeoutSec')!r}, not 5")
        if matcher is not None and hook.get("matcher") != matcher:
            problems.append(f"{event} lifecycle matcher is {hook.get('matcher')!r}")
    return problems


def _is_pretooluse_ending(hook: dict, suffix: str) -> bool:
    return (hook.get("eventName") == "preToolUse"
            and str(hook.get("command") or "").endswith(suffix))


def _is_guard(hook: dict) -> bool:
    """A worker-guard hook of either generation (v2 canonical, v1 rollback)."""
    return (_is_pretooluse_ending(hook, GUARD_SUFFIX)
            or _is_pretooluse_ending(hook, ROLLBACK_GUARD_SUFFIX))


def _guards(hooks: list[dict]) -> list[dict]:
    """The guard hooks: v2 ones; only when there are none, the v1 (rollback) ones."""
    v2 = [h for h in hooks if _is_pretooluse_ending(h, GUARD_SUFFIX)]
    if v2:
        return v2
    return [h for h in hooks if _is_pretooluse_ending(h, ROLLBACK_GUARD_SUFFIX)]


def evaluate(hooks: list[dict], expected: set[str]) -> list[str]:
    """Problems, or [] when the seat is guarded and carries no undeclared trusted hook."""
    problems: list[str] = []
    guards = _guards(hooks)
    guard_ids = {id(h) for h in guards}
    live = [h for h in guards
            if h.get("trustStatus") == "trusted" and h.get("enabled", True)
            and all(_matcher_covers(h.get("matcher"), t) for t in GUARDED_TOOLS)]
    if not guards:
        problems.append("no PreToolUse worker-guard hook configured")
    elif not live:
        problems.append("worker-guard hook present but not live: " + "; ".join(
            f"{h.get('key')}: trust={h.get('trustStatus')} enabled={h.get('enabled')} "
            f"matcher={h.get('matcher')!r}" for h in guards))
    # T-4160: the injection direction counts every ENABLED undeclared hook, trusted or
    # not. The control-plane codex launch passes --dangerously-bypass-hook-trust
    # (harness_adapter/codex.py:402), so an untrusted hook RUNS on those seats.
    for hook in hooks:
        if id(hook) in guard_ids or not hook.get("enabled", True):
            continue
        if hook.get("command") not in expected:
            problems.append(f"hook not declared by the codex harness snippet "
                            f"(trust={hook.get('trustStatus')}): "
                            f"{hook.get('key')} -> {hook.get('command')!r}")
    return problems


def _account() -> str:
    """The OS account this check runs as (parity runs it AS the seat account)."""
    return pwd.getpwuid(os.geteuid()).pw_name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--snippet", type=Path, default=None,
                   help="codex config.toml.snippet to declare, skipping the seat-type lookup "
                        "(default: by instance.yaml harness; codex seats read "
                        "harness/codex/templates/config.toml.snippet)")
    p.add_argument("--instance", type=Path, default=DEFAULT_INSTANCE,
                   help="instance.yaml to read the seat type from (default: this clone's)")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--lifecycle-only", action="store_true",
                   help="check only the trusted SessionStart/Stop lifecycle pair")
    args = p.parse_args(argv)
    home = Path.home()
    try:
        if args.snippet is not None:
            # An explicit snippet is declared as-is; unreadable is UNMEASURABLE.
            expected = snippet_commands(args.snippet, home)
        elif _HARNESS_DECLARES_SNIPPET[seat_harness(_account(), args.instance)]:
            expected = snippet_commands(DEFAULT_SNIPPET, home)
        else:
            # A Claude seat with a Codex fallback does not declare the primary-seat
            # snippet wholesale; T-4990's lifecycle pair is added explicitly below.
            expected = set()
        starts, stops = lifecycle_commands(home, _account())
        expected |= starts | stops
        # Every seat with a codex runtime gets the shell-box hook (installer op
        # harness.codex-shell-box), Claude fallback seats included.
        expected = expected | shell_box_commands(home)
        hooks = list_hooks(codex_bin=_codex_bin(), cwd=home, timeout=args.timeout)
    except Unavailable as exc:
        print(_parity_safe(json.dumps({"unmeasurable": str(exc)})), file=sys.stderr)
        return EXIT_UNMEASURABLE
    except Exception as exc:  # noqa: BLE001 - a crash is "could not evaluate", not a verdict
        print(_parity_safe(json.dumps({"unmeasurable": f"{type(exc).__name__}: {exc}"})),
              file=sys.stderr)
        return EXIT_UNMEASURABLE
    problems = evaluate_lifecycle(hooks, starts, stops, home=home)
    if not args.lifecycle_only:
        problems += evaluate(hooks, expected)
    if problems:
        print(json.dumps({"problems": problems}), file=sys.stderr)
    print("false" if problems else "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
