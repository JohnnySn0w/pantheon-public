"""T-5133: the real git-backed pantheon-config EffectHandler (dark).

Every repo here is real git under pytest ``tmp_path``: a local bare "remote"
(``tools.git_fixture.BubbleGit``) plus clones inside a ``guard_sim_root``-vetted
sandbox. No real checkout path is ever named. Git config is made hermetic for
the handler's and the reconciler's subprocesses (GIT_CONFIG_GLOBAL /
GIT_CONFIG_NOSYSTEM via monkeypatch); the fixture builder keeps its own
hermetic identity env.
"""

from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.checkout_spec import CheckoutEntry, CheckoutMode, CheckoutSpec
from tools.germinate.components import pantheon_config_git as PG
from tools.germinate.engine.backend import KILL_POINTS, SimRootRefused, SimulatedHostBackend, SimulatedKill
from tools.germinate.engine.components import discover, merged_kinds
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.git_fixture import BubbleGit
from tools.git_fixture import git as fgit
from tools.persona_installer import InstallOperation, OperationFailed
from tools import repo_reconciler as RR

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("pantheon-config.yaml", "pantheon-config.py", "pantheon-config_handlers.py")
CID = "pantheon-config"
CK = "pantheon-config-test"
SIM_URL = "sim://pantheon-config"


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


# --- fixtures -----------------------------------------------------------------


class World:
    """A bare remote seeded with commit A, a sandbox, and a handler over one checkout."""

    def __init__(self, tmp_path: Path, *, health=None):
        self.tmp = tmp_path
        self.bubble = BubbleGit(tmp_path / "bubble")
        Path(self.bubble.root).mkdir(parents=True, exist_ok=True)
        self.remote = self.bubble.new_remote()
        self.A = self.bubble.seed(self.remote, content="v1")
        self.sandbox = tmp_path / "sandbox"
        self.url = self.remote
        self.target = str(Path(os.path.realpath(tmp_path)) / "sandbox" / "ck")
        self.health = health
        self.handler = self.make_handler()

    def entry(self, target=None) -> CheckoutEntry:
        return CheckoutEntry(
            checkout_id=CK,
            logical_repo="pantheon-config",
            remote_ref="manifest",
            target_account="role:engineering-orchestrator-2",
            target_path=target or self.target,
            mode=CheckoutMode(tracking="main"),
            update_policy="ff-only",
        )

    def make_handler(self, target=None):
        return PG.PantheonConfigRepoGit(
            CheckoutSpec({CK: self.entry(target)}),
            remote_resolver=lambda _r: self.url,
            sandbox_root=self.sandbox,
            health_probe=self.health,
        )

    def advance(self, content):
        return self.bubble.advance_remote(self.remote, content=content)

    def head(self):
        return fgit("rev-parse", "HEAD", cwd=Path(self.target)).stdout.strip()


def _op(kind, *, pre=None, desired=None, op_id=None):
    return InstallOperation(op_id or f"{kind}-{CK}", 1, kind, CK, pre or {}, desired or {}, None, "seat", "role:x", {})


def _run(handler, op):
    """The executor's order: preflight, write-ahead inverse, execute, verify."""

    pre = handler.preflight(op)
    assert pre.ok, pre.detail
    intent = handler.rollback_intent(op)
    mutation, _ev = handler.execute(op)
    assert mutation == intent
    return mutation, handler.verify_operation(op)


def _installed(w: World):
    mutation, ev = _run(w.handler, _op("repo.clone", pre={"exists": "false"}, desired={"head": w.A}))
    assert ev.ok, ev.detail
    return mutation


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- clone --------------------------------------------------------------------


def test_clone_installs_at_the_target_and_records_it(world):
    _installed(world)
    assert world.head() == world.A
    assert world.handler.installs[CK]["installed_sha"] == world.A
    raw = world.handler.observe(CID, "system")["checkouts"][CK]
    assert (raw["exists"], raw["is_managed"], raw["dirty"], raw["installed_sha"], raw["desired_target"]) == (True, True, False, world.A, world.A)


