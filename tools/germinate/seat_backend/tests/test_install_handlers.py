"""T-6167 PR-2: the dark live-seat handlers for the 37 persona-installer kinds.

(a) delegation over all 37 kinds through the backend core, with the installer op rebuilt from
provenance, roots re-derived from the trusted manifest and ids mapped back; (b) PARITY against
the installer's own ``LocalHostBackend`` on the same ops (identical results, argv and file
effects); (c) adversarial guards: a ``none`` op cannot mutate, the pin is exact, owner / root /
install-root / dark-pin refusals, and the pins in ``ports.py`` stay literal False.

Nothing here touches the real host: runners are recording fakes, file effects land under tmp
roots, and the write-trap test patches every mutating primitive.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import dataclasses
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import persona_installer as PI
from tools.germinate.engine.components import load_repo_op_registry
from tools.germinate.op_registry import load_op_registry
from tools.germinate.seat_backend import backend as B
from tools.germinate.seat_backend import install_handlers as IH
from tools.germinate.seat_backend import ports as P
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PreflightResult,
)

from tools.germinate.components.tests import test_harness_manager_install as HMI

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = load_repo_op_registry(REPO_ROOT)
MANIFEST = HMI._manifest()
UID = 4242
READ_ONLY = B.READ_ONLY_NONE_KINDS
SEAT_KINDS = IH.SEAT_INSTALL_KINDS
ALL_KINDS = sorted(READ_ONLY | SEAT_KINDS)
SEAT_BACKEND_DIR = Path(IH.__file__).resolve().parent


def _account(persona: str) -> str:
    return MANIFEST.binding(persona).account


def _pwnam(_name):
    return SimpleNamespace(pw_uid=UID)


def _handlers(persona="mason", *, local_factory=LocalHostBackend, euid=lambda: UID, pwnam=_pwnam):
    return IH.live_install_handlers(MANIFEST, persona, local_factory=local_factory, euid=euid, pwnam=pwnam)


def _core(tmp_path, persona="mason", handlers=None, registry=REGISTRY, **hk):
    root = Path(os.path.realpath(tmp_path)) / "state"
    root.mkdir(parents=True, exist_ok=True)
    ident = B.SeatIdentity(persona, _account(persona), UID, root)
    return B._SeatBackendCore(
        ident, object(), op_registry=registry, handlers=_handlers(persona, **hk) if handlers is None else handlers
    )


def _eng_op(kind, *, op_id=None, owner=None, persona="mason", privilege="unprivileged", provenance=None, target="t", desired=None):
    iid = f"conn.{kind}"
    prov = {"installer_operation_id": iid, "installer_privilege": "operator-required", "install_root": "/"}
    prov.update(provenance or {})
    return InstallOperation(
        op_id or iid.replace(".", "_"), 1, kind, target, {}, dict(desired or {}), None, privilege,
        owner or _account(persona), prov,
    )


class StubLocal:
    """A LocalHostBackend stand-in that records exactly what the bridge hands it."""

    def __init__(self):
        self.calls = []
        self.changed = False

    def preflight(self, op, context=None):
        self.calls.append(("preflight", op, context))
        return PreflightResult(True, "ok", "stub")

    def execute(self, op, context):
        self.calls.append(("execute", op, context))
        mutation = MutationRecord(op.operation_id, op.kind, op.target, op.owner, self.changed, {"state": "not-applicable"}, None)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, "stub")

    def verify_operation(self, op, *, staged_evidence=None):
        self.calls.append(("verify", op, None))
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "stub")

    def rollback_intent(self, op):
        self.calls.append(("intent", op, None))
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, False, {"state": "enabled"}, "timer-enabled")

    def can_rollback(self, mutation):
        self.calls.append(("can_rollback", mutation, None))
        return PreflightResult(True, "owned", "stub")

    def rollback_mutation(self, mutation, *, allowed_roots=()):
        self.calls.append(("rollback", mutation, tuple(allowed_roots)))


def _stub_core(tmp_path, persona="mason"):
    made = []

    def factory():
        made.append(StubLocal())
        return made[0]

    return _core(tmp_path, persona, local_factory=factory), made


# =============================================================================
# (c) delegation over all 37 kinds, through the core
# =============================================================================


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_kind_delegates_with_the_rebuilt_installer_op(kind, tmp_path):
    core, made = _stub_core(tmp_path)
    op = _eng_op(kind)
    assert core.handles(kind)
    assert core.preflight(op).ok
    mutation, evidence = core.execute(op)
    verified = core.verify_operation(op)
    assert (mutation.operation_id, evidence.operation_id, verified.operation_id) == (op.operation_id,) * 3
    assert len(made) == 1  # one LocalHostBackend per run
    (_, pre, pctx), (_, exe, ectx), (_, ver, _v) = made[0].calls
    for rebuilt in (pre, exe, ver):
        assert rebuilt.operation_id == f"conn.{kind}" and rebuilt.privilege == "operator-required"
        assert rebuilt.kind == kind and rebuilt.owner == _account("mason")
    derived = tuple(PI._derive_allowed_roots(MANIFEST, _account("mason"), Path("/")).split(","))
    assert pctx.workflow == ectx.workflow == "all-in-one" and pctx.allowed_roots == ectx.allowed_roots == derived


def test_the_local_backend_is_built_lazily_once_and_shared(tmp_path):
    made = []
    ro, mut = _handlers(local_factory=lambda: made.append(StubLocal()) or made[-1])
    assert made == []  # building handlers does no I/O
    ro.preflight(_eng_op("brief-check"))
    mut.preflight(_eng_op("mkdir"))
    assert len(made) == 1 and [c[0] for c in made[0].calls] == ["preflight", "preflight"]


def test_forged_provenance_allowed_roots_are_ignored(tmp_path):
    core, made = _stub_core(tmp_path)
    op = _eng_op("mkdir", provenance={PI.ALLOWED_TARGET_ROOTS_KEY: "/,/etc"})
    core.preflight(op)
    core.execute(op)
    roots = made[0].calls[1][2].allowed_roots
    assert roots == tuple(PI._derive_allowed_roots(MANIFEST, _account("mason"), Path("/")).split(","))
    assert "/" not in roots and "/etc" not in roots


def test_rollback_receipt_passes_through_with_manifest_roots(tmp_path):
    core, made = _stub_core(tmp_path)
    receipt = MutationRecord("conn_mkdir", "mkdir", "t", _account("mason"), True, {"state": "absent"}, "x")
    assert core.can_rollback(receipt).classification == "owned"
    core.rollback_mutation(receipt)
    assert made[0].calls[0] == ("can_rollback", receipt, None)
    assert made[0].calls[1][2] == tuple(PI._derive_allowed_roots(MANIFEST, _account("mason"), Path("/")).split(","))


def test_rollback_intent_native_for_timer_enable_maps_the_id_back(tmp_path):
    core, made = _stub_core(tmp_path)
    op = _eng_op("timer-enable")
    intent = core.rollback_intent(op)
    assert made[0].calls[0][1].operation_id == "conn.timer-enable"  # the installer's id reached the backend
    assert intent.operation_id == op.operation_id and intent.before["state"] == "enabled"


@pytest.mark.parametrize("kind", sorted(SEAT_KINDS - {"timer-enable", "mkdir"} - IH._FILE_KINDS))
def test_rollback_intent_is_intent_only_and_never_guessed(kind, tmp_path):
    core, made = _stub_core(tmp_path)
    op = _eng_op(kind)
    intent = core.rollback_intent(op)
    assert intent is not None and intent.changed and intent.before == {"state": "intent-only"}
    assert core.can_rollback(intent).classification == "drifted" and not core.can_rollback(intent).ok
    with pytest.raises(OperationFailed, match="no automatic inverse"):
        core.rollback_mutation(intent)
    assert made == [] or made[0].calls == []  # nothing reached the installer backend


# =============================================================================
# pre-image intent for file kinds and mkdir (real LocalHostBackend, tmp roots)
# =============================================================================


def _rerooted_bridge_core(tmp_path, persona="mason", rec=None, fs=None):
    """A core whose allowed roots are under tmp (the production root is Path('/'))."""
    tmp = Path(os.path.realpath(tmp_path))
    fs = tmp / "fs" if fs is None else fs
    account = _account(persona)
    bridge = IH._LocalBackendBridge(
        account, MANIFEST, local_factory=lambda: rec or Recorder(), root=fs, euid=lambda: UID, pwnam=_pwnam
    )
    handlers = (IH.ReadOnlyInstallHandler(bridge), IH.MutatingInstallHandler(bridge))
    return _core(tmp_path, persona, handlers=handlers), bridge, fs


def _file_op(kind, fs, persona="mason", content="hello\n", rel=None):
    account = _account(persona)
    target = str(fs / "home" / account / (rel or ".config/systemd/user/x.service"))
    return _eng_op(
        kind, persona=persona, target=target, desired={"content": content, "mode": "0644"},
        provenance={"installer_privilege": "privileged-helper"},
    )


@pytest.mark.parametrize("kind", sorted(IH._FILE_KINDS))
def test_file_kind_pre_image_intent_answers_not_applied_then_drifted(kind, tmp_path):
    core, bridge, fs = _rerooted_bridge_core(tmp_path)
    op = _file_op(kind, fs)
    target = Path(op.target)
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    intent = core.rollback_intent(op)
    assert intent.changed and intent.before["state"] == "file" and intent.after_fingerprint == IH.INTENT_SENTINEL
    assert core.can_rollback(intent).classification == "not-applied"  # untouched
    target.write_text("moved on\n", encoding="utf-8")
    check = core.can_rollback(intent)
    assert (check.ok, check.classification) == (False, "drifted")  # changed: escalate, never restore blindly


def test_file_kind_pre_image_refuses_a_target_outside_the_roots(tmp_path):
    core, bridge, fs = _rerooted_bridge_core(tmp_path)
    op = _file_op("atomic-write", fs)
    op = dataclasses.replace(op, target=str(Path(os.path.realpath(tmp_path)) / "elsewhere" / "x"))
    with pytest.raises(OperationFailed, match="target path check"):
        core.rollback_intent(op)


def test_mkdir_pre_image_intent_and_owned_mkdir_is_lstat_only(tmp_path):
    core, bridge, fs = _rerooted_bridge_core(tmp_path)
    plain = _file_op("mkdir", fs, rel="newdir")
    intent = core.rollback_intent(plain)
    assert intent.before == {"state": "absent"} and core.can_rollback(intent).classification == "not-applied"
    owned = _file_op("mkdir", fs, rel="owned")
    owned = dataclasses.replace(owned, desired={"owner": "x", "group": "y", "mode": "0750"})
    Path(owned.target).parent.mkdir(parents=True, exist_ok=True)
    Path(owned.target).symlink_to(tmp_path)  # a planted link: lstat sees "other", never reads through it
    intent = core.rollback_intent(owned)
    assert intent.before == {"state": "other", "nofollow_root": "/"}


# =============================================================================
# refusals at the bridge (owner, root, install_root, provenance, label)
# =============================================================================


@pytest.mark.parametrize("kind", ["mkdir", "brief-check"])
def test_owner_other_than_the_bound_account_is_refused_in_preflight_and_execute(kind, tmp_path):
    made = []
    ro, mut = _handlers(local_factory=lambda: made.append(StubLocal()) or made[-1])
    handler = ro if kind in READ_ONLY else mut
    op = _eng_op(kind, owner="someone-else")
    pre = handler.preflight(op)
    assert (pre.ok, pre.classification) == (False, "refused") and "someone-else" in pre.detail
    for call in (handler.execute, handler.verify_operation):
        with pytest.raises(OperationFailed, match="refused"):
            call(op)
    assert made == []


@pytest.mark.parametrize("kind", ["mkdir", "brief-check"])
def test_root_is_refused_before_the_local_backend_exists(kind):
    made = []
    ro, mut = _handlers(local_factory=lambda: made.append(StubLocal()) or made[-1], euid=lambda: 0)
    handler = ro if kind in READ_ONLY else mut
    op = _eng_op(kind)
    assert handler.preflight(op).classification == "refused"
    with pytest.raises(OperationFailed, match="root"):
        handler.execute(op)
    receipt = MutationRecord("o", kind, "t", _account("mason"), True, {"state": "absent"}, "x")
    if kind == "mkdir":
        assert mut.can_rollback(receipt).classification == "refused"
        with pytest.raises(OperationFailed, match="root"):
            mut.rollback_mutation(receipt)
    assert made == []


def test_another_uid_is_refused():
    ro, mut = _handlers(pwnam=lambda a: SimpleNamespace(pw_uid=UID + 1))
    assert mut.preflight(_eng_op("mkdir")).classification == "refused"


@pytest.mark.parametrize("root", ["/tmp/other-root", "", None])
def test_install_root_other_than_slash_is_refused(root):
    made = []
    ro, mut = _handlers(local_factory=lambda: made.append(StubLocal()) or made[-1])
    prov = {"install_root": root} if root is not None else {}
    op = _eng_op("mkdir", provenance=prov)
    if root is None:
        op = dataclasses.replace(op, provenance={k: v for k, v in op.provenance.items() if k != "install_root"})
    assert mut.preflight(op).classification == "refused"
    with pytest.raises(OperationFailed, match="install_root"):
        mut.execute(op)
    assert made == []


@pytest.mark.parametrize("drop", ["installer_operation_id", "installer_privilege"])
def test_missing_provenance_is_refused(drop):
    ro, mut = _handlers(local_factory=lambda: pytest.fail("local backend must not be built"))
    op = _eng_op("mkdir")
    op = dataclasses.replace(op, provenance={k: v for k, v in op.provenance.items() if k != drop})
    assert mut.preflight(op).classification == "refused"
    with pytest.raises(OperationFailed, match="refused"):
        mut.execute(op)


def test_an_unknown_installer_privilege_label_is_refused():
    ro, mut = _handlers(local_factory=lambda: pytest.fail("local backend must not be built"))
    op = _eng_op("mkdir", provenance={"installer_privilege": "root"})
    assert mut.preflight(op).classification == "refused"


def test_a_seat_kind_with_a_non_unprivileged_installer_privilege_is_refused_by_the_core(tmp_path):
    core, made = _stub_core(tmp_path)
    for privilege in ("privileged-helper", "operator-required"):
        op = _eng_op("mkdir", privilege=privilege)
        assert core.preflight(op).classification == "refused"
        with pytest.raises(OperationFailed, match="refused"):
            core.execute(op)
    assert made == []


def test_dark_pin_refuses_before_any_io_and_never_builds_the_local_backend(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("reached while dark")

    monkeypatch.setattr(LocalHostBackend, "__init__", boom)
    monkeypatch.setattr(B, "resolve_identity", boom)
    handlers = _handlers()
    with pytest.raises(P.LiveActuationDisabled, match="disabled"):
        B.LiveSeatBackend("artisan", object(), op_registry=REGISTRY, handlers=handlers)
    factory = B.live_seat_factory("artisan", object(), handlers)
    with pytest.raises(P.LiveActuationDisabled):
        factory(REGISTRY, ())


# =============================================================================
# (d1) a `none` op cannot mutate
# =============================================================================


def test_read_only_handler_has_no_rollback_intent_and_cannot_roll_back():
    ro, _mut = _handlers(local_factory=lambda: pytest.fail("must not be built"))
    assert not hasattr(ro, "rollback_intent")
    assert ro.kinds == READ_ONLY
    receipt = MutationRecord("o", "brief-check", "t", _account("mason"), False, {"state": "not-applicable"}, None)
    assert ro.can_rollback(receipt).classification == "not-applied"
    with pytest.raises(OperationFailed, match="nothing to roll back"):
        ro.rollback_mutation(receipt)


class _RollbackBomb:
    """A handler for a pinned none kind that blows up if the core ever routes a rollback verb to it."""

    name = "bomb"
    tier = "live-seat"
    kinds = frozenset({"brief-check"})
    changed = False

    def observe(self, component, scope):
        return {}

    def preflight(self, op):
        return PreflightResult(True, "ok", "x")

    def execute(self, op):
        return (
            MutationRecord(op.operation_id, op.kind, op.target, op.owner, self.changed, {"state": "not-applicable"}, None),
            OperationEvidence(op.operation_id, op.layer, op.kind, True, "x"),
        )

    def verify_operation(self, op):
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "x")

    def rollback_intent(self, op):
        raise AssertionError("rollback_intent reached a none-kind handler")

    def can_rollback(self, m):
        raise AssertionError("can_rollback reached a none-kind handler")

    def rollback_mutation(self, m):
        raise AssertionError("rollback_mutation reached a none-kind handler")


def test_core_never_routes_a_rollback_verb_to_a_none_kind_handler(tmp_path):
    core = _core(tmp_path, handlers=(_RollbackBomb(),))
    op = _eng_op("brief-check")
    assert core.rollback_intent(op) is None
    receipt = MutationRecord(op.operation_id, "brief-check", "t", _account("mason"), False, {"state": "not-applicable"}, None)
    check = core.can_rollback(receipt)
    assert (check.ok, check.classification) == (True, "not-applied")
    with pytest.raises(OperationFailed, match="nothing to roll back"):
        core.rollback_mutation(receipt)


def test_core_refuses_a_none_execute_that_reports_a_change(tmp_path):
    handler = _RollbackBomb()
    core = _core(tmp_path, handlers=(handler,))
    op = _eng_op("brief-check")
    core.execute(op)  # a quiet handler is fine
    handler.changed = True
    with pytest.raises(OperationFailed, match="read-only"):
        core.execute(op)


@pytest.mark.parametrize("bad", [
    {"changed": True, "before": {"state": "not-applicable"}},
    {"changed": False, "before": {"state": "file"}},
])
def test_read_only_handler_raises_when_the_installer_backend_reports_a_change(bad, tmp_path):
    class Lying(StubLocal):
        def execute(self, op, context):
            return (
                MutationRecord(op.operation_id, op.kind, op.target, op.owner, bad["changed"], bad["before"], "x"),
                OperationEvidence(op.operation_id, op.layer, op.kind, True, "x"),
            )

    ro, _ = _handlers(local_factory=Lying)
    with pytest.raises(OperationFailed, match="read-only kind"):
        ro.execute(_eng_op("brief-check"))


def test_unpinned_none_kind_is_refused_at_registration_and_at_every_op(tmp_path):
    synthetic = load_op_registry({"zz.synthetic-check": ("diagnostics.read", "none")})
    assert synthetic.get("zz.synthetic-check").privilege == "none"
    bomb = _RollbackBomb()
    bomb.kinds = frozenset({"zz.synthetic-check"})
    with pytest.raises(B.SeatBackendRefused, match="pinned read-only"):
        _core(tmp_path, handlers=(bomb,), registry=synthetic)
    core = _core(tmp_path, handlers=(), registry=synthetic)
    reason = core._refusal("zz.synthetic-check", _account("mason"), "unprivileged")
    assert reason is not None and "pinned read-only" in reason
    pre = core.preflight(_eng_op("zz.synthetic-check"))
    assert (pre.ok, pre.classification) == (False, "refused")


def test_a_handler_mixing_none_and_seat_kinds_is_refused(tmp_path):
    mixed = _RollbackBomb()
    mixed.kinds = frozenset({"brief-check", "mkdir"})
    with pytest.raises(B.SeatBackendRefused, match="mixes none and seat"):
        _core(tmp_path, handlers=(mixed,))


def test_the_ceiling_admits_none_and_seat_only_and_the_pin_is_exact():
    assert B.PRIVILEGE_CEILING == frozenset({"none", "seat"})
    assert B.READ_ONLY_NONE_KINDS == frozenset({
        "brief-check", "claude-mcp-server-check", "codex-standing-instructions-override-check",
        "codex-standing-instructions-source-check", "codex-standing-instructions-target-check",
        "codex-telegram-disabled-first", "harness-check", "identity-check", "manager-check",
        "policy-check", "source-check", "timer-enablement-absence-check",
    })
    assert {k for k, e in REGISTRY.entries.items() if e.privilege == "none"} == set(B.READ_ONLY_NONE_KINDS)


# --- the write trap: every mutating primitive is patched, over all 12 read-only kinds ----------

#: The read-only argv the installer's checks may run (argv[0] plus the systemctl / loginctl verb).
_READ_ONLY_ARGV = {
    ("systemctl", "--user", "show"), ("systemctl", "--user", "is-enabled"), ("systemctl", "--user", "is-active"),
    ("systemctl", "--user", "cat"), ("systemctl", "--user", "list-unit-files"), ("systemctl", "--user", "status"),
    ("systemctl", "--user", "list-units"), ("systemctl", "--user", "list-timers"),
    ("loginctl", "show-user"),
}


_GIT_READ_VERBS = {"rev-parse", "status", "ls-files", "show", "cat-file", "log", "diff", "ls-tree", "describe"}


def _argv_is_read_only(argv):
    argv = tuple(argv)
    if argv[:1] == ("git",):
        rest = [a for a in argv[1:] if not a.startswith("/") and a != "-C"]  # drop `-C <path>`
        return bool(rest) and rest[0] in _GIT_READ_VERBS
    return any(argv[: len(allowed)] == allowed for allowed in _READ_ONLY_ARGV) or argv[:1] in {("env",), ("/usr/bin/env",)} and _argv_is_read_only(argv[1:]) or (
        argv[:2] == ("claude", "mcp") and argv[2:3] in {("list",), ("get",)}
    )


class Recorder(LocalHostBackend):
    """A LocalHostBackend whose every process spawn is recorded and faked (success, empty output)."""

    def __init__(self):
        super().__init__()
        self.log = []

    def _run(self, argv, *, timeout=None):
        self.log.append(("run", tuple(argv)))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def _run_claude_mcp(self, args, *, home, phase):
        self.log.append(("claude-mcp", tuple(args), home, phase))
        if args and args[0] == "add-json":  # model the CLI's effect on ~/.claude.json
            doc_path = Path(home) / ".claude.json"
            doc = json.loads(doc_path.read_text(encoding="utf-8")) if doc_path.exists() else {}
            doc.setdefault("mcpServers", {})[args[3]] = json.loads(args[4])
            doc_path.write_text(json.dumps(doc), encoding="utf-8")
        return subprocess.CompletedProcess(["claude", "mcp", *args], 0, "", "")

    def _run_claude_mcp_remove(self, name, *, home, scope):
        self.log.append(("claude-mcp-remove", name, home, scope))
        return subprocess.CompletedProcess(["claude"], 0, "", "")


def _none_ops_from_real_plans():
    """One real engine-shaped op per read-only kind, from whichever persona plans it."""

    found = {}
    installer = PI.PersonaInstaller()
    for persona in HMI.PERSONAS:
        plan = installer.plan(MANIFEST, persona, source_revision=HMI.REV)
        for item in plan.operations:
            if item.kind in READ_ONLY and item.kind not in found:
                engine_id = HMI.HM._engine_op_id(item.operation_id)
                prov = dict(item.provenance) | {
                    "installer_operation_id": item.operation_id, "installer_privilege": item.privilege,
                }
                found[item.kind] = (persona, dataclasses.replace(item, operation_id=engine_id, privilege="unprivileged", provenance=prov))
    return found


@contextlib.contextmanager
def _write_trap(trapped, spawned):
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def deny(name):
        def _deny(*a, **k):
            trapped.append((name, a[:2]))
            raise AssertionError(f"write trap: {name}")
        return _deny

    def guard_open(real, name):
        def _open(file, mode="r", *a, **k):
            if any(c in str(mode) for c in "wax+"):
                trapped.append((name, (file, mode)))
                raise AssertionError(f"write trap: {name} {mode}")
            return real(file, mode, *a, **k)
        return _open

    def guard_os_open(file, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            trapped.append(("os.open", (file, flags)))
            raise AssertionError("write trap: os.open")
        return real_os_open(file, flags, *a, **k)

    def fake_run(argv, *a, **k):
        spawned.append(("run", tuple(argv)))  # recorded and faked: nothing really spawns
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    patches = [
        (os, "replace", deny("os.replace")), (os, "rename", deny("os.rename")), (os, "mkdir", deny("os.mkdir")),
        (os, "makedirs", deny("os.makedirs")), (os, "unlink", deny("os.unlink")), (os, "remove", deny("os.remove")),
        (os, "rmdir", deny("os.rmdir")), (os, "chmod", deny("os.chmod")), (os, "chown", deny("os.chown")),
        (os, "fchown", deny("os.fchown")), (os, "symlink", deny("os.symlink")), (os, "link", deny("os.link")),
        (os, "open", guard_os_open), (builtins, "open", guard_open(real_open, "open")),
        (io, "open", guard_open(real_io_open, "io.open")), (tempfile, "mkstemp", deny("mkstemp")),
        (Path, "write_text", deny("Path.write_text")), (Path, "write_bytes", deny("Path.write_bytes")),
        (Path, "mkdir", deny("Path.mkdir")), (Path, "touch", deny("Path.touch")), (Path, "unlink", deny("Path.unlink")),
        (Path, "rmdir", deny("Path.rmdir")), (Path, "rename", deny("Path.rename")), (Path, "replace", deny("Path.replace")),
        (Path, "symlink_to", deny("Path.symlink_to")),
        (subprocess, "run", fake_run), (subprocess, "Popen", deny("subprocess.Popen")),
    ]
    originals = [(obj, attr, getattr(obj, attr)) for obj, attr, _ in patches]
    for obj, attr, fake in patches:
        setattr(obj, attr, fake)
    try:
        yield
    finally:
        for obj, attr, original in originals:
            setattr(obj, attr, original)


def test_none_kinds_are_structurally_unable_to_mutate_over_all_twelve_real_ops():
    found = _none_ops_from_real_plans()
    assert set(found) == set(READ_ONLY)
    trapped, runs, spawned = [], [], []
    for kind in sorted(found):
        persona, op = found[kind]
        rec = Recorder()
        ro, _ = IH.live_install_handlers(
            MANIFEST, persona, local_factory=lambda rec=rec: rec, euid=lambda: UID, pwnam=_pwnam
        )
        # trap only the handler calls: the verb outcomes (a result or an installer refusal) both stand
        with _write_trap(trapped, spawned):
            for verb in (ro.preflight, ro.execute, ro.verify_operation):
                try:
                    verb(op)
                except AssertionError:
                    raise
                except Exception:  # noqa: BLE001 -- an installer refusal on this host is fine; a write is not
                    pass
        runs.extend(rec.log)
    assert trapped == [], trapped
    runs.extend(spawned)
    assert runs, "the read-only checks ran nothing at all: the allowlist assertion would be vacuous"
    bad = [entry for entry in runs if entry[0] == "run" and not _argv_is_read_only(entry[1])]
    assert bad == [], bad
    assert not [entry for entry in runs if entry[0] != "run"], runs  # no `claude mcp add/remove` from a check


# =============================================================================
# (d7) the dark pins stay literal False; nothing under seat_backend sets one True
# =============================================================================

PIN_NAME = "LIVE_ACTUATION_ENABLED"


def _pin_violations(source: str, *, declaration_allowed: bool) -> list[str]:
    """Findings in one module's source: a non-False pin literal, or any (re)binding of the pin."""

    tree = ast.parse(source)
    found: list[str] = []

    def names(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
        }

    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            if PIN_NAME not in names(target):
                continue
            if not (declaration_allowed and isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Name)):
                found.append(f"line {node.lineno}: rebinding of {PIN_NAME}")
                continue
            call = node.value
            table = ast.literal_eval(call.args[0]) if isinstance(call, ast.Call) and call.args else None
            if not isinstance(table, dict) or any(v is not False for v in table.values()):
                found.append(f"line {node.lineno}: {PIN_NAME} is not a literal all-False table")
    return found


