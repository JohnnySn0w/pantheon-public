"""T-5940 (S5): the dark live seat backend. Pinned refusal before any I/O, the seat
binding (one persona, its own account, never root), per-seat scope refusal, the
seat-only privilege refusal before a handler is reached, handler admission, and the
engine wiring (discovered simulated handlers discarded, no preflight port)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools._organ_lifecycle import FakeCommandRunner
from tools.germinate.engine.backend import BackendRefused
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_seam import _component
from tools.germinate.op_registry import load_op_registry
from tools.germinate.planner_sandbox.postboot import marker as _marker
from tools.germinate.seat_backend import backend as B
from tools.germinate.seat_backend import ports as P
from tools.persona_installer import InstallOperation, MutationRecord, OperationEvidence, OperationFailed, PreflightResult

KIND = "repo.ff-update"
REGISTRY = load_op_registry({KIND: ("repo.local", "seat")})
# sorted: REGISTRY.entries order is hash-seeded, and xdist workers differ (#1509 collection mismatch)
ROOT_KIND = min(k for k, e in REGISTRY.entries.items() if e.privilege == "root")
NONE_KIND = min(k for k, e in REGISTRY.entries.items() if e.privilege == "none")
ACCOUNT = "suborch2"


class _Ops(P._LiveSeatOps):
    def __init__(self, runner, state_root):
        self._runner, self._uid, self._state_root = runner, 1009, Path(state_root)


class LiveHandler:
    """A fake live-seat handler that records every call it receives."""

    name = "fake-live"
    tier = "live-seat"
    kinds = frozenset({KIND})

    def __init__(self):
        self.calls = []

    def observe(self, component, scope):
        self.calls.append(("observe", scope))
        return {"seen": scope}

    def preflight(self, op):
        self.calls.append(("preflight", op.operation_id))
        return PreflightResult(True, "ok", "fake")

    def execute(self, op):
        self.calls.append(("execute", op.operation_id))
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {}, "x"), OperationEvidence(
            op.operation_id, op.layer, op.kind, True, "fake")

    def verify_operation(self, op):
        self.calls.append(("verify", op.operation_id))
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "fake")

    def rollback_intent(self, op):
        self.calls.append(("intent", op.operation_id))
        return None

    def can_rollback(self, m):
        self.calls.append(("can_rollback", m.operation_id))
        return PreflightResult(True, "owned", "fake")

    def rollback_mutation(self, m):
        self.calls.append(("rollback", m.operation_id))


def _identity(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "state"
    root.mkdir(exist_ok=True)
    return B.SeatIdentity("artisan", ACCOUNT, 1009, root)


def _core(tmp_path, handlers=(), registry=REGISTRY):
    ident = _identity(tmp_path)
    return B._SeatBackendCore(ident, _Ops(FakeCommandRunner(), ident.state_root), op_registry=registry, handlers=handlers)


def _op(kind=KIND, owner=ACCOUNT, privilege="unprivileged", op_id="op-1"):
    return InstallOperation(op_id, 1, kind, "t", {}, {}, None, privilege, owner, {})


# --- the dark gate -------------------------------------------------------------------


@pytest.mark.parametrize("persona", ["artisan", "mason", "nobody"])
def test_live_backend_refuses_while_pinned_before_any_io(persona, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("resolve_identity reached while dark")

    monkeypatch.setattr(B, "resolve_identity", boom)
    runner = FakeCommandRunner()
    with pytest.raises(P.LiveActuationDisabled, match="disabled"):
        B.LiveSeatBackend(persona, runner, op_registry=REGISTRY, handlers=(LiveHandler(),))
    assert runner.calls == []


def test_live_factory_through_from_checkout_refuses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.make_checkout(repo, [H.row("comp-r", _component(repo))])
    runner = FakeCommandRunner()
    with pytest.raises(BackendRefused, match="disabled for 'artisan'"):
        Engine.from_checkout(None, repo_root=repo, backend_factory=B.live_seat_factory("artisan", runner, (LiveHandler(),)))
    assert runner.calls == []


# --- the seat binding ----------------------------------------------------------------


def _pw(uid, home):
    return SimpleNamespace(pw_uid=uid, pw_dir=str(home))


def _resolve(tmp_path, *, binding=None, uid=1009, euid=1009, state=None):
    home = Path(os.path.realpath(tmp_path)) / "home"
    home.mkdir(exist_ok=True)
    return B.resolve_identity(
        "artisan",
        binding=lambda p: {"artisan": {"account": ACCOUNT}}.get(p, {}) if binding is None else binding,
        pwnam=lambda a: _pw(uid, home) if a == ACCOUNT else (_ for _ in ()).throw(KeyError(a)),
        euid=lambda: euid,
        state_root=(lambda default: default) if state is None else (lambda default: state),
    )


def test_identity_binds_the_persona_to_its_own_account(tmp_path):
    ident = _resolve(tmp_path)
    assert (ident.persona, ident.account, ident.uid) == ("artisan", ACCOUNT, 1009)
    assert ident.state_root == Path(os.path.realpath(tmp_path)) / "home" / ".local" / "state" / "pantheon"


def test_identity_refuses_root(tmp_path):
    with pytest.raises(B.SeatBackendRefused, match="root"):
        _resolve(tmp_path, euid=0, uid=0)


def test_identity_refuses_another_account(tmp_path):
    """An artisan binding run from any other account (e.g. mason's suborch1, uid 1007) refuses."""

    with pytest.raises(B.SeatBackendRefused, match="runs as uid 1007"):
        _resolve(tmp_path, euid=1007)


def test_identity_refuses_an_unbound_persona(tmp_path):
    with pytest.raises(B.SeatBackendRefused, match="no instance.yaml account"):
        _resolve(tmp_path, binding={})


def test_identity_refuses_a_state_root_outside_the_seat_home(tmp_path):
    with pytest.raises(B.SeatBackendRefused, match="outside the seat's own home"):
        _resolve(tmp_path, state=tmp_path / "elsewhere")


# --- scope ---------------------------------------------------------------------------


def test_scope_is_the_bound_seat_only(tmp_path):
    core = _core(tmp_path)
    assert core.validate_scope("seat-suborch2") == "seat-suborch2"
    for bad in ("seat-suborch1", "system", "seat-", "seat-suborch2x"):
        with pytest.raises(B.SeatBackendRefused):
            core.validate_scope(bad)
    assert core.privilege_ceiling == frozenset({"none", "seat"}) and core.bound_account == ACCOUNT
    assert core.evidence_scope == "live-seat"
    assert core.postboot_marker_path == _marker.MARKER_PATH and core.boot_id_path == _marker.BOOT_ID_PATH


def test_run_state_roots(tmp_path):
    core = _core(tmp_path)
    assert core.run_state_root("system") == core.root / "run-state"
    with pytest.raises(B.SeatBackendRefused):
        core.ensure_run_state_root("system")
    with pytest.raises(B.SeatBackendRefused):
        core.run_state_root("seat-suborch1")
    made = core.ensure_run_state_root("seat-suborch2")
    assert made == core.root / "seat-state" / "seat-suborch2" / "run-state" and made.is_dir()
    for path in (core.root, made.parent.parent, made.parent, made):
        assert (path.stat().st_mode & 0o777) == 0o700
    assert not (core.root / "run-state").exists()


# --- handlers ------------------------------------------------------------------------


def test_only_live_tier_handlers_register(tmp_path):
    sim = LiveHandler()
    sim.tier = None
    with pytest.raises(B.SeatBackendRefused, match="not a 'live-seat' handler"):
        _core(tmp_path, (sim,))


# NONE_KIND is a pinned read-only kind since T-6167 PR-2 and may register; an UNPINNED none kind
# refuses (test_install_handlers.py::test_unpinned_none_kind_*).
@pytest.mark.parametrize("kind", [ROOT_KIND, "no.such-kind"])
def test_a_handler_for_a_non_seat_kind_refuses(kind, tmp_path):
    handler = LiveHandler()
    handler.kinds = frozenset({kind})
    with pytest.raises(B.SeatBackendRefused):
        _core(tmp_path, (handler,))


# --- per-op refusal (nothing reaches the handler) ------------------------------------


@pytest.mark.parametrize("op", [
    _op(owner="suborch1"),  # another seat's op
    _op(kind=ROOT_KIND),  # declared root
    _op(kind=NONE_KIND),  # declared none (read-only)
    _op(privilege="privileged-helper"),
    _op(kind="no.such-kind"),
], ids=["other-owner", "root", "none", "installer-privileged", "unknown"])
def test_non_seat_or_foreign_ops_are_refused_before_the_handler(op, tmp_path):
    handler = LiveHandler()
    core = _core(tmp_path, (handler,))
    pre = core.preflight(op)
    assert not pre.ok and pre.classification == "refused"
    for call in (core.execute, core.verify_operation, core.rollback_intent):
        with pytest.raises(OperationFailed, match="refused"):
            call(op)
    if op.privilege == "unprivileged":
        # a MutationRecord carries kind and owner, not privilege: the undo path re-checks those
        # (the installer privilege was checked when the mutation's op was admitted)
        mutation = MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {}, "x")
        check = core.can_rollback(mutation)
        assert not check.ok and check.classification == "refused"
        with pytest.raises(OperationFailed, match="refused"):
            core.rollback_mutation(mutation)
    assert handler.calls == []