def test_clone_of_a_pin_behind_the_tip_is_refused_and_leaves_nothing(world):
    world.advance("v2")
    op = _op("repo.clone", pre={"exists": "false"}, desired={"head": world.A})
    assert not world.handler.preflight(op).ok
    with pytest.raises(OperationFailed, match="missing-ref"):  # the reconciler: a non-tip pin is absent on the remote
        world.handler.execute(op)
    assert not Path(world.target).exists() and CK not in world.handler.installs


def test_clone_landing_on_the_wrong_commit_is_preserved_for_supervised_cleanup(world):
    world.advance("v2")
    S = _side_branch(world)  # a tip, but not of the default branch: clone lands on main's tip
    op = _op("repo.clone", pre={"exists": "false"}, desired={"head": S})
    assert not world.handler.preflight(op).ok
    with pytest.raises(OperationFailed, match="clone landed on .*left in place"):
        world.handler.execute(op)
    assert Path(world.target).exists() and CK not in world.handler.installs


def test_failed_clone_race_preserves_foreign_content_and_receipt_refusal(world, monkeypatch):
    world.advance("v2")
    side = _side_branch(world)
    original = RR._run_git
    def raced(args, **kwargs):
        result = original(args, **kwargs)
        if args and args[0] == "clone" and result.returncode == 0:
            Path(world.target, "foreign.txt").write_text("do not delete", encoding="utf-8")
        return result
    monkeypatch.setattr(RR, "_run_git", raced)
    with pytest.raises(OperationFailed, match="left in place"):
        world.handler.execute(_op("repo.clone", pre={"exists": "false"}, desired={"head": side}))
    assert Path(world.target, "foreign.txt").read_text(encoding="utf-8") == "do not delete"


# --- ff-advance -------------------------------------------------------------------


def test_ff_advance_moves_head_and_verifies(world):
    _installed(world)
    B = world.advance("v2")
    mutation, ev = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    assert ev.ok and "current" in ev.detail
    assert world.head() == B and mutation.before == {"head": world.A, "ref": "refs/heads/main"} and mutation.after_fingerprint == B


def test_ff_rollback_rewinds_own_clean_advance(world):
    _installed(world)
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (True, "owned")
    world.handler.rollback_mutation(mutation)
    assert world.head() == world.A
    assert (Path(world.target) / "file.txt").read_text() == "v1"
    assert fgit("status", "--porcelain", "--untracked-files=all", cwd=Path(world.target)).stdout == ""
    assert world.handler.can_rollback(mutation).classification == "not-applied"


def _drift(w: World, how: str, B: str):
    t = Path(w.target)
    if how == "moved":
        w.advance("v3")
        fgit("pull", "--ff-only", "origin", "main", cwd=t)
    elif how == "dirty":
        (t / "file.txt").write_text("edited", encoding="utf-8")
    elif how == "untracked":
        (t / "stray.txt").write_text("x", encoding="utf-8")
    elif how == "local-commit":
        (t / "file.txt").write_text("local", encoding="utf-8")
        fgit("commit", "-am", "local", cwd=t)


@pytest.mark.parametrize("how", ["moved", "dirty", "untracked", "local-commit"])
def test_ff_rollback_refuses_drift(world, how):
    _installed(world)
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    _drift(world, how, B)
    before_head = world.head()
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (False, "drifted")
    with pytest.raises(OperationFailed, match="drifted"):  # the re-check right before the rewind
        world.handler.rollback_mutation(mutation)
    assert world.head() == before_head


def test_ff_rollback_after_someone_else_rewound_is_not_applied(world):
    _installed(world)
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    fgit("reset", "--hard", world.A, cwd=Path(world.target))
    assert world.handler.can_rollback(mutation).classification == "not-applied"


