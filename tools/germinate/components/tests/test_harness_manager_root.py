"""T-5827 (T-5091 U2, GF2): the harness manager's root half through host-substrate.

T-5829 ruling (1) Option A: ``harness_manager_root`` resolves per-seat
``pantheon-host-substrate-manifest/v0`` fragments from the typed instance.yaml
identity block and host-substrate merges them add-only. One test (or test group)
per ruling condition (a)-(g), each with a refusal case, plus the order-independence
of the merged digest and the mason amendment-2 pins (no fleet-wide regression: the
real repo's host-substrate plan is byte-identical with the blocks populated).

Every observation here is INJECTED (a subject mapping or the simulated port);
nothing reads passwd or group, and nothing touches a live host. Only ``test_real_*``
reads the real repo (read-only).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tools.germinate.components import harness_manager_root as HR
from tools.germinate.components.tests import test_host_substrate_component as HS
from tools.germinate.engine.backend import SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.handlers_digest import compute as compute_handlers_digest
from tools.germinate.engine.planner import PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import families as F
from tools.germinate.planner_sandbox.admission import coordinator

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
CID = "host-substrate"

#: The values read from getent passwd/group, stat and getfacl on 2026-11-02 (T-5827),
#: as (role, account, uid, gid, groups). A captured fixture: tests never read the host.
#: Amended 2026-11-03 (T-6115): codex + codex-fallback joined pantheon-shared (re-read via getent).
CAPTURED_2026_11_02 = (
    ("head-engineering-orchestrator", "claude", 1000, 1000, ("pantheon-mailbox", "pantheon-shared", "sudo", "users")),
    ("specialist", "claude-research", 1002, 1002, ("pantheon-mailbox", "pantheon-shared")),
    ("head-system-orchestrator", "claude-fallback", 1001, 1001,
     ("pantheon-health", "pantheon-mailbox", "pantheon-shared", "systemd-journal")),
    ("peer-daemon-codex", "codex", 1003, 1004, ("pantheon-mailbox", "pantheon-shared")),
    ("engineering-orchestrator-1", "suborch1", 1007, 1009, ("pantheon-mailbox", "pantheon-shared")),
    ("engineering-orchestrator-2", "suborch2", 1008, 1010, ("pantheon-mailbox", "pantheon-shared")),
    ("head-memory-orchestrator", "exocortex", 1006, 1008, ("pantheon-mailbox", "pantheon-shared")),
    ("system-orchestrator-1", "codex-fallback", 1011, 1014,
     ("pantheon-health", "pantheon-mailbox", "pantheon-shared", "systemd-journal")),
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ident(uid=3500, *, root_half="managed", groups=(), acl=(), home_mode="0750", shell="/bin/bash"):
    return {"uid": uid, "shell": shell, "home_mode": home_mode, "groups": list(groups), "acl": list(acl),
            "root_half": root_half}


def _instance(**seats):
    """roles: one role per seat, keyed role-<account>: {account: identity-or-None}."""

    roles = {}
    for account, ident in seats.items():
        binding = {"persona": f"p-{account}", "account": account, "harness": "claude-code"}
        if ident is not None:
            binding["identity"] = ident
        roles[f"role-{account}"] = binding
    return {"roles": roles}


def _existing(account, uid, *, gid=None, groups=(), home_mode="0750", shell="/bin/bash"):
    """Injected subjects for an existing account matching its block."""

    subjects = {
        f"passwd:{account}": {"uid": uid, "gid": uid if gid is None else gid, "home": f"/home/{account}",
                              "shell": shell, "locked": False},
        f"group:{account}": {"gid": uid if gid is None else gid},
        f"owner_mode:/home/{account}": {"owner": account, "group": account, "mode": home_mode},
    }
    subjects.update({f"member:{g}:{account}": True for g in groups})
    return subjects


def _base():
    return HS.fixture_manifest()


#: R1-1: passwd:root is part of the proof that passwd was fully listed.
ROOT = {"passwd:root": {"uid": 0, "gid": 0, "home": "/root", "shell": "/bin/bash", "locked": False}}


def _seat_engine(tmp_path, repo, seed=None):
    """An engine over the simulated host, seeded with a fully listed passwd (root)."""

    eng = H.engine(tmp_path, repo)
    HS._seed(eng, dict(ROOT) | (seed or {}))
    return eng


# --- the real identity blocks (2026-11-02) -------------------------------------------


def test_real_identity_blocks_validate_all_observe_only_and_match_the_capture():
    idents = HR.identities(HR.load_instance(REPO_ROOT / "instance.yaml"))
    assert {i.root_half for i in idents.values()} == {"observe-only"}  # mason amendment 1 (option alpha)
    got = {(r, i.account, i.uid, i.groups) for r, i in idents.items()}
    assert got == {(r, a, u, g) for r, a, u, _gid, g in CAPTURED_2026_11_02}
    assert all(i.shell == "/bin/bash" and i.home_mode == "0750" and i.acl == () for i in idents.values())


def test_real_blocks_pass_the_drift_check_against_the_captured_observation():
    subjects = {}
    for _role, account, uid, gid, groups in CAPTURED_2026_11_02:
        subjects.update(_existing(account, uid, gid=gid, groups=groups))
    assert HR.drift_check(HR.load_instance(REPO_ROOT / "instance.yaml"), subjects) == []


def test_real_repo_emits_no_fragment_so_host_substrate_needs_no_seat_observation():
    merged = HR.seat_merge({"schema": "x"}, HR.load_instance(REPO_ROOT / "instance.yaml"),
                           lambda: pytest.fail("a seat observation was asked for"), plan_time=True)
    assert merged.fragment_sha256 == {} and merged.doc == {"schema": "x"}


@pytest.mark.parametrize("leg", ("plan_install", "plan_configure", "plan_update", "plan_repair", "plan_remove"))
def test_real_repo_host_substrate_plan_is_byte_identical_with_the_identity_blocks(leg, monkeypatch):
    """mason amendment 2(a)/(c): with the eight blocks populated (all observe-only), the
    real repo's host-substrate plan (op ids AND op bytes) equals the plan with no
    instance.yaml at all, and an observation carrying NO seat never refuses."""

    planner = _load(COMPONENTS / "host-substrate.py", f"_t5827_hs_{leg}")
    request = {"observed": {CID: {"subjects": {}}}}
    with_blocks = json.dumps(getattr(planner, leg)(request))
    monkeypatch.setattr(planner.HR, "load_instance", lambda _path: None)
    without = json.dumps(getattr(planner, leg)(request))
    assert with_blocks == without
    assert "fragment_sha256" not in with_blocks and "base_manifest_sha256" not in with_blocks


def test_fleet_uid_range_restates_codex_recovery():
    from tools import codex_recovery

    assert (HR.MIN_FLEET_UID, HR.MAX_FLEET_UID) == (codex_recovery.MIN_FLEET_UID, codex_recovery.MAX_FLEET_UID)


# --- fragments ------------------------------------------------------------------------


def test_a_managed_seat_fragment_validates_alone_and_merged():
    doc = _instance(newseat=_ident(acl=["user::rwx", "group::r-x", "other::---"]), oldseat=_ident(3600, root_half="observe-only"))
    merged = HR.seat_merge(_base(), doc, lambda: ROOT | _existing("oldseat", 3600), plan_time=True)
    assert set(merged.fragment_sha256) == {"role-newseat"}  # observe-only emits nothing
    [frag] = [HR.fragment_for(i) for i in HR.identities(doc).values() if i.root_half == "managed"]
    F.load_manifest(frag)
    m = F.load_manifest(merged.doc)
    assert m.section("accounts")["newseat"] == {"uid": 3500, "home": "/home/newseat", "shell": "/bin/bash", "linger": True}
    assert m.section("paths")["/home/newseat"]["mode"] == "0750"
    assert m.doc["installer_roots"] == sorted(["/srv/pantheon", "/home/newseat"])


def test_observe_only_seats_never_reach_host_substrate_even_when_unobserved():
    """mason amendment 2(b): the merge gates only fragment-emitting seats."""

    doc = _instance(oldseat=_ident(3600, root_half="observe-only"))
    assert HR.seat_merge(_base(), doc, lambda: pytest.fail("asked"), plan_time=True).fragment_sha256 == {}


# --- (a) add-only, collision refuses -----------------------------------------------


def _frag(account, uid, **extra):
    return HR.fragment_for(HR.Identity(f"role-{account}", account, uid, "/bin/bash", "0750", (), (), "managed")) | extra


@pytest.mark.parametrize("case", ("account", "path", "root-equal", "root-nested", "uid", "fragment-fragment-path"))
def test_a_collisions_refuse_and_never_override(case):
    base = _base()
    frags = {"role-x": _frag("xseat", 3500)}
    if case == "account":
        frags = {"role-alice": _frag("alice", 3500)}
    elif case == "path":
        base["paths"]["/home/xseat"] = {"owner": "alice", "group": "pmail", "mode": "0750"}
    elif case == "root-equal":
        base["installer_roots"] = ["/srv/pantheon", "/home/xseat"]
    elif case == "root-nested":
        base["installer_roots"] = ["/srv/pantheon", "/home"]
    elif case == "uid":
        frags["role-y"] = _frag("yseat", 3500)
    else:
        frags["role-y"] = _frag("yseat", 3501)
        frags["role-y"]["paths"] = {"/home/xseat": {"owner": "yseat", "group": "yseat", "mode": "0750"}}
    snapshot = copy.deepcopy(base)
    with pytest.raises(HR.IdentityRefused, match="collides|overlaps"):
        HR.merge(base, frags)
    assert base == snapshot  # the base is never rewritten


def test_a_alice_collision_through_the_resolver_refuses():
    with pytest.raises(HR.IdentityRefused, match="accounts.alice .*collides with base"):
        HR.seat_merge(_base(), _instance(alice=_ident(3500)), lambda: {}, plan_time=False)


# --- (b) FAMILIES unchanged -----------------------------------------------------------


def test_b_families_is_the_closed_org082_enum_and_the_contract_adds_no_kind():
    from tools.host_control.envelope import ORG082_SUBSTRATE_FAMILIES

    expected = {"account.ensure", "account.retire", "group.ensure", "group.member", "acl.converge", "owner_mode.set",
                "sudoers_fragment.ensure", "sudoers_fragment.retire", "unit.system.install", "unit.system.enable",
                "dbus_policy.install", "dbus_policy.retire", "mailbox_dir.ensure", "mailbox_dir.retire",
                "linger.enable", "linger.disable"}
    assert set(F.FAMILIES) == expected == set(ORG082_SUBSTRATE_FAMILIES)
    contract = yaml.safe_load((COMPONENTS / "host-substrate.yaml").read_text(encoding="utf-8"))["component"]
    assert set(contract["privilege"]["bootstrap_root"]) == expected and set(contract["privilege"]) == {"bootstrap_root"}


# --- (c) order-independent merged digest -------------------------------------------


def test_c_merged_digest_is_order_independent_and_the_empty_merge_is_the_base():
    a, b, c = _frag("aseat", 3500), _frag("bseat", 3501), _frag("cseat", 3502)
    digests = set()
    for order in ((a, b, c), (c, b, a), (b, c, a)):
        frags = {f"role-{next(iter(f['accounts']))}": f for f in order}
        base = _base()
        base["installer_roots"] = list(reversed(base["installer_roots"] + ["/srv/zz"]))
        reordered = {k: frags[k] for k in reversed(list(frags))}
        for fr in (frags, reordered):
            digests.add(F.load_manifest(HR.merge(base, fr)).sha256)
    assert len(digests) == 1
    base = _base()
    assert HR.merge(base, {}) is base
    assert F.load_manifest(HR.merge(base, {})).sha256 == F.load_manifest(_base()).sha256


# --- (d) the D15 repin ----------------------------------------------------------------


def test_d_host_substrate_handlers_digest_is_pinned_and_covers_the_resolver(tmp_path):
    contract = yaml.safe_load((COMPONENTS / "host-substrate.yaml").read_text(encoding="utf-8"))["component"]
    pinned = compute_handlers_digest(COMPONENTS / "host-substrate_handlers.py")
    assert contract["handlers_digest"] == pinned.digest
    assert "harness_manager_root.py" in pinned.files
    copy_dir = tmp_path / "tools" / "germinate" / "components"
    copy_dir.mkdir(parents=True)
    for name in ("host-substrate_handlers.py", "harness_manager_root.py"):
        shutil.copy(COMPONENTS / name, copy_dir / name)
    (copy_dir / "harness_manager_root.py").write_text(
        (copy_dir / "harness_manager_root.py").read_text(encoding="utf-8") + "\n# byte\n", encoding="utf-8")
    assert compute_handlers_digest(copy_dir / "host-substrate_handlers.py").digest != pinned.digest


# --- (e) seat scope --------------------------------------------------------------------

SEATS = frozenset({"newseat", "otherseat"})
OBSERVED = {**ROOT,
            "passwd:otherseat": {"uid": 3501, "gid": 3501, "home": "/home/otherseat", "shell": "/bin/bash"},
            "passwd:kino": {"uid": 997, "gid": 987, "home": "/var/lib/kino", "shell": "/usr/sbin/nologin"},
            "passwd:nobody": {"uid": 65534, "gid": 65534, "home": "/nonexistent", "shell": "/usr/sbin/nologin"},
            "passwd:helper": {"uid": 3700, "gid": 3700, "home": "/home/helper", "shell": "/bin/sh"},
            "group:sysgrp": {"gid": 120}}
ACL_BASE = ["user::rwx", "group::r-x", "other::---", "mask::r-x"]


def _scope_frag(mutate):
    frag = _frag("newseat", 3500)
    mutate(frag)
    return frag


def _acl(entry):
    def mutate(frag):
        frag["paths"]["/home/newseat"]["acl"] = ACL_BASE + [entry]
    return mutate


_ROOT_FRAG = {"accounts": {"root": {"uid": 3500, "home": "/home/root", "shell": "/bin/bash"}},
              "installer_roots": ["/home/root"],
              "paths": {"/home/root": {"owner": "root", "group": "root", "mode": "0750"}}}

#: case -> (account, seat_accounts, mutate, match). Every case is pinned by message
#: (R1-4), and the observation carries passwd:root and passwd:otherseat, so the
#: by-name checks are what refuse, never a fallback "not observed".
E_REFUSALS = {
    # R1-4: a hostile instance.yaml listing root as a role account reaches the by-name branch.
    "root-seat": ("root", SEATS | {"root"}, lambda f: (f.clear(), f.update(schema=F.MANIFEST_SCHEMA, **copy.deepcopy(_ROOT_FRAG))),
                  "'root' is not a seat account"),
    "not-a-seat": ("stranger", SEATS, lambda f: None, "'stranger' is not a seat account"),
    "root-account": ("newseat", SEATS, lambda f: f.update(accounts={"root": {"uid": 3500, "home": "/home/root", "shell": "/bin/bash"}}),
                     "accounts must be exactly the seat's own"),
    "system-uid": ("newseat", SEATS, lambda f: f["accounts"]["newseat"].update(uid=999), "root or a system uid"),
    "uid-zero": ("newseat", SEATS, lambda f: f["accounts"]["newseat"].update(uid=0), "root or a system uid"),
    "uid-nobody": ("newseat", SEATS, lambda f: f["accounts"]["newseat"].update(uid=65534), "outside the fleet range"),
    "owner-root": ("newseat", SEATS, lambda f: f["paths"]["/home/newseat"].update(owner="root"), "must be owned by newseat:newseat"),
    "group-other-seat": ("newseat", SEATS, lambda f: f["paths"]["/home/newseat"].update(group="otherseat"),
                         "must be owned by newseat:newseat"),
    "other-seat-home": ("newseat", SEATS, lambda f: f["paths"].update(
        {"/home/otherseat/x": {"owner": "newseat", "group": "newseat", "mode": "0750"}}), "'/home/otherseat/x' is not the seat's own"),
    "prefix-not-home": ("newseat", SEATS, lambda f: f["paths"].update(
        {"/home/newseatx": {"owner": "newseat", "group": "newseat", "mode": "0750"}}), "'/home/newseatx' is not the seat's own"),
    "outside-home": ("newseat", SEATS, lambda f: f["paths"].update(
        {"/srv/pantheon/x": {"owner": "newseat", "group": "newseat", "mode": "0750"}}), "'/srv/pantheon/x' is not the seat's own"),
    "other-seat-account": ("newseat", SEATS, lambda f: f["accounts"].update(
        otherseat={"uid": 3501, "home": "/home/otherseat", "shell": "/bin/bash"}), "accounts must be exactly the seat's own"),
    "home-elsewhere": ("newseat", SEATS, lambda f: f["accounts"]["newseat"].update(home="/srv/newseat"),
                       "home '/srv/newseat' is not '/home/newseat'"),
    # T-6167 Q3: memberships are in scope; with no declared/base groups handed in, any one refuses.
    "memberships-undeclared": ("newseat", SEATS, lambda f: f["accounts"]["newseat"].update(groups=["pmail"]),
                               "not named in this seat's identity block"),
    "roots": ("newseat", SEATS, lambda f: f.update(installer_roots=["/home"]), "installer_roots must be exactly"),
    "groups-section": ("newseat", SEATS, lambda f: f.update(groups={"seatgrp": {"gid": 3900}}), "may not populate 'groups'"),
    "mailbox-section": ("newseat", SEATS, lambda f: f.update(mailbox_dirs={"m": {
        "path": "/home/newseat/m", "owner": "newseat", "group": "newseat", "mode": "2770"}}), "may not populate 'mailbox_dirs'"),
    "sudoers-section": ("newseat", SEATS, lambda f: f.update(sudoers_templates={"t": {}}), "may not populate 'sudoers_templates'"),
    "signoff": ("newseat", SEATS, lambda f: f.update(signoff="approved"), "may not carry 'signoff'"),
    "acl-root": ("newseat", SEATS, _acl("user:root:r-x"), "names root"),
    "acl-system-user": ("newseat", SEATS, _acl("user:kino:--x"), "names a system id 997"),
    "acl-system-group": ("newseat", SEATS, _acl("group:sysgrp:r-x"), "names a system id 120"),
    "acl-nobody": ("newseat", SEATS, _acl("user:nobody:r-x"), "names id 65534, above the fleet range"),
    "acl-other-seat": ("newseat", SEATS, _acl("user:otherseat:r-x"), "names another seat's account"),
    "acl-unobserved": ("newseat", SEATS, _acl("user:ghost:r-x"), "is not observed"),
}


@pytest.mark.parametrize("case", sorted(E_REFUSALS))
def test_e_seat_scope_refusals(case):
    account, seats, mutate, match = E_REFUSALS[case]
    with pytest.raises(HR.IdentityRefused, match=re.escape(match)):
        HR.check_seat_scope(f"role-{account}", account, _scope_frag(mutate), seat_accounts=seats, subjects=OBSERVED)


def test_e_own_paths_and_a_non_seat_non_system_qualifier_pass():
    frag = _scope_frag(_acl("user:helper:r-x"))
    frag["paths"]["/home/newseat/work"] = {"owner": "newseat", "group": "newseat", "mode": "0700"}
    HR.check_seat_scope("role-newseat", "newseat", frag, seat_accounts=SEATS, subjects=OBSERVED)
    assert HR.own_path("newseat", "/home/newseat") and HR.own_path("newseat", "/home/newseat/a/b")
    assert not HR.own_path("newseat", "/home/newseatx") and not HR.own_path("newseat", "/home/other")


def test_e_a_kino_shaped_acl_in_an_identity_block_refuses_at_merge():
    doc = _instance(newseat=_ident(acl=["user::rwx", "group::r-x", "other::---", "user:kino:--x", "mask::r-x"]))
    with pytest.raises(HR.IdentityRefused, match="system id 997"):
        HR.seat_merge(_base(), doc, lambda: {k: v for k, v in OBSERVED.items() if k != "passwd:otherseat"},
                      plan_time=True)


# --- (f) provenance --------------------------------------------------------------------


def test_f_provenance_refusal_classifies_extra_missing_and_changed():
    assert HR.provenance_refusal(None, {}) is None
    assert HR.provenance_refusal({"r": "a"}, {"r": "a"}) is None
    assert "unrecorded" in HR.provenance_refusal({}, {"r": "a"})
    assert "absent" in HR.provenance_refusal({"r": "a"}, {})
    assert "changed since plan" in HR.provenance_refusal({"r": "a"}, {"r": "b"})
    assert "malformed" in HR.provenance_refusal(["r"], {})


def _seat_repo(tmp_path, instance_doc, manifest=None):
    repo = HS._repo(tmp_path, manifest)
    (repo / "instance.yaml").write_text(yaml.safe_dump(instance_doc, sort_keys=False), encoding="utf-8")
    return repo


def _seat_op(eng):
    plan = eng.plan("install", "system", CID)
    ops = plan.nodes[0].operations
    return plan, ops, next(op for op in ops if op.target == "passwd:newseat")


def test_f_plan_records_base_and_fragment_digests_next_to_the_merged_digest(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = _seat_engine(tmp_path, repo)
    _plan, ops, _op = _seat_op(eng)
    base = F.load_manifest(HS.fixture_manifest()).sha256
    frag = HR.fragment_sha256(HR.fragment_for(HR.identities(_instance(newseat=_ident()))["role-newseat"]))
    assert {json.dumps(op.provenance, sort_keys=True) for op in ops} == {json.dumps(
        {"planner": "host-substrate", "manifest_sha256": ops[0].provenance["manifest_sha256"],
         "base_manifest_sha256": base, "fragment_sha256:role-newseat": frag}, sort_keys=True)}
    assert HR.recorded_fragments(ops[0].provenance) == {"role-newseat": frag}
    assert ops[0].provenance["manifest_sha256"] != base


@pytest.mark.parametrize("case", ("unrecorded", "absent", "changed"))
def test_f_handler_refuses_an_extra_unrecorded_or_changed_fragment(tmp_path, case):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = _seat_engine(tmp_path, repo)
    _plan, _ops, op = _seat_op(eng)
    handler = eng.backend.handler(op.kind)
    assert handler.preflight(op).ok
    if case == "unrecorded":  # instance.yaml gains a managed seat after plan
        (repo / "instance.yaml").write_text(yaml.safe_dump(_instance(newseat=_ident(), second=_ident(3501))), encoding="utf-8")
        bad = op
    elif case == "absent":
        bad = replace(op, provenance=op.provenance | {"fragment_sha256:role-ghost": "0" * 64})
    else:
        bad = replace(op, provenance=op.provenance | {"fragment_sha256:role-newseat": "0" * 64})
    result = handler.preflight(bad)
    assert not result.ok and result.classification == "refused"
    assert {"unrecorded": "unrecorded fragment", "absent": "recorded fragment(s) absent",
            "changed": "changed since plan"}[case] in result.detail


def test_f_the_host_broker_manifest_doc_seam_carries_no_fragments(tmp_path):
    handlers = _load(COMPONENTS / "host-substrate_handlers.py", "_t5827_hsh")
    doc = HS.fixture_manifest() | {"units": {}, "dbus_policies": {}}  # no sources to re-hash
    m, frags = handlers._seat_manifest(doc, tmp_path)
    assert frags == {} and m.sha256 == F.load_manifest(doc).sha256


def test_f_no_external_fragment_channel():
    import inspect

    planner = _load(COMPONENTS / "host-substrate.py", "_t5827_hs_sig")
    assert list(inspect.signature(planner._manifest).parameters) == ["request"]
    assert list(inspect.signature(HR.seat_merge).parameters) == ["base_doc", "instance_doc", "subjects", "plan_time"]


# --- (2) drift check -------------------------------------------------------------------


def _doc_one(**ident):
    return _instance(seat=_ident(3600, **({"root_half": "observe-only"} | ident)))


DRIFT = {
    "uid": ({}, lambda s: s["passwd:seat"].update(uid=3601), "drift"),
    "home": ({}, lambda s: s["passwd:seat"].update(home="/srv/seat"), "drift"),
    "shell": ({}, lambda s: s["passwd:seat"].update(shell="/bin/sh"), "drift"),
    "home_mode": ({}, lambda s: s["owner_mode:/home/seat"].update(mode="0755"), "drift"),
    "home_owner": ({}, lambda s: s["owner_mode:/home/seat"].update(owner="root"), "drift"),
    "group-missing": ({"groups": ["pmail"]}, lambda s: s.update({"member:pmail:seat": None}), "drift"),
    "group-extra": ({}, lambda s: s.update({"member:extra:seat": True}), "drift"),
    "acl": ({"acl": ["user::rwx", "group::r-x", "other::---"]},
            lambda s: s.update({"acl:/home/seat": {"entries": ["user::rwx"]}}), "drift"),
    "unobserved-passwd": ({}, lambda s: s.pop("passwd:seat"), "could-not-ask"),
    "unobserved-home": ({}, lambda s: s.pop("owner_mode:/home/seat"), "could-not-ask"),
    "unobserved-member": ({"groups": ["pmail"]}, lambda s: s.pop("member:pmail:seat"), "could-not-ask"),
    "unobserved-acl": ({"acl": ["user::rwx", "group::r-x", "other::---"]}, lambda s: s.pop("acl:/home/seat"), "could-not-ask"),
    "managed-gid-ne-uid": ({"root_half": "managed"}, lambda s: s["passwd:seat"].update(gid=3999), "not-family-expressible"),
}


@pytest.mark.parametrize("case", sorted(DRIFT))
def test_2_drift_check_finds_each_mismatch_fail_closed(case):
    ident, mutate, code = DRIFT[case]
    subjects = _existing("seat", 3600, groups=ident.get("groups", ()))
    if ident.get("acl"):
        subjects["acl:/home/seat"] = {"entries": sorted(ident["acl"])}
    assert HR.drift_check(_doc_one(**ident), copy.deepcopy(subjects)) == []  # clean before the mutation
    mutate(subjects)
    findings = HR.drift_check(_doc_one(**ident), subjects)
    assert findings and {f.code for f in findings} == {code}


def test_2_observe_only_gid_ne_uid_is_not_a_finding():
    """Option alpha: an observe-only block carries no gid; only managed needs gid == uid."""

    assert HR.drift_check(_doc_one(), _existing("seat", 3600, gid=3999)) == []


def test_2_a_managed_existing_account_with_gid_ne_uid_refuses_the_plan(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = H.engine(tmp_path, repo)
    HS._seed(eng, _existing("newseat", 3500, gid=3999))
    with pytest.raises(EngineRefusal, match="never re-gid a live account"):
        eng.plan("install", "system", CID)
    assert "passwd:newseat" in HS._subjects(eng) and HS._subjects(eng)["passwd:newseat"]["gid"] == 3999


def test_2_a_managed_existing_account_that_matches_plans_no_account_op(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = H.engine(tmp_path, repo)
    seed = _existing("newseat", 3500)
    seed["passwd:newseat"]["locked"] = False
    seed["linger:newseat"] = True
    HS._seed(eng, seed)
    _plan = eng.plan("install", "system", CID)
    targets = {op.target for op in _plan.nodes[0].operations}
    assert not targets & {"passwd:newseat", "linger:newseat", "owner_mode:/home/newseat"}


def test_2_drift_check_and_merge_never_read_passwd_group_or_a_process(monkeypatch):
    import grp
    import pwd
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("the host was read")

    for mod, names in ((pwd, ("getpwnam", "getpwuid", "getpwall")), (grp, ("getgrnam", "getgrgid", "getgrall")),
                       (subprocess, ("run", "Popen", "check_output"))):
        for name in names:
            monkeypatch.setattr(mod, name, boom)
    doc = HR.load_instance(REPO_ROOT / "instance.yaml")
    assert HR.drift_check(doc, {})  # every real seat is could-not-ask against an empty observation
    assert {f.code for f in HR.drift_check(doc, {})} == {"could-not-ask"}
    HR.seat_merge(_base(), _instance(newseat=_ident()), lambda: ROOT, plan_time=True)


# --- (g) new-seat safety ---------------------------------------------------------------

G_REFUSALS = {
    "uid-below-range": ({"uid": 999}, {}),
    "uid-above-range": ({"uid": 60001}, {}),
    "uid-used": ({}, {"passwd:someone": {"uid": 3500, "gid": 3500, "home": "/home/someone", "shell": "/bin/sh"}}),
    "uid-is-a-gid": ({}, {"group:somegroup": {"gid": 3500}}),
    "private-group-exists": ({}, {"group:newseat": {"gid": 4100}}),
    "home-0755": ({"home_mode": "0755"}, {}),
    "home-0770": ({"home_mode": "0770"}, {}),
    "home-0751": ({"home_mode": "0751"}, {}),
    **{f"group-{g}": ({"groups": [g]}, {}) for g in sorted(HR.ROOT_EQUIVALENT_GROUPS)},
}


@pytest.mark.parametrize("case", sorted(G_REFUSALS))
def test_g_new_seat_refusals(case):
    ident, subjects = G_REFUSALS[case]
    doc = _instance(newseat=_ident(**({"uid": 3500} | ident)))
    subjects = ROOT | subjects
    findings = HR.drift_check(doc, subjects)
    assert findings and {f.code for f in findings} == {"unsafe-new-seat"}
    with pytest.raises(HR.IdentityRefused, match="unsafe-new-seat"):
        HR.seat_merge(_base(), doc, lambda: subjects, plan_time=True)


@pytest.mark.parametrize("mode", ("0750", "0700", "0710"))
def test_g_a_safe_new_seat_passes(mode):
    doc = _instance(newseat=_ident(home_mode=mode, groups=["pantheon-mailbox"]))
    assert HR.drift_check(doc, ROOT | {"group:pantheon-mailbox": {"gid": 3001}}) == []


def test_g_uid_is_explicit_and_acl_has_no_path_field():
    ident = _ident()
    del ident["uid"]
    with pytest.raises(HR.IdentityRefused, match="never auto-allocated"):
        HR.identities(_instance(newseat=ident))
    with pytest.raises(HR.IdentityRefused, match="acl"):
        HR.identities(_instance(newseat=_ident() | {"acl": {"/srv/elsewhere": ["user::rwx"]}}))
    with pytest.raises(HR.IdentityRefused, match="root_half"):
        HR.identities(_instance(newseat=_ident(root_half="auto")))


# --- engine tier: ordering, apply, verify, rollback -----------------------------------


def test_engine_seat_install_waits_for_the_root_half_then_proceeds(tmp_path):
    """S3 (executor.py _dependency_scope): a seat-scope dependent of host-substrate is
    refused until host-substrate's system install (carrying the seat's root half) is SATISFIED."""

    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    _p, dep_digest = H.write_contract(repo, "seat-user", ops={"install": [H.dir_op("u-1", "/home/newseat/x")]},
                                      scopes=["seat"], depends_on=[CID])
    hs_digest = yaml.safe_load((repo / "tools/germinate/components/host-substrate.yaml").read_text())["component"]["contract_digest"]
    H.make_checkout(repo, [H.row(CID, hs_digest), H.row("seat-user", dep_digest)])
    eng = _seat_engine(tmp_path, repo)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", "seat-newseat", "seat-user")
    assert info.value.code == "dependency-not-installed"
    plan = eng.plan("install", "system", CID)
    kinds = [(op.kind, op.target) for op in plan.nodes[0].operations if "newseat" in op.target]
    assert kinds == [("account.ensure", "passwd:newseat"), ("linger.enable", "linger:newseat"),
                     ("owner_mode.set", "owner_mode:/home/newseat")]  # linger after the account
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    subjects = HS._subjects(eng)
    assert subjects["passwd:newseat"] == {"uid": 3500, "gid": 3500, "home": "/home/newseat", "shell": "/bin/bash", "locked": False}
    assert subjects["linger:newseat"] is True
    [result] = eng.verify("system", CID)
    assert result["ok"]
    eng.plan("install", "seat-newseat", "seat-user")  # now admitted


def test_engine_every_seat_op_resolves_against_the_merged_manifest(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident(acl=["user::rwx", "group::r-x", "other::---"])))
    eng = _seat_engine(tmp_path, repo)
    _plan, ops, _op = _seat_op(eng)
    merged = F.load_manifest(HR.seat_merge(HS.fixture_manifest(), _instance(newseat=_ident(
        acl=["user::rwx", "group::r-x", "other::---"])), lambda: ROOT, plan_time=True).doc)
    for op in ops:
        assert F.resolve(op.kind, dict(op.desired), merged).subject == op.target.removeprefix("staged:")
    assert "acl.converge" in {op.kind for op in ops}


def test_engine_kill_mid_seat_root_half_then_rollback_restores_the_preimage(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = H.engine(tmp_path, repo)
    seed = HS._converged() | ROOT
    HS._seed(eng, seed)
    plan = eng.plan("install", "system", CID)
    ops = plan.nodes[0].operations
    assert [op.target for op in ops] == ["passwd:newseat", "linger:newseat", "owner_mode:/home/newseat"]
    eng.backend.faults.kill = (ops[1].operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    assert restarted.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert HS._subjects(restarted) == seed


# --- purity and the dark pins ------------------------------------------------------------


def test_resolver_and_host_substrate_plan_are_pure_under_planner_guard():
    planner = _load(COMPONENTS / "host-substrate.py", "_t5827_hs_guard")
    doc = HR.load_instance(REPO_ROOT / "instance.yaml")
    guard = PlannerGuard()
    with guard:
        HR.drift_check(doc, {})
        HR.seat_merge(_base(), _instance(newseat=_ident()), lambda: ROOT, plan_time=True)
        planner.plan_install({"observed": {CID: {"subjects": {}}}})
    assert guard.violations == []


def test_dark_pins_hold_and_the_resolver_imports_no_live_backend():
    import ast

    from tools import host_broker_install
    from tools.germinate.host_substrate import ports

    assert ports.LIVE_ACTUATION_ENABLED is False and host_broker_install.LIVE_ACTUATION_ENABLED is False
    tree = ast.parse((COMPONENTS / "harness_manager_root.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{a.name}" for a in node.names)
    banned = ("subprocess", "pwd", "grp", "socket", "tools.runtime_controller", "tools.persona_installer_apply",
              "tools.germinate.host_substrate.ports", "tools.germinate.host_substrate.helper")
    assert not {n for n in names if any(n == b or n.startswith(b + ".") for b in banned)}
    assert "LIVE_ACTUATION_ENABLED" not in (COMPONENTS / "harness_manager_root.py").read_text(encoding="utf-8")


# --- U1 unchanged --------------------------------------------------------------------------


@pytest.mark.parametrize("persona", ("mason", "orchestrator"))
def test_u1_plan_install_is_identical_with_and_without_the_identity_blocks(persona, monkeypatch):
    from tools import persona_installer as PI

    hm = _load(COMPONENTS / "harness-manager.py", f"_t5827_hm_{persona}")
    manifest = PI.Manifest.load(REPO_ROOT / "instance.yaml")
    account = manifest.binding(persona).account
    request = {"scope": f"seat-{account}", "desired": {"source_revision": "0123456789abcdef" * 2 + "01234567"}, "observed": {}}
    with_blocks = json.dumps(hm.plan_install(request))
    raw = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
    for binding in raw["roles"].values():
        binding.pop("identity", None)
    stripped = yaml.safe_dump(raw, sort_keys=False).encode("utf-8")
    monkeypatch.setattr(PI.Manifest, "load", classmethod(lambda cls, path, **kw: PI.Manifest.from_bytes(path, stripped, **kw)))
    assert json.dumps(hm.plan_install(request)) == with_blocks


# --- review round 1 ---------------------------------------------------------------------


@pytest.mark.parametrize("observation", ("empty", "root-only", "others-only"))
def test_r1_1_a_managed_absent_account_is_new_only_when_passwd_is_proven_listed(observation):
    """R1-1: absence means "new seat" only when passwd:root AND every non-managed role
    account is observed; otherwise could-not-ask (fail closed)."""

    doc = _instance(newseat=_ident(), oldseat=_ident(3600, root_half="observe-only"))
    subjects = {"empty": {}, "root-only": dict(ROOT), "others-only": _existing("oldseat", 3600)}[observation]
    findings = [f for f in HR.drift_check(doc, subjects) if f.account == "newseat"]
    assert [(f.code, f.field) for f in findings] == [("could-not-ask", "passwd")]
    assert not HR.passwd_fully_listed(doc, subjects)
    with pytest.raises(HR.IdentityRefused, match="not proven fully listed"):
        HR.seat_merge(_base(), doc, lambda: subjects, plan_time=True)
    listed = ROOT | _existing("oldseat", 3600)
    assert HR.passwd_fully_listed(doc, listed)
    assert set(HR.seat_merge(_base(), doc, lambda: listed, plan_time=True).fragment_sha256) == {"role-newseat"}


@pytest.mark.parametrize("observation", ("empty", "root-only"))
def test_r1_1_the_review_probe_suborch1_flipped_to_managed_refuses(observation):
    doc = HR.load_instance(REPO_ROOT / "instance.yaml")
    doc["roles"]["engineering-orchestrator-1"]["identity"]["root_half"] = "managed"
    subjects = {"empty": {}, "root-only": dict(ROOT)}[observation]
    with pytest.raises(HR.IdentityRefused, match="suborch1.*could-not-ask passwd"):
        HR.seat_merge(_base(), doc, lambda: subjects, plan_time=True)


def test_r1_1_an_empty_sim_observation_refuses_the_host_substrate_plan(tmp_path):
    repo = _seat_repo(tmp_path, _instance(newseat=_ident()))
    eng = H.engine(tmp_path, repo)  # nothing seeded: passwd not proven listed
    with pytest.raises(EngineRefusal, match="not proven fully listed"):
        eng.plan("install", "system", CID)


def _break_a_block(repo, doc):
    broken = copy.deepcopy(doc)
    broken["roles"]["role-broken"] = {"persona": "p-broken", "account": "broken", "harness": "claude-code",
                                      "identity": _ident(3990, root_half="observe-only", shell="/bin/zsh")}
    (repo / "instance.yaml").write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")


def test_r1_2_a_base_only_op_rolls_back_despite_a_broken_identity_block(tmp_path):
    doc = _instance(oldseat=_ident(3600, root_half="observe-only"))
    repo = _seat_repo(tmp_path, doc)
    eng = H.engine(tmp_path, repo)
    plan = eng.plan("install", "system", CID)
    op = plan.nodes[0].operations[0]
    assert not HR.recorded_fragments(op.provenance)
    handler = eng.backend.handler(op.kind)
    before = handler.port.get(op.target)
    mutation, _evidence = handler.execute(op)
    assert "fragment_sha256" not in mutation.before
    assert handler.port.get(op.target) != before
    _break_a_block(repo, doc)
    assert not handler.preflight(op).ok  # plan/apply stay fail-closed
    handler.rollback_mutation(mutation)
    assert handler.port.get(op.target) == before


def test_r1_2_a_fragment_op_refuses_rollback_on_a_broken_identity_block(tmp_path):
    doc = _instance(newseat=_ident())
    repo = _seat_repo(tmp_path, doc)
    eng = _seat_engine(tmp_path, repo)
    _plan, _ops, op = _seat_op(eng)
    handler = eng.backend.handler(op.kind)
    mutation, _evidence = handler.execute(op)
    assert json.loads(mutation.before["fragment_sha256"]) == HR.recorded_fragments(op.provenance)
    applied = handler.port.get(op.target)
    _break_a_block(repo, doc)
    with pytest.raises(F.ManifestError, match="seat identity refused"):
        handler.rollback_mutation(mutation)
    assert handler.port.get(op.target) == applied  # never resolved against the base alone


def test_r1_2_a_base_only_kill_then_rollback_completes_after_a_block_breaks(tmp_path):
    doc = _instance(oldseat=_ident(3600, root_half="observe-only"))
    repo = _seat_repo(tmp_path, doc)
    eng = H.engine(tmp_path, repo)
    seed = {k: v for k, v in HS._converged().items() if not k.startswith("group:")}
    HS._seed(eng, seed)
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    eng.backend.faults.kill = (op.operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    _break_a_block(repo, doc)
    restarted = H.engine(tmp_path, repo)
    assert restarted.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert HS._subjects(restarted) == seed
