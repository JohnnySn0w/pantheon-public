"""T-2918: ORG-082 build steps 3 (daemon digest capture) and 5 (structural
substrate exclusion at daemon start), plus the ORG-082 §2 Plan-time refusal
(F1) and its wire payload. Pure unit tests; nothing touches a real host path."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from pathlib import Path
from tools.germinate.host_substrate import gate32a
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.op_registry import SUBSTRATE_CLASS, OpRegistryError, load_op_registry
from tools.host_control import wire
from tools.host_control.contracts import (
    RegistryDigestMismatch,
    RuntimeSubsetMismatch,
    SubstrateFamilyNotExposed,
)
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    IMPLEMENTED_EXECUTION_CLASSES,
    ORG082_SUBSTRATE_FAMILIES,
)
from tools.host_control.runtime_table import (
    STARTUP_RECORD_SCHEMA,
    DaemonStartRefused,
    gate32a_check,
    load_daemon_runtime,
)

FAMILIES = frozenset({"persona.install", "persona.verify"})


def load(**kw):
    kw.setdefault("host_operation_families", FAMILIES)
    return load_daemon_runtime(**kw)


# --- step 3: digest capture at the daemon's load path ---------------------------


def test_daemon_load_captures_the_sha256_of_the_loaded_registry_blob():
    import hashlib

    rt = load()
    assert rt.loaded_op_registry_digest == "sha256:" + hashlib.sha256(rt.op_registry.blob).hexdigest()
    assert rt.loaded_op_registry_digest == rt.op_registry.digest


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """The REAL bootstrap in-process root context: the engine the CLI builds
    (Engine.from_checkout over this checkout, every registered component)."""

    from tools.germinate.engine.executor import Engine

    return Engine.from_checkout(tmp_path_factory.mktemp("t2918-engine") / "sim", repo_root=REPO)


def test_daemon_digest_equals_the_real_engines_digest(engine):
    """Gate 32a digest-equality half (review B1): the engine Engine.from_checkout
    builds (component OP_KINDS merged) and the broker daemon load the same blob."""

    rt = load()
    assert rt.loaded_op_registry_digest == engine.backend.loaded_op_registry_digest
    assert rt.op_registry.blob == engine.backend.op_registry.blob
    assert gate32a.check(
        root_registry=engine.backend.op_registry,
        daemon_registry=rt.op_registry,
        projection=rt.dispatch_table,
        expected_union=rt.dispatch_table,
        expected_digest=rt.runtime_subset_digest,
    ) == []
    assert gate32a_check(
        root_digest=engine.backend.loaded_op_registry_digest,
        startup_record=rt.startup_record(),
        expected_union=rt.dispatch_table,
        expected_digest=rt.runtime_subset_digest,
    ) == []


def test_the_registry_includes_component_kinds_and_the_bare_registry_differs():
    rt = load()
    component = {k for k, e in rt.op_registry.entries.items() if k not in load_op_registry().entries}
    assert component, "the checkout registers component op kinds (harness.*, repo.*)"
    assert rt.loaded_op_registry_digest != load_op_registry().digest


def _merged_real_kinds():
    from tools.germinate.engine.components import discover, merged_kinds
    from tools.germinate.registry.validate import REGISTRY_REL

    return merged_kinds(discover(REPO, REPO / REGISTRY_REL, import_handlers=False))


def test_a_component_kind_skew_fails_gate32a(engine):
    """An extra component kind on the daemon side only is a digest mismatch."""

    extra = {**_merged_real_kinds(), "fixture.extra-component-kind": ("fixture.extra", "seat")}
    skewed = load(registry_loader=lambda: load_op_registry(extra))
    assert skewed.dispatch_table == load().dispatch_table  # the table does not even move...
    failures = gate32a_check(
        root_digest=engine.backend.loaded_op_registry_digest,
        startup_record=skewed.startup_record(),
        expected_union=skewed.dispatch_table,
        expected_digest=skewed.runtime_subset_digest,
    )
    assert failures and failures[0].startswith("REGISTRY_DIGEST_MISMATCH")  # ...the digest catches it
    missing = {k: v for k, v in _merged_real_kinds().items() if k != sorted(_merged_real_kinds())[0]}
    short = load(registry_loader=lambda: load_op_registry(missing))
    assert gate32a_check(root_digest=engine.backend.loaded_op_registry_digest, startup_record=short.startup_record(),
                         expected_union=short.dispatch_table, expected_digest=short.runtime_subset_digest)


def test_component_kinds_never_reach_dispatch_or_substrate():
    """Component kinds are seat-privilege, non-substrate, never broker-admitted:
    in the digest, never in the daemon's dispatch table (ORG-082 §2a)."""

    rt = load()
    for kind in _merged_real_kinds():
        entry = rt.op_registry.get(kind)
        assert entry.privilege == "seat" and not entry.substrate and not entry.broker_admitted, kind
        assert kind not in rt.dispatch_table, kind
    assert rt.dispatch_table == gate32a.daemon_projection(rt.op_registry)  # step 5 holds over the merged registry


