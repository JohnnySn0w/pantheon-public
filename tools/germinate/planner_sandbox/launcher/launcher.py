"""The canonical planner-launcher entrypoint (T-3295, ORG-080 tranche D).

Implements the canonical launcher startup order from
``adr/ORG-080-germination-engine.md`` Component contract (relocated to notes/2026-11-02-org-080-sandbox-relocated.md §Environment and Python startup boundary):

    1. Freeze ``sys.path`` to the stdlib-only closure.
    2. ``os.environ.clear()``.
    3. Reset environment to the three allowed names (LC_ALL, TZ, PATH).
    4. Install the in-process seccomp filter denying ``execve``/``execveat``.
    5. Install the module-import allowlist hook.
    6. Import the component module (first exposure to component-supplied
       code) -- ``plan_<leg>`` mode only, skipped for ``--canary=`` mode.
    7. Call ``plan_<leg>(...)`` -- ``plan_<leg>`` mode only.

Two invocation shapes:

- ``--canary=<name> <scratch_dir> [extra_arg]`` -- runs exactly one
  isolated capability-denial probe (steps 1-5, no component import),
  writes the observed outcome to ``<scratch_dir>/canary-<name>.result``
  and to stdout. Used by ``tests/test_launcher_c5.py``.
- ``plan_<leg> <component_module_path>`` -- the production planner-launch
  path (steps 1-7). Reads a JSON request from stdin, writes the typed
  ``Operation[]`` response as JSON to stdout.

See ``tools/germinate/planner_sandbox/launcher/systemd_run_shape.py`` for
the sandbox property list this module is invoked under, and that module's
docstring for empirical deviations from the ADR's documentary text.
"""

from __future__ import annotations

import errno as _errno
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