def test_the_pins_in_ports_are_literal_false_and_nothing_else_binds_them():
    for path in sorted(SEAT_BACKEND_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert _pin_violations(source, declaration_allowed=path.name == "ports.py") == [], path.name
    assert dict(P.LIVE_ACTUATION_ENABLED) and all(v is False for v in P.LIVE_ACTUATION_ENABLED.values())


@pytest.mark.parametrize("source", [
    'LIVE_ACTUATION_ENABLED = MappingProxyType({"artisan": True})',
    'LIVE_ACTUATION_ENABLED = MappingProxyType({"artisan": False, "mason": 1})',
    'LIVE_ACTUATION_ENABLED = {"artisan": False}',
])
def test_the_pin_scan_catches_a_true_or_non_literal_pin(source):
    assert _pin_violations(source, declaration_allowed=True)


@pytest.mark.parametrize("source", [
    'LIVE_ACTUATION_ENABLED = MappingProxyType({"artisan": False})\nLIVE_ACTUATION_ENABLED = 1',
    'import x\nx.LIVE_ACTUATION_ENABLED = {"artisan": True}',
    'P.LIVE_ACTUATION_ENABLED["artisan"] = True',
])
def test_the_pin_scan_catches_a_rebinding_anywhere(source):
    assert _pin_violations(source, declaration_allowed=False)
    assert _pin_violations(source.replace("import x\n", ""), declaration_allowed=False)


# =============================================================================
# the import closure: nothing in the broker closure reaches the live seat backend
# =============================================================================


def test_host_control_and_engine_components_never_import_the_seat_backend():
    targets = [REPO_ROOT / "tools" / "germinate" / "engine" / "components.py"]
    targets += sorted((REPO_ROOT / "tools" / "host_control").rglob("*.py"))
    assert len(targets) > 3
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            assert not [m for m in mods if m.startswith("tools.germinate.seat_backend") or "seat_backend" in m.split(".")], (
                f"{path}: imports {mods}"
            )


# =============================================================================
# (b) PARITY against the installer's own LocalHostBackend
# =============================================================================


def _engine_shaped(item, persona=None):
    """The op the engine hands the backend for an installer op (``to_install_operation`` of the planned op)."""

    prov = dict(item.provenance) | {
        "installer_operation_id": item.operation_id, "installer_privilege": item.privilege,
    }
    return dataclasses.replace(
        item, operation_id=HMI.HM._engine_op_id(item.operation_id), privilege="unprivileged", provenance=prov
    )


def _installer_plan(persona):
    return PI.PersonaInstaller().plan(MANIFEST, persona, source_revision=HMI.REV)


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Nothing really spawns: every direct subprocess.run is recorded and answers success."""

    calls = []

    def fake_run(argv, *a, **k):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _norm(value, fs=None):
    def plain(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return plain(dataclasses.asdict(obj))
        if isinstance(obj, dict):
            return {k: ("<ID>" if k == "operation_id" else plain(v)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [plain(v) for v in obj]
        return obj

    text = json.dumps(plain(value), sort_keys=True, default=str)
    return text.replace(str(fs), "<FS>") if fs is not None else text


def _outcome(fn, fs=None):
    try:
        return ("ok", _norm(fn(), fs))
    except Exception as exc:  # noqa: BLE001 -- parity includes the failure shape
        text = f"{type(exc).__name__}: {exc}"
        return ("raise", text.replace(str(fs), "<FS>") if fs is not None else text)


def test_rebuild_is_the_identity_on_every_real_installer_op():
    """The engine op round-trips to exactly the installer op it was planned from (all 8 seats)."""

    for persona in HMI.PERSONAS:
        bridge = IH._LocalBackendBridge(_account(persona), MANIFEST, euid=lambda: UID, pwnam=_pwnam)
        for item in _installer_plan(persona).operations:
            assert bridge.rebuild(_engine_shaped(item)) == item, (persona, item.operation_id)


@pytest.mark.parametrize("persona", HMI.PERSONAS)
def test_preflight_parity_all_personas_real_plans(persona, tmp_path, fake_subprocess):
    plan = _installer_plan(persona)
    direct = Recorder()
    roots = tuple(PI._derive_allowed_roots(MANIFEST, _account(persona), Path("/")).split(","))
    context = ExecutionContext("all-in-one", roots)
    bridged = Recorder()
    core = _core(tmp_path, persona, local_factory=lambda: bridged)
    compared = 0
    for item in plan.operations:
        want = _outcome(lambda: direct.preflight(item, context))
        got = _outcome(lambda: core.preflight(_engine_shaped(item)))
        assert got == want, (persona, item.operation_id)
        compared += 1
    assert compared == len(plan.operations) > 40
    assert bridged.log == direct.log
    assert set(bridged.events) == set(direct.events)


def _reroot(item, fs, persona):
    """The installer op with every host path moved under ``fs`` (provenance install_root stays "/")."""

    account = _account(persona)
    moves = (("/home/post-office/mailbox", f"{fs}/hub"), (f"/home/{account}", f"{fs}/home/{account}"))

    def move(value):
        if isinstance(value, str):
            for old, new in moves:
                value = value.replace(old, new)
        return value

    return dataclasses.replace(
        item, target=move(item.target),
        precondition={k: move(v) for k, v in item.precondition.items()},
        desired={k: move(v) for k, v in item.desired.items()},
        inverse=None if item.inverse is None else {k: move(v) for k, v in item.inverse.items()},
    )


def _tree(fs):
    out = {}
    for path in sorted(Path(fs).rglob("*")):
        rel = str(path.relative_to(fs))
        out[rel] = "<link>" if path.is_symlink() else ("<dir>" if path.is_dir() else path.read_bytes())
    return out


def _parity_run(tmp_path, kinds, *, persona="mason", seed=None, steps=("execute",)):
    """Run the persona's ops of ``kinds`` through the installer backend directly and through the
    handlers (core), on two identical tmp trees; return both sides for comparison."""

    sides = {}
    for side in ("direct", "bridge"):
        # ONE path for both sides (file contents embed it, so fingerprints compare); wiped between runs
        fs = Path(os.path.realpath(tmp_path)) / "fs"
        if fs.exists():
            shutil.rmtree(fs)
        (fs / "home" / _account(persona)).mkdir(parents=True)
        (fs / "hub").mkdir()
        if seed:
            seed(fs, persona)
        rec = Recorder()
        core = None
        if side == "bridge":
            core, _bridge, _ = _rerooted_bridge_core(tmp_path / "core", persona, rec=rec, fs=fs)
        roots = tuple(PI._derive_allowed_roots(MANIFEST, _account(persona), fs).split(","))
        ops = [
            _reroot(item, fs, persona)
            for item in _installer_plan(persona).operations if item.kind in kinds
        ]
        assert ops, kinds
        results = []
        for item in ops:
            for step in steps:
                if side == "direct":
                    ctx = ExecutionContext("all-in-one", roots)
                    call = {
                        "preflight": lambda: rec.preflight(item, ctx),
                        "execute": lambda: rec.execute(item, ctx),
                        "verify": lambda: rec.verify_operation(item),
                    }[step]
                else:
                    eng = _engine_shaped(item)
                    call = {
                        "preflight": lambda: core.preflight(eng),
                        "execute": lambda: core.execute(eng),
                        "verify": lambda: core.verify_operation(eng),
                    }[step]
                results.append((item.kind, step, _outcome(call, fs)))
        sides[side] = SimpleNamespace(results=results, tree=_tree(fs), log=_norm(rec.log, fs), events=sorted(rec.events), fs=fs, rec=rec,
                                      ids=[i.operation_id for i in ops], core=core, roots=roots, ops=ops)
    return sides["direct"], sides["bridge"]


def _assert_parity(direct, bridge):
    assert bridge.results == direct.results
    assert bridge.tree == direct.tree
    assert bridge.log == direct.log
    assert bridge.events == direct.events


def test_execute_parity_files_and_mkdir(tmp_path, fake_subprocess):
    direct, bridge = _parity_run(tmp_path, IH._FILE_KINDS | {"mkdir"}, steps=("preflight", "execute", "verify", "preflight"))
    _assert_parity(direct, bridge)
    outcomes = [r[2][0] for r in direct.results]
    assert "ok" in outcomes and len(direct.results) > 40
    assert any(isinstance(v, bytes) for v in direct.tree.values())  # real files were written under the tmp root
    # cross-op state is keyed by the installer's own dotted ids, never the engine ids
    assert set(bridge.events) <= set(bridge.ids) and all("." in e for e in bridge.events)


def test_execute_parity_runner_kinds(tmp_path, fake_subprocess):
    kinds = {"daemon-reload", "seat-periodic-daemon-reload", "stale-unit-failure-reset", "timer-observe",
             "seat-periodic-timer-observe", "seat-service-enable-observe", "mailbox-writable-root-check"}
    direct, bridge = _parity_run(tmp_path, kinds, steps=("preflight", "execute"))
    _assert_parity(direct, bridge)
    assert direct.log != "[]"  # the fake runner saw argv


def test_execute_parity_settings_kinds(tmp_path, fake_subprocess):
    kinds = {"seat-claude-settings-env", "seat-claude-workspace-trust", "seat-codex-hook-config"}
    direct, bridge = _parity_run(tmp_path, kinds, steps=("preflight", "execute", "verify"))
    _assert_parity(direct, bridge)
    assert any(r[2][0] == "ok" for r in direct.results)


def test_execute_parity_mcp_kinds(tmp_path, fake_subprocess):
    kinds = {"claude-mcp-server-registration", "claude-mcp-server-check"}
    direct, bridge = _parity_run(tmp_path, kinds, steps=("preflight", "execute", "verify", "preflight"))
    _assert_parity(direct, bridge)
    assert '"claude-mcp"' in direct.log or "add-json" in direct.log


def test_execute_parity_cross_op_events_and_timer_chain(tmp_path, fake_subprocess):
    """timer-observe -> unit-fire -> timer-enable under rebuilt ids: the installer-keyed events
    the later ops read are the ones the earlier ops wrote, so the failure shape matches exactly."""

    kinds = {"timer-observe", "unit-fire", "timer-enable", "mailbox-probe", "direct-fire"}
    direct, bridge = _parity_run(tmp_path, kinds, steps=("preflight",))
    _assert_parity(direct, bridge)


def _rollback_scenario(tmp_path, side, drift, persona="mason"):
    """execute -> verify -> (drift) -> can_rollback -> rollback_mutation -> tree, on one side."""

    fs = Path(os.path.realpath(tmp_path)) / "fs"
    if fs.exists():
        shutil.rmtree(fs)
    (fs / "home" / _account(persona)).mkdir(parents=True)
    rec = Recorder()
    roots = tuple(PI._derive_allowed_roots(MANIFEST, _account(persona), fs).split(","))
    item = next(_reroot(i, fs, persona) for i in _installer_plan(persona).operations if i.kind == "atomic-write")
    ctx = ExecutionContext("all-in-one", roots)
    out = []
    if side == "direct":
        executed = rec.execute(item, ctx)
        out.append(_outcome(lambda: executed, fs))
        out.append(_outcome(lambda: rec.verify_operation(item), fs))
        receipt = executed[0]
        check = lambda: rec.can_rollback(receipt)  # noqa: E731
        undo = lambda: rec.rollback_mutation(receipt, allowed_roots=roots)  # noqa: E731
    else:
        core, _bridge, _ = _rerooted_bridge_core(tmp_path / "core", persona, rec=rec, fs=fs)
        eng = _engine_shaped(item)
        executed = core.execute(eng)
        assert executed[0].operation_id == eng.operation_id  # the receipt carries the engine id
        out.append(_outcome(lambda: executed, fs))
        out.append(_outcome(lambda: core.verify_operation(eng), fs))
        receipt = executed[0]
        check = lambda: core.can_rollback(receipt)  # noqa: E731
        undo = lambda: core.rollback_mutation(receipt)  # noqa: E731
    if drift:
        Path(item.target).write_bytes(b"drifted\n")
    out.append(_outcome(check, fs))
    out.append(_outcome(undo, fs))
    out.append(_tree(fs))
    return out


@pytest.mark.parametrize("drift", [False, True], ids=["owned", "drifted"])
def test_verify_and_rollback_parity_file(drift, tmp_path, fake_subprocess):
    want = _rollback_scenario(tmp_path, "direct", drift)
    got = _rollback_scenario(tmp_path, "bridge", drift)
    assert got == want
    check = json.loads(want[2][1])
    assert check["classification"] == ("drifted" if drift else "owned")


def test_rollback_intent_parity_timer_enable(tmp_path, fake_subprocess):
    d2, b2 = _parity_run(tmp_path / "t", {"timer-enable"}, steps=("preflight",))
    item_d, item_b = d2.ops[0], b2.ops[0]
    want = _outcome(lambda: d2.rec.rollback_intent(item_d), d2.fs)
    got = _outcome(lambda: b2.core.rollback_intent(_engine_shaped(item_b)), b2.fs)
    assert got == want