def _side_branch(w: World) -> str:
    """A remote branch off A that does not descend from main's tip."""

    work = Path(w.bubble.path("_side_work"))
    fgit("clone", w.remote, str(work))
    fgit("checkout", "-b", "side", w.A, cwd=work)
    (work / "side.txt").write_text("s", encoding="utf-8")
    fgit("add", "side.txt", cwd=work)
    fgit("commit", "-m", "side", cwd=work)
    fgit("push", "origin", "side", cwd=work)
    return fgit("rev-parse", "HEAD", cwd=work).stdout.strip()


def test_ff_rollback_refuses_a_preimage_that_is_not_an_ancestor(world):
    """A record whose preimage is not in HEAD's history (corrupt or foreign) is never 'rewound' to."""

    import dataclasses

    _installed(world)
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    S = _side_branch(world)
    fgit("fetch", "origin", "side", cwd=Path(world.target))
    forged = dataclasses.replace(mutation, before={"head": S, "ref": "refs/heads/main"})
    check = world.handler.can_rollback(forged)
    assert (check.ok, check.classification) == (False, "drifted") and "ancestor" in check.detail
    with pytest.raises(OperationFailed, match="drifted"):
        world.handler.rollback_mutation(forged)
    assert world.head() == B


def test_non_ff_target_not_local_is_refused_by_merge_ff_only(world):
    _installed(world)
    B = world.advance("v2")
    _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    S = _side_branch(world)
    op = _op("repo.ff-advance", pre={"head": B}, desired={"head": S})
    assert world.handler.preflight(op).ok  # unprovable locally: deferred to merge --ff-only
    mutation = world.handler.rollback_intent(op)
    with pytest.raises(OperationFailed, match="merge --ff-only refused"):
        world.handler.execute(op)
    assert world.head() == B
    assert world.handler.can_rollback(mutation).classification == "not-applied"


def test_non_ff_target_present_locally_is_refused_at_preflight(world):
    _installed(world)
    B = world.advance("v2")
    _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    S = _side_branch(world)
    fgit("fetch", "origin", "side", cwd=Path(world.target))
    op = _op("repo.ff-advance", pre={"head": B}, desired={"head": S})
    pre = world.handler.preflight(op)
    assert not pre.ok and "fast-forward" in pre.detail
    with pytest.raises(OperationFailed, match="diverged"):
        world.handler.execute(op)
    assert world.head() == B


def test_unreachable_remote_refuses_and_touches_nothing(world):
    _installed(world)
    B = world.advance("v2")
    world.url = str(world.tmp / "bubble" / "gone.git")
    op = _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B})
    pre = world.handler.preflight(op)
    assert not pre.ok and "unreachable" in pre.detail
    with pytest.raises(OperationFailed, match="credential-fail"):
        world.handler.execute(op)
    assert world.head() == world.A
    raw = world.handler.observe(CID, "system")["checkouts"][CK]
    assert raw["remote_reachable"] is False and raw["desired_target"] is None


# --- clone rollback ----------------------------------------------------------------


def test_clone_rollback_reclaims_a_pristine_clone(world):
    mutation = _installed(world)
    assert world.handler.can_rollback(mutation).classification == "owned"
    world.handler.rollback_mutation(mutation)
    assert not Path(world.target).exists() and CK not in world.handler.installs
    assert world.handler.can_rollback(mutation).classification == "not-applied"


@pytest.mark.parametrize("how", ["dirty", "untracked", "local-commit"])
def test_clone_rollback_refuses_a_changed_clone(world, how):
    mutation = _installed(world)
    _drift(world, how, None)
    check = world.handler.can_rollback(mutation)
    if how == "untracked":
        assert (check.ok, check.classification) == (False, "drifted")
    else:
        assert not check.ok
    with pytest.raises(OperationFailed, match="clone rollback refused"):
        world.handler.rollback_mutation(mutation)
    assert Path(world.target).exists()


# --- reclaim -------------------------------------------------------------------------


def _reclaim_op(w):
    return _op("repo.reclaim", pre={"head": w.A}, desired={"exists": "false"})


