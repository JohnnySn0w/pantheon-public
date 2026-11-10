"""T-5074 validator additions: pinned rows checked against their contract modules.

Red-before-green reasoning: before T-5074, ``validate_repo`` accepted any
``sha256:<64 hex>`` digest on a pinned row without opening the module, so
every error asserted below (digest mismatch, missing module, steward drift,
binding drift, D-8 presence, unresolved external) was silently accepted. Only
``test_clean_pinned_row_is_accepted`` is a control that passes either way.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from tools.germinate.engine.tests import helpers as H
from tools.germinate.registry.tests import fixtures as F
from tools.germinate.registry.validate import check_contracts, errors, validate_repo

TODAY = dt.date(2026, 10, 17)
REF = f"org.pantheon.test/v1@{H.LIST_DIGEST}"


def _msgs(findings) -> str:
    return "\n".join(str(f) for f in errors(findings))


def _repo(tmp_path: Path, row_overrides=None, **contract_overrides):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, digest = H.write_contract(repo, "comp-a", **contract_overrides)
    row = H.row("comp-a", digest, **(row_overrides or {}))
    H.make_checkout(repo, [row])
    return repo, path, digest


def _partial(digest: str, gaps: dict) -> dict:
    return {
        "contract_adoption": "partial",
        "adoption_evidence": {f: f"ref-{f}" for f in ("plan", "apply", "receipt", "rollback", "crash_contract", "gates", "emitter")},
        "adoption_gaps": gaps,
        "legs": F.all_legs(),
        "legacy_coverage": None,
    }


def _strip_none(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


def test_clean_pinned_row_is_accepted(tmp_path):
    repo, _path, _digest = _repo(tmp_path)
    assert _msgs(validate_repo(repo, today=TODAY)) == ""


def test_registry_digest_mismatch_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a")
    H.make_checkout(repo, [H.row("comp-a", "sha256:" + "e" * 64)])
    assert "recomputed" in _msgs(validate_repo(repo, today=TODAY))


def test_module_written_digest_mismatch_is_an_error(tmp_path):
    repo, path, digest = _repo(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["component"]["contract_digest"] = "sha256:" + "f" * 64
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")  # digest unchanged: the field is the sentinel slot
    assert "the module's own contract_digest" in _msgs(validate_repo(repo, today=TODAY))


def test_pinned_row_with_missing_module_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.make_checkout(repo, [H.row("comp-a", "sha256:" + "a" * 64)])
    assert "does not exist" in _msgs(validate_repo(repo, today=TODAY))


def test_steward_mirror_drift_is_an_error(tmp_path):
    repo, _p, _d = _repo(tmp_path, row_overrides={"steward": "recovery"})
    assert "digest-bound mirror" in _msgs(validate_repo(repo, today=TODAY))


def test_contract_id_mismatch_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a", id="comp-b")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    assert "declares id 'comp-b'" in _msgs(validate_repo(repo, today=TODAY))


def test_invariant_binding_drift_is_an_error(tmp_path):
    repo, _p, _d = _repo(
        tmp_path,
        invariant_list_id="org.pantheon.test/v1",
        invariant_list_digest=H.LIST_DIGEST,
        legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
        emitter={"invariant_list_ref": "org.pantheon.test/v1@sha256:" + "d" * 64},
    )
    assert "D-11(b)" in _msgs(validate_repo(repo, today=TODAY))


def test_invariant_binding_requires_id_and_digest(tmp_path):
    repo, _p, _d = _repo(tmp_path, legs={"install": "plan_install", "verify": {"invariant_list_ref": REF}})
    assert "both required" in _msgs(validate_repo(repo, today=TODAY))


def test_invariant_list_digest_is_recomputed_by_the_validator(tmp_path):
    """T-5135 S4: the format-valid, ref-consistent digest must also match the list file."""

    bound = dict(
        invariant_list_id="org.pantheon.test/v1",
        invariant_list_digest=H.LIST_DIGEST,
        legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
        emitter={"invariant_list_ref": REF},
    )
    repo, path, _d = _repo(tmp_path, **bound)
    assert "recomputed" not in _msgs(validate_repo(repo, today=TODAY))
    list_file = path.with_name("comp-a.invariants.yaml")
    list_file.write_text(list_file.read_text().replace("T1", "T9"), encoding="utf-8")
    assert "recomputed" in _msgs(validate_repo(repo, today=TODAY))
    list_file.unlink()
    assert "missing" in _msgs(validate_repo(repo, today=TODAY))


def test_d8_gap_listed_for_a_present_predicate_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a")
    gaps = {
        "plan_configure": {"gap": "x", "ticket": F.TICKET},
        "plan_update": {"gap": "x", "ticket": F.TICKET},
        "plan_repair": {"gap": "x", "ticket": F.TICKET},
        "verify_contract": {"gap": "x", "ticket": F.TICKET},
        "receipt_schema": {"gap": "wrongly claimed missing", "ticket": F.TICKET},
    }
    H.make_checkout(repo, [_strip_none(H.row("comp-a", digest, **_partial(digest, gaps)))])
    msgs = _msgs(validate_repo(repo, today=TODAY))
    assert "receipt_schema" in msgs and "ships it" in msgs


def test_d8_absent_predicate_without_gap_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a")
    gaps = {"plan_configure": {"gap": "x", "ticket": F.TICKET}}  # plan_update/repair/verify_contract absent, not listed
    H.make_checkout(repo, [_strip_none(H.row("comp-a", digest, **_partial(digest, gaps)))])
    msgs = _msgs(validate_repo(repo, today=TODAY))
    assert "lacks plan_update" in msgs and "lacks verify_contract" in msgs


def test_d8_partial_without_plan_remove_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a", legs={"install": "plan_install"})
    gaps = {k: {"gap": "x", "ticket": F.TICKET} for k in ("plan_configure", "plan_update", "plan_repair", "verify_contract")}
    H.make_checkout(repo, [_strip_none(H.row("comp-a", digest, **_partial(digest, gaps)))])
    assert "partial requires plan_remove" in _msgs(validate_repo(repo, today=TODAY))


def test_d8_adopted_requires_every_static_predicate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a")
    adopted = F.adopted(id="comp-a", contract={"repo": "pantheon-config", "module": "tools/germinate/components/comp-a.yaml", "digest": digest})
    H.make_checkout(repo, [adopted])
    msgs = _msgs(validate_repo(repo, today=TODAY))
    assert "adopted, but the contract module lacks plan_configure" in msgs


def test_planner_function_must_exist_for_presence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = H.write_contract(repo, "comp-a", planner_src="def plan_install(request):\n    return []\n")
    gaps = {k: {"gap": "x", "ticket": F.TICKET} for k in ("plan_configure", "plan_update", "plan_repair", "verify_contract")}
    H.make_checkout(repo, [_strip_none(H.row("comp-a", digest, **_partial(digest, gaps)))])
    assert "partial requires plan_remove" in _msgs(validate_repo(repo, today=TODAY))


class _Resolver:
    def __init__(self, files: dict):
        self.files = files
        self.calls = []

    def resolve(self, repo, revision, module):
        self.calls.append((repo, revision, module))
        return self.files.get((repo, revision, module))


def _external_doc(digest: str) -> dict:
    return F.doc(
        F.component(
            id="comp-a",
            contract={"repo": "echo-prismatic", "module": "pantheon-component.yaml", "digest": digest, "repo_revision": F.REVISION},
        )
    )


def test_external_pinned_row_without_resolver_fails_closed(tmp_path):
    findings = check_contracts(_external_doc("sha256:" + "a" * 64), tmp_path)
    assert "no resolver is configured" in _msgs(findings)


def _pin_repos_yaml(root: Path) -> None:
    """T-5113: a resolver is only consulted for a repo whose repos.yaml row carries the ratified pin."""
    (root / "repos.yaml").write_text(
        yaml.safe_dump({"repos": [{"name": "echo-prismatic", "revision": F.REVISION}]}), encoding="utf-8"
    )


def test_external_pinned_row_resolves_and_is_checked(tmp_path):
    _pin_repos_yaml(tmp_path)
    ext = tmp_path / "ext"
    ext.mkdir()
    path, digest = H.write_contract(ext, "comp-a")
    resolver = _Resolver({("echo-prismatic", F.REVISION, "pantheon-component.yaml"): path})
    assert _msgs(check_contracts(_external_doc(digest), tmp_path, resolver)) == ""
    assert resolver.calls == [("echo-prismatic", F.REVISION, "pantheon-component.yaml")]
    assert "recomputed" in _msgs(check_contracts(_external_doc("sha256:" + "a" * 64), tmp_path, resolver))


def test_external_module_absent_at_revision_is_an_error(tmp_path):
    _pin_repos_yaml(tmp_path)
    assert "has no pantheon-component.yaml" in _msgs(check_contracts(_external_doc("sha256:" + "a" * 64), tmp_path, _Resolver({})))


def test_unpinned_rows_are_untouched(tmp_path):
    assert check_contracts(F.doc(F.component()), tmp_path) == []


# --- T-5477: the handlers pin reds CI ------------------------------------------


def _handlers_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a")
    digest = H.write_handlers(repo, "comp-a", "HANDLERS = ()\n")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    return repo, doc, repo / "tools" / "germinate" / "components" / "comp-a_handlers.py"


def test_a_pinned_handlers_module_is_accepted(tmp_path):
    repo, doc, _ = _handlers_repo(tmp_path)
    assert errors(check_contracts(doc, repo)) == []


def test_handler_drift_is_a_validator_error(tmp_path):
    repo, doc, handlers = _handlers_repo(tmp_path)
    handlers.write_text("HANDLERS = ()\nEVIL = 1\n", encoding="utf-8")
    msgs = _msgs(check_contracts(doc, repo))
    assert "handlers_digest" in msgs and "!= recomputed" in msgs and "registry repin" in msgs


def test_handler_drift_is_reported_even_when_another_error_suppresses_the_full_load(tmp_path):
    """Review finding 4: the validator's own handlers check, not the engine load, must report drift."""

    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a")
    digest = H.write_handlers(repo, "comp-a", "HANDLERS = ()\n")
    H.make_checkout(repo, [H.row("comp-a", digest, steward="recovery")])  # steward drift suppresses load_contract
    (repo / "tools" / "germinate" / "components" / "comp-a_handlers.py").write_text("HANDLERS = ()\nEVIL = 1\n", encoding="utf-8")
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    msgs = _msgs(check_contracts(doc, repo))
    assert "digest-bound mirror" in msgs and "would refuse this contract at plan" not in msgs
    assert "handlers_digest" in msgs and "!= recomputed" in msgs


def test_a_handlers_module_without_a_pin_is_a_validator_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a")
    (repo / "tools" / "germinate" / "components" / "comp-a_handlers.py").write_text("HANDLERS = ()\n", encoding="utf-8")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    assert "exists but the contract pins no handlers_digest" in _msgs(check_contracts(doc, repo))


def test_an_unpinnable_handlers_module_is_a_validator_error(tmp_path):
    repo, doc, handlers = _handlers_repo(tmp_path)
    handlers.write_text("import importlib\nHANDLERS = ()\n", encoding="utf-8")
    assert "handlers module unpinnable: comp-a_handlers.py:1: imports 'importlib'" in _msgs(check_contracts(doc, repo))


def test_the_real_registry_handlers_pins_validate():
    root = Path(__file__).resolve().parents[4]
    doc = yaml.safe_load((root / "registry" / "systems.yaml").read_text())
    assert [f for f in errors(check_contracts(doc, root)) if "handlers" in str(f)] == []
