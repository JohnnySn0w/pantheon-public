"""ORG-083 coverage adapter: one negative test per independent refusal predicate (T-5971).

Proposal section 10a (R1a..R17c): each row below is a fixture-driven negative test that runs the
WHOLE adapter pipeline (real git fixture repository, scripted venv python, stdlib-built coverage
data files) and asserts, through one shared helper, that the row's predicate id is listed in
refusal.json AND that RA-1..RA-6 hold. The positive control (a fully admissible run) and the
R11f over-refusal control live here too. No test skips and none imports coverage.

Mutation receipts (the adapter is mutated, these tests are not) are produced by
tools/org083_coverage_adapter_assets/tests/mutants.py.
"""
from __future__ import annotations

import json
import shutil
import signal
import subprocess

import pytest

from tools import test_org083_adapter_kit as K

A = K.A
S = K.Scenario
FILES = K.FILES


def junit_with(body: str, tests: int = 1) -> str:
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}">{body}</testsuite></testsuites>'


def workers_only(n: int = 3, **kw):
    return [dict(argv0="-c", files=K.REPORTED, **kw) for _ in range(n)]


def full_files(**kw):
    return [dict(argv0=K.CONTROLLER_ARGV0, files=K.REPORTED, **kw)] + workers_only(2, **kw)


def edit_assets(tmp, name, fn):
    dst = tmp / "assets"
    shutil.copytree(A.ASSETS_DIR, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    path = dst / name
    path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")
    return dst


def cov_with(files=K.REPORTED, extra=(), totals=None):
    data = K.default_cov_json(files, totals)
    for key in extra:
        data["files"][key] = {"executed_lines": [1], "excluded_lines": [], "summary": {}}
    return data


def raise_sigterm(argv, kw):
    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)


def stray_untracked(wt):
    (wt / "tools" / "stray.py").write_text("x = 1\n", encoding="utf-8")


def edit_tracked(wt):
    (wt / "tools" / "a.py").write_text("# edited\n", encoding="utf-8")