def test_reclaim_removes_engine_installed_pristine_clone(world):
    _installed(world)
    mutation, ev = _run(world.handler, _reclaim_op(world))
    assert ev.ok and not Path(world.target).exists() and CK not in world.handler.installs
    assert mutation.before == {"head": world.A, "path": world.target}


def test_reclaim_refuses_a_hand_clone_without_record(world):
    fgit("clone", world.remote, world.target)
    assert not world.handler.preflight(_reclaim_op(world)).ok
    with pytest.raises(OperationFailed, match="not engine-installed"):
        world.handler.execute(_reclaim_op(world))
    assert Path(world.target).exists()


@pytest.mark.parametrize("how", ["dirty", "untracked", "local-commit"])
def test_reclaim_refuses_a_changed_clone(world, how):
    _installed(world)
    _drift(world, how, None)
    with pytest.raises(OperationFailed, match="refusing to reap|remove refused"):
        world.handler.execute(_reclaim_op(world))
    assert Path(world.target).exists()


def test_reclaim_rollback_reclones_while_sha_is_a_remote_tip(world):
    _installed(world)
    mutation, _ = _run(world.handler, _reclaim_op(world))
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (True, "owned")
    world.handler.rollback_mutation(mutation)
    assert world.head() == world.A and world.handler.installs[CK]["installed_sha"] == world.A


def test_reclaim_rollback_refuses_once_sha_is_no_longer_a_tip(world):
    _installed(world)
    mutation, _ = _run(world.handler, _reclaim_op(world))
    world.advance("v2")
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (False, "drifted")
    with pytest.raises(OperationFailed):
        world.handler.rollback_mutation(mutation)  # never a wrong-commit restore
    assert not Path(world.target).exists()


# --- darkness and selection ------------------------------------------------------------


def test_live_actuation_is_a_literal_false():
    tree = ast.parse(Path(PG.__file__).read_text(encoding="utf-8"))
    [node] = [n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "LIVE_ACTUATION_ENABLED"]
    assert isinstance(node.value, ast.Constant) and node.value.value is False
    assert PG.LIVE_ACTUATION_ENABLED is False


def test_no_sandbox_root_is_refused(world):
    with pytest.raises(PG.LiveActuationDisabled, match="dark"):
        PG.PantheonConfigRepoGit(CheckoutSpec({CK: world.entry()}), remote_resolver=lambda _r: world.url)


def test_target_outside_the_sandbox_is_refused(world, tmp_path):
    with pytest.raises(PG.SandboxRefused):
        world.make_handler(target=str(tmp_path / "elsewhere" / "ck"))
    with pytest.raises(PG.SandboxRefused):
        world.make_handler(target=str(world.sandbox))  # the root itself is not "under" it


def test_sandbox_root_vetting_reuses_guard_sim_root(world, tmp_path):
    repo_like = tmp_path / "inside-git"
    fgit("init", str(repo_like))
    with pytest.raises(SimRootRefused, match="inside a git checkout"):
        PG.PantheonConfigRepoGit(CheckoutSpec({}), remote_resolver=lambda _r: "", sandbox_root=repo_like / "sb")
    with pytest.raises(SimRootRefused, match="production prefix"):
        PG.PantheonConfigRepoGit(CheckoutSpec({}), remote_resolver=lambda _r: "", sandbox_root=Path("/etc/pantheon-sb"))


def test_registered_handlers_stay_simulated_only():
    regs = {r.component: r for r in discover(REPO_ROOT, REPO_ROOT / "registry" / "systems.yaml")}
    names = [type(h).__name__ for h in regs[CID].handlers]
    assert names == ["PantheonConfigRepoSim"]
    assert frozenset(regs[CID].op_kinds) == PG.KINDS == regs[CID].handlers[0].kinds