def test_an_admissible_op_reaches_the_live_handler(tmp_path):
    handler = LiveHandler()
    core = _core(tmp_path, (handler,))
    op = _op()
    assert core.handles(KIND) and core.preflight(op).ok
    core.execute(op)
    core.verify_operation(op)
    core.can_rollback(MutationRecord("op-1", KIND, "t", ACCOUNT, True, {}, "x"))
    assert core.observe("comp-r", "seat-suborch2", frozenset({KIND})) == {"fake-live": {"seen": "seat-suborch2"}}
    with pytest.raises(B.SeatBackendRefused):
        core.observe("comp-r", "seat-suborch1", frozenset({KIND}))
    assert [c[0] for c in handler.calls] == ["preflight", "execute", "verify", "can_rollback", "observe"]


# --- engine wiring (a test-built core; the production class is dark) ------------------


@pytest.fixture
def postboot_done(tmp_path, monkeypatch):
    boot = tmp_path / "boot-id"
    boot.write_text("00000000-0000-4000-8000-000000000001\n", encoding="utf-8")
    marker_path = tmp_path / "run" / "postboot-complete.marker"
    marker_path.parent.mkdir()
    monkeypatch.setattr(_marker, "MARKER_PATH", marker_path)
    monkeypatch.setattr(_marker, "BOOT_ID_PATH", boot)
    _marker.write_marker(_marker.current_boot_id(boot), marker_path)