def other_head(argv):
    if argv[:2] == ["git", "-C"] and argv[3:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(argv, 0, "0" * 40 + "\n", "")
    return None


def duplicate_ls_files(argv):
    if "ls-files" in argv and "-z" in argv:
        out = subprocess.run(argv, capture_output=True, text=True).stdout
        return subprocess.CompletedProcess(argv, 0, out + "tools/a.py\0", "")
    return None


def seed_stale(data_dir, out):
    (data_dir / ".coverage.stale.pid1.x.y").write_bytes(b"stale")


def no_checker_files():
    files = dict(FILES)
    del files["tools/check_shell_suite_outcomes.py"]
    return files


def failing_checker_files():
    return {**FILES, "tools/check_shell_suite_outcomes.py": "import sys\nsys.exit(1)\n"}


UNDISC = list(K.UNDISCOVERABLE)
MIXED = K.MIXED_ERROR.format(d="/o/data")
ERRORED = ("Couldn't combine data file ../data/.coverage.host.pid1000.aaaa.bbbb0: Couldn't use data file '/o/data/x': "
           "{reason}\nCombined 2 files, 1 file errored\n")

# row id -> (scenario factory taking tmp, expected predicate ids)
ROWS = {
    "R1a": (lambda t: S(pytest_rc=1), {"R1a"}),
    "R1b": (lambda t: S(junit=junit_with('<testcase classname="c" name="t"><failure message="x"/></testcase>')), {"R1b"}),
    "R1c": (lambda t: S(junit=junit_with('<testcase classname="c" name="t"><error message="x"/></testcase>')), {"R1c"}),
    "R2a": (lambda t: S(junit=None), {"R2a"}),
    "R2b": (lambda t: S(junit='<testsuites><testsuite name="pytest"><testcase classname="c"'), {"R2b"}),
    "R2c": (lambda t: S(junit=junit_with('<testcase classname="c" name="t"><skipped/></testcase>')), {"R2c"}),
    "R2d": (lambda t: S(junit='<testsuites><testsuite name="pytest" tests="0"/></testsuites>'), {"R2d"}),
    "R3a": (lambda t: S(repo_files=failing_checker_files()), {"R3a"}),
    "R3b": (lambda t: S(repo_files=no_checker_files(), cov_json=cov_with(("tools/__init__.py", "tools/a.py"))), {"R3b"}),
    "R4a": (lambda t: S(post_worktree=stray_untracked), {"R4a"}),
    "R4b": (lambda t: S(post_worktree=edit_tracked), {"R4b"}),
    "R4c": (lambda t: S(git_override=other_head), {"R4c"}),
    "R5a": (lambda t: S(version="7.16.2"), {"R5a"}),
    "R5b": (lambda t: S(ctracer=False), {"R5b"}),
    "R5c": (lambda t: S(config_core="sysmon"), {"R5c"}),
    "R5d": (lambda t: S(data_files=full_files(has_arcs=False), combined_has_arcs=False), {"R5d"}),
    "R6a": (lambda t: S(data_files=[dict(argv0=K.CONTROLLER_ARGV0, files=K.REPORTED)]), {"R6a"}),
    "R6b": (lambda t: S(data_files=[dict(argv0=K.CONTROLLER_ARGV0, files=K.REPORTED)] + [dict(argv0="-c", files=())] * 2), {"R6b"}),
    "R6c": (lambda t: S(data_files=full_files(arcs=0)), {"R6c"}),
    "R6d": (lambda t: S(data_files=workers_only(3)), {"R6d"}),
    "R7a": (lambda t: S(data_files=[dict(argv0=K.CONTROLLER_ARGV0, files=K.REPORTED), dict(argv0="-c", files=K.REPORTED),
                                    dict(raw=b"not a sqlite file at all" * 20)],
                        combine_stderr=ERRORED.format(reason=K.SQLITE_NOTADB)), {"R7a"}),
    "R7b": (lambda t: S(data_files=[dict(argv0=K.CONTROLLER_ARGV0, files=K.REPORTED), dict(argv0="-c", files=K.REPORTED),
                                    dict(argv0="-c", files=K.REPORTED, mode=0)],
                        combine_stderr=ERRORED.format(reason=K.SQLITE_CANTOPEN)), {"R7b"}),
    "R7c": (lambda t: S(combine_stderr_extra="Skipping data file ../data/.coverage.h.pid9.x.y: schema mismatch\n"), {"R7c"}),
    "R8": (lambda t: S(combine_rc=1, combine_stdout=MIXED), {"R8"}),
    "R9a": (lambda t: S(cov_json=cov_with(extra=("/tmp/x/tools/a.py",))), {"R9a"}),
    "R9b": (lambda t: S(cov_json=cov_with(extra=("a.py",))), {"R9b"}),
    "R9c": (lambda t: S(repo_symlinks={"tools/lnk": "hooks"}, cov_json=cov_with(extra=("tools/lnk/x-y.py",))), {"R9c"}),
    "R9d": (lambda t: S(cov_json=cov_with(extra=("tools/a\nb.py",))), {"R9d"}),
    "R10a": (lambda t: S(cov_json=cov_with(extra=("tools/gen.py",))), {"R10a"}),
    "R10b": (lambda t: S(seams_kw=dict(undiscoverable_paths=[])), {"R10b"}),
    "R10c": (lambda t: S(cov_json=cov_with(extra=("tools/hooks/x-y.py",))), {"R10c"}),
    "R10d": (lambda t: S(seams_kw=dict(undiscoverable_paths=UNDISC + ["tools/hooks/ghost-gate.py"])), {"R10d"}),
    "R10e": (lambda t: S(git_override=duplicate_ls_files), {"R10e"}),
    "R11a": (lambda t: S(log_text=K.WARN_NO_DATA), {"R11a"}),
    "R11b": (lambda t: S(log_text=K.WARN_NOT_MEASURED.format(m="tools.x")), {"R11b"}),
    "R11c": (lambda t: S(log_text=K.WARN_COULDNT_PARSE.format(p="tools/a.py")), {"R11c"}),
    "R11d": (lambda t: S(log_text=K.WARN_INCLUDE_IGNORED), {"R11d"}),
    "R11e": (lambda t: S(junit_system_err=K.WARN_NO_DATA), {"R11a"}),
    "R12a": (lambda t: S(cov_json=cov_with(totals={"num_statements": 0, "covered_lines": 0})), {"R12a"}),
    "R12b": (lambda t: S(cov_json=cov_with(totals={"covered_lines": 11})), {"R12b"}),
    "R12c": (lambda t: S(cov_json=cov_with(totals={"covered_lines": 8.5})), {"R12c"}),
    "R13a": (lambda t: S(args=["--paths", "tools/a.py"]), {"R13a"}),
    "R13b": (lambda t: S(args=["--pytest-arg=-k", "--pytest-arg=test_a"]), {"R13b"}),
    "R14a": (lambda t: S(seams_kw=dict(pre_start=seed_stale)), {"R14a"}),
    "R14b": (lambda t: S(data_age=3600), {"R14b"}),
    "R14c": (lambda t: S(junit_age=3600), {"R14c"}),
    "R15a": (lambda t: S(run_hook=raise_sigterm), {"R15a"}),
    "R15b": (lambda t: S(timeout=True), {"R15b"}),
    "R15c": (lambda t: S(pytest_rc=2, junit='<testsuites><testsuite name="pytest"><testcase classname="c"'), {"R15c"}),
    "R16a": (lambda t: S(seams_kw=dict(assets_dir=edit_assets(t, A.CONFIG_NAME, lambda s: s + "# edited\n"))), {"R16a"}),
    "R16b": (lambda t: S(environ_extra={"COVERAGE_CORE": "sysmon"}), {"R16b"}),
    "R17a": (lambda t: S(seams_kw=dict(assets_dir=edit_assets(t, A.STATIC_MAP_NAME, lambda s: s + "\n"))), {"R17a"}),
    "R17b": (lambda t: S(seams_kw=_r17b_seams(t)), {"R17b"}),
    "R17c": (lambda t: S(config_core="sysmon"), {"R17c"}),
    "R17c.arcs": (lambda t: S(combined_has_arcs=False), {"R17c"}),
}


def _missing_key_map(tmp):
    def drop(text):
        data = json.loads(text)
        del data["fields"]["function_semantics"]
        return json.dumps(data, indent=2)
    return edit_assets(tmp, A.STATIC_MAP_NAME, drop)


def _r17b_seams(tmp):
    assets = _missing_key_map(tmp)
    return dict(assets_dir=assets, pinned_map_sha=A.sha256_file(assets / A.STATIC_MAP_NAME))


def refused(tmp_path, scenario, extra_args=()):
    """Run the adapter on a fresh fixture repo with a retained-evidence dir alongside; return the result
    plus the RA-5 'before' digests."""
    repo = K.make_fixture_repo(tmp_path / "fx", scenario.repo_files, scenario.repo_symlinks)
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "evidence.json").write_text('{"digest": "kept"}', encoding="utf-8")
    before = (K.tree_digest(retained), K.repo_snapshot(repo[0]))
    res = K.run_adapter(tmp_path, scenario, repo=repo, extra_args=extra_args)
    return res, retained, before


