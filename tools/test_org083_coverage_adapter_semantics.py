"""ORG-083 coverage adapter: semantic-source map, comparator, fingerprints, environment and assets (T-5971).

Proposal sections 7, 7a, 7b: SM-1..SM-10 (comparator unit tests plus whole-pipeline runs through the
scripted venv), FC-1..FC-8, the env-strip union pins, the asset pins (lock, config, static map) and
the parsers' captured-real-text fixtures. Collected by `pytest tools/`: zero skips, stdlib only,
coverage is never imported. The real-venv variants live in the non-collected e2e module.

Coupling note (T-5971 ruling Q6): the repository_evidence shape checks import the private contract
scaffolding of tools/test_repository_evidence.py (_base_contract, _measurement) instead of
duplicating about 150 lines of it.
"""
from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import test_org083_adapter_kit as K
from tools.repository_evidence import COMPARABILITY_CHECKED_FIELDS, validate_repository_evidence
from tools import test_repository_evidence as TRE

A = K.A
S = K.Scenario
KEYS = list(COMPARABILITY_CHECKED_FIELDS)
ROOT = K.REPO


# --------------------------------------------------------------------------------------------
# Comparator: synthetic maps and manifests (unit level).
# --------------------------------------------------------------------------------------------

def base_compared(**override):
    compared = {k: {"declaration": f"decl {k}", "instance": f"inst {k}"} for k in KEYS}
    for key, value in override.items():
        compared[key.replace("__", ".")] = {"declaration": f"decl {key}", "instance": value}
    return compared