def _engine(tmp_path, handlers=()):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.make_checkout(repo, [H.row("comp-r", _component(repo))])
    got = {}

    def factory(op_registry, discovered):
        got["discovered"] = [h.name for h in discovered]
        ident = _identity(tmp_path)
        return B._SeatBackendCore(ident, _Ops(FakeCommandRunner(), ident.state_root),
                                  op_registry=op_registry, handlers=handlers)

    return Engine.from_checkout(None, repo_root=repo, backend_factory=factory), got


def test_engine_on_a_seat_core_discards_sim_handlers_and_has_no_preflight_port(tmp_path, postboot_done):
    eng, got = _engine(tmp_path)
    assert got["discovered"] == ["repo-sim"]  # handed to the factory ...
    assert not eng.backend.handles(KIND)  # ... and never registered
    assert eng.preflight_port is None
    # the planner reads the simulated handler's observation, which a live backend never makes
    assert eng.backend.observe("comp-r", "seat-suborch2", frozenset({KIND})) == {}
    with pytest.raises(EngineRefusal, match="plan-refused"):
        eng.plan("install", "seat-suborch2", "comp-r")


def test_engine_on_a_seat_core_refuses_another_scope(tmp_path, postboot_done):
    eng, _got = _engine(tmp_path)
    for scope in ("seat-suborch1", "system"):
        with pytest.raises(EngineRefusal, match="scope-refused"):
            eng.plan("install", scope, "comp-r")