@pytest.mark.parametrize("row", sorted(ROWS))
def test_refusal_row(row, tmp_path):
    """Every section 10a row: its predicate id is listed, and RA-1..RA-6 hold."""
    factory, expected = ROWS[row]
    scenario = factory(tmp_path)
    res, retained, before = refused(tmp_path, scenario)
    ra = K.assert_refusal(res, expected, retained=retained, retained_before=before[0], repo_before=before[1])
    assert set(ra) == {"RA-1", "RA-2", "RA-3", "RA-4", "RA-5", "RA-6"} and all(ra.values())


def test_positive_control_admissible(tmp_path):
    """The shared admissible run: exit 0, and every section 8 artifact, each digest bound by the manifest."""
    res = K.run_adapter(tmp_path, S())
    assert res.rc == 0, (res.out / "refusal.json").read_text() if (res.out / "refusal.json").exists() else res.rc
    names = {p.name for p in res.out.iterdir()}
    for needed in ("coverage.json.gz", "accounting.json", "semantic-map.resolved.json", "run-manifest.json",
                   "measurement-inputs.json", "coverage.md"):
        assert needed in names
    assert "refusal.json" not in names
    manifest = json.loads((res.out / "run-manifest.json").read_text())
    for member in manifest["members"]:
        assert K.A.sha256_file(res.out / member["name"]) == member["sha256"]
    inputs = json.loads((res.out / "measurement-inputs.json").read_text())
    assert inputs["admissible"] is True and {r["dimension"] for r in inputs["records"]} == {"line", "branch"}
    assert not (res.out / "repo").exists()
    assert K.git(res.repo, "status", "--porcelain") == ""


