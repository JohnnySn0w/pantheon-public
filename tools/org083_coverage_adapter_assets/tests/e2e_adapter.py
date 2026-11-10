"""ORG-083 coverage adapter: end-to-end tests against a REAL built adapter venv (T-5971).

NOT collected by `pytest tools/` (the name does not match test_*.py, and the tests/ directory is
covered by the proposal's own `*/tools/**/tests/*` omit). Run explicitly:

    nice -n 10 python3 -m pytest tools/org083_coverage_adapter_assets/tests/e2e_adapter.py -q -p no:cacheprovider

Needs the adapter venv (default ~/.cache/pantheon/org083-adapter-venv, or $ORG083_E2E_VENV): CI's pins
plus the hash-locked coverage 7.17.0, built by the adapter's own `ensure_adapter_venv`. These tests run
the real tool on tiny fixture repositories; they never run the whole-tree evidence pass, and they do not
recurse into a measured run (no COVERAGE_* is ever set in this process, which R16b would refuse).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools import test_org083_adapter_kit as K  # noqa: E402
from tools import test_org083_coverage_adapter_semantics as SEM  # noqa: E402

A = K.A
REAL_CI_YML = (K.REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
VENV = Path(os.environ.get("ORG083_E2E_VENV") or A.DEFAULT_VENV)
VPY = VENV / "bin" / "python"

PROJECT = {
    ".github/workflows/ci.yml": "name: ci\njobs:\n  t:\n    steps:\n      - name: Install test dependencies\n        run: "
                                + [ln for ln in REAL_CI_YML.splitlines() if "python -m pip install --upgrade pip pyyaml" in ln][0].split("run: ", 1)[1] + "\n",
    ".gitignore": "__pycache__/\n*.pyc\n",
    "tools/__init__.py": "",
    "tools/conftest.py": "",
    "tools/a.py": "def f(x):\n    if x:\n        return 1\n    return 2\n\n\ndef g(x):\n    return x + 1\n",
    "tools/b.py": "def h(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n",
    "tools/test_a.py": ("import subprocess, sys\nfrom tools.a import f, g\nfrom tools.b import h\n\n\ndef test_a():\n    assert f(1) == 1\n"
                        "    assert h(3) == 3\n\n\ndef test_g():\n    assert g(1) == 2\n\n\ndef test_sub():\n"
                        "    subprocess.run([sys.executable, '-c', 'import tools.a as a; a.f(0)'], check=True)\n"),
    "tools/hooks/x-y.py": "print('hook')\n",
    "tools/check_shell_suite_outcomes.py": "import sys\nsys.exit(0)\n",
}


def real_environ():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return env


def real_run(tmp, *, files=None, out=None, extra=(), seams=None, repo=None, baseline=None, wrap=None, workers=2):
    repo = repo or K.make_fixture_repo(tmp / "fx", files or PROJECT)
    out = out or tmp / "out"
    runner = wrap(subprocess.run) if wrap else subprocess.run
    argv = ["--rev", repo[1], "--out-dir", str(out), "--venv", str(VENV), "--workers", str(workers), *extra]
    if baseline:
        argv += ["--baseline-run", str(baseline)]
    rc = A.execute(argv, runner=runner, repo_root=repo[0], environ=real_environ(),
                   seams=seams or A.Seams(undiscoverable_paths=[], runner_label="local:e2e"))
    return K.Result(rc, out, repo[0], None), repo


def assert_admissible(res):
    refusal = (res.out / "refusal.json").read_text() if (res.out / "refusal.json").exists() else ""
    assert res.rc == 0, refusal


def rewrite_venv_output(real, match, fn):
    def wrap(run):
        def runner(argv, **kw):
            r = run(argv, **kw)
            if match(argv):
                r = subprocess.CompletedProcess(r.args, r.returncode, fn(r.stdout or ""), r.stderr)
            return r
        return runner
    return wrap


def test_venv_is_the_hash_locked_coverage_with_a_c_tracer():
    assert VPY.exists(), f"build the adapter venv first: {VENV}"
    sys_out = subprocess.run([str(VPY), "-m", "coverage", "debug", "sys"], capture_output=True, text=True).stdout
    print([ln for ln in sys_out.splitlines() if "coverage_version" in ln or "CTracer" in ln or "core" in ln])
    obs = A.parse_observation(sys_out, subprocess.run([str(VPY), "-m", "coverage", "debug", "config"], capture_output=True, text=True,
                                                      env={**real_environ(), "COVERAGE_RCFILE": str(A.ASSETS_DIR / A.CONFIG_NAME)}).stdout)
    assert (obs.version, obs.ctracer, obs.core) == ("7.17.0", True, "ctrace")
    assert subprocess.run([str(VPY), "-c", "import pytest, xdist, yaml"], capture_output=True).returncode == 0


def test_drift_fixture_data_layout_matches_a_real_coverage_data_file(tmp_path):
    """The stdlib-built fixtures used by the collected tests have the real 7.17.0 table and column layout."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "noop.py").write_text("x = 1\nif x:\n    y = 2\n", encoding="utf-8")
    subprocess.run([str(VPY), "-m", "coverage", "run", "--branch", "--data-file", str(real / ".coverage"), str(tmp_path / "noop.py")],
                   cwd=tmp_path, env=real_environ(), check=True, capture_output=True)
    fake = K.write_data_file(tmp_path / "fake")

    def layout(path):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = sorted(r[0] for r in con.execute("select name from sqlite_master where type='table'"))
        return {t: [c[1] for c in con.execute(f"pragma table_info({t})")] for t in tables}, con.execute("select version from coverage_schema").fetchone()
    assert layout(real / ".coverage") == layout(fake)
    info = A.read_data_file(real / ".coverage")
    assert info.readable and info.has_arcs and info.executed_rows > 0


