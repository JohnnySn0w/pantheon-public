"""The seat-side child of the root launcher (T-6167 PR-4). DARK.

Run as the seat's own uid by the launcher's transient unit, from the rev-pinned framework
copy: ``python3 -I -B -c <template> install <account> <sha256:digest>`` or
``... selfcheck <account>``. The template inserts only the framework root on ``sys.path``
and calls ``main`` here.

* ``install`` resolves the persona from the account in the framework copy's trusted
  ``instance.yaml`` (never the seat's checkout), builds the live install handlers and the
  live seat backend, and runs the engine's existing ``apply install --seat`` path. The
  backend's own pin (``seat_backend.ports.LIVE_ACTUATION_ENABLED``) is the third pin: a
  False pin refuses construction here, before any I/O, and is printed as the typed
  refusal ``seat-pin-disabled`` so the parent can classify it.
* ``selfcheck`` is mutation-free: it prints one JSON object (uid, euid, gid, groups, cwd,
  environment key names, ``sys.flags.isolated``, the framework root and ``NoNewPrivs``)
  and nothing else. It is the supervised first-launch proof (the ``-B`` and empty
  ``SupplementaryGroups=`` behaviours are checked from this output).

Output is data for the operator; the parent never uses it to authorise anything. Exit 0 ok,
1 a typed refusal (``refused: <code>: <message>`` on stderr), 2 a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def framework_root() -> Path:
    """The rev-pinned copy this module was imported from (``<root>/tools/germinate/seat_child.py``)."""

    return Path(__file__).resolve().parents[2]


def _no_new_privs() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("NoNewPrivs:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def selfcheck_report(account: str, *, root: Path | None = None) -> dict[str, Any]:
    """The mutation-free first-launch proof. Reads only process state."""

    return {
        "account": account,
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "gid": os.getgid(),
        "groups": sorted(os.getgroups()),
        "cwd": os.getcwd(),
        "env_keys": sorted(os.environ),
        "isolated": sys.flags.isolated,
        "dont_write_bytecode": bool(sys.dont_write_bytecode),
        "framework_root": str(root if root is not None else framework_root()),
        "no_new_privs": _no_new_privs(),
        "interpreter": sys.executable,
    }


def persona_for_account(manifest: Any, account: str) -> str:
    """The single persona whose instance.yaml role is bound to ``account``."""

    found = sorted(
        str(raw["persona"])
        for raw in manifest.roles.values()
        if isinstance(raw, dict) and raw.get("account") == account and isinstance(raw.get("persona"), str)
    )
    if len(found) != 1:
        raise LookupError(f"{account!r} is bound to {len(found)} personas in instance.yaml; exactly one is required")
    return found[0]


def _refuse(code: str, message: str) -> int:
    print(f"refused: {code}: {message}", file=sys.stderr)
    return 1


def _default_runner() -> Any:
    from tools._organ_lifecycle import SubprocessCommandRunner

    return SubprocessCommandRunner()


def run_install(
    account: str,
    plan_digest: str,
    *,
    root: Path | None = None,
    runner_factory: Callable[[], Any] = _default_runner,
) -> int:
    """Run ``apply install --seat <account>`` on the live seat backend, as this uid."""

    from tools.germinate.engine import cli as engine_cli
    from tools.germinate.engine.backend import BackendRefused
    from tools.germinate.engine.context import InvocationContext
    from tools.germinate.engine.executor import Engine, EngineRefusal
    from tools.germinate.engine.run_state import RunStateError
    from tools.germinate.seat_backend.backend import live_seat_factory
    from tools.germinate.seat_backend.install_handlers import live_install_handlers
    from tools.germinate.seat_backend.ports import LiveActuationDisabled
    from tools.persona_installer import Manifest

    fw = root if root is not None else framework_root()
    try:
        manifest = Manifest.load(fw / "instance.yaml", strict_flip_keys=False)
        persona = persona_for_account(manifest, account)
    except (LookupError, OSError, ValueError, RuntimeError) as exc:
        return _refuse("seat-persona-unresolved", f"{type(exc).__name__}: {exc}")

    parser = argparse.ArgumentParser(prog="seat_child")
    sub = parser.add_subparsers(dest="command", required=True)
    engine_cli.register(sub)
    args = parser.parse_args(
        ["apply", "install", "--seat", account, "--plan-digest", plan_digest, "--repo-root", str(fw),
         "--invoking-account", account]
    )
    try:
        handlers = live_install_handlers(manifest, persona)
        engine = Engine.from_checkout(
            None,
            repo_root=fw,
            instance=manifest.instance_id,
            echo=sys.stdout,
            backend_factory=live_seat_factory(persona, runner_factory(), handlers),
            context=InvocationContext(kind="operator", account=account),
        )
        return engine_cli._apply(engine, args)
    except LiveActuationDisabled as exc:
        return _refuse("seat-pin-disabled", str(exc))
    except BackendRefused as exc:
        return _refuse("seat-backend-refused", f"{type(exc).__name__}: {exc}")
    except EngineRefusal as exc:
        return _refuse(exc.code, exc.message)
    except RunStateError as exc:
        return _refuse("run-state-refused", str(exc))


def main(argv: list[str], *, out: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    if len(argv) not in (2, 3) or argv[0] not in ("install", "selfcheck"):
        print("usage: seat_child (install <account> <sha256:digest> | selfcheck <account>)", file=sys.stderr)
        return 2
    mode, account = argv[0], argv[1]
    if not ACCOUNT_RE.fullmatch(account):
        print(f"usage error: bad account {account!r}", file=sys.stderr)
        return 2
    if mode == "selfcheck":
        if len(argv) != 2:
            print("usage: seat_child selfcheck <account>", file=sys.stderr)
            return 2
        out.write(json.dumps(selfcheck_report(account), sort_keys=True) + "\n")
        return 0
    if len(argv) != 3 or not DIGEST_RE.fullmatch(argv[2]):
        print("usage: seat_child install <account> <sha256:64 hex>", file=sys.stderr)
        return 2
    return run_install(account, argv[2])