@pytest.mark.parametrize(
    "kinds",
    [
        {"account.ensure": ("fixture.x", "seat")},          # shadows a substrate family
        {"fixture.rootkind": ("fixture.x", "root")},        # root privilege
        {"fixture.k": ("substrate.root", "seat")},           # reserved substrate class
        {"fixture.k": ("files.seat_root", "seat")},          # reserved broker envelope class
        {"mkdir": ("fixture.x", "seat")},                    # shadows a broker dispatch kind
    ],
)
def test_no_component_declaration_can_reach_substrate_or_broker_dispatch(kinds):
    from tools.germinate.engine.components import ComponentLoadError, _op_kinds

    with pytest.raises(ComponentLoadError):
        _op_kinds("fixture-component", kinds)


def test_the_daemon_load_imports_no_component_code():
    """Review B1: the daemon reads OP_KINDS literals only; no *_handlers module runs."""

    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.host_control.runtime_table import load_daemon_runtime\n"
        "load_daemon_runtime(host_operation_families={'persona.install'})\n"
        "bad = sorted(n for n, m in list(sys.modules.items())\n"
        "             if (getattr(m, '__file__', '') or '').endswith('_handlers.py')\n"
        "             or '/germinate/components/' in (getattr(m, '__file__', '') or ''))\n"
        "print('IMPORTED', bad)\n"
    ) % str(REPO)
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "IMPORTED []"


def test_startup_record_is_canonical_and_carries_what_t2924_compares():
    rt = load()
    record = json.loads(rt.startup_record())
    assert record == {
        "schema": STARTUP_RECORD_SCHEMA,
        "op_registry_digest": rt.loaded_op_registry_digest,
        "dispatch_table": sorted(rt.dispatch_table),
        "runtime_subset_digest": rt.runtime_subset_digest,
    }


def test_out_of_process_check_flags_a_leaked_family_and_a_bad_schema():
    rt = load()
    doc = json.loads(rt.startup_record())
    doc["dispatch_table"] = sorted(rt.dispatch_table | {"account.ensure"})
    failures = gate32a_check(root_digest=rt.loaded_op_registry_digest, startup_record=json.dumps(doc),
                             expected_union=rt.dispatch_table, expected_digest=rt.runtime_subset_digest)
    assert any("SUBSTRATE_FAMILY_NOT_EXPOSED" in f for f in failures)
    assert any(f.startswith("RUNTIME_SUBSET_MISMATCH") for f in failures)
    assert gate32a_check(root_digest="x", startup_record="{}", expected_union=frozenset(), expected_digest="y")[0].startswith(
        "RUNTIME_SUBSET_MISMATCH")


# --- step 5: structural exclusion asserted at daemon start ------------------------


def test_the_loaded_dispatch_table_is_exactly_the_registry_runtime_projection():
    rt = load()
    assert rt.dispatch_table == frozenset(EXECUTOR_KIND_CLASSES)
    assert rt.dispatch_table == gate32a.daemon_projection(rt.op_registry)
    assert not rt.dispatch_table & ORG082_SUBSTRATE_FAMILIES


@pytest.mark.parametrize("family", sorted(ORG082_SUBSTRATE_FAMILIES))
def test_an_injected_substrate_family_in_the_dispatch_table_refuses_start(family):
    with pytest.raises(DaemonStartRefused) as info:
        load(dispatch_kinds=frozenset(EXECUTOR_KIND_CLASSES) | {family})
    assert isinstance(info.value.cause, SubstrateFamilyNotExposed)
    assert info.value.cause.family == family


def test_a_substrate_family_advertised_as_a_host_family_refuses_start():
    with pytest.raises(DaemonStartRefused, match="host operation families"):
        load(host_operation_families=FAMILIES | {"sudoers_fragment.ensure"})


def test_a_substrate_kind_in_the_envelope_table_refuses_start():
    """A substrate kind added to the executor kind table (the planner's own
    admission table) refuses start even though the registry would also choke."""

    table = MappingProxyType({**EXECUTOR_KIND_CLASSES, "account.retire": "files.seat_root"})
    with pytest.raises(DaemonStartRefused):
        load(dispatch_kinds=table)


def test_the_substrate_class_in_an_implemented_execution_class_refuses_start():
    classes = MappingProxyType({**IMPLEMENTED_EXECUTION_CLASSES, "apply": IMPLEMENTED_EXECUTION_CLASSES["apply"] | {SUBSTRATE_CLASS}})
    with pytest.raises(DaemonStartRefused, match="substrate capability class"):
        load(implemented_classes=classes)


