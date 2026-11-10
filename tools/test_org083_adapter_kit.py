"""Shared fixtures and helpers for the ORG-083 coverage adapter tests (T-5971). Holds NO tests.

The name matches the `*/tools/test_*.py` omit pattern, so this file is never measured. It
provides: the adapter loader (honoring ORG083_ADAPTER_UNDER_TEST, the mutation driver's hook),
tiny real git fixture repositories, a scripted stand-in for the adapter venv's python
(coverage is NOT importable in the plain suite env, and none of these helpers import it), the
split runner (real git, scripted venv calls), stdlib-built coverage data files and the shared
RA-1..RA-6 refusal assertion helper.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
UNDER_TEST_ENV = "ORG083_ADAPTER_UNDER_TEST"
RA_LOG_ENV = "ORG083_RA_LOG"
SYS_MODULE = "tools.org083_coverage_adapter"


def load_adapter():
    """The adapter module under test: the committed one, or the mutant the driver points at."""
    path = os.environ.get(UNDER_TEST_ENV)
    if not path:
        return importlib.import_module(SYS_MODULE)
    cached = sys.modules.get(SYS_MODULE)
    if cached is not None and Path(cached.__file__).resolve() == Path(path).resolve():
        return cached
    spec = importlib.util.spec_from_file_location(SYS_MODULE, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[SYS_MODULE] = module
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == Path(path).resolve()
    return module


A = load_adapter()
CI_INSTALL = "python -m pip install --upgrade pip pyyaml==6.0.3 pytest==9.1.1 pytest-xdist==3.8.0"
CI_YML = f"""name: ci
jobs:
  test:
    steps:
      - name: Install test dependencies
        run: {CI_INSTALL}
