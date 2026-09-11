"""Real-host fixture helpers for the planner_sandbox reference-implementation.

Each fixture stands in for a production filesystem primitive named in
ORG-080 Base engine mechanics (gate tree, run-state store) or a generic
scratch region (sandbox bind-mount source), isolated under a temp root so
tests never touch the real /var/lib/pantheon paths. See T-3290 DECISIONS
for the layout rationale (ancestor-chain-only for the gate tree; leaf
records are the consuming tranche's responsibility).

"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: World-traversable ancestor-chain mode (ORG-080 Base engine mechanics
#: "Gate location" -- every directory level in the gate tree, root-owned in
#: production, mode 0755 world-readable-and-traversable).
_GATE_ANCESTOR_MODE = 0o755

#: Per-run scratch/record subtree mode (ORG-080 "File modes" -- owned by
#: the writing uid, not shared).
_PRIVATE_MODE = 0o700


@dataclass(frozen=True)
class GateTree:
    """Layout handle for a temp gate-tree root.

    Mirrors ORG-080 Base engine mechanics L306-327: a system-scope leaf plus
    zero or more seat-scope leaves, all under a shared ancestor chain. Only
    the ancestor chain is pre-created (mode 0755) -- per-component leaf
    ``_component_gate.json`` files are the admission tier's (tranche A's)
    responsibility, mirroring the ADR's bootstrap-vs-runtime split.
    """

    root: Path

    @property
    def gates_dir(self) -> Path:
        return self.root / "gates"

    @property
    def system_dir(self) -> Path:
        return self.gates_dir / "system"

    def seat_dir(self, uid: int) -> Path:
        return self.gates_dir / f"seat-{uid}"


@contextlib.contextmanager
def temp_gate_tree(seat_uids: Sequence[int] = ()) -> Iterator[GateTree]:
    """Real temp-dir standing in for the ORG-080 gate tree.

    Pre-creates ``gates/system/`` and one ``gates/seat-<uid>/`` per
    requested uid, all mode 0755 (world-traversable ancestor chain per the
    ADR's documented mode scheme). Component/leaf-level directories are NOT
    pre-created; callers (tranche A admission code) create those on first
    ``plan``, exactly as production bootstrap vs. runtime is split.

    Every level of the chain is created and chmod'd explicitly (NOT via
    ``mkdir(parents=True, mode=...)``, whose ``mode`` applies only to the
    final component -- intermediate parents get default, umask-affected
    permissions per the stdlib docs) so the mode-0755 guarantee holds at
    every level, matching the ADR's "ancestor tree ... is installed ...
    mode 0755" statement precisely.
    """

    tmp_root = Path(tempfile.mkdtemp(prefix="t3290-gate-tree-"))
    try:
        tmp_root.chmod(0o711)
        tree = GateTree(root=tmp_root)
        tree.gates_dir.mkdir()
        tree.gates_dir.chmod(_GATE_ANCESTOR_MODE)
        tree.system_dir.mkdir()
        tree.system_dir.chmod(_GATE_ANCESTOR_MODE)
        for uid in seat_uids:
            seat_dir = tree.seat_dir(uid)
            seat_dir.mkdir()
            seat_dir.chmod(_GATE_ANCESTOR_MODE)
        yield tree
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


@dataclass(frozen=True)
class StateStore:
    """Layout handle for a temp run-state store root.

    Mirrors ORG-080 Base engine mechanics L306-311:
    ``<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/
    record.json``. ``root`` here stands in for one ``<scope_root>`` at a
    time (system OR one seat) -- tests that need both scopes open two
    ``temp_state_store()``s.
    """

    root: Path

    @property
    def schema_root(self) -> Path:
        return self.root / "schema-v1"

    def record_path(
        self, instance: str, component: str, scope: str, leg: str, run_id: str
    ) -> Path:
        return (
            self.schema_root
            / instance
            / component
            / scope
            / leg
            / run_id
            / "record.json"
        )


@contextlib.contextmanager
def temp_scratch_dir(prefix: str = "t3290-scratch-") -> Iterator[Path]:
    """Real temp-dir standing in for a per-run scratch region.

    Mirrors the mode-0700 ownership the ADR gives per-run scratch/record
    subtrees (ORG-080 File modes). Used both as generic throwaway space and
    as the bind-mount source for ``run_in_sandbox()``'s scratch-writable
    region.
    """

    tmp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    tmp_dir.chmod(_PRIVATE_MODE)  # tempfile.mkdtemp already defaults to
    # 0700, but chmod explicitly so the guarantee doesn't depend on stdlib
    # implementation details.
    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@contextlib.contextmanager
def temp_state_store() -> Iterator[StateStore]:
    """Real temp-dir standing in for one run-state ``<scope_root>``.

    Pre-creates only the root; ``schema-v1/...`` subtrees are created by
    callers via ``record_path()`` + their own atomic temp-file+rename
    write, exercising the real write pattern tranche B's reconciler builds
    on.
    """

    tmp_root = Path(tempfile.mkdtemp(prefix="t3290-state-store-"))
    tmp_root.chmod(_PRIVATE_MODE)
    try:
        yield StateStore(root=tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