def test_nothing_outside_tests_imports_the_real_handler():
    offenders = []
    for path in (REPO_ROOT / "tools").rglob("*.py"):
        if "tests" in path.parts or path.name.startswith("test_") or path.name == "pantheon_config_git.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pantheon_config_git" in text and "import" in text:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""] + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                if any("pantheon_config_git" in m for m in mods):
                    offenders.append(str(path))
    assert offenders == []


# --- r1 hardening: repo redirection (B1), the rewind's CAS and refusals (N1-N3), re-check (N4) ---


def _advanced(w: World):
    _installed(w)
    B = w.advance("v2")
    mutation, _ = _run(w.handler, _op("repo.ff-advance", pre={"head": w.A}, desired={"head": B}))
    return B, mutation


def _decoy_at(w: World, sha: str) -> Path:
    """A clean clone OUTSIDE the sandbox, same origin, at ``sha``."""

    decoy = w.tmp / "decoy"
    fgit("clone", w.remote, str(decoy))
    fgit("reset", "--hard", sha, cwd=decoy)
    return decoy


def _redirect(monkeypatch, decoy: Path):
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))


def _head_of(path: Path) -> str:
    return fgit("rev-parse", "HEAD", cwd=path).stdout.strip()  # fixture env: no inherited GIT_DIR


def test_redirect_env_covers_gits_own_local_env_vars():
    listed = set(fgit("rev-parse", "--local-env-vars").stdout.split())
    assert listed and listed <= PG.REPO_REDIRECT_ENV


def test_b1_redirected_env_refuses_rollback_and_leaves_the_decoy(world, monkeypatch):
    B, mutation = _advanced(world)
    decoy = _decoy_at(world, B)
    _redirect(monkeypatch, decoy)
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (False, "drifted")
    with pytest.raises(PG.LiveActuationDisabled, match="redirecting"):
        world.handler.rollback_mutation(mutation)
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert _head_of(decoy) == B and world.head() == B


def test_b1_redirected_env_refuses_ff_advance_and_leaves_the_decoy(world, monkeypatch):
    _installed(world)
    B = world.advance("v2")
    decoy = _decoy_at(world, world.A)
    _redirect(monkeypatch, decoy)
    op = _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B})
    assert not world.handler.preflight(op).ok
    with pytest.raises(PG.LiveActuationDisabled):
        world.handler.execute(op)
    with pytest.raises(PG.LiveActuationDisabled):
        world.handler.observe(CID, "system")
    assert not world.handler.verify_operation(op).ok
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert _head_of(decoy) == world.A and world.head() == world.A


def test_b1_redirected_env_refuses_clone(world, monkeypatch):
    decoy = _decoy_at(world, world.A)
    _redirect(monkeypatch, decoy)
    with pytest.raises(PG.LiveActuationDisabled):
        world.handler.execute(_op("repo.clone", pre={"exists": "false"}, desired={"head": world.A}))
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert not Path(world.target).exists() and _head_of(decoy) == world.A


@pytest.mark.parametrize("var", sorted(PG.REPO_REDIRECT_ENV))
def test_every_redirect_var_closes_the_gate(world, monkeypatch, var):
    monkeypatch.setenv(var, "/nonexistent")
    assert not world.handler._sandboxed(world.entry())
    with pytest.raises(PG.SandboxRefused, match=var):
        world.handler._require_sandboxed(world.entry())


def test_handler_git_calls_strip_redirect_env(world, monkeypatch):
    _installed(world)
    B = world.advance("v2")
    decoy = _decoy_at(world, B)  # the decoy differs from the target in sha AND symref
    fgit("checkout", "--detach", cwd=decoy)
    _redirect(monkeypatch, decoy)
    assert world.handler._head(world.target) == world.A  # its own git ignores the redirect
    assert world.handler._symref(world.target) == "refs/heads/main"
    assert world.handler._remote_tips(world.url) == {B}