def test_drift_real_combine_and_warning_texts_parse_as_the_collected_fixtures_assume(tmp_path):
    """Corrupt / unreadable / mixed combine outputs of the real tool, against the parser the unit tests exercise."""
    (tmp_path / "noop.py").write_text("x = 1\n", encoding="utf-8")
    cfg = tmp_path / "rc"
    cfg.write_text("[run]\nbranch = True\nparallel = True\n", encoding="utf-8")
    stmt = tmp_path / "rc_stmt"
    stmt.write_text("[run]\nbranch = False\nparallel = True\n", encoding="utf-8")

    def make(data_dir, rc, n):
        data_dir.mkdir()
        for _ in range(n):
            subprocess.run([str(VPY), "-m", "coverage", "run", f"--rcfile={rc}", str(tmp_path / "noop.py")], cwd=tmp_path,
                           env={**real_environ(), "COVERAGE_FILE": str(data_dir / ".coverage")}, check=True, capture_output=True)

    def combine(data_dir, rc):
        return subprocess.run([str(VPY), "-m", "coverage", "combine", f"--rcfile={rc}", "--debug=combine", "--keep"], cwd=tmp_path,
                              env={**real_environ(), "COVERAGE_FILE": str(data_dir / ".coverage")}, capture_output=True, text=True)
    bad = tmp_path / "bad"
    make(bad, cfg, 2)
    sorted(bad.glob(".coverage.*"))[0].write_bytes(b"not a sqlite file at all" * 20)
    corrupt = A.parse_combine(*(lambda r: (r.returncode, r.stdout, r.stderr))(combine(bad, cfg)))
    assert corrupt.errored and "SQLITE_NOTADB" in corrupt.errored[0][1]
    nolook = tmp_path / "nolook"
    make(nolook, cfg, 2)
    victim = sorted(nolook.glob(".coverage.*"))[0]
    os.chmod(victim, 0)
    unreadable = A.parse_combine(*(lambda r: (r.returncode, r.stdout, r.stderr))(combine(nolook, cfg)))
    assert unreadable.errored and "SQLITE_CANTOPEN" in unreadable.errored[0][1]
    mixed_dir = tmp_path / "mixed"
    make(mixed_dir, cfg, 1)
    other = tmp_path / "other"
    make(other, stmt, 1)
    for f in other.glob(".coverage.*"):
        f.rename(mixed_dir / (".coverage.stmt." + f.name.split(".", 2)[2]))
    r = combine(mixed_dir, cfg)
    assert A.parse_combine(r.returncode, r.stdout, r.stderr).mixed and r.returncode != 0
    assert A.parse_warnings(K.WARN_NO_DATA) and "(no-data-collected)" in K.WARN_NO_DATA


