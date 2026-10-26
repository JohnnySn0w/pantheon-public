"""ORG-081 validator: schema, closed enums, evidence/adoption rules, refs, D-13 aliases.

Every test builds its document in memory or under tmp_path; nothing reads the
real registry/, tickets/ or schedules/ of this checkout.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tools.germinate.registry import schema as S
from tools.germinate.registry.load import RegistryLoadError, loads
from tools.germinate.registry.validate import (
    ERROR,
    WARNING,
    check_aliases,
    check_refs,
    errors,
    schedule_systems,
    validate_document,
    validate_repo,
)
from tools.germinate.registry.tests import fixtures as F


def _msgs(findings) -> str:
    return "\n".join(str(f) for f in errors(findings))


def assert_clean(d) -> None:
    bad = errors(validate_document(d))
    assert not bad, _msgs(bad)


def assert_refused(d, needle: str) -> None:
    text = _msgs(validate_document(d))
    assert needle in text, f"expected {needle!r} in:\n{text}"


# ---------------------------------------------------------------- shape


def test_minimal_document_is_clean():
    assert_clean(F.doc())


def test_adopted_and_partial_fixtures_are_clean():
    assert_clean(F.doc(F.adopted(id="a"), F.partial(id="p"), F.external()))


@pytest.mark.parametrize("schema_value", [None, "pantheon-system-registry/v1", "pantheon-system-registry/v0 "])
def test_schema_line_must_be_exact(schema_value):
    d = F.doc()
    if schema_value is None:
        del d["schema"]
        assert_refused(d, "missing required field 'schema'")
    else:
        d["schema"] = schema_value
        assert_refused(d, "schema must be exactly")


def test_unknown_top_level_key_refused():
    d = F.doc()
    d["extras"] = 1
    assert_refused(d, "field 'extras' is not allowed")


def test_unknown_row_key_refused_per_kind():
    assert_refused(F.doc(F.component(colour="red")), "field 'colour' is not allowed")
    assert_refused(F.doc(F.external(colour="red")), "field 'colour' is not allowed")


def test_unknown_cell_key_refused():
    row = F.component(legacy_coverage={"install": F.leg("covered", ticket=F.TICKET)})
    assert_refused(F.doc(row), "field 'ticket' is not allowed")


def test_unknown_leg_refused():
    row = F.component(legacy_coverage={"repair": F.leg("covered")})
    assert_refused(F.doc(row), "leg 'repair' is not one of")


def test_duplicate_yaml_key_refused_at_load():
    with pytest.raises(RegistryLoadError, match="duplicate key 'status'"):
        loads("a:\n  status: covered\n  status: missing\n")


def test_duplicate_id_refused():
    assert_refused(F.doc(F.component(), F.component()), "duplicate id 'comp-a'")


@pytest.mark.parametrize("bad_id", ["Comp", "comp_a", "-comp", "", 7])
def test_id_must_be_kebab(bad_id):
    assert_refused(F.doc(F.component(id=bad_id)), "is not a lower-kebab-case id")


@pytest.mark.parametrize("kind", [None, "plane", "Component"])
def test_kind_is_a_required_discriminator(kind):
    row = F.component()
    if kind is None:
        del row["kind"]
    else:
        row["kind"] = kind
    assert_refused(F.doc(row), "kind")


@pytest.mark.parametrize("field", ["contract", "contract_adoption", "adoption_evidence", "legs", "legacy_coverage"])
def test_external_plane_refuses_component_fields(field):
    row = F.external(**{field: {}})
    assert_refused(F.doc(row), f"field {field!r} is not allowed")


@pytest.mark.parametrize("field", ["adr", "tracking", "relationship"])
def test_external_plane_requires_its_fields(field):
    row = F.external()
    del row[field]
    assert_refused(F.doc(row), f"missing required field {field!r}")


@pytest.mark.parametrize("field", ["tracking", "relationship"])
def test_component_refuses_external_fields(field):
    assert_refused(F.doc(F.component(**{field: "x"})), f"field {field!r} is not allowed")


# ---------------------------------------------------------------- status enum + evidence rules


@pytest.mark.parametrize("status", ["partial-by-design", "n-a", "Covered", "unknown", None])
def test_status_bytes_are_exact(status):
    row = F.component(legacy_coverage={"install": {"status": status, "waiver": "x"}})
    assert_refused(F.doc(row), "is not one of covered")


@pytest.mark.parametrize(
    "cell, needle",
    [
        ({"status": "covered"}, "missing required field 'evidence'"),
        ({"status": "covered", "evidence": "  "}, "evidence must be a non-empty string"),
        ({"status": "partial", "gap": "x"}, "missing required field 'ticket'"),
        ({"status": "partial", "ticket": F.TICKET}, "missing required field 'gap'"),
        ({"status": "missing", "gap": "x"}, "missing required field 'ticket'"),
        ({"status": "partial_by_design"}, "missing required field 'waiver'"),
        ({"status": "not_applicable"}, "missing required field 'waiver'"),
        ({"status": "not_applicable", "waiver": ""}, "waiver must be a non-empty string"),
        ({"status": "covered", "evidence": "x", "waiver": "y"}, "field 'waiver' is not allowed"),
    ],
)
def test_each_status_requires_its_evidence(cell, needle):
    assert_refused(F.doc(F.component(legacy_coverage={"install": cell})), needle)


@pytest.mark.parametrize("ticket", ["T-12", "t-1234", "1234", "T-1234x", 1234])
def test_ticket_must_be_a_ticket_id(ticket):
    cell = {"status": "partial", "gap": "x", "ticket": ticket}
    assert_refused(F.doc(F.component(legacy_coverage={"install": cell})), "is not a T-NNNN id")


def test_note_is_allowed_on_a_cell():
    assert_clean(F.doc(F.component(legacy_coverage={"install": F.leg("covered", note="context")})))


# ---------------------------------------------------------------- health (D-11)


def test_covered_health_needs_three_part_evidence():
    row = F.component(legacy_coverage={"health": F.leg("covered")})
    assert_refused(F.doc(row), "covered health needs evidence as a mapping")


def test_covered_health_with_two_predicates_refused():
    cell = F.health_covered()
    del cell["evidence"]["collector_alert"]
    assert_refused(F.doc(F.component(legacy_coverage={"health": cell})), "covered health needs evidence")


def test_covered_health_with_three_predicates_is_clean():
    assert_clean(F.doc(F.component(legacy_coverage={"health": F.health_covered()})))


def test_health_waiver_must_acknowledge_e9c3():
    row = F.component(legacy_coverage={"health": F.leg("partial_by_design", waiver="no listener")})
    assert_refused(F.doc(row), "must acknowledge E9.C3")
    row = F.component(legacy_coverage={"health": F.leg("partial_by_design", waiver="E9.C3 not covered: no listener")})
    assert_clean(F.doc(row))


def test_health_cannot_be_not_applicable():
    row = F.component(legacy_coverage={"health": F.leg("not_applicable", waiver="E9.C3 acknowledged: no process")})
    assert_refused(F.doc(row), "health cannot be not_applicable")
    row = F.adopted()
    row["legs"]["health"] = F.leg("not_applicable")
    assert_refused(F.doc(row), "health cannot be not_applicable")


def test_other_legs_may_be_not_applicable():
    assert_clean(F.doc(F.component(legacy_coverage={"remove": F.leg("not_applicable")})))


def test_health_rule_applies_to_adopted_legs_too():
    row = F.adopted()
    row["legs"]["health"] = F.leg("covered")
    assert_refused(F.doc(row), "covered health needs evidence")


# ---------------------------------------------------------------- adoption (D-2, D-8)


@pytest.mark.parametrize("field", ["legs", "adoption_evidence", "adoption_gaps"])
def test_not_adopted_forbids_contract_fields(field):
    row = F.component(**{field: {}})
    assert_refused(F.doc(row), f"{field} is forbidden when contract_adoption is not_adopted")


@pytest.mark.parametrize("value", ["adoptedd", "Partial", None])
def test_adoption_enum_is_closed(value):
    assert_refused(F.doc(F.component(contract_adoption=value)), "contract_adoption")


def test_adopted_forbids_legacy_coverage_and_gaps():
    assert_refused(F.doc(F.adopted(legacy_coverage={"install": F.leg()})), "legacy_coverage is forbidden")
    assert_refused(
        F.doc(F.adopted(adoption_gaps={"sandbox": {"gap": "x", "ticket": F.TICKET}})), "adoption_gaps is forbidden"
    )


@pytest.mark.parametrize("field", S.EVIDENCE_FIELDS)
def test_adopted_requires_every_evidence_field(field):
    row = F.adopted()
    del row["adoption_evidence"][field]
    assert_refused(F.doc(row), f"adopted requires adoption_evidence.{field}")


@pytest.mark.parametrize("status", ["partial", "missing"])
def test_adopted_requires_every_leg_covered_or_waived(status):
    row = F.adopted()
    row["legs"]["update"] = F.leg(status)
    assert_refused(F.doc(row), "adopted requires every leg covered or explicitly waived")


@pytest.mark.parametrize("status", ["partial_by_design", "not_applicable"])
def test_adopted_accepts_waived_legs(status):
    row = F.adopted()
    row["legs"]["remove"] = F.leg(status)
    assert_clean(F.doc(row))


@pytest.mark.parametrize("adoption", ["partial", "adopted"])
def test_non_not_adopted_requires_all_five_legs(adoption):
    row = F.adopted() if adoption == "adopted" else F.partial()
    del row["legs"]["configure"]
    assert_refused(F.doc(row), "missing leg 'configure'")
    del row["legs"]
    assert_refused(F.doc(row), "legs is required")


def test_partial_gap_key_outside_closed_set_refused():
    row = F.partial()
    row["adoption_gaps"]["plan_verify"] = {"gap": "x", "ticket": F.TICKET}
    assert_refused(F.doc(row), "not in the closed 16-predicate set")


@pytest.mark.parametrize("key", sorted(S.PARTIAL_MANDATORY))
def test_partial_mandatory_methods_cannot_be_gaps(key):
    row = F.partial()
    row["adoption_gaps"][key] = {"gap": "x", "ticket": F.TICKET}
    assert_refused(F.doc(row), "mandatory for partial")


def test_partial_absent_evidence_needs_a_gap():
    row = F.partial()
    del row["adoption_evidence"]["emitter"]
    assert_refused(F.doc(row), "adoption_evidence.emitter is absent and has no adoption_gaps entry")


def test_partial_present_evidence_listed_as_gap_refused():
    row = F.partial()
    row["adoption_gaps"]["adoption_evidence.plan"] = {"gap": "x", "ticket": F.TICKET}
    assert_refused(F.doc(row), "adoption_evidence.plan is present but also listed as a gap")


def test_partial_gap_entry_needs_ticket():
    row = F.partial()
    row["adoption_gaps"]["sandbox"] = {"gap": "x"}
    assert_refused(F.doc(row), "missing required field 'ticket'")


def test_partial_with_no_gaps_refused():
    row = F.partial(adoption_evidence={f: "r" for f in S.EVIDENCE_FIELDS}, adoption_gaps={})
    assert_refused(F.doc(row), "partial with no adoption_gaps")


def test_closed_predicate_set_has_cardinality_16():
    assert len(S.PREDICATE_KEYS) == 16
    assert len(S.PREDICATE_KEYS - S.PARTIAL_MANDATORY) == 14


# ---------------------------------------------------------------- contract (D-9, §7)


def test_unpinned_needs_reason_and_not_adopted():
    row = F.component()
    del row["contract"]["unpinned_reason"]
    assert_refused(F.doc(row), "requires a non-empty unpinned_reason")
    row = F.partial(contract={"repo": "pantheon-config", "module": "m", "digest": "unpinned", "unpinned_reason": "r"})
    assert_refused(F.doc(row), "allowed only when contract_adoption is not_adopted")


@pytest.mark.parametrize("digest", ["sha256:abc", "md5:" + "a" * 32, "", None])
def test_digest_format(digest):
    row = F.component(contract=F.pinned_contract(digest=digest))
    assert_refused(F.doc(row), "digest")


def test_pinned_digest_forbids_unpinned_reason():
    row = F.component(contract=F.pinned_contract(unpinned_reason="r"))
    assert_refused(F.doc(row), "unpinned_reason is only allowed with digest: unpinned")


def test_external_repo_pinned_needs_repo_revision():
    row = F.component(contract=F.pinned_contract(repo="echo-prismatic", module="pantheon-component.yaml"))
    assert_refused(F.doc(row), "requires repo_revision")
    row["contract"]["repo_revision"] = F.REVISION
    assert_clean(F.doc(row))
    row["contract"]["repo_revision"] = "main"
    assert_refused(F.doc(row), "is not a 40-hex commit sha")


def test_external_repo_unpinned_needs_no_revision():
    row = F.component(
        contract={"repo": "echo-prismatic", "module": "pantheon-component.yaml", "digest": "unpinned", "unpinned_reason": "r"}
    )
    assert_clean(F.doc(row))


# ---------------------------------------------------------------- repo-root refs


def test_check_refs_clean_on_tmp_repo(tmp_path):
    root = F.make_repo(tmp_path)
    assert errors(check_refs(F.doc(), root)) == []


def test_check_refs_missing_ticket(tmp_path):
    root = F.make_repo(tmp_path, tickets=())
    text = _msgs(check_refs(F.doc(), root))
    assert "ticket T-1234 not found" in text


def test_check_refs_gap_citing_done_ticket_is_an_error(tmp_path):
    root = F.make_repo(tmp_path, tickets=(("done", F.TICKET),))
    text = _msgs(check_refs(F.doc(F.component()), root))
    assert "comp-a.legacy_coverage.remove: ticket T-1234 is done, so it cannot close this gap" in text


def test_check_refs_adoption_gap_citing_done_ticket_is_an_error(tmp_path):
    root = F.make_repo(tmp_path, tickets=(("done", F.TICKET),))
    assert "adoption_gaps.sandbox: ticket T-1234 is done" in _msgs(check_refs(F.doc(F.partial()), root))


def test_check_refs_done_tracking_ticket_only_warns(tmp_path):
    root = F.make_repo(tmp_path, tickets=(("done", F.TICKET),))
    findings = check_refs(F.doc(F.external()), root)
    assert errors(findings) == []
    assert any(f.level == WARNING and "is done" in f.message for f in findings)


def test_check_refs_missing_adr(tmp_path):
    root = F.make_repo(tmp_path, adrs=())
    assert "ORG-081 not found" in _msgs(check_refs(F.doc(), root))


def test_check_refs_unknown_repo(tmp_path):
    root = F.make_repo(tmp_path)
    row = F.component(contract={"repo": "nope", "module": "m", "digest": "unpinned", "unpinned_reason": "r"})
    assert "repo 'nope' is not in repos.yaml" in _msgs(check_refs(F.doc(row), root))


def test_check_refs_unpinned_with_existing_module_refused(tmp_path):
    root = F.make_repo(tmp_path)
    module = root / "tools/germinate/components/comp-a.yaml"
    module.parent.mkdir(parents=True)
    module.write_text("component: {}\n", encoding="utf-8")
    assert "must be pinned" in _msgs(check_refs(F.doc(), root))


# ---------------------------------------------------------------- D-13 aliases


ALIASES = {
    "schema": S.ALIASES_SCHEMA_ID,
    "aliases": [{"alias": "Component A — legacy title", "id": "comp-a", "expires": dt.date(2027, 1, 15), "ticket": F.TICKET}],
}
TODAY = dt.date(2026, 10, 17)


def test_alias_and_canonical_ids_resolve():
    systems = [("s[a]", "Component A — legacy title"), ("s[b]", "comp-a")]
    assert errors(check_aliases(F.doc(), ALIASES, systems, TODAY)) == []


def test_expired_alias_refused():
    findings = check_aliases(F.doc(), ALIASES, [("s[a]", "Component A — legacy title")], dt.date(2027, 1, 16))
    text = _msgs(findings)
    assert "expired 2027-01-15" in text
    assert "neither a registry id nor an unexpired alias" in text


def test_alias_last_day_still_valid():
    assert errors(check_aliases(F.doc(), ALIASES, [("s", "Component A — legacy title")], dt.date(2027, 1, 15))) == []


def test_unknown_system_refused():
    assert "neither a registry id" in _msgs(check_aliases(F.doc(), None, [("s", "Mystery")], TODAY))


def test_alias_target_must_exist():
    bad = {"schema": S.ALIASES_SCHEMA_ID, "aliases": [dict(ALIASES["aliases"][0], id="ghost")]}
    assert "alias target 'ghost' is not a registry id" in _msgs(check_aliases(F.doc(), bad, [], TODAY))


def test_alias_entry_shape_is_closed():
    bad = {"schema": S.ALIASES_SCHEMA_ID, "aliases": [dict(ALIASES["aliases"][0], extra=1)]}
    assert "field 'extra' is not allowed" in _msgs(check_aliases(F.doc(), bad, [], TODAY))


def test_schedule_systems_reads_entries(tmp_path):
    F.write_yaml(tmp_path / "schedules" / "global.yaml", {"entries": [{"name": "a", "system": "comp-a"}, {"name": "b"}]})
    assert schedule_systems(tmp_path / "schedules") == [("schedules/global.yaml[a]", "comp-a")]


# ---------------------------------------------------------------- composition


def test_validate_repo_end_to_end(tmp_path):
    root = F.make_repo(tmp_path)
    F.write_yaml(root / "registry" / "scheduler-aliases.yaml", ALIASES)
    F.write_yaml(root / "schedules" / "global.yaml", {"entries": [{"name": "a", "system": "Component A — legacy title"}]})
    assert errors(validate_repo(root, today=TODAY)) == []
    assert "expired" in _msgs(validate_repo(root, today=dt.date(2027, 2, 1)))


def test_validate_repo_without_aliases_file(tmp_path):
    # Post-cutover shape (T-5105, ORG-081 D-13): the alias file is retired and the
    # schedules name registry ids directly; a legacy title is then refused.
    root = F.make_repo(tmp_path)
    assert not (root / "registry" / "scheduler-aliases.yaml").exists()
    F.write_yaml(root / "schedules" / "global.yaml", {"entries": [{"name": "a", "system": "comp-a"}]})
    assert errors(validate_repo(root, today=TODAY)) == []
    F.write_yaml(root / "schedules" / "global.yaml", {"entries": [{"name": "a", "system": "Component A — legacy title"}]})
    assert "neither a registry id" in _msgs(validate_repo(root, today=TODAY))


def test_validate_repo_alias_ticket_must_exist(tmp_path):
    root = F.make_repo(tmp_path)
    bad = {"schema": S.ALIASES_SCHEMA_ID, "aliases": [dict(ALIASES["aliases"][0], ticket="T-0000")]}
    F.write_yaml(root / "registry" / "scheduler-aliases.yaml", bad)
    assert "ticket T-0000 not found" in _msgs(validate_repo(root, today=TODAY))


def test_validate_repo_reports_load_error(tmp_path):
    root = F.make_repo(tmp_path)
    (root / "registry" / "systems.yaml").write_text("systems: [\n", encoding="utf-8")
    findings = validate_repo(root, today=TODAY)
    assert findings and findings[0].level == ERROR and "not valid YAML" in findings[0].message


def test_gap_cells_citing_matches_the_done_ticket_rule():
    from tools.germinate.registry.validate import gap_cells_citing

    doc = {"systems": [F.component(legacy_coverage={"install": F.leg("missing"), "remove": F.leg("covered")})]}
    assert gap_cells_citing(doc, F.TICKET) == ["comp-a.legacy_coverage.install"]
    assert gap_cells_citing(doc, "T-9999") == []