def test_n1_rewind_refuses_a_tracked_edit_and_keeps_it(world, monkeypatch):
    B, mutation = _advanced(world)
    (Path(world.target) / "file.txt").write_text("my edit", encoding="utf-8")  # file.txt differs between A and B
    monkeypatch.setattr(world.handler, "_ff_owned", lambda *_a: (True, "forced"))
    with pytest.raises(OperationFailed, match="local changes.*ref restored"):
        world.handler.rollback_mutation(mutation)
    assert (Path(world.target) / "file.txt").read_text() == "my edit"
    assert world.head() == B and world.handler._symref(world.target) == "refs/heads/main"


def test_n2_concurrent_commit_makes_the_rewind_refuse_and_keeps_it(world, monkeypatch):
    B, mutation = _advanced(world)
    t = Path(world.target)
    (t / "new.txt").write_text("c", encoding="utf-8")
    fgit("add", "new.txt", cwd=t)
    fgit("commit", "-m", "landed after the check", cwd=t)
    C = world.head()
    monkeypatch.setattr(world.handler, "_ff_owned", lambda *_a: (True, "forced: the commit lands after the check"))
    with pytest.raises(OperationFailed, match="no longer the receipted"):
        world.handler.rollback_mutation(mutation)
    assert world.head() == C and (t / "new.txt").read_text() == "c"


def test_n2_detached_head_rewind_uses_the_cas_on_head(world):
    _installed(world)
    fgit("checkout", "--detach", cwd=Path(world.target))
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    assert mutation.before["ref"] == ""
    assert world.handler.can_rollback(mutation).classification == "owned"
    world.handler.rollback_mutation(mutation)
    assert world.head() == world.A and world.handler._symref(world.target) == ""
    assert fgit("rev-parse", "refs/heads/main", cwd=Path(world.target)).stdout.strip() == world.A


def test_r2_kill_between_ref_swap_and_read_tree_is_never_not_applied(world, monkeypatch):
    B, mutation = _advanced(world)
    real_git = world.handler._git

    def dying_git(path, *args):
        if args and args[0] == "read-tree":
            raise SimulatedKill("killed after the CAS, before read-tree")
        return real_git(path, *args)

    monkeypatch.setattr(world.handler, "_git", dying_git)
    with pytest.raises(SimulatedKill):
        world.handler.rollback_mutation(mutation)
    assert world.head() == world.A  # the ref was swapped; B's tree is still checked out
    fresh = world.make_handler()  # a restarted process
    check = fresh.can_rollback(mutation)
    assert not check.ok and check.classification == "drifted"


def test_r2_no_deref_pins_a_detached_rewind_to_head_itself(world, monkeypatch):
    _installed(world)
    t = Path(world.target)
    fgit("checkout", "--detach", cwd=t)
    B = world.advance("v2")
    mutation, _ = _run(world.handler, _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B}))
    fgit("checkout", "-b", "attached", cwd=t)  # HEAD becomes a symref at B after the receipt
    monkeypatch.setattr(world.handler, "_ff_owned", lambda *_a: (True, "forced: HEAD attached after the check"))
    world.handler.rollback_mutation(mutation)
    # --no-deref swaps HEAD itself; the branch HEAD was attached to is never rewritten.
    assert fgit("rev-parse", "refs/heads/attached", cwd=t).stdout.strip() == B
    assert world.head() == world.A


def test_n3_head_on_another_branch_at_the_same_sha_is_drifted(world):
    B, mutation = _advanced(world)
    fgit("checkout", "-b", "other", cwd=Path(world.target))
    check = world.handler.can_rollback(mutation)
    assert (check.ok, check.classification) == (False, "drifted") and "other" in check.detail
    with pytest.raises(OperationFailed, match="drifted"):
        world.handler.rollback_mutation(mutation)
    assert world.head() == B


def _swap_for_outside_symlink(w: World) -> Path:
    outside = w.tmp / "outside"
    outside.mkdir()
    moved = outside / "ck"
    shutil.move(w.target, moved)
    os.symlink(moved, w.target)
    return moved