"""
GIT_ID = ["-c", "user.email=t@example.test", "-c", "user.name=T"]


# ---------------------------------------------------------------------------------------------
# Real text shapes captured from coverage 7.17.0 (D11): see the e2e drift test.
# ---------------------------------------------------------------------------------------------

def kv(key: str, value: str) -> str:
    return f"{key:>31}: {value}"


def debug_sys_text(version="7.17.0", ctracer=True, core="-none-", python="3.12.3 (main, Aug 31 2026, 10:18:26) [GCC 13.3.0]") -> str:
    tracer = "available from /venv/lib/python3.12/site-packages/coverage/tracer.cpython-312-x86_64-linux-gnu.so" if ctracer else "unavailable"
    return "\n".join(["-- sys -------------------------------------------------------",
                      kv("coverage_version", version), kv("coverage_module", "/venv/lib/python3.12/site-packages/coverage/__init__.py"),
                      kv("core", core), kv("CTracer", tracer), kv("plugins.file_tracers", "-none-"),
                      kv("python", python), kv("platform", "Linux-6.8.0-147-generic-x86_64-with-glibc2.39"),
                      kv("implementation", "CPython"), kv("gil_enabled", "True"), kv("pid", "123"), kv("cwd", "/x")]) + "\n"


DEFAULT_EXCLUDE = [r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)",
                   r"^\s*(((async )?def .*?)?[\])]+(\s*->.*?)?:\s*)?\.\.\.\s*(#|$)", r"^\s*if (typing\.)?TYPE_CHECKING:"]


def debug_config_text(core="ctrace", branch="True") -> str:
    lines = ["-- config ----------------------------------------------------", kv("branch", branch), kv("core", core)]
    lines.append(kv("exclude_list", DEFAULT_EXCLUDE[0]))
    lines += [" " * 33 + ln for ln in DEFAULT_EXCLUDE[1:]]
    lines += [kv("parallel", "True"), kv("partial_list", r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(branch|BRANCH)"),
              kv("patch", "subprocess"), kv("relative_files", "False"), kv("source", "tools"),
              kv("data_file", "/out/data/.coverage")]
    return "\n".join(lines) + "\n"


COMBINE_OK = "Combined data file ../data/{n}\n" * 1
SQLITE_NOTADB = "file is not a database: (SQLITE_NOTADB)"
SQLITE_CANTOPEN = "unable to open database file: (SQLITE_CANTOPEN)"
WARN_NO_DATA = ("/v/coverage/control.py:967: CoverageWarning: No data was collected. (no-data-collected); "
                "see https://coverage.readthedocs.io/en/7.17.0/messages.html#warning-no-data-collected\n")
WARN_NOT_MEASURED = ("/v/coverage/inorout.py:508: CoverageWarning: Module {m} was previously imported, but not "
                     "measured (module-not-measured); see https://coverage.readthedocs.io/en/7.17.0/messages.html\n")
WARN_COULDNT_PARSE = ("/v/coverage/report_core.py:99: CoverageWarning: Couldn't parse Python file '{p}' "
                      "(couldnt-parse); see https://coverage.readthedocs.io/en/7.17.0/messages.html\n")
WARN_INCLUDE_IGNORED = ("/v/coverage/inorout.py:1: CoverageWarning: --include is ignored because --source is set "
                        "(include-ignored); see https://coverage.readthedocs.io/en/7.17.0/messages.html\n")
MIXED_ERROR = ("Can't combine branch coverage data with statement data: data file '{d}/.coverage.h.pid1.a.b' measured "
               "branch coverage, but data file '{d}/.coverage' didn't; see "
               "https://coverage.readthedocs.io/en/7.17.0/messages.html#error-cant-combine\n")


# ---------------------------------------------------------------------------------------------
# Data files (stdlib-built, coverage 7.17.0 schema 7) and fixture repositories.
# ---------------------------------------------------------------------------------------------

DATA_DDL = """
create table coverage_schema (version integer);
create table meta (key text, value text, unique (key));
create table file (id integer primary key, path text, unique (path));
create table context (id integer primary key, context text, unique (context));
create table line_bits (file_id integer, context_id integer, numbits blob, unique (file_id, context_id));
create table arc (file_id integer, context_id integer, fromno integer, tono integer, unique (file_id, context_id, fromno, tono));
create table tracer (file_id integer primary key, tracer text);
"""


def write_data_file(path: Path, *, argv0: str = "-c", files: tuple[str, ...] = ("tools/a.py",), arcs: int = 3,
                    has_arcs: bool = True, schema: int = 7) -> Path:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.executescript(DATA_DDL)
        con.execute("insert into coverage_schema values (?)", (schema,))
        con.executemany("insert into meta values (?, ?)", [("version", "7.17.0"), ("sys_argv", repr([argv0])),
                                                           ("has_arcs", "1" if has_arcs else "0")])
        con.execute("insert into context values (1, '')")
        for i, name in enumerate(files, start=1):
            con.execute("insert into file values (?, ?)", (i, name))
            for n in range(arcs if has_arcs else 0):
                con.execute("insert into arc values (?, 1, ?, ?)", (i, n, n + 1))
            if not has_arcs:
                con.execute("insert into line_bits values (?, 1, ?)", (i, b"\x06"))
        con.commit()
    finally:
        con.close()
    return path


CONTROLLER_ARGV0 = "/venv/lib/python3.12/site-packages/pytest/__main__.py"
FILES = {
    "tools/__init__.py": "",
    "tools/a.py": "def f(x):\n    if x:\n        return 1\n    return 2\n",
    "tools/test_a.py": "def test_a():\n    assert True\n",
    "tools/conftest.py": "",
    "tools/hooks/x-y.py": "print('x')\n",
    "tools/check_shell_suite_outcomes.py": "import sys\nsys.exit(0)\n",
    ".github/workflows/ci.yml": CI_YML,
}
UNDISCOVERABLE = ("tools/hooks/x-y.py",)
REPORTED = ("tools/__init__.py", "tools/a.py", "tools/check_shell_suite_outcomes.py")


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def make_fixture_repo(root: Path, files: dict[str, str] | None = None, symlinks: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    for rel, text in (files or FILES).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for rel, target in (symlinks or {}).items():
        os.symlink(target, repo / rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo, git(repo, "rev-parse", "HEAD")


def make_fake_venv(root: Path, ci_pins=None) -> Path:
    """A stand-in adapter venv: bin/python exists and both reuse markers are current, so
    ensure_adapter_venv reuses it (no pip, no network)."""
    venv = Path(root) / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    pins = tuple(ci_pins or A.soak_leg.parse_ci_pins(CI_YML))
    (venv / A.soak_leg.PINS_MARKER).write_text(A.soak_leg._marker_text(pins), encoding="utf-8")
    (venv / A.ADAPTER_MARKER).write_text(A._adapter_marker_text(pins, A.sha256_file(A.ASSETS_DIR / A.LOCK_NAME)), encoding="utf-8")
    return venv


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# Scenario + scripted backend + split runner.
# ---------------------------------------------------------------------------------------------

def default_junit(executed: int = 2) -> str:
    cases = "".join(f'<testcase classname="tools.test_a" name="test_{i}" time="0.01"/>' for i in range(executed))
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="{executed}">{cases}</testsuite></testsuites>'


def default_cov_json(files=REPORTED, totals=None) -> dict:
    t = {"covered_lines": 8, "num_statements": 10, "covered_branches": 3, "num_branches": 4, "missing_lines": 2,
         "excluded_lines": 1, "num_partial_branches": 1, "missing_branches": 1}
    t.update(totals or {})
    return {"meta": {"format": 3, "version": "7.17.0", "branch_coverage": True},
            "files": {f: {"executed_lines": [1], "excluded_lines": [], "summary": {}} for f in files}, "totals": t}


@dataclass
class Scenario:
    """What the scripted venv python does. Defaults describe one fully admissible 2-worker run."""
    workers: int = 2
    version: str = "7.17.0"
    ctracer: bool = True
    sys_core: str = "-none-"
    config_core: str = "ctrace"
    pytest_rc: int = 0
    junit: str | None = default_junit()           # None: no report written
    junit_age: float = 0.0
    data_files: list | None = None                 # None: controller + N workers; entries are dicts for write_data_file
    data_age: float = 0.0
    combine_rc: int = 0
    combine_stdout: str = ""
    combine_stderr_extra: str = ""
    combine_stderr: str | None = None              # replaces the whole default combine stderr
    combined_has_arcs: bool = True
    cov_json: dict | None = None
    json_rc: int = 0
    json_stderr: str = ""
    log_text: str = ""
    junit_system_err: str = ""
    run_hook: Callable | None = None               # (argv, kwargs) at the measured run
    timeout: bool = False
    args: list = field(default_factory=list)
    environ_extra: dict = field(default_factory=dict)
    seams_kw: dict = field(default_factory=dict)
    repo_files: dict | None = None
    repo_symlinks: dict | None = None
    post_worktree: Callable | None = None          # (worktree path) after `git worktree add`
    git_override: Callable | None = None           # (argv) -> CompletedProcess | None
    redirect_vars: str | None = None               # replaces `git rev-parse --local-env-vars` stdout
    freeze: str = "pytest==9.1.1\npyyaml==6.0.3\npytest-xdist==3.8.0\ncoverage==7.17.0\n"
    checker_script: str | None = None              # overrides tools/check_shell_suite_outcomes.py content in the repo


class SplitRunner:
    """Real subprocess for git and plain python; scripted answers for the venv python."""

    def __init__(self, venv: Path, scenario: Scenario):
        self.venv, self.s = venv, scenario
        self.vpy = str(venv / "bin" / "python")
        self.calls: list[list[str]] = []
        self.measured_env: dict | None = None

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == self.vpy:
            return self._venv_python(argv, kw)
        if argv[:2] == ["git", "rev-parse"] and "--local-env-vars" in argv and self.s.redirect_vars is not None:
            return subprocess.CompletedProcess(argv, 0, self.s.redirect_vars, "")
        if self.s.git_override:
            got = self.s.git_override(argv)
            if got is not None:
                return got
        result = subprocess.run(argv, **kw)
        if "worktree" in argv and "add" in argv and self.s.post_worktree:
            self.s.post_worktree(Path(argv[argv.index("--detach") + 1]))
        return result

    def _ok(self, argv, out="", err="", rc=0):
        return subprocess.CompletedProcess(argv, rc, out, err)

    def _venv_python(self, argv, kw):
        s = self.s
        if argv[1:4] == ["-m", "coverage", "debug"]:
            text = debug_sys_text(s.version, s.ctracer, s.sys_core) if argv[4] == "sys" else debug_config_text(s.config_core)
            return self._ok(argv, text)
        if argv[1:3] == ["-m", "pip"]:
            return self._ok(argv, s.freeze if argv[3] == "freeze" else "")
        sub = argv[3]
        if sub == "run":
            return self._measured(argv, kw)
        env = kw.get("env") or {}
        data_dir = Path(env.get("COVERAGE_FILE", "/nonexistent/.coverage")).parent
        if sub == "combine":
            infos = sorted(data_dir.glob(".coverage.*"))
            err = "".join(f"Combined data file ../data/{p.name}\n" for p in infos) + s.combine_stderr_extra
            err += f"Combined {len(infos)} files\n"
            if s.combine_stderr is not None:
                err = s.combine_stderr
            if s.combined_has_arcs is not None:
                write_data_file(data_dir / ".coverage", argv0=CONTROLLER_ARGV0, has_arcs=s.combined_has_arcs)
            return self._ok(argv, s.combine_stdout, err, s.combine_rc)
        if sub == "json":
            out = Path(argv[argv.index("-o") + 1])
            if s.json_rc == 0:
                out.write_text(json.dumps(s.cov_json if s.cov_json is not None else default_cov_json()), encoding="utf-8")
            return self._ok(argv, "", s.json_stderr, s.json_rc)
        if sub == "report":
            return self._ok(argv, "| Name | Stmts |\n|---|---|\n| tools/a.py | 4 |\n")
        raise AssertionError(f"unscripted venv call: {argv}")

    def _measured(self, argv, kw):
        s = self.s
        self.measured_env = dict(kw.get("env") or {})
        if s.run_hook:
            s.run_hook(argv, kw)
        if s.timeout:
            raise subprocess.TimeoutExpired(argv, 1)
        junit = Path(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        if s.junit is not None:
            text = s.junit
            if s.junit_system_err:
                text = text.replace("</testsuite>", f"<system-err>{s.junit_system_err}</system-err></testsuite>")
            junit.write_text(text, encoding="utf-8")
            if s.junit_age:
                os.utime(junit, (time.time() - s.junit_age,) * 2)
        data_dir = Path(kw["env"]["COVERAGE_FILE"]).parent
        specs = s.data_files
        if specs is None:
            specs = [dict(argv0=CONTROLLER_ARGV0, files=REPORTED)] + [dict(argv0="-c", files=REPORTED) for _ in range(s.workers)]
        for i, spec in enumerate(specs):
            path = data_dir / f".coverage.host.pid{1000 + i}.aaaa.bbbb{i}"
            if spec.get("raw") is not None:
                path.write_bytes(spec["raw"])
            else:
                write_data_file(path, **{k: v for k, v in spec.items() if k != "mode"})
            if spec.get("mode") is not None:
                os.chmod(path, spec["mode"])
            if s.data_age:
                os.utime(path, (time.time() - s.data_age,) * 2)
        if kw.get("stdout") is not None:
            kw["stdout"].write(s.log_text)
            kw["stdout"].flush()
        return self._ok(argv, rc=s.pytest_rc)


@dataclass
class Result:
    rc: int
    out: Path
    repo: Path
    runner: SplitRunner
    stdout: str = ""
    stderr: str = ""

    def refusal(self) -> dict:
        return json.loads((self.out / "refusal.json").read_text(encoding="utf-8"))

    def refusal_ids(self) -> set[str]:
        return {r["id"] for r in self.refusal()["refusals"]}


def run_adapter(tmp: Path, scenario: Scenario | None = None, *, capfd=None, out: Path | None = None,
                repo: tuple[Path, str] | None = None, venv: Path | None = None, extra_args=(), baseline: Path | None = None) -> Result:
    s = scenario or Scenario()
    repo_path, rev = repo or make_fixture_repo(Path(tmp) / "fx", s.repo_files, s.repo_symlinks)
    venv = venv or make_fake_venv(Path(tmp) / "vx")
    out = out or Path(tmp) / "out"
    runner = SplitRunner(venv, s)
    seams_kw = dict(undiscoverable_paths=list(UNDISCOVERABLE), runner_label="local:test")
    seams_kw.update(s.seams_kw)
    seams = A.Seams(**seams_kw)
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    environ.update(s.environ_extra)
    argv = ["--rev", rev, "--out-dir", str(out), "--venv", str(venv), "--workers", str(s.workers), *s.args, *extra_args]
    if baseline is not None:
        argv += ["--baseline-run", str(baseline)]
    rc = A.execute(argv, runner=runner, repo_root=repo_path, environ=environ, seams=seams)
    res = Result(rc, out, repo_path, runner)
    if capfd is not None:
        cap = capfd.readouterr()
        res.stdout, res.stderr = cap.out, cap.err
    return res


# ---------------------------------------------------------------------------------------------
# The shared refusal assertion (RA-1..RA-6). Every row test goes through this one helper.
# ---------------------------------------------------------------------------------------------

def repo_snapshot(repo: Path) -> str:
    """Everything under the repository except .git internals that git itself rewrites."""
    h = hashlib.sha256()
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts[:1]:
            continue
        h.update(str(p.relative_to(repo)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def assert_refusal(res: Result, expected_ids, *, retained: Path | None = None, retained_before: str | None = None,
                   repo_before: str | None = None, tc=None) -> dict:
    """Asserts RA-1..RA-6 plus that every expected predicate id is listed. Returns the per-RA results."""
    ra: dict[str, bool] = {}

    def check(key, cond, msg):
        ra[key] = bool(cond)
        assert cond, f"{key}: {msg}"

    check("RA-1", res.rc == 1, f"exit code {res.rc}, expected exactly 1")
    refusal = json.loads((res.out / "refusal.json").read_text(encoding="utf-8")) if (res.out / "refusal.json").exists() else {}
    listed = {r["id"] for r in refusal.get("refusals", [])}
    check("RA-2", refusal.get("admissible") is False and set(expected_ids) <= listed
          and all(r.get("reason") and "evidence" in r for r in refusal.get("refusals", [])),
          f"refusal.json must list {sorted(expected_ids)} with reason+evidence; listed {sorted(listed)}")
    inputs = json.loads((res.out / "measurement-inputs.json").read_text(encoding="utf-8"))
    md = (res.out / "coverage.md").read_text(encoding="utf-8")
    check("RA-3", not (res.out / "run-manifest.json").exists() and inputs.get("admissible") is False
          and set(inputs) == {"schema", "adapter_id", "admissible", "refusal_codes"}
          and "records" not in inputs and not re.search(r"\d+\s*/\s*\d+", md) and "line (statement)" not in md,
          "a refusal must publish no manifest, record body, numerator, denominator or coverage totals")
    diag = res.out / "diagnostics"
    published = [p.name for p in res.out.iterdir() if p.name.startswith("coverage.json") or p.name == "accounting.json"
                 or p.name == "semantic-map.resolved.json"]
    check("RA-4", diag.is_dir() and not published and "diagnostics" not in json.dumps(inputs) and "run-manifest" not in json.dumps(refusal),
          f"diagnostics must stay quarantined and unreferenced; published {published}")
    retained_ok = True
    if retained is not None:
        retained_ok = tree_digest(retained) == retained_before
    repo_ok = True if repo_before is None else repo_snapshot(res.repo) == repo_before
    check("RA-5", retained_ok and repo_ok, "prior retained evidence and the repository must be byte-identical after a refused run")
    wt = subprocess.run(["git", "-C", str(res.repo), "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    status = subprocess.run(["git", "-C", str(res.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    check("RA-6", not (res.out / "repo").exists() and str(res.out / "repo") not in wt and status.strip() == "",
          "the temporary worktree must be removed and the repository clean")
    log = os.environ.get(RA_LOG_ENV)
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST", ""), "expected": sorted(expected_ids), "ra": ra}) + "\n")
    return ra