def test_a_dispatch_table_that_is_not_the_projection_refuses_start():
    with pytest.raises(DaemonStartRefused) as info:
        load(dispatch_kinds=frozenset(EXECUTOR_KIND_CLASSES) - {"mkdir"})
    assert isinstance(info.value.cause, RuntimeSubsetMismatch)


def test_a_registry_that_cannot_build_refuses_start():
    def broken():
        raise OpRegistryError("op kind 'account.ensure' is declared twice")

    with pytest.raises(DaemonStartRefused, match="declared twice"):
        load(registry_loader=broken)


def test_the_broker_daemon_refuses_to_start_as_uid_0():
    """ORG-052 gate 19 (T-2918 D7): the broker is unprivileged."""

    with pytest.raises(DaemonStartRefused, match="uid 0"):
        load(geteuid=lambda: 0)
    assert load(geteuid=lambda: 3201).dispatch_table


def test_exclusion_is_not_a_call_time_filter():
    """D-B: the composer guard consults only the loaded table (and the
    registry's substrate tag for the refusal's NAME); no per-call substrate
    list decides admission. A kind in the table is never refused by it."""

    rt = load()
    rt.refuse_outside_table(sorted(rt.dispatch_table))
    with pytest.raises(SubstrateFamilyNotExposed) as info:
        rt.refuse_outside_table(["mkdir", "linger.enable"], manifest_revision="manifest-r1")
    assert info.value.payload() == {
        "code": "SUBSTRATE_FAMILY_NOT_EXPOSED",
        "family": "linger.enable",
        "amendment": "ORG-082",
        "retry_context": "bootstrap-in-process-root",
        "manifest_revision": "manifest-r1",
    }


# --- ORG-082 §2 wire shape and parity with the GE root helper ----------------------


@pytest.mark.parametrize("family", sorted(ORG082_SUBSTRATE_FAMILIES))
def test_broker_and_root_helper_substrate_payloads_are_identical(family):
    broker = SubstrateFamilyNotExposed(family)
    helper = Hp.SubstrateFamilyNotExposed(family)
    assert broker.payload() == helper.payload()
    assert wire.error_name_for(broker) == helper.dbus_name == wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR


@pytest.mark.parametrize(
    "exc, name",
    [
        (SubstrateFamilyNotExposed("account.ensure", manifest_revision="manifest-r1"), wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR),
        (RegistryDigestMismatch("digest skew"), wire.REGISTRY_DIGEST_MISMATCH_ERROR),
        (RuntimeSubsetMismatch("subset skew"), wire.RUNTIME_SUBSET_MISMATCH_ERROR),
    ],
)
def test_org082_errors_round_trip_the_presence_rules(exc, name):
    assert wire.error_name_for(exc) == name
    body = wire.error_body_for(exc)
    assert json.loads(body) == exc.payload()
    assert wire.validate_org082_error(name, body) == exc.payload()


@pytest.mark.parametrize(
    "name, payload",
    [
        (wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR, {"code": "SUBSTRATE_FAMILY_NOT_EXPOSED", "amendment": "ORG-082",
                                                   "retry_context": "bootstrap-in-process-root"}),  # family missing
        (wire.REGISTRY_DIGEST_MISMATCH_ERROR, {"code": "REGISTRY_DIGEST_MISMATCH", "family": "x", "amendment": "ORG-082",
                                               "retry_context": "not-retryable"}),  # family forbidden
        (wire.RUNTIME_SUBSET_MISMATCH_ERROR, {"code": "REGISTRY_DIGEST_MISMATCH", "amendment": "ORG-082",
                                              "retry_context": "not-retryable"}),  # code/name mismatch
        (wire.RUNTIME_SUBSET_MISMATCH_ERROR, {"code": "RUNTIME_SUBSET_MISMATCH", "amendment": "ORG-052",
                                              "retry_context": "not-retryable"}),  # amendment constant
        (wire.RUNTIME_SUBSET_MISMATCH_ERROR, {"code": "RUNTIME_SUBSET_MISMATCH", "amendment": "ORG-082",
                                              "retry_context": "bootstrap-in-process-root"}),  # retry domain
        (wire.RUNTIME_SUBSET_MISMATCH_ERROR, {"code": "RUNTIME_SUBSET_MISMATCH", "amendment": "ORG-082",
                                              "retry_context": "not-retryable", "extra": "x"}),  # unknown field
    ],
)
def test_malformed_org082_payloads_are_refused_at_the_boundary(name, payload):
    with pytest.raises(ValueError):
        wire.validate_org082_error(name, json.dumps(payload))


def test_an_unsafe_family_string_never_reaches_the_payload():
    assert SubstrateFamilyNotExposed("../etc/passwd\n").payload()["family"] == "invalid-family"
