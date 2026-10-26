"""The simulated-host guarantee: it cannot be pointed at, or reach, the real host."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.germinate.engine import backend as B
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry
from tools.persona_installer import InstallOperation, OperationFailed

PASSWD = Path("/etc/passwd")


@pytest.mark.parametrize(
    "root",
    [
        "/var/lib/pantheon/germinate",
        "/run/pantheon/germinate",
        "/opt/pantheon",
        "/etc/germinate-sim",
        "/home/post-office/mailbox",
        "/proc/self",
    ],
)
def test_production_prefixes_refuse_before_touching_anything(root):
    with pytest.raises(B.SimRootRefused, match="production prefix"):
        B.guard_sim_root(Path(root))


def test_seat_state_tree_refuses(tmp_path):
    with pytest.raises(B.SimRootRefused, match="seat-scope pantheon state"):
        B.guard_sim_root(tmp_path / ".local" / "state" / "pantheon" / "sim")


def test_symlink_into_production_is_resolved_and_refused(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to("/etc")
    with pytest.raises(B.SimRootRefused, match="production prefix"):
        B.guard_sim_root(link)


def test_git_checkout_refuses(tmp_path):
    (tmp_path / "checkout" / ".git").mkdir(parents=True)
    with pytest.raises(B.SimRootRefused, match="git checkout"):
        B.guard_sim_root(tmp_path / "checkout" / "sim")


def test_this_repo_refuses():
    repo = Path(__file__).resolve().parents[4]
    with pytest.raises(B.SimRootRefused):
        B.guard_sim_root(repo / "scratch-sim")


def test_non_empty_unsentineled_dir_refuses(tmp_path):
    target = tmp_path / "busy"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    (target / "something").write_text("x")
    with pytest.raises(B.SimRootRefused, match="sentinel"):
        B.guard_sim_root(target)


def test_wrong_mode_refuses(tmp_path):
    target = tmp_path / "open"
    target.mkdir()
    os.chmod(target, 0o755)
    with pytest.raises(B.SimRootRefused, match="mode-0700"):
        B.guard_sim_root(target)


def test_forged_sentinel_refuses(tmp_path):
    target = tmp_path / "forged"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    (target / B.SENTINEL_NAME).write_text("not the sentinel")
    with pytest.raises(B.SimRootRefused, match="not a simulated-host sentinel"):
        B.guard_sim_root(target)


def test_new_root_is_initialized_and_reusable(tmp_path):
    root = B.guard_sim_root(tmp_path / "fresh")
    assert (root / B.SENTINEL_NAME).read_bytes() == B.SENTINEL_BODY
    assert oct((root / B.SENTINEL_NAME).stat().st_mode & 0o777) == "0o600"
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert B.guard_sim_root(root) == root


def test_root_euid_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(B.os, "geteuid", lambda: 0)
    with pytest.raises(B.SimRootRefused, match="never runs as root"):
        B.guard_sim_root(tmp_path / "x")


def test_every_store_is_a_child_of_the_root(tmp_path):
    backend = H.sim(tmp_path)
    for path in (
        backend.gates_root,
        backend.postboot_marker_path,
        backend.boot_id_path,
        backend.run_state_root("system"),
        backend.run_state_root("seat-orchestrator"),
    ):
        assert backend.root in path.parents


def _op(kind, target, **desired):
    return InstallOperation(target.replace("/", "_"), 1, kind, target, {}, desired, None, "unprivileged", "sim", {})


def test_op_targets_are_keys_not_paths(tmp_path):
    before = PASSWD.stat()
    backend = H.sim(tmp_path)
    op = _op("atomic-write", str(PASSWD), content="pwned\n", sha256="x")
    backend.execute(op)
    assert backend.entry(str(PASSWD))["content_b64"]
    after = PASSWD.stat()
    assert (before.st_mtime_ns, before.st_size, before.st_ino) == (after.st_mtime_ns, after.st_size, after.st_ino)


def test_state_persists_across_instances(tmp_path):
    backend = H.sim(tmp_path)
    backend.execute(_op("mkdir", "/srv/d", mode="0755"))
    again = B.SimulatedHostBackend(tmp_path / "sim")
    assert again.entry("/srv/d") == {"state": "directory", "mode": "0755"}


def test_write_ahead_inverse_lets_installer_classify(tmp_path):
    backend = H.sim(tmp_path)
    op = _op("atomic-write", "/srv/f", content="v1\n")
    inverse = backend.rollback_intent(op)
    assert backend.can_rollback(inverse).classification == "not-applied"
    backend.execute(op)
    assert backend.can_rollback(inverse).classification == "owned"
    backend.rollback_mutation(inverse)
    assert backend.entry("/srv/f") is None


def test_unhandled_kind_refuses(tmp_path):
    backend = H.sim(tmp_path)
    with pytest.raises(OperationFailed, match="no handler"):
        backend.preflight(_op("account.ensure", "seat"))


def test_handler_kinds_must_be_registry_kinds(tmp_path):
    class Handler:
        name = "bogus"
        kinds = frozenset({"not-a-kind"})

    with pytest.raises(ValueError, match="not an op-registry kind"):
        B.SimulatedHostBackend(tmp_path / "sim", handlers=(Handler(),))


def test_custom_registry_is_the_one_digested(tmp_path):
    reg = load_op_registry({"repo.ff-update": ("repo.local", "seat")})
    backend = B.SimulatedHostBackend(tmp_path / "sim", op_registry=reg)
    assert backend.loaded_op_registry_digest == reg.digest


def test_boot_id_and_run_dir_modes(tmp_path):
    backend = H.sim(tmp_path)
    assert oct(backend.boot_id_path.stat().st_mode & 0o777) == "0o600"
    assert oct(backend.postboot_marker_path.parent.stat().st_mode & 0o777) == "0o700"