def test_r11f_out_of_scope_module_not_measured_is_not_refused(tmp_path):
    """R11f positive control: `module-not-measured` for an OUT-of-scope module must NOT refuse."""
    res = K.run_adapter(tmp_path, S(log_text=K.WARN_NOT_MEASURED.format(m="harness.y")))
    assert res.rc == 0
    acct = json.loads((res.out / "accounting.json").read_text())
    assert [w["slug"] for w in acct["warnings"]] == ["module-not-measured"]


def test_ra5_out_dir_must_be_fresh(tmp_path):
    """A non-empty <OUT> is a setup error (exit 2) and is left byte-identical (RA-5)."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "retained.txt").write_text("evidence", encoding="utf-8")
    before = K.tree_digest(out)
    res = K.run_adapter(tmp_path, S(), out=out)
    assert res.rc == 2 and K.tree_digest(out) == before


def test_adapter_never_writes_under_the_repository(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx")
    res = K.run_adapter(tmp_path, S(), repo=repo, out=repo[0] / "inside")
    assert res.rc == 2 and not (repo[0] / "inside").exists()


def test_r13_subset_is_never_admissible_even_when_everything_else_is_green(tmp_path):
    res = K.run_adapter(tmp_path, S(args=["--paths", "tools/a.py"]))
    assert res.rc == 1 and res.refusal_ids() == {"R13a"}
    assert not (res.out / "run-manifest.json").exists()


def test_every_fired_predicate_is_listed_not_just_the_first(tmp_path):
    """RA-2: two independent predicates fired in one run are both in refusal.json."""
    res = K.run_adapter(tmp_path, S(pytest_rc=1, log_text=K.WARN_NO_DATA))
    assert res.rc == 1 and {"R1a", "R11a"} <= res.refusal_ids()


def test_test_only_seams_are_not_reachable_from_the_cli(capsys):
    """The R13b-disable harness and every other seam is a keyword-only parameter of `execute`, never an argv option."""
    import inspect
    for flag in ("--disable", "--seams", "--disabled", "--skip-predicate", "--assets-dir", "--pinned-config-sha"):
        with pytest.raises(SystemExit) as exc:
            A.parse_args([flag, "R13b"])
        assert exc.value.code == 2
    assert inspect.signature(A.execute).parameters["seams"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "seams" not in inspect.signature(A.main).parameters
    with pytest.raises(SystemExit) as exc:
        A.parse_args(["--help"])
    assert exc.value.code == 0
    assert "--baseline-run" in capsys.readouterr().out
