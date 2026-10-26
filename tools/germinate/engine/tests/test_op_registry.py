from __future__ import annotations

import pytest

from tools.germinate import op_registry as R
from tools.germinate.engine.tests import helpers as H
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, ORG082_SUBSTRATE_FAMILIES


def test_blob_and_digest_are_deterministic():
    a, b = R.load_op_registry(), R.load_op_registry()
    assert a.blob == b.blob and a.digest == b.digest and a.digest.startswith("sha256:")


def test_built_from_the_envelope_and_org082_without_new_vocabulary():
    reg = R.load_op_registry()
    assert set(EXECUTOR_KIND_CLASSES) | set(ORG082_SUBSTRATE_FAMILIES) <= set(reg.entries)
    for kind in ORG082_SUBSTRATE_FAMILIES:
        entry = reg.get(kind)
        assert entry.privilege == "root" and entry.substrate and not entry.broker_admitted
    assert reg.get("identity-check").read_only and reg.get("identity-check").privilege == "none"
    assert reg.get("atomic-write").privilege == "seat" and reg.get("atomic-write").broker_admitted


def test_llm_path_kinds_are_never_registered():
    reg = R.load_op_registry()
    assert not (R.LLM_PATH_KINDS & set(reg.entries))
    with pytest.raises(R.OpRegistryError):
        R.load_op_registry({"harness-turn": ("custom.class", "seat")})


def test_engine_kinds_extend_the_blob_and_change_the_digest():
    base = R.load_op_registry()
    extended = R.load_op_registry({"repo.ff-update": ("repo.local", "seat")})
    assert extended.get("repo.ff-update").privilege == "seat"
    assert extended.digest != base.digest


def test_engine_kind_cannot_collide_or_misdeclare():
    with pytest.raises(R.OpRegistryError, match="twice"):
        R.load_op_registry({"mkdir": ("files.seat_root", "seat")})
    with pytest.raises(R.OpRegistryError, match="read-only"):
        R.load_op_registry({"x.read": ("diagnostics.read", "seat")})


def test_digest_is_captured_at_the_simulated_backend_load_path(tmp_path):
    backend = H.sim(tmp_path)
    assert backend.loaded_op_registry_digest == R.load_op_registry().digest


def test_same_registry_assertion_refuses_a_different_load(tmp_path):
    bootstrap_ctx = H.sim(tmp_path, "a")
    other_ctx = H.sim(tmp_path, "b", op_registry=R.load_op_registry({"repo.ff-update": ("repo.local", "seat")}))
    R.assert_same_registry(bootstrap_ctx.loaded_op_registry_digest, H.sim(tmp_path, "c").loaded_op_registry_digest)
    with pytest.raises(R.OpRegistryError, match="gate 32a"):
        R.assert_same_registry(bootstrap_ctx.loaded_op_registry_digest, other_ctx.loaded_op_registry_digest)