def test_positive_real_run_is_admissible_and_every_dimension_is_tool_native(tmp_path):
    res, _ = real_run(tmp_path)
    assert_admissible(res)
    inputs = json.loads((res.out / "measurement-inputs.json").read_text())
    line = next(r for r in inputs["records"] if r["dimension"] == "line")
    branch = next(r for r in inputs["records"] if r["dimension"] == "branch")
    assert 0 < line["numerator"] <= line["denominator"] and 0 < branch["numerator"] <= branch["denominator"]
    resolved = json.loads((res.out / "semantic-map.resolved.json").read_text())
    assert resolved["compared"]["branch_semantics"]["instance"] == {"effective_core": "ctrace", "has_arcs": True}
    acct = json.loads((res.out / "accounting.json").read_text())
    assert len([d for d in acct["data_files"] if d["controller"]]) == 1 and len(acct["data_files"]) >= 3
    assert not (res.out / "repo").exists()


def test_sm1_two_real_runs_are_comparable_and_validator_accepted(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx", PROJECT)
    r1, _ = real_run(tmp_path, repo=repo, out=tmp_path / "o1")
    r2, _ = real_run(tmp_path, repo=repo, out=tmp_path / "o2", baseline=r1.out)
    assert_admissible(r1)
    assert_admissible(r2)
    for rec in SEM.records(r2):
        assert rec["comparability"]["changed_fields"] == [] and rec["comparability"]["verdict"] == "comparable"
    assert SEM.comparability_codes(SEM.shape_check(r1, r2)) == set()
    f1, f2 = (json.loads((r.out / "measurement-inputs.json").read_text())["fingerprints"] for r in (r1, r2))
    assert f1 == f2  # FC-6 end to end


def test_sm3_real_runs_detect_each_r1_falsifier(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx", PROJECT)
    base, _ = real_run(tmp_path, repo=repo, out=tmp_path / "base")
    assert_admissible(base)

    def second(name, intended, **kw):
        res, _ = real_run(tmp_path, repo=repo, out=tmp_path / name, baseline=base.out, **kw)
        assert_admissible(res)
        SEM.assert_reset(base, res, intended)
    second("a", ["branch_semantics"], seams=_map_seams(tmp_path, "a", "branch_semantics"))
    second("b", ["function_semantics"], seams=_map_seams(tmp_path, "b", "function_semantics"))
    second("c", ["test_selection"], extra=["--pytest-arg=--deselect=tools/test_a.py::test_g"],
           seams=A.Seams(undiscoverable_paths=[], runner_label="local:e2e", disabled=frozenset({"R13b", "R17c.selection"})))
    second("d", ["collector.tool_version"], wrap=rewrite_venv_output(None, lambda a: a[-2:] == ["debug", "sys"],
                                                                      lambda out: out.replace("coverage_version: 7.17.0", "coverage_version: 7.16.2")),
           seams=A.Seams(undiscoverable_paths=[], runner_label="local:e2e",
                         coverage_pin=A.CoveragePin("7.16.2", {"cp311": "a" * 64, "cp312": "b" * 64})))
    _ = A.ENV_SET


def _map_seams(tmp, name, field):
    kw = SEM.edited_map(tmp, "map_" + name, lambda f: f[field].update(statement="REVISED " + field))
    return A.Seams(undiscoverable_paths=[], runner_label="local:e2e", **kw)


def test_sm3e_env_allowlist_without_ci_is_detected(tmp_path, monkeypatch):
    repo = K.make_fixture_repo(tmp_path / "fx", PROJECT)
    base, _ = real_run(tmp_path, repo=repo, out=tmp_path / "base")
    monkeypatch.setattr(A, "ENV_SET", {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1"})
    cur, _ = real_run(tmp_path, repo=repo, out=tmp_path / "cur", baseline=base.out)
    assert_admissible(cur)
    SEM.assert_reset(base, cur, ["test_selection", "collector.command_fingerprint"])


def test_sm3x_and_sm3y_content_edit_does_not_reset_but_an_added_file_does(tmp_path):
    repo = K.make_fixture_repo(tmp_path / "fx", PROJECT)
    base, _ = real_run(tmp_path, repo=repo, out=tmp_path / "base")
    rev2 = SEM.commit(repo[0], {"tools/a.py": PROJECT["tools/a.py"].replace("return x + 1", "return x + 1  # edited body"),
                                "tools/test_extra.py": "def test_extra():\n    assert True\n"})
    content, _ = real_run(tmp_path, repo=(repo[0], rev2), out=tmp_path / "content", baseline=base.out)
    assert_admissible(content)
    assert all(r["comparability"]["changed_fields"] == [] for r in SEM.records(content))
    rev3 = SEM.commit(repo[0], {"tools/c.py": "def k():\n    return 1\n"})
    added, _ = real_run(tmp_path, repo=(repo[0], rev3), out=tmp_path / "added", baseline=base.out)
    assert_admissible(added)
    SEM.assert_reset(base, added, ["scope.scope_fingerprint"])


def test_fc4_two_real_checkouts_at_different_roots_fingerprint_identically(tmp_path):
    a, _ = real_run(tmp_path / "one", out=tmp_path / "one" / "out")
    b, _ = real_run(tmp_path / "two" / "deeper", out=tmp_path / "two" / "deeper" / "out")
    fa, fb = (json.loads((r.out / "measurement-inputs.json").read_text())["fingerprints"] for r in (a, b))
    for key in ("scope_fingerprint", "command_fingerprint", "config_digest"):
        assert fa[key] == fb[key]


def test_sm8_a_sysmon_core_observation_contradicts_the_ctrace_declaration(tmp_path):
    res, _ = real_run(tmp_path, wrap=rewrite_venv_output(None, lambda a: a[-2:] == ["debug", "config"],
                                                         lambda out: out.replace("core: ctrace", "core: sysmon")))
    assert res.rc == 1 and {"R17c", "R5c"} <= res.refusal_ids()


def test_r6a_f2_shape_a_missing_subprocess_patch_exits_zero_with_no_worker_data(tmp_path):
    """Local finding F2, real: without `patch = subprocess` the xdist workers are never measured."""
    dst = tmp_path / "assets"
    import shutil
    shutil.copytree(A.ASSETS_DIR, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    cfg = dst / A.CONFIG_NAME
    cfg.write_text(cfg.read_text().replace("patch = subprocess\n", "").replace("parallel = True\n", "parallel = True\n"), encoding="utf-8")
    res, _ = real_run(tmp_path, seams=A.Seams(undiscoverable_paths=[], runner_label="local:e2e", assets_dir=dst,
                                              pinned_config_sha=A.sha256_file(cfg)))
    assert res.rc == 1 and "R6a" in res.refusal_ids()


def test_r9b_f1_shape_relative_files_corrupts_the_denominator(tmp_path):
    """Local finding F1, real: relative_files = True records alias keys from a subprocess started in tools/."""
    files = dict(PROJECT)
    files["tools/helper_script.py"] = "print('helper')\n"
    files["tools/test_a.py"] += ("\n\ndef test_cwd_tools():\n    import pathlib\n    subprocess.run([sys.executable, 'helper_script.py'], "
                                 "cwd=pathlib.Path(__file__).parent, check=True)\n")
    dst = tmp_path / "assets"
    import shutil
    shutil.copytree(A.ASSETS_DIR, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    cfg = dst / A.CONFIG_NAME
    cfg.write_text(cfg.read_text().replace("relative_files = False", "relative_files = True"), encoding="utf-8")
    res, _ = real_run(tmp_path, files=files, seams=A.Seams(undiscoverable_paths=[], runner_label="local:e2e", assets_dir=dst,
                                                           pinned_config_sha=A.sha256_file(cfg)))
    assert res.rc == 1 and ({"R9b"} <= res.refusal_ids() or {"R12c"} <= res.refusal_ids())


def test_r10b_f4_shape_a_tracked_in_scope_file_the_tool_cannot_discover(tmp_path):
    """Local finding F4 shape, real: a name coverage's discovery regex rejects stays absent from the report."""
    files = dict(PROJECT)
    files["tools/odd.name.py"] = "x = 1\n"
    res, _ = real_run(tmp_path, files=files)
    assert res.rc == 1 and "R10b" in res.refusal_ids()
    recorded, _ = real_run(tmp_path / "again", files=files, out=tmp_path / "again" / "out",
                           seams=A.Seams(undiscoverable_paths=["tools/odd.name.py"], runner_label="local:e2e"))
    assert_admissible(recorded)


def tree_copy(tmp):
    """A tmp git repository holding a copy of this tree's tools/ (the real-state guard forbids git against the
    real repository from a test); built once per test and reused by the no-op measurements."""
    import shutil
    wt = tmp / "treecopy"
    if not wt.exists():
        shutil.copytree(K.REPO / "tools", wt / "tools", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".coverage*"))
        subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(wt), *K.GIT_ID, "commit", "-q", "-m", "copy"], check=True, capture_output=True)
    return wt


def noop_report(tmp, cfg_text, name):
    """No-op measurement (`coverage run noop.py`, then json) of the CURRENT tree: the discoverable file set."""
    wt = tree_copy(tmp)
    (tmp / f"{name}.rc").write_text(cfg_text, encoding="utf-8")
    data = tmp / f"data_{name}"
    data.mkdir()
    env = {**real_environ(), "COVERAGE_FILE": str(data / ".coverage")}
    (tmp / "noop.py").write_text("pass\n", encoding="utf-8")
    for step in (["run", f"--rcfile={tmp / (name + '.rc')}", str(tmp / "noop.py")], ["combine", f"--rcfile={tmp / (name + '.rc')}", "--keep"],
                 ["json", f"--rcfile={tmp / (name + '.rc')}", "-o", str(tmp / f"{name}.json")]):
        r = subprocess.run([str(VPY), "-m", "coverage", *step], cwd=wt, env=env, capture_output=True, text=True)
        assert r.returncode == 0, (step, r.stderr)
    reported = set(json.loads((tmp / f"{name}.json").read_text())["files"])
    tracked = [p for p in subprocess.run(["git", "-C", str(wt), "ls-files", "-z"], capture_output=True, text=True).stdout.split("\0")
               if p.endswith(".py")]
    in_scope, fires = A.build_in_scope(tracked, wt)
    return reported, in_scope, fires, r.stderr


def test_q3_include_namespace_packages_outcome_and_the_undiscoverable_list_is_honest(tmp_path):
    """Q3: the option is a [report] option (ignored, with a warning, under [run]); with it on the F4 files ARE
    discovered, so the recorded undiscoverable list is exactly the no-op measurement's residual."""
    base = (A.ASSETS_DIR / A.CONFIG_NAME).read_text(encoding="utf-8")
    off = base.replace("include_namespace_packages = True\n", "")
    on_reported, in_scope, fires, _ = noop_report(tmp_path, base, "on")
    assert fires == []
    assert sorted(set(in_scope) - on_reported) == sorted(A.UNDISCOVERABLE_PATHS)
    off_reported, _, _, _ = noop_report(tmp_path, off, "off")
    assert len(set(in_scope) - off_reported) > 0 and on_reported >= off_reported
    run_placement = off.replace("relative_files = False\n", "relative_files = False\ninclude_namespace_packages = True\n")
    r_reported, _, _, _stderr = noop_report(tmp_path, run_placement, "runsec")
    assert r_reported == off_reported
    print("Q3: [report] on ->", len(on_reported), "files; off ->", len(off_reported), "; residual", sorted(set(in_scope) - on_reported))
