"""Shared test helpers for T-5533 root-registered broker plans.

The broker (``tools.host_control.planner.HostPlanService``) no longer plans
in-process: it reads a plan the root installer registered in the seat
registry's ``plans`` list. Test fixtures that used to hand the broker an
``installer=`` now place the plan bytes under their trusted root and register
them with these helpers.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
from typing import Callable, Iterator, Mapping
from unittest import mock

from tools.persona_installer_contracts import InstallPlan, _canonical_json


def plan_bytes(plan: InstallPlan) -> bytes:
    """The canonical registered form of ``plan`` (what the producer emits)."""

    return _canonical_json(plan.to_dict()).encode("utf-8")


def write_plan_file(directory: Path, name: str, data: bytes) -> Path:
    path = Path(directory) / name
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def register_plan(
    registry: dict,
    directory: Path,
    plan: InstallPlan | bytes,
    *,
    seat_id: str,
    manifest_revision: str,
) -> dict:
    """Write ``plan`` (an InstallPlan or raw bytes) as
    ``directory/plan-<seat>-<revision>.json`` and add (or replace) its record in
    the ``registry`` dict. Returns the record."""

    data = plan if isinstance(plan, bytes) else plan_bytes(plan)
    path = write_plan_file(directory, f"plan-{seat_id}-{manifest_revision}.json", data)
    record = {
        "seat_id": seat_id,
        "manifest_revision": manifest_revision,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    plans = [
        item
        for item in registry.get("plans", [])
        if (item["seat_id"], item["manifest_revision"]) != (seat_id, manifest_revision)
    ]
    plans.append(record)
    registry["plans"] = plans
    return record


def _real_read_plan_bytes():
    from tools.host_control.registry import SeatRegistry

    return SeatRegistry.__dict__["read_plan_bytes"]


_REAL_READ_PLAN_BYTES = _real_read_plan_bytes()


_ACTIVE_TRANSFORMS: list[Callable[[InstallPlan], InstallPlan]] = []


@contextlib.contextmanager
def registered_plan_transform(
    transform: Callable[[InstallPlan], InstallPlan],
    *,
    compose: bool = True,
) -> Iterator[None]:
    """Serve ``transform(<the really registered plan>)`` as the registered plan.

    For tests that used to wrap ``installer.plan`` to hand the broker a crafted
    plan (narrowed kinds, injected kinds). The real registry read still runs
    first (registered record, root-owned loader, sha256); the crafted plan's
    canonical bytes are then returned, so the broker still runs
    ``parse_registered_plan`` and every later check on them.

    Nesting mirrors the old nested ``installer.plan`` wrappers: by default an
    inner transform runs on the OUTER transforms' result (``compose=True``, as a
    wrapper of an already-patched ``installer.plan`` did); ``compose=False``
    starts from the unmodified registered plan (as wrapping a fresh, unpatched
    ``PersonaInstaller(...).plan`` did).
    """

    from tools.host_control.planner import parse_registered_plan
    from tools.host_control.registry import SeatRegistry

    outer = tuple(_ACTIVE_TRANSFORMS) if compose else ()

    def read(self, record):
        plan = parse_registered_plan(_REAL_READ_PLAN_BYTES(self, record))
        for step in (*outer, transform):
            plan = step(plan)
        return plan_bytes(plan)

    _ACTIVE_TRANSFORMS.append(transform)
    try:
        with mock.patch.object(SeatRegistry, "read_plan_bytes", read):
            yield
    finally:
        _ACTIVE_TRANSFORMS.pop()


def seat_entry(registry: Mapping, seat_id: str) -> dict:
    return dict(next(item for item in registry["seats"] if item["seat_id"] == seat_id))


def manifest_entry(registry: Mapping, revision: str) -> dict:
    return dict(next(item for item in registry["manifests"] if item["revision"] == revision))


def framework_overlay(dest: Path, repo_root: Path, template_ids: tuple[str, ...]) -> Path:
    """A throwaway framework tree at ``dest`` for digest-staleness tests: the
    framework.source artifacts (FRAMEWORK_CODE_ARTIFACTS plus ``template_ids``)
    are REAL copies, so a test can edit them; every other entry is a symlink into
    ``repo_root``. ``dest`` must not exist."""

    import shutil

    from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS

    copied = {Path(item) for item in (*FRAMEWORK_CODE_ARTIFACTS, *template_ids)}
    real_dirs = {parent for item in copied for parent in item.parents if parent != Path(".")}

    def build(relative: Path) -> None:
        (dest / relative).mkdir(mode=0o700)
        for entry in sorted((repo_root / relative).iterdir()):
            child = relative / entry.name
            if entry.name in (".git", "__pycache__"):
                continue
            if child in real_dirs:
                build(child)
            elif child in copied:
                shutil.copyfile(entry, dest / child)
                (dest / child).chmod(0o600)
            else:
                (dest / child).symlink_to(entry)

    build(Path("."))
    return dest