def test_n4_symlink_swapped_in_after_construction_refuses_ops(world):
    _installed(world)
    B = world.advance("v2")
    moved = _swap_for_outside_symlink(world)
    op = _op("repo.ff-advance", pre={"head": world.A}, desired={"head": B})
    assert not world.handler.preflight(op).ok
    with pytest.raises(PG.SandboxRefused, match="outside sandbox"):
        world.handler.execute(op)
    with pytest.raises(PG.SandboxRefused):
        world.handler.execute(_reclaim_op(world))
    assert _head_of(moved) == world.A


def test_n4_symlink_swapped_in_refuses_rollback(world):
    B, mutation = _advanced(world)
    moved = _swap_for_outside_symlink(world)
    assert world.handler.can_rollback(mutation).classification == "drifted"
    with pytest.raises(PG.SandboxRefused):
        world.handler.rollback_mutation(mutation)
    assert _head_of(moved) == B


# --- engine-level: parity with the simulation, and an end-to-end run ----------------------


def _repo(tmp_path: Path, target: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "tools" / "germinate" / "components").mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, repo / "tools" / "germinate" / "components" / name)
    (repo / "checkouts").mkdir()
    doc = {
        "schema_version": 1,
        "checkouts": [
            {
                "checkout_id": CK,
                "logical_repo": "pantheon-config",
                "remote_ref": "manifest",
                "target_account": "role:engineering-orchestrator-2",
                "target_path": target,
                "mode": {"tracking": "main"},
                "update_policy": "ff-only",
                "health_contract": [],
            }
        ],
    }
    (repo / "checkouts" / "global.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    digest = compute_contract_digest(repo / "tools" / "germinate" / "components" / "pantheon-config.yaml")
    H.make_checkout(repo, [H.row(CID, digest)])
    return repo


def _real_engine(tmp_path: Path, repo: Path, handler, name="real-host"):
    regs = discover(repo, repo / "registry" / "systems.yaml")
    backend = SimulatedHostBackend(tmp_path / name, op_registry=load_op_registry(merged_kinds(regs)), handlers=(handler,))
    backend.simulate_postboot()
    return H.engine(tmp_path, repo, backend=backend)


def _sim_engine(tmp_path: Path, repo: Path, state):
    eng = H.engine(tmp_path / "simside", repo)
    eng.backend.handler("repo.ff-advance").load_state(state)
    eng.backend.save()
    return eng


COMPARABLE = ("remote_reachable", "desired_present", "exists", "is_managed", "dirty", "head_sha", "desired_target", "installed_sha", "health_ok")


def _op_view(op):
    return (op.kind, op.target, dict(op.precondition), dict(op.desired), dict(op.inverse or {}), op.provenance["reconciler_state"], op.provenance["reconciler_action"])


def test_parity_sim_and_real_observe_plan_and_verify_agree(tmp_path):
    (tmp_path / "simside").mkdir()
    w = World(tmp_path)
    repo = _repo(tmp_path, w.target)
    real = _real_engine(tmp_path, repo, w.handler)
    fgit("clone", w.remote, w.target)  # a hand clone at A
    B = w.advance("v2")
    state = {
        "remote": {"url": SIM_URL, "reachable": True, "refs": {"main": B}, "parents": {w.A: None, B: w.A}},
        "checkouts": {CK: {"head": w.A, "origin": SIM_URL, "dirty": False, "installed_sha": None, "local_parents": {}}},
        "health": {},
    }
    sim = _sim_engine(tmp_path, repo, state)

    def obs(eng):
        return eng.backend.observe(CID, "system", frozenset(PG.KINDS))["pantheon-config-repo"]["checkouts"][CK]

    r, s = obs(real), obs(sim)
    assert {k: r[k] for k in COMPARABLE} == {k: s[k] for k in COMPARABLE}
    # Target not fetched yet: the real probe cannot prove ancestry locally; both still plan behind/ff-advance.
    assert (r["desired_resolvable"], s["desired_resolvable"]) == (False, True)
    fgit("fetch", "origin", cwd=Path(w.target))
    r = obs(real)
    assert {k: r[k] for k in r} == {k: s[k] for k in r}  # fetched: every field agrees

    plans = [eng.plan("update", "system", CID, source_revs=(w.A, B)) for eng in (real, sim)]
    assert [_op_view(p.nodes[0].operations[0]) for p in plans] == [_op_view(plans[1].nodes[0].operations[0])] * 2
    outcomes = [eng.apply("update", "system", CID, plan_digest=p.digest, source_revs=(w.A, B))[0] for eng, p in zip((real, sim), plans)]
    assert [o.state for o in outcomes] == ["SATISFIED", "SATISFIED"]
    verdicts = [eng.verify("system", CID)[0]["ok"] for eng in (real, sim)]
    assert verdicts == [True, True] and w.head() == B
    assert [eng.plan("remove", "system", CID).nodes[0].operations for eng in (real, sim)] == [(), ()]  # hand clone: never reaped


def test_engine_end_to_end_install_update_halt_rollback_reapply_remove(tmp_path):
    bad = set()
    holder = {}

    def health(entry):
        head = fgit("rev-parse", "HEAD", cwd=Path(entry.target_path), check=False).stdout.strip()
        return head not in bad

    w = World(tmp_path, health=health)
    repo = _repo(tmp_path, w.target)
    eng = _real_engine(tmp_path, repo, w.handler)

    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.desired["head"]) == ("repo.clone", w.A)
    assert eng.apply("install", "system", CID, plan_digest=plan.digest)[0].state == "SATISFIED"
    assert w.head() == w.A

    B = w.advance("v2")
    bad.add(B)
    plan = eng.plan("update", "system", CID, source_revs=(w.A, B))
    with pytest.raises(EngineRefusal, match="HALTED .*health-fail"):
        eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(w.A, B))
    holder["run"] = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert w.head() == B  # advanced, then halted on the health postcondition
    assert eng.rollback(holder["run"]).state == "ROLLED_BACK"
    assert w.head() == w.A and (Path(w.target) / "file.txt").read_text() == "v1"

    bad.clear()
    plan = eng.plan("update", "system", CID, source_revs=(w.A, B))
    assert eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(w.A, B))[0].state == "SATISFIED"
    assert w.head() == B and eng.verify("system", CID)[0]["ok"]

    with pytest.raises(EngineRefusal, match="refusing to reap"):  # installed at A, now at B: not pristine
        eng.plan("remove", "system", CID)