class Store:
    """Retained artifacts by id: manifest + resolved map bytes, plus their ArtifactRecord digests."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.records: dict[str, dict] = {}

    def add_run(self, name, compared, observations=None, *, map_bytes=None, record_digest=None, manifest_digest=None):
        resolved = {"compared": compared, "observations": observations or {}}
        data = map_bytes if map_bytes is not None else A.dump_json(resolved)
        map_id, man_id = f"{name}-map", f"{name}-manifest"
        manifest = {"members": [{"name": "semantic-map.resolved.json", "artifact_id": map_id,
                                 "sha256": manifest_digest or A.sha256_bytes(A.dump_json(resolved)), "size": len(data)}]}
        self.blobs[map_id], self.blobs[man_id] = data, A.dump_json(manifest)
        self.records[map_id] = {"digest": record_digest or A.sha256_bytes(A.dump_json(resolved))}
        self.records[man_id] = {"digest": A.sha256_bytes(self.blobs[man_id])}
        return man_id

    def compare(self, base, cur, **kw):
        return A.compare(base, cur, retrieve=self.blobs.__getitem__, records=self.records, **kw)


def two_runs(cur_compared, base_compared_=None, cur_obs=None, base_obs=None):
    st = Store()
    b = st.add_run("base", base_compared_ or base_compared(), base_obs)
    c = st.add_run("cur", cur_compared, cur_obs)
    return st.compare(b, c)


@pytest.mark.parametrize("key", KEYS)
def test_sm2_single_key_exact(key):
    """SM-2: two maps differing in exactly one compared key -> that key alone, reset_required."""
    cur = base_compared()
    cur[key] = {"declaration": "decl", "instance": "CHANGED"}
    res = two_runs(cur)
    assert res.changed_fields == [key] and res.verdict == "reset_required"


def test_sm2b_observation_only_differences_do_not_reset():
    res = two_runs(base_compared(), cur_obs={"junit_node_id_set_sha256": "b", "excluded_lines": {"total": 99}},
                   base_obs={"junit_node_id_set_sha256": "a", "excluded_lines": {"total": 1}})
    assert res.changed_fields == [] and res.verdict == "comparable"


def test_sm4_three_keys_exactly():
    cur = base_compared()
    for key in (KEYS[1], KEYS[8], KEYS[14]):
        cur[key] = {"declaration": "x", "instance": "y"}
    res = two_runs(cur)
    assert set(res.changed_fields) == {KEYS[1], KEYS[8], KEYS[14]} and res.changed_fields == [k for k in KEYS if k in res.changed_fields]


def test_sm5_resolved_map_key_set_must_equal_the_validator_tuple():
    for mutate in (lambda c: c.pop(KEYS[3]), lambda c: c.update({"collector.extra": {"declaration": "x", "instance": 1}})):
        cur = base_compared()
        mutate(cur)
        res = two_runs(cur)
        assert res.verdict == "refused" and res.changed_fields == []


def test_sm7_retained_digest_checks_manifest_and_artifact_record_separately():
    st = Store()
    b = st.add_run("base", base_compared())
    wrong = st.add_run("cur", base_compared(), manifest_digest="0" * 64)
    assert st.compare(b, wrong).verdict == "refused"
    st2 = Store()
    b2 = st2.add_run("base", base_compared())
    wrong2 = st2.add_run("cur", base_compared(), record_digest="0" * 64)
    assert st2.compare(b2, wrong2).verdict == "refused"
    ok = Store()
    assert ok.compare(ok.add_run("base", base_compared()), ok.add_run("cur", base_compared())).verdict == "comparable"


def test_sm9_key_source_is_the_imported_validator_tuple(monkeypatch):
    """The comparator demands a key the validator tuple later gains: it must not use a copied list."""
    monkeypatch.setattr(A, "COMPARABILITY_CHECKED_FIELDS", tuple(KEYS) + ("collector.future_key",))
    assert two_runs(base_compared()).verdict == "refused"
    both = base_compared()
    both["collector.future_key"] = {"declaration": "d", "instance": 1}
    cur = copy.deepcopy(both)
    cur["collector.future_key"]["instance"] = 2
    res = two_runs(cur, base_compared_=both)
    assert res.changed_fields == ["collector.future_key"]


def test_static_map_keys_are_the_imported_tuple_not_a_copy():
    data = json.loads((A.ASSETS_DIR / A.STATIC_MAP_NAME).read_text(encoding="utf-8"))
    assert list(data["fields"]) == KEYS or set(data["fields"]) == set(KEYS)
    assert A.COMPARABILITY_CHECKED_FIELDS is COMPARABILITY_CHECKED_FIELDS


# --------------------------------------------------------------------------------------------
# Whole-pipeline semantic scenarios through the scripted venv.
# --------------------------------------------------------------------------------------------

def edited_map(tmp, name, fn):
    dst = tmp / name
    shutil.copytree(A.ASSETS_DIR, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    path = dst / A.STATIC_MAP_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data["fields"])
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return dict(assets_dir=dst, pinned_map_sha=A.sha256_file(path))


def two_pipeline_runs(tmp, second: K.Scenario, *, first: K.Scenario | None = None):
    """Run 1 (the baseline) and run 2 (--baseline-run run 1) on one fixture repo; returns both results."""
    repo = K.make_fixture_repo(tmp / "fx")
    r1 = K.run_adapter(tmp / "r1", first or S(), repo=repo, out=tmp / "out1")
    assert r1.rc == 0, r1.refusal() if (tmp / "out1" / "refusal.json").exists() else r1.rc
    r2 = K.run_adapter(tmp / "r2", second, repo=repo, out=tmp / "out2", baseline=r1.out)
    return r1, r2


def records(res):
    return json.loads((res.out / "measurement-inputs.json").read_text(encoding="utf-8"))["records"]


def retained_semantic_artifacts(res, revision):
    """The run's real ArtifactRecords carrying their real bytes inline, so the validator derives from them.

    The adapter emits `storage_locator {"kind": "repo_path", "path": ...}`, which is not the validator's
    StorageLocator shape (`value`), so the locator is rewritten to inline here (adapter left unchanged).
    The only bytes rewritten are the run manifest's `revision` (the contract pins every record to its own
    revision); its ArtifactRecord digest/size are recomputed from the rewritten bytes.  The resolved-map
    bytes are left untouched, so their manifest member sha256 and ArtifactRecord digest still verify.
    """
    out = []
    for art in json.loads((res.out / "measurement-inputs.json").read_text(encoding="utf-8"))["artifact_records"]:
        content = (res.out / art["storage_locator"]["path"]).read_bytes()
        if art["storage_locator"]["path"] == "run-manifest.json":
            manifest = json.loads(content.decode("utf-8"))
            manifest["revision"] = revision
            content = json.dumps(manifest, sort_keys=True).encode("utf-8")
        art = copy.deepcopy(art)
        art.update(
            revision=revision,
            storage_locator={"kind": "inline", "value": base64.b64encode(content).decode("ascii")},
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
        out.append(art)
    return out


def shape_check(*results):
    """Feed adapter-emitted records through tools.repository_evidence on the existing contract scaffolding.

    The records keep their real scope (exclusion ids included) and artifact_id (the run manifest): the
    validator derives comparability from the retained manifest and resolved map, end to end.
    """
    contract = TRE.RepositoryEvidenceTests()._contract()
    revision = contract["evidence_revision"]
    out, artifacts = [], []
    for res in results:
        artifacts.extend(retained_semantic_artifacts(res, revision))
        for rec in copy.deepcopy(records(res)):
            rec.update(revision=revision, repository="pantheon-config")
            out.append(rec)
    contract["quantitative_measurements"] = out
    contract["artifact_records"].extend(artifacts)
    result = validate_repository_evidence(contract, repo_root=ROOT, now=TRE.NOW)
    return {issue.code for issue in result.issues}


def comparability_codes(codes):
    return {c for c in codes if c.startswith("comparability") or c == "measurement_arithmetic"}


def test_sm1_identical_adapter_is_comparable_and_accepted_by_the_validator(tmp_path):
    r1, r2 = two_pipeline_runs(tmp_path, S())
    for rec in records(r2):
        assert rec["comparability"]["changed_fields"] == [] and rec["comparability"]["verdict"] == "comparable"
        assert rec["comparability"]["baseline_measurement_id"] != rec["measurement_id"]
    assert comparability_codes(shape_check(r1, r2)) == set()


def assert_reset(r1, r2, intended):
    for rec in records(r2):
        changed = rec["comparability"]["changed_fields"]
        assert set(intended) <= set(changed), (intended, changed)
        assert rec["comparability"]["verdict"] != "comparable"
    assert "comparability_reset_required" in shape_check(r1, r2)


def test_sm3a_changed_branch_semantics_declaration_is_detected(tmp_path):
    seams = edited_map(tmp_path, "ma", lambda f: f["branch_semantics"].update(statement="REVISED branch semantics"))
    assert_reset(*two_pipeline_runs(tmp_path, S(seams_kw=seams)), ["branch_semantics"])


def test_sm3b_changed_function_semantics_declaration_is_detected(tmp_path):
    seams = edited_map(tmp_path, "mb", lambda f: f["function_semantics"].update(statement="REVISED function semantics"))
    assert_reset(*two_pipeline_runs(tmp_path, S(seams_kw=seams)), ["function_semantics"])


def test_sm3c_deselect_in_the_argv_is_detected(tmp_path):
    """R13b and the R17c selection contradiction are disabled in this test-only harness so a record is emitted."""
    second = S(args=["--pytest-arg=--deselect=tools/test_a.py::test_0"], seams_kw=dict(disabled=frozenset({"R13b", "R17c.selection"})))
    assert_reset(*two_pipeline_runs(tmp_path, second), ["test_selection", "collector.command_fingerprint"])


def test_sm3d_other_coverage_version_is_detected(tmp_path):
    second = S(version="7.16.2", seams_kw=dict(coverage_pin=A.CoveragePin("7.16.2", {"cp311": "a" * 64, "cp312": "b" * 64})))
    assert_reset(*two_pipeline_runs(tmp_path, second), ["collector.tool_version"])


def test_sm3e_env_allowlist_without_ci_is_detected(tmp_path, monkeypatch):
    r1, _ = two_pipeline_runs(tmp_path / "x", S())
    monkeypatch.setattr(A, "ENV_SET", {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1"})
    repo = K.make_fixture_repo(tmp_path / "fx2")
    r2 = K.run_adapter(tmp_path / "r2", S(), repo=repo, out=tmp_path / "out2", baseline=r1.out)
    assert r2.rc == 0
    assert_reset(r1, r2, ["test_selection", "collector.command_fingerprint"])


def commit(repo, rev_files):
    for rel, text in rev_files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    K.git(repo, "add", "-A")
    K.git(repo, "commit", "-q", "-m", "next")
    return K.git(repo, "rev-parse", "HEAD")


def test_sm3x_content_only_edit_does_not_reset(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx")
    r1 = K.run_adapter(tmp_path / "r1", S(), repo=repo, out=tmp_path / "out1")
    rev2 = commit(repo[0], {"tools/a.py": "def f(x):\n    # edited body\n    if x:\n        return 3\n    return 4\n",
                            "tools/test_new.py": "def test_new():\n    assert True\n"})
    r2 = K.run_adapter(tmp_path / "r2", S(junit=K.default_junit(3)), repo=(repo[0], rev2), out=tmp_path / "out2", baseline=r1.out)
    assert r1.rc == 0 and r2.rc == 0
    f1, f2 = (json.loads((r.out / "measurement-inputs.json").read_text())["fingerprints"] for r in (r1, r2))
    assert f1["scope_fingerprint"] == f2["scope_fingerprint"]
    assert all(rec["comparability"]["changed_fields"] == [] and rec["comparability"]["verdict"] == "comparable"
               for rec in records(r2))


def test_sm3y_added_in_scope_file_resets_scope(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx")
    r1 = K.run_adapter(tmp_path / "r1", S(), repo=repo, out=tmp_path / "out1")
    rev2 = commit(repo[0], {"tools/c.py": "x = 1\n"})
    second = S(cov_json=K.default_cov_json(K.REPORTED + ("tools/c.py",)))
    r2 = K.run_adapter(tmp_path / "r2", second, repo=(repo[0], rev2), out=tmp_path / "out2", baseline=r1.out)
    assert r2.rc == 0
    assert_reset(r1, r2, ["scope.scope_fingerprint"])


def test_sm6_edited_static_map_without_updating_the_pin_is_refused(tmp_path):
    seams = edited_map(tmp_path, "m6", lambda f: f["dimension"].update(statement="edited"))
    seams.pop("pinned_map_sha")
    res = K.run_adapter(tmp_path / "r", S(seams_kw=seams))
    assert res.rc == 1 and "R17a" in res.refusal_ids()


def test_sm8_contradictions_are_refused(tmp_path):
    core = K.run_adapter(tmp_path / "a", S(config_core="sysmon"))
    arcs = K.run_adapter(tmp_path / "b", S(combined_has_arcs=False))
    assert core.rc == 1 and "R17c" in core.refusal_ids() and arcs.rc == 1 and "R17c" in arcs.refusal_ids()


def test_sm10_config_digest_has_no_circularity(tmp_path):
    res = K.run_adapter(tmp_path, S())
    recorded = json.loads((res.out / "measurement-inputs.json").read_text())["fingerprints"]["config_digest"]
    pins = A.soak_leg.parse_ci_pins(K.CI_YML)
    assert recorded == A.config_digest(A.sha256_file(A.ASSETS_DIR / A.CONFIG_NAME), pins)
    resolved = json.loads((res.out / "semantic-map.resolved.json").read_text())
    assert resolved["compared"]["collector.config_digest"]["instance"] == recorded
    assert recorded == A.config_digest(A.sha256_file(A.ASSETS_DIR / A.CONFIG_NAME), pins, A.CoveragePin())


# --------------------------------------------------------------------------------------------
# FC-1..FC-8: canonicalization and determinism.
# --------------------------------------------------------------------------------------------

PATHS = ["tools/a.py", "tools/b/c.py", "tools/__init__.py", "tools/z.py"]
RULES = dict(unreported_paths=[], exclusion_regexes=["a", "b"])


def test_fc1_path_set_order_independence(tmp_path):
    base = A.scope_fingerprint(in_scope_paths=PATHS, **RULES)
    for seed in range(5):
        shuffled = PATHS[:]
        random.Random(seed).shuffle(shuffled)
        assert A.scope_fingerprint(in_scope_paths=shuffled, **RULES) == base
    repo, _rev = K.make_fixture_repo(tmp_path)
    via_git = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True).stdout.split("\0")
    via_walk = [str(p.relative_to(repo)) for p in sorted(repo.rglob("*"), reverse=True) if p.is_file() and ".git" not in p.parts]
    assert A.build_in_scope(via_git, repo)[0] == A.build_in_scope(via_walk, repo)[0]


def test_fc2_env_order_independence():
    kw = dict(argv=["python", "-m", "x"], mapping={}, python3_identity="/usr/bin/python3", workers=4)
    a = A.command_fingerprint(env_set={"CI": "true", "PANTHEON_DISABLE_LIVE_TELEGRAM": "1"}, env_unset=["B", "A", "C"], **kw)
    b = A.command_fingerprint(env_set={"PANTHEON_DISABLE_LIVE_TELEGRAM": "1", "CI": "true"}, env_unset=["C", "B", "A"], **kw)
    assert a == b


def test_fc3_stable_key_order_and_unicode_normalization():
    assert A.digest({"a": 1, "b": {"y": 1, "x": 2}}) == A.digest({"b": {"x": 2, "y": 1}, "a": 1})
    nfc, nfd = "tools/\u00e9.py", unicodedata.normalize("NFD", "tools/\u00e9.py")
    assert nfc != nfd and A.digest({"p": nfc}) == A.digest({"p": nfd})
    assert A.scope_fingerprint(in_scope_paths=[nfc], **RULES) == A.scope_fingerprint(in_scope_paths=[nfd], **RULES)


def fingerprints(res):
    return json.loads((res.out / "measurement-inputs.json").read_text())["fingerprints"]


def test_fc4_absolute_path_normalization_across_checkouts(tmp_path):
    a = K.run_adapter(tmp_path / "x" / "a", S(), out=tmp_path / "x" / "a" / "out")
    b = K.run_adapter(tmp_path / "y" / "deeper" / "b", S(), out=tmp_path / "y" / "deeper" / "b" / "out")
    fa, fb = fingerprints(a), fingerprints(b)
    for key in ("scope_fingerprint", "command_fingerprint", "config_digest"):
        assert fa[key] == fb[key]


def test_fc5_realpath_not_abspath_and_no_double_counting(tmp_path):
    repo, _ = K.make_fixture_repo(tmp_path, symlinks={"tools/alias.py": "a.py", "tools/outside.py": str(tmp_path)})
    tracked = ["tools/a.py", "tools/alias.py", "tools/outside.py", "tools/__init__.py"]
    paths, fires = A.build_in_scope(tracked, repo)
    assert "tools/alias.py" not in paths and paths.count("tools/a.py") == 1
    assert {f.id for f in fires} == {"R9c", "R9a"}


def test_fc6_determinism_across_runs(tmp_path):
    r1 = K.run_adapter(tmp_path / "a", S())
    r2 = K.run_adapter(tmp_path / "b", S())
    assert fingerprints(r1) == fingerprints(r2)
    c1 = json.loads((r1.out / "semantic-map.resolved.json").read_text())["compared"]
    c2 = json.loads((r2.out / "semantic-map.resolved.json").read_text())["compared"]
    assert c1 == c2


def test_fc7_determinism_across_runners(monkeypatch):
    base = (A.scope_fingerprint(in_scope_paths=PATHS, **RULES),
            A.command_fingerprint(["x"], mapping={}, env_set={"CI": "true"}, env_unset=["A"], python3_identity="/usr/bin/python3", workers=2),
            A.config_digest("0" * 64, ["pytest==9.1.1"]))
    monkeypatch.setenv("HOME", "/home/other-user")
    monkeypatch.setenv("TMPDIR", "/var/tmp/other")
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("HOSTNAME", "another-host")
    monkeypatch.setattr("socket.gethostname", lambda: "another-host")
    monkeypatch.setattr("platform.node", lambda: "another-host")
    after = (A.scope_fingerprint(in_scope_paths=PATHS, **RULES),
             A.command_fingerprint(["x"], mapping={}, env_set={"CI": "true"}, env_unset=["A"], python3_identity="/usr/bin/python3", workers=2),
             A.config_digest("0" * 64, ["pytest==9.1.1"]))
    assert base == after


def test_fc8_duplicates_and_control_characters_are_refused(tmp_path):
    repo, _ = K.make_fixture_repo(tmp_path)
    _paths, fires = A.build_in_scope(["tools/a.py", "tools/a.py"], repo)
    assert "R10e" in {f.id for f in fires}
    _paths, fires = A.build_in_scope(["tools/a\nb.py"], repo)
    assert "R9d" in {f.id for f in fires}
    with pytest.raises(A.Refusal) as exc:
        A.scope_fingerprint(in_scope_paths=["tools/a.py", "tools/a.py"], **RULES)
    assert exc.value.fires[0].id == "R10e"
    with pytest.raises(A.Refusal) as exc2:
        A.scope_fingerprint(in_scope_paths=["tools/a\nb.py"], **RULES)
    assert exc2.value.fires[0].id == "R9d"


# --------------------------------------------------------------------------------------------
# Environment strip: the section 6 / section 12.3 union (T-5971 addendum pins a, b, c).
# --------------------------------------------------------------------------------------------

EXPECTED_STATIC_UNSET = ["COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START", "COVERAGE_RCFILE", "PYTEST_ADDOPTS",
                         "PYTEST_PLUGINS", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "TMUX", "VIRTUAL_ENV"]


def test_env_union_exact_list():
    """(a) The unset set is exactly section 6's seven names plus soak_leg.STRIPPED_ENV, plus git's redirect vars."""
    assert A.static_unset_names() == EXPECTED_STATIC_UNSET
    assert A.env_unset_names({"GIT_DIR", "GIT_NAMESPACE"}) == sorted(EXPECTED_STATIC_UNSET + ["GIT_DIR", "GIT_NAMESPACE"])


def expected_command_fp(res, venv, unset_names):
    out, wt = res.out, res.out / "repo"
    argv = next(c for c in res.runner.calls if c[1:4] == ["-m", "coverage", "run"])
    cfg = A.ASSETS_DIR / A.CONFIG_NAME
    return A.command_fingerprint(argv, mapping={str(wt): "<ROOT>", str(out): "<OUT>", str(venv): "<VENV>", str(cfg): "<CFG>"},
                                 env_set=A.ENV_SET, env_unset=unset_names, workers=2,
                                 python3_identity=A.resolve_python3(venv, os.environ.get("PATH", "/usr/bin:/bin")))


def test_env_redirect_vars_are_asked_of_git_stripped_and_fingerprinted(tmp_path):
    """(b) A redirect var reported by `git rev-parse --local-env-vars` is stripped from the measured env and in the fingerprint names."""
    venv = K.make_fake_venv(tmp_path / "v")
    extra = S(redirect_vars="GIT_DIR GIT_FAKE_REDIRECT\n", environ_extra={"GIT_FAKE_REDIRECT": "leak"})
    res = K.run_adapter(tmp_path / "a", extra, venv=venv)
    assert res.rc == 0
    assert "GIT_FAKE_REDIRECT" not in res.runner.measured_env
    names = A.env_unset_names({"GIT_DIR", "GIT_FAKE_REDIRECT", "GIT_NAMESPACE"})
    assert fingerprints(res)["command_fingerprint"] == expected_command_fp(res, venv, names)
    assert json.loads((res.out / "semantic-map.resolved.json").read_text())["observations"]["env_unset_names"] == names
    plain = K.run_adapter(tmp_path / "b", S(redirect_vars="GIT_DIR\n"), venv=venv)
    assert fingerprints(plain)["command_fingerprint"] != fingerprints(res)["command_fingerprint"]


def test_command_fingerprint_is_sensitive_to_exactly_the_name_set():
    """(c) one more unset name changes the fingerprint; the same set in another order does not."""
    kw = dict(argv=["python"], mapping={}, env_set=A.ENV_SET, python3_identity="/usr/bin/python3", workers=4)
    names = A.env_unset_names({"GIT_DIR"})
    base = A.command_fingerprint(env_unset=names, **kw)
    assert A.command_fingerprint(env_unset=list(reversed(names)), **kw) == base
    assert A.command_fingerprint(env_unset=names + ["ONE_MORE"], **kw) != base
    assert A.command_fingerprint(env_unset=names[:-1], **kw) != base


# --------------------------------------------------------------------------------------------
# Assets, pins, source hygiene.
# --------------------------------------------------------------------------------------------

def test_lock_pins_only_the_two_binary_wheels_with_the_proposal_hashes():
    text = (A.ASSETS_DIR / A.LOCK_NAME).read_text(encoding="utf-8")
    requirement_lines = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", " "))]
    assert requirement_lines == ["coverage==7.17.0 \\"]
    hashes = sorted(ln.split("sha256:")[1].rstrip(" \\") for ln in text.splitlines() if "--hash=sha256:" in ln)
    assert hashes == sorted(A.COVERAGE_WHEELS.values()) == sorted([
        "8e8113d3852e9377421a61de1917df27b3abb25404a4ecf69f9e64612c4345b5",
        "85932227e9dd38e62cd29c17fefdfecfb919eead6af1e3259522d03cf85de983"])
    assert all(ln.startswith("#") for ln in text.splitlines() if A.SDIST_SHA256 in ln)
    assert "py3-none-any" not in "".join(ln for ln in text.splitlines() if not ln.startswith("#"))


def test_config_matches_the_contract_and_its_pin():
    cfg = A.ASSETS_DIR / A.CONFIG_NAME
    assert A.sha256_file(cfg) == A.CONFIG_SHA256
    text = cfg.read_text(encoding="utf-8")
    run_part, report_part = text.split("[report]")
    for needle in ("branch = True", "core = ctrace", "patch = subprocess", "relative_files = False", "source = tools"):
        assert needle in run_part
    assert "include_namespace_packages" not in run_part
    assert "\ninclude_namespace_packages = True" in report_part
    omit = [ln.strip() for ln in run_part.split("omit =")[1].splitlines() if ln.strip().startswith("*/")]
    assert omit == list(A.OMIT_PATTERNS)
    assert "fail_under" not in text and "exclude_lines" not in text and "exclude_also" not in text
    assert "no_cover" not in text


def test_static_map_matches_its_pin_and_key_set():
    path = A.ASSETS_DIR / A.STATIC_MAP_NAME
    assert A.sha256_file(path) == A.STATIC_MAP_SHA256
    data, fires = A.load_static_map(A.ASSETS_DIR, A.STATIC_MAP_SHA256)
    assert fires == [] and set(data["fields"]) == set(COMPARABILITY_CHECKED_FIELDS)
    assert data["fields"]["collector.adapter_id"]["statement"] == A.ADAPTER_ID


def test_discovery_rule_constant_records_the_q3_outcome():
    assert A.DISCOVERY_RULE == "coveragepy-7.17.0 source-dir discovery; relative_files=false; include_namespace_packages=true"
    assert A.UNDISCOVERABLE_PATHS == ()


def test_no_auto_discovered_coverage_config_at_the_repo_root():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", ".coveragerc", "setup.cfg", "tox.ini", "pyproject.toml"],
                             capture_output=True, text=True).stdout.split()
    assert tracked == [] and not any((ROOT / n).exists() for n in (".coveragerc", "setup.cfg", "tox.ini", "pyproject.toml"))


@pytest.mark.parametrize("name", ["org083_coverage_adapter.py", "test_org083_adapter_kit.py", "test_org083_coverage_adapter_refusals.py",
                                  "test_org083_coverage_adapter_semantics.py"])
def test_coverage_is_never_imported(name):
    tree = ast.parse((ROOT / "tools" / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "coverage" for a in node.names), name
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "coverage", name


def test_pantheon_hook_trigger_bytes_never_appear():
    needle = "HERME" + "S.md"
    for p in (ROOT / "tools").glob("*org083*"):
        if p.is_file():
            assert needle not in p.read_text(encoding="utf-8", errors="ignore"), p
    for p in (A.ASSETS_DIR).rglob("*"):
        if p.is_file():
            assert needle not in p.read_text(encoding="utf-8", errors="ignore"), p


# --------------------------------------------------------------------------------------------
# Parsers against text captured from the real coverage 7.17.0 (D11).
# --------------------------------------------------------------------------------------------

REAL_COMBINE_OK = ("Combined data file ../data/.coverage.swarm.pid2229924.XJV28o8x.HNQIp837rWOh\n"
                   "Combined data file ../data/.coverage.swarm.pid2230891.XzvLaVSx.HZke5yK9eFTh\nCombined 2 files\n")
REAL_COMBINE_CORRUPT = (
    "/v/coverage/data.py:213: CoverageWarning: Couldn't use data file '/o/data/.coverage.swarm.pid1.XJV28o8x.HNQ': file is "
    "not a database: (SQLITE_NOTADB)\n  data._warn(str(exc))\n"
    "Couldn't combine data file ../data/.coverage.swarm.pid1.XJV28o8x.HNQ: Couldn't use data file '/o/data/.coverage.swarm.pid1."
    "XJV28o8x.HNQ': file is not a database: (SQLITE_NOTADB)\nCombined data file ../data/.coverage.swarm.pid2.Xz.HZ\n"
    "Combined 1 file, 1 file errored\n")
REAL_COMBINE_MIXED = ("Can't combine branch coverage data with statement data: data file '/o/data/.coverage.swarm.pid1.X.H' measured "
                      "branch coverage, but data file '/o/data/.coverage' didn't; see "
                      "https://coverage.readthedocs.io/en/7.17.0/messages.html#error-cant-combine\n")


def test_parse_combine_real_shapes():
    ok = A.parse_combine(0, "", REAL_COMBINE_OK)
    assert set(ok.results.values()) == {"combined"} and not ok.errored and not ok.mixed
    bad = A.parse_combine(0, "", REAL_COMBINE_CORRUPT)
    assert [b for b, _ in bad.errored] == [".coverage.swarm.pid1.XJV28o8x.HNQ"] and "SQLITE_NOTADB" in bad.errored[0][1]
    mixed = A.parse_combine(1, REAL_COMBINE_MIXED, "")
    assert mixed.mixed
    dup = A.parse_combine(0, "", "Skipping duplicate data ../data/.coverage.h.pid1.a.b\nCombined 0 files\n")
    assert dup.results == {".coverage.h.pid1.a.b": "duplicate"} and dup.other_skips == []


def test_parse_observation_real_layout_and_effective_core():
    obs = A.parse_observation(K.debug_sys_text(), K.debug_config_text("ctrace"))
    assert (obs.version, obs.ctracer, obs.core, obs.python_version, obs.glibc) == ("7.17.0", True, "ctrace", "3.12.3", "2.39")
    assert A.parse_observation(K.debug_sys_text(ctracer=False), K.debug_config_text("sysmon")).ctracer is False
    assert A.parse_observation(K.debug_sys_text(core="pytrace"), K.debug_config_text("ctrace")).core == "pytrace"
    cfg = A.parse_kv_report(K.debug_config_text())
    assert cfg["exclude_list"].count("\n") == 2


def test_warning_parsing_real_shapes():
    assert A.parse_warnings(K.WARN_NO_DATA) == [("no-data-collected", "No data was collected.")]
    assert A.parse_warnings("nothing here") == []
    assert A.warning_names_in_scope_module("Module tools.x was previously imported, but not measured")
    assert not A.warning_names_in_scope_module("Module harness.y was previously imported, but not measured")
    assert not A.warning_names_in_scope_module("Module toolsy was previously imported, but not measured")


def test_omit_matching_follows_coverage_glob_semantics():
    match = A.omit_matcher(A.OMIT_PATTERNS)
    for path in ("/r/tools/test_a.py", "/r/tools/sub/deep/test_b.py", "/r/tools/sub/tests/x.py", "/r/tools/conftest.py",
                 "/r/tools/sub/conftest.py", "/r/tools/fixtures/f.py", "/r/tools/testdata/t.py"):
        assert match(path), path
    for path in ("/r/tools/a.py", "/r/tools/hooks/x-y.py", "/r/tools/testing.py", "/r/tools/org083_coverage_adapter.py"):
        assert not match(path), path


def test_data_files_are_read_with_the_asserted_schema(tmp_path):
    good = K.write_data_file(tmp_path / ".coverage.a", argv0=K.CONTROLLER_ARGV0, files=("tools/a.py",))
    info = A.read_data_file(good)
    assert info.readable and info.controller and info.has_arcs and info.measured_files == 1 and info.executed_rows == 3
    odd = K.write_data_file(tmp_path / ".coverage.b", schema=99)
    assert not A.read_data_file(odd).readable and "unsupported" in A.read_data_file(odd).reason
    junk = tmp_path / ".coverage.c"
    junk.write_bytes(b"x" * 100)
    assert not A.read_data_file(junk).readable
    stmt = K.write_data_file(tmp_path / ".coverage.d", has_arcs=False)
    assert A.read_data_file(stmt).has_arcs is False


# ---------------------------------------------------------------------------------------------
# T-5971: ids must be unique per run even under a frozen clock (same-second runs on one revision).
# ---------------------------------------------------------------------------------------------
FROZEN = datetime(2026, 11, 3, 1, 9, 47, tzinfo=timezone.utc)


def frozen_now():
    return FROZEN


def manifest_id(res):
    return json.loads((res.out / "measurement-inputs.json").read_text(encoding="utf-8"))["manifest_artifact_id"]


def test_sm_ids_are_unique_per_run_under_a_frozen_clock_and_a_change_is_still_detected(tmp_path):
    seams = edited_map(tmp_path, "mf", lambda f: f["branch_semantics"].update(statement="REVISED branch semantics"))
    r1, r2 = two_pipeline_runs(tmp_path, S(seams_kw=dict(now=frozen_now, **seams)), first=S(seams_kw=dict(now=frozen_now)))
    assert manifest_id(r1) != manifest_id(r2)
    ids1 = {m["artifact_id"] for m in json.loads((r1.out / "run-manifest.json").read_text())["members"]}
    ids2 = {m["artifact_id"] for m in json.loads((r2.out / "run-manifest.json").read_text())["members"]}
    assert not ids1 & ids2
    assert {r["measurement_id"] for r in records(r1)}.isdisjoint(r["measurement_id"] for r in records(r2))
    for rec in records(r2):
        assert rec["comparability"]["changed_fields"] and rec["comparability"]["verdict"] != "comparable"
    assert_reset(r1, r2, ["branch_semantics"])


def test_sm_stamp_and_collected_at_come_from_one_clock_reading(tmp_path):
    ticks = iter(datetime(2026, 11, 3, 1, 9, 47 + i, tzinfo=timezone.utc) for i in range(100))
    r1 = K.run_adapter(tmp_path / "r1", S(seams_kw=dict(now=lambda: next(ticks))))
    assert r1.rc == 0
    for rec in records(r1):
        stamp = rec["measurement_id"].split("-")[3]
        assert stamp.replace("T", "").replace("Z", "") == rec["collected_at"].replace("-", "").replace(":", "").replace("T", "").replace("Z", "")


def test_sm_baseline_id_collision_fails_closed_with_setup_error_and_clean_exit(tmp_path, capfd):
    frozen = dict(now=frozen_now, run_nonce="same")
    repo = K.make_fixture_repo(tmp_path / "fx")
    r1 = K.run_adapter(tmp_path / "r1", S(seams_kw=frozen), repo=repo, out=tmp_path / "out1")
    assert r1.rc == 0
    capfd.readouterr()
    r2 = K.run_adapter(tmp_path / "r2", S(seams_kw=frozen), repo=repo, out=tmp_path / "out2", baseline=r1.out, capfd=capfd)
    assert r2.rc == A.EXIT_SETUP == 2
    assert "shares ids" in r2.stderr
    assert not (r2.out / "refusal.json").exists()
    # nothing in <OUT> may claim admissibility
    assert not (r2.out / "run-manifest.json").exists() and not (r2.out / "measurement-inputs.json").exists()
    # RA-6: the measurement worktree is removed and the fixture repository is clean
    wt = subprocess.run(["git", "-C", str(repo[0]), "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    status = subprocess.run(["git", "-C", str(repo[0]), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert not (r2.out / "repo").exists() and str(r2.out / "repo") not in wt and status.strip() == ""