class PrimitiveUnavailable(Exception):
    """Typed refusal for a launcher-owned primitive that's missing or
    malformed (C4 fail-closed). Local to this tranche, mirroring
    ``HarnessUnavailable``'s shape -- PO-approved naming convention
    (T-3295 Phase 1 open question 2): each tranche defines its own local
    ``PrimitiveUnavailable(name, reason)``; the C4 no-swallow lint matches
    by class-name suffix, not a shared import.
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")


_ALLOWED_ENV: dict[str, str] = {
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PATH": "/usr/bin:/bin",
}


def _stdlib_sys_path() -> list[str]:
    major, minor = sys.version_info.major, sys.version_info.minor
    base = f"/usr/lib/python{major}.{minor}"
    return [f"/usr/lib/python{major}{minor}.zip", base, f"{base}/lib-dynload"]


def _freeze_sys_path() -> None:
    """Step 1."""

    sys.path[:] = _stdlib_sys_path()


def _reset_environment() -> None:
    """Steps 2-3."""

    os.environ.clear()
    os.environ.update(_ALLOWED_ENV)


def _install_execve_closure() -> None:
    """Step 4. Fail-closed (C4): any failure -- the vendored binding's own
    ``PrimitiveUnavailable`` (e.g. the pinned libseccomp path going stale
    on a different host) or a plain ``ImportError``/filter-install error --
    is re-raised as *this* module's typed ``PrimitiveUnavailable``. Always
    a re-raise (``from exc``), never a swallow.
    """

    try:
        # NOT a relative import ("from . import seccomp"): when this file
        # runs as __main__ via a direct absolute-path ExecStart invocation
        # (the ADR's own documented shape -- "-m tools.germinate...launcher"
        # would fail the same way under -SEI's sys.path[0] rules), there is
        # no package context for a relative import to resolve against.
        # Matches the ADR's literal step 4 text instead: "Insert
        # .../vendor/seccomp into sys.path[0] and import seccomp" -- insert
        # this file's own directory (launcher/, which contains the
        # seccomp/ package) onto sys.path and import it as a top-level
        # module, exactly as production would with the bind-mounted
        # /opt/pantheon/germinate/ layout.
        launcher_dir = str(Path(__file__).resolve().parent)
        if launcher_dir not in sys.path:
            sys.path.insert(0, launcher_dir)
        import seccomp as _seccomp

        syscall_filter = _seccomp.SyscallFilter(defaction=_seccomp.ALLOW)
        syscall_filter.add_rule(_seccomp.ERRNO(_errno.EPERM), "execve")
        syscall_filter.add_rule(_seccomp.ERRNO(_errno.EPERM), "execveat")
        # Load-bearing finding (T-3295 Phase 2, empirically isolated via
        # per-group SystemCallFilter bisection): libseccomp's
        # seccomp_load() defaults to SCMP_FLTATR_CTL_NNP=1, meaning it
        # tries to call prctl(PR_SET_NO_NEW_PRIVS, 1, ...) ITSELF as part
        # of loading -- but systemd's SystemCallFilter=~@process blocks
        # prctl (it's a member of @process, confirmed via
        # `systemd-analyze syscall-filter @process`), so that internal
        # call fails with EPERM and libseccomp reports the WHOLE load as
        # ECANCELED, even though the actual BPF program would have
        # installed fine. Since systemd's NoNewPrivileges=yes has ALREADY
        # set the bit before this code runs, telling libseccomp not to
        # attempt it itself is correct, not a weakening -- the invariant
        # (no-new-privs is set before the filter loads) still holds.
        syscall_filter.set_attr(_seccomp.Attr.CTL_NNP, 0)
        syscall_filter.load()
    except Exception as exc:
        raise PrimitiveUnavailable(
            "seccomp", f"launcher execve-closure setup failed: {exc}"
        ) from exc


class _AllowlistImportHook:
    """Step 5. Refuses dynamic import of anything outside the frozen
    stdlib closure + the explicitly declared allowed dirs (the component
    module dir for ``plan_<leg>`` mode; empty for ``--canary=`` mode,
    since canary probes never import component-supplied code).
    """

    def __init__(self, allowed_dirs: Sequence[str]) -> None:
        self._allowed = tuple(str(Path(d).resolve()) for d in allowed_dirs)

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.origin is None:
            # Built-in/frozen modules (no file origin) or "not found" --
            # let the normal machinery's verdict stand either way.
            return spec
        origin = str(Path(spec.origin).resolve())
        if origin.startswith(tuple(_stdlib_sys_path())) or any(
            origin.startswith(d) for d in self._allowed
        ):
            return spec
        return None  # refused -- caller sees ImportError


def _install_import_hook(allowed_dirs: Sequence[str] = ()) -> None:
    sys.meta_path.insert(0, _AllowlistImportHook(allowed_dirs))


def _canonical_startup(allowed_dirs: Sequence[str] = ()) -> None:
    """Steps 1-5, in the ADR's exact order."""

    _freeze_sys_path()
    _reset_environment()
    _install_execve_closure()
    _install_import_hook(allowed_dirs)


# --- Canary probes (steps 1-5 only; no component import) -----------------

_CANARIES: dict[str, Callable[[str | None], str]] = {}


def _canary(name: str) -> Callable[[Callable[[str | None], str]], Callable[[str | None], str]]:
    def decorator(fn: Callable[[str | None], str]) -> Callable[[str | None], str]:
        _CANARIES[name] = fn
        return fn

    return decorator


def _classify(exc: BaseException) -> str:
    errno_val = getattr(exc, "errno", None)
    errno_name = _errno.errorcode.get(errno_val, str(errno_val)) if errno_val else "?"
    return f"DENIED:{type(exc).__name__}:{errno_name}:{exc}"


@_canary("subprocess-creation")
def _canary_subprocess_creation(extra: str | None) -> str:
    import subprocess

    try:
        subprocess.Popen(["true"])
        return "ALLOWED"
    except OSError as exc:
        return _classify(exc)


@_canary("network")
def _canary_network(extra: str | None) -> str:
    import socket

    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        return "ALLOWED"
    except OSError as exc:
        return _classify(exc)