def test_engine_install_then_remove_reclaims(tmp_path):
    w = World(tmp_path)
    repo = _repo(tmp_path, w.target)
    eng = _real_engine(tmp_path, repo, w.handler)
    plan = eng.plan("install", "system", CID)
    eng.apply("install", "system", CID, plan_digest=plan.digest)
    plan = eng.plan("remove", "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == "repo.reclaim"
    assert eng.apply("remove", "system", CID, plan_digest=plan.digest)[0].state == "SATISFIED"
    assert not Path(w.target).exists()


@pytest.mark.parametrize("point", ["after-intent", "after-execute", "after-receipt"])
def test_engine_kill_then_reconcile_then_rollback_restores_head(tmp_path, point):
    w = World(tmp_path)
    repo = _repo(tmp_path, w.target)
    eng = _real_engine(tmp_path, repo, w.handler)
    plan = eng.plan("install", "system", CID)
    eng.apply("install", "system", CID, plan_digest=plan.digest)
    B = w.advance("v2")
    plan = eng.plan("update", "system", CID, source_revs=(w.A, B))
    [op] = plan.nodes[0].operations
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(w.A, B))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = _real_engine(tmp_path, repo, w.make_handler())  # a fresh process: install records reload
    assert restarted.backend.handler("repo.ff-advance").installs[CK]["installed_sha"] == w.A
    assert restarted.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert w.head() == w.A


def test_kill_points_cover_the_tested_subset():
    assert {"after-intent", "after-execute", "after-receipt"} <= set(KILL_POINTS)
