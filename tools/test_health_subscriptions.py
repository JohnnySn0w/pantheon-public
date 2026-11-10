"""T-5980: the health collector's subscription set is DATA derived from registry emitter declarations.

Synthetic component directories live under tmp_path; the real-repo tests read only committed files.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools import health_collector as hc
from tools import health_subscriptions as hs
from tools.health_export import fleet_personas

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
PERSONAS = (("alpha", "acct-a", None), ("beta", "acct-b", None))
PC = "org.pantheon.PersonaController1"
H1 = "org.pantheon.Health1"
# the pre-T-5980 code constant, kept here as the literal the derived set must reproduce
TODAYS_HOST_SERVICES = (
    ("host-approval", "org.pantheon.HostApproval1", "/org/pantheon/HostApproval1/Health", "action-broker"),
    ("host-broker", "org.pantheon.HostBroker1", "/org/pantheon/HostBroker1/Health", "action-broker"),
)


def _write(directory: Path, name: str, doc) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(yaml.safe_dump(doc), encoding="utf-8")


def _contract(directory, cid, emitter):
    _write(directory, f"{cid}.yaml", {"component": {"id": cid, "emitter": emitter}})


def _endpoint(cid="svc-health", bus="org.example.Svc1", path="/org/example/Svc1/Health"):
    return {"component_id": cid, "bus_name": bus, "object_path": path}


def _sidecar(directory, *rows):
    _write(directory, hs.SIDECAR_NAME, {"subscriptions": list(rows)})


def _srow(reg="action-broker", cid="svc-health", **kw):
    row = {"registry_component": reg, "component_id": cid, "interface": H1,
           "bus_name": "org.example.Svc1", "object_path": "/org/example/Svc1/Health", "ticket": "T-2920"}
    row.update(kw)
    return row


# ----------------------------------------------------------------- the real repo
def test_derived_set_equals_todays_literal_set():
    subs = hs.derive(fleet_personas())
    hosts = tuple(sorted((s.component_id, s.bus_name, s.object_path, s.registry_component)
                         for s in subs if s.kind == "host_service"))
    assert hosts == TODAYS_HOST_SERVICES
    personas = sorted(s.persona for s in subs if s.kind == "persona_controller")
    assert personas == sorted(p for p, _a, _t in fleet_personas())
    assert all(s.registry_component == "harness-manager" and s.component_id == f"persona-controller.{s.persona}"
               and s.bus_class == "session" for s in subs if s.kind == "persona_controller")
    assert len(subs) == len(TODAYS_HOST_SERVICES) + len(fleet_personas())


class _Reader:
    def getall_system(self, bus_name, path, interface):
        return "unreachable", None

    def getall_session(self, account, persona, interface):
        return "unreachable", None


def test_collect_over_the_derived_set_equals_collect_over_the_literal_set():
    personas = fleet_personas()
    literal = [hs.Subscription(c, "host_service", r, "system", H1, b, p) for c, b, p, r in TODAYS_HOST_SERVICES]
    literal += [hs.Subscription(f"persona-controller.{p}", "persona_controller", "harness-manager", "session",
                                PC, persona=p, account=a) for p, a, _t in personas]
    a = hc.collect(personas, reader=_Reader(), now=1_800_000_000.0)
    b = hc.collect(personas, reader=_Reader(), now=1_800_000_000.0, subscriptions=literal)
    a["collector"]["duration_ms"] = b["collector"]["duration_ms"] = 0
    assert a == b


def test_sidecar_rows_are_well_formed_and_name_registry_systems():
    rows = yaml.safe_load((REAL_COMPONENTS / hs.SIDECAR_NAME).read_text(encoding="utf-8"))["subscriptions"]
    systems = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))["systems"]
    ids = {s["id"] for s in systems}
    assert rows and all(re.fullmatch(r"T-[0-9]+", r["ticket"]) and r["registry_component"] in ids for r in rows)
    assert len(hs.derive(fleet_personas(), registry_ids=ids)) == len(TODAYS_HOST_SERVICES) + len(fleet_personas())


# ------------------------------------------------------------- emitter declarations
def test_a_health1_emitter_with_endpoints_yields_host_rows(tmp_path):
    _contract(tmp_path, "svc", {"bus": "system", "interface": H1, "endpoints": [_endpoint()]})
    [sub] = hs.derive(PERSONAS, tmp_path)
    assert (sub.component_id, sub.kind, sub.registry_component, sub.bus_class) == ("svc-health", "host_service", "svc", "system")


def test_a_persona_controller_emitter_fans_out_over_the_roster(tmp_path):
    _contract(tmp_path, "hm", {"bus": "session", "interface": PC})
    subs = hs.derive(PERSONAS + (("../bad", "acct-c", None),), tmp_path)
    assert [(s.component_id, s.account, s.registry_component) for s in subs] == [
        ("persona-controller.alpha", "acct-a", "hm"), ("persona-controller.beta", "acct-b", "hm")]


@pytest.mark.parametrize("emitter, match", [
    ({"bus": "system", "interface": H1}, "must declare endpoints"),
    ({"bus": "system", "interface": H1, "endpoints": []}, "must declare endpoints"),
    ({"bus": "session", "interface": PC, "endpoints": [_endpoint()]}, "not allowed"),
    ({"bus": "system", "interface": "org.example.Other1"}, "not a known health interface"),
    ({"bus": "session", "interface": H1, "endpoints": [_endpoint()]}, "disagrees"),
    ({"bus": "system", "interface": H1, "endpoints": [_endpoint(cid="Bad Id")]}, "malformed component_id"),
    ({"bus": "system", "interface": H1, "endpoints": [_endpoint(bus="not a bus")]}, "malformed bus_name"),
    ({"bus": "system", "interface": H1, "endpoints": [_endpoint(path="relative/x")]}, "malformed object_path"),
    ({"bus": "system", "interface": H1, "endpoints": ["nope"]}, "not a mapping"),
])
def test_unusable_emitter_declarations_raise(tmp_path, emitter, match):
    _contract(tmp_path, "svc", emitter)
    with pytest.raises(hs.SubscriptionError, match=match):
        hs.derive(PERSONAS, tmp_path)


def test_duplicate_component_ids_raise(tmp_path):
    _contract(tmp_path, "one", {"bus": "system", "interface": H1, "endpoints": [_endpoint()]})
    _contract(tmp_path, "two", {"bus": "system", "interface": H1, "endpoints": [_endpoint()]})
    with pytest.raises(hs.SubscriptionError, match="duplicate component_id 'svc-health'"):
        hs.derive(PERSONAS, tmp_path)


def test_sidecar_files_are_not_scanned_as_contracts(tmp_path):
    assert ".subscriptions.yaml" in hs.NON_CONTRACT_SUFFIXES
    _write(tmp_path, "other.subscriptions.yaml",
           {"component": {"id": "ghost", "emitter": {"bus": "system", "interface": H1}}})
    assert hs.declared_emitters(tmp_path) == {}
    assert hs.derive(PERSONAS, tmp_path) == ()


# ------------------------------------------------------------------------- sidecar
def test_sidecar_rows_join_the_set(tmp_path):
    _sidecar(tmp_path, _srow())
    [sub] = hs.derive(PERSONAS, tmp_path)
    assert (sub.component_id, sub.registry_component, sub.bus_name) == ("svc-health", "action-broker", "org.example.Svc1")


def test_sidecar_row_refused_once_its_component_declares_an_emitter(tmp_path):
    _sidecar(tmp_path, _srow(reg="svc"))
    _contract(tmp_path, "svc", {"bus": "system", "interface": H1, "endpoints": [_endpoint(cid="other")]})
    with pytest.raises(hs.SubscriptionError, match="now declares an emitter"):
        hs.derive(PERSONAS, tmp_path)


def test_unknown_sidecar_registry_component_raises_only_when_ids_are_given(tmp_path):
    _sidecar(tmp_path, _srow(reg="nonesuch"))
    assert len(hs.derive(PERSONAS, tmp_path)) == 1                      # the runtime path: registry_ids=None
    assert len(hs.derive(PERSONAS, tmp_path, registry_ids={"nonesuch"})) == 1
    with pytest.raises(hs.SubscriptionError, match="not a registry system"):
        hs.derive(PERSONAS, tmp_path, registry_ids={"action-broker"})


@pytest.mark.parametrize("row, match", [
    (_srow(interface=PC), "interface must be"),
    (_srow(ticket="soon"), "malformed ticket"),
    (_srow(reg="Bad Reg"), "malformed registry_component"),
    (_srow(object_path="x"), "malformed object_path"),
])
def test_malformed_sidecar_rows_raise(tmp_path, row, match):
    _sidecar(tmp_path, row)
    with pytest.raises(hs.SubscriptionError, match=match):
        hs.derive(PERSONAS, tmp_path)


# ---------------------------------------------------------------- collector wiring
def test_sanitize_entry_keeps_a_well_formed_unlisted_registry_component_and_drops_a_bad_one():
    good = hc.collect([], reader=_Reader(), now=0)["entries"][0]
    assert hc.sanitize_entry({**good, "registry_component": "brand-new-organ"})["registry_component"] == "brand-new-organ"
    assert hc.sanitize_entry({**good, "registry_component": "Bad Reg!"})["registry_component"] is None


def test_derive_failure_is_rc_2_with_a_named_stderr(tmp_path, monkeypatch, capsys):
    _contract(tmp_path, "svc", {"bus": "system", "interface": H1})
    monkeypatch.setattr(hs, "COMPONENTS_DIR", tmp_path)
    assert hc.main(["--dry-run"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("health_collector: subscription set: ") and "svc" in err


def test_module_imports_without_the_germinate_engine():
    code = "import sys; import tools.health_subscriptions; sys.exit(any(m.startswith('tools.germinate.engine') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=False).returncode == 0