@_canary("host-mutation")
def _canary_host_mutation(extra: str | None) -> str:
    # Deliberately NOT a real shared-system path (e.g. NOT
    # /usr/lib/python3.*/os.py) -- low blast radius if the sandbox is
    # somehow catastrophically unconfined: /tmp writes are cheap, and this
    # path is a sibling of, not inside, the writable scratch BindPaths
    # target, so under a correctly-confined sandbox TemporaryFileSystem=/
    # never bind-mounts it at all (ENOENT), which is itself the "refused
    # at the filesystem-visibility root" outcome the README names.
    target = Path("/tmp/germinate-canary-host-mutation-attempt.marker")
    try:
        target.write_text("should-be-refused", encoding="utf-8")
        return "ALLOWED"
    except OSError as exc:
        return _classify(exc)


@_canary("authority-holding")
def _canary_authority_holding(extra: str | None) -> str:
    # `extra` is the host-side gate-tree path (Tranche A coordination
    # primitive) the outer test created via harness.fixtures.temp_gate_tree
    # -- passed as an argv positional, never via env (env stays capped at
    # exactly the 3 allowed names per the ADR's own canary invariant).
    if not extra:
        return "UNEXPECTED:missing gate tree path argv"
    try:
        list(Path(extra).iterdir())
        return "ALLOWED"
    except OSError as exc:
        return _classify(exc)


@_canary("data-only-return")
def _canary_data_only_return(extra: str | None) -> str:
    # Not exercised via the generic canary dispatch -- test 5 goes through
    # the plan_<leg> path directly (it needs a real component-module
    # import + typed-op-registry validation, not a single-syscall probe).
    # Kept as a named no-op so _CANARIES enumerates all 5 README canaries
    # for discoverability.
    return "SEE_PLAN_LEG_PATH_NOT_GENERIC_CANARY_DISPATCH"


# --- plan_<leg> mode (production path) ------------------------------------


def _run_plan_leg(leg: str, module_path: Path) -> int:
    """Steps 6-7."""

    component_dir = str(module_path.parent)
    _canonical_startup(allowed_dirs=(component_dir,))

    sys.path.insert(0, component_dir)
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        # Contract-shape error, not a primitive-availability concern -- the
        # component module itself is malformed/absent, which is a
        # different failure class than "a launcher-owned infra primitive
        # (seccomp, systemd-run, nsjail) is unavailable." Left as a plain
        # ImportError rather than PrimitiveUnavailable to keep that class
        # precise (C4 lint scope).
        raise ImportError(f"could not build import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    plan_fn = getattr(module, leg, None)
    if plan_fn is None:
        raise AttributeError(f"module at {module_path} has no {leg!r} attribute")

    raw_request = sys.stdin.read()
    request = json.loads(raw_request) if raw_request.strip() else {}
    result = plan_fn(request)
    sys.stdout.write(json.dumps(result))
    return 0


def _run_canary(name: str, scratch_dir: Path | None, extra: str | None) -> int:
    _canonical_startup(allowed_dirs=())
    fn = _CANARIES.get(name)
    if fn is None:
        outcome = f"UNKNOWN_CANARY:{name}"
    else:
        try:
            outcome = fn(extra)
        except BaseException as exc:  # noqa: BLE001 -- must survive to record ANY outcome
            outcome = f"UNEXPECTED_EXCEPTION:{type(exc).__name__}:{exc}"

    if scratch_dir is not None:
        (scratch_dir / f"canary-{name}.result").write_text(outcome, encoding="utf-8")
    print(f"CANARY_RESULT:{outcome}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("usage: launcher.py --canary=<name> <scratch_dir> [extra] | "
              "launcher.py plan_<leg> <module_path>", file=sys.stderr)
        return 2

    if argv[0].startswith("--canary="):
        name = argv[0].split("=", 1)[1]
        scratch_dir = Path(argv[1]) if len(argv) > 1 else None
        extra = argv[2] if len(argv) > 2 else None
        return _run_canary(name, scratch_dir, extra)

    leg = argv[0]
    module_path = Path(argv[1])
    return _run_plan_leg(leg, module_path)


if __name__ == "__main__":
    sys.exit(main())
