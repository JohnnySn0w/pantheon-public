"""test_shell_suite_coverage.py — the shell suites must stay EXECUTED, exactly ONCE (T-2330, T-4280).

The defect behind T-2330 was never "a suite is red", it was "78 cases are executed by
nothing and nobody can tell": test_handoff_and_restart.sh sat at 4/33 through a commit
and several full-suite runs (T-2319), and test_verify_startup.sh went 39 days unrun while
passing on exactly one account (T-2322). Presence is not activation.

T-2330 fixed it with tools/run_shell_suites.sh, a separate CI step. T-2278 (2026-08-02)
then wrapped the same suites in tools/test_shell_suites.py, collected by the ordinary
`pytest tools/` step, which made the second path redundant: CI ran every suite twice,
~218s median of serial-runner time per run on the one pve2 runner. T-4280 removed the
runner and its step.

THE PRIMARY NET IS AN OUTCOME CHECK, not any of the guards below. The runner's real
guarantee was an outcome (every suite ran and passed, whatever pytest had been told).
Guards that read the tree for the SPELLINGS that switch the wrapper off close one spelling
per review round and can never be complete (three rounds: marks, then a body skip, then a
failure converted to a skip and a skip keyed on the suite name). So CI now has pytest write
a junit report and the next step, tools/check_shell_suite_outcomes.py, fails unless every
tools/test_*.sh PASSED in it. It does not care how a suite was neutered. (0) below pins that
wiring; the checker's own tests are tools/test_check_shell_suite_outcomes.py. Everything
else here is EARLY WARNING: fast local feedback, and the reason a neutering edit fails on a
developer's machine instead of only in CI. This file guards that all of it stays true:

  0. the outcome check is WIRED: the whole-tree pytest step writes `--junitxml=<path>` with
     `-o junit_family=xunit1` (record_property is incompatible with the default xunit2) and
     a LATER step in the same job runs check_shell_suite_outcomes.py on that same path;
  1. the surviving path is WIRED: CI has exactly ONE whole-tree pytest step (`tools` or
     `tools/`), with nothing in it that can drop tools/test_shell_suites.py;
  2. the duplicate does not come back: no step in any workflow re-runs the suites
     (the old runner, a bare `test_*.sh` loop, a second pytest on the wrapper);
  3. the wrapper still refuses a vacuous pass (`test_every_shell_suite_is_run` keeps an
     assert on SHELL_SUITES);
  4. ci.yml's pull_request trigger still fires on tools/** (judged on the parsed YAML):
     neither a narrowed `paths` nor a `paths-ignore` that swallows tools/ or a shell suite;
     a push trigger, if present, is held to the same rule; and the nightly `schedule` exists,
     because since T-5001 (the operator TG 8719-8720: spare pve2) main is covered by the nightly,
     not by a re-run of the suite on every merge push;
  5. nothing IN THE TREE switches the wrapper off while CI stays green: no skip / skipif /
     xfail mark on its test function or module, no `pytest.skip()` / `pytest.xfail()` / raise
     in the function BODY (it is run once on a fake passing suite and must return normally),
     no collect_ignore* or pytest_ignore_collect in the repo-root or tools conftest (see
     wrapper_neutering). The retired runner ran the suites regardless of what pytest had
     been told, so it was an independent net against all of this; with one path left the
     guard has to be explicit. It lives HERE, not in the wrapper, because a module-level
     skip in the wrapper would skip its own guard.

Discovery itself (a new tools/test_*.sh can never be skipped) is pinned behaviourally in
tools/test_shell_suites.py, not by a regex over source here.

Scope of (1) is deliberately narrow so it does not go brittle: only WHOLE-DIRECTORY
targets count as "the" step, so a workflow that runs pytest on a single
tools/test_x.py stays legal; the nightly 3.11/3.12 matrix is ONE step in the YAML, not two.

Every pytest argv here is read by ONE parser, _options (T-4333's, moved here by T-4367 so that
tools/test_ci_integrity.py and tools/test_check_suite_outcome_floor.py import the same one): a new
value-taking pytest flag is taught in one place, and the guards cannot disagree about it. Where that
parser reads a token differently from the raw-token scans it replaced (an option-looking token after
`--`, an option value that looks like an option, `tools` given only as an option value, a repeated
report flag or `junit_family`), the step is refused by name (fail closed), so no earlier refusal was lost.

What the outcome check does NOT cover, and where it is guarded instead: the STEP-LEVEL
CI-integrity class (T-4333). Those routes change what a workflow STEP does, not what pytest
reports: `continue-on-error` or a non-trivial `if:` on the pytest step, its job or a job it
`needs:`, `|| true` / `| tee` / anything after pytest, PYTEST_ADDOPTS / `-o addopts=...` /
`--override-ini` / a pytest config file changing what is collected, a collection hook in a
conftest, a duplicate spelled `pytest .` / bare `pytest`. They neuter every Python test in CI,
not only the shell suites, so they live in tools/test_ci_integrity.py (its docstring lists the
rules and its own residual). The CHECKER STEP ITSELF is pinned here (outcome_check_violations:
exactly the invocation, no pipe / `||` / `;` / `&&` / redirect / extra flag / trailing command,
no `if:`, no continue-on-error), because everything now rests on that one line; and the JOB
that holds the pytest and checker steps has no `if:` and no truthy continue-on-error either.

What is left after this is deliberate FORGERY, which code review must catch: a hand-written
`record_property`, or a rewritten run_suite that reports a result the suite did not produce.
The wrapper records the suite's real result (suite_returncode, suite_killed) before it judges,
and the checker judges from that record, so an early `return` keyed on the suite name (no
record) and a weakened pass criterion (a non-zero code recorded) both turn the checker red.
Other limits, none silent: a NON-strict XPASS reads as a pass in a junit report (the suite
did run and exit 0); a suite file with a non-ASCII name reads as missing plus "not on disk"
(pytest escapes the id; fails closed, no such file exists); and the wiring guard compares
the report path as a literal token, so `"$RUNNER_TEMP/x.xml"` for `${{ runner.temp }}/x.xml`
is refused (a false red on a legitimate refactor, never a false green).

The early-warning source guards keep this boundary: "plain statements in the two conftests
the guard reads". They see `collect_ignore*` assignments and append/extend/insert, and a
`def pytest_ignore_collect`; they do NOT see an alias (`_ci = collect_ignore;
_ci.append(..)`), `pytest_ignore_collect = <lambda|alias>`, slice assignment,
`globals()[...]`, `pytest_plugins`, or a skipping autouse fixture, and the body call runs ONE
passing input, so a failure-path conversion or a skip keyed on the suite name is beyond it.
None of that needs closing here: the outcome check reads the result. (tools/test_ci_integrity.py
refuses a collection hook such as pytest_collection_modifyitems in ANY conftest, and follows a
conftest's `pytest_plugins` into the modules it names, refusing the same hooks there; only a
non-literal or unresolvable `pytest_plugins` entry fails closed.) A lint step that merely names tools/test_*.sh (e.g. shellcheck)
reads as a re-run; the message says so and the guard is meant to be extended consciously.
"""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import inspect
import posixpath
import pathlib
import re
import shlex
import sys
import tempfile
import unittest
from typing import Any

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
WORKFLOWS = REPO / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
WRAPPER = TOOLS / "test_shell_suites.py"

_WRAPPER_REL = "tools/test_shell_suites.py"
# A step that re-runs the shell suites by any route: the retired runner, the wrapper
# named directly, or any test_*.sh path/glob. Applied to every `run:` in every workflow.
_RERUN = re.compile(r"run_shell_suites|test_shell_suites|test_[\w*?\[\]-]*\.sh")
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_IGNORE_LIKE = ("--ignore", "--ignore-glob", "--deselect")


# --- pytest argv: the ONE parser (T-4333, shared since T-4367) ------------------------------
# Every argv reading in this file, in tools/test_ci_integrity.py and in
# tools/test_check_suite_outcome_floor.py goes through _options, so a new value-taking pytest flag
# is taught in ONE place. It lives here, not in test_ci_integrity, because that module imports
# this one at load time (the reverse import is lazy).

# Short options that take a value (the rest of the token, or the next argument).
_SHORT_WITH_VALUE = frozenset("kmnpcorW")
_LONG_WITH_VALUE = frozenset({
    "--ignore", "--ignore-glob", "--deselect", "--override-ini", "--config-file", "--inifile",
    "--rootdir", "--confcutdir", "--basetemp", "--junitxml", "--junit-xml", "--junit-prefix",
    "--numprocesses", "--maxprocesses", "--dist", "--tx", "--maxfail", "--tb", "--durations",
    "--durations-min", "--timeout", "--capture", "--import-mode", "--color", "--code-highlight",
    "--log-level", "--log-file", "--log-file-level", "--log-format", "--log-date-format",
    "--log-cli-level", "--log-cli-format", "--cov", "--cov-report", "--pythonwarnings",
    "--last-failed-no-failures", "--lfnf", "--doctest-glob", "--show-capture", "--rsyncdir",
})


def _options(args: list[str]) -> tuple[list[str], list[tuple[str, str | None]]]:
    """(positionals, [(option, value)]) for a pytest argv. Short clusters are walked left to
    right (`-qk expr` is -q plus -k expr); a value-taking short option ends the cluster."""
    positionals: list[str] = []
    opts: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals += args[i + 1:]
            break
        if arg.startswith("--"):
            flag, eq, inline = arg.partition("=")
            if eq:
                opts.append((flag, inline))
            elif flag in _LONG_WITH_VALUE and i + 1 < len(args):
                opts.append((flag, args[i + 1]))
                i += 1
            else:
                opts.append((flag, None))
        elif arg.startswith("-") and len(arg) > 1:
            for j, ch in enumerate(arg[1:], start=1):
                if ch in _SHORT_WITH_VALUE:
                    rest = arg[j + 1:]
                    if rest:
                        value: str | None = rest
                    elif i + 1 < len(args):
                        value = args[i + 1]
                        i += 1
                    else:
                        value = None
                    opts.append(("-" + ch, value))
                    break
                opts.append(("-" + ch, None))
        else:
            positionals.append(arg)
        i += 1
    return positionals, opts


def _steps(doc: dict) -> list[dict]:
    return [s for job in (doc.get("jobs") or {}).values() for s in (job.get("steps") or [])]


def _invocations(run: str, is_command: Any) -> list[list[str]]:
    """The argv AFTER the first command token satisfying `is_command`, for every such
    invocation in a run script.

    Handles `\\`-continued lines, `&&` / `||` / `;` / `|` chains, `python -m pytest`, a
    path-qualified command and leading `VAR=x` assignments; skips `#` comment lines."""
    found: list[list[str]] = []
    for line in run.replace("\\\n", " ").splitlines():
        for segment in _SEGMENT_SPLIT.split(line.strip()):
            try:
                tokens = shlex.split(segment, comments=True)
            except ValueError:
                tokens = segment.split()
            for i, tok in enumerate(tokens):
                if is_command(tok):
                    found.append(tokens[i + 1:])
                    break
    return found


def _pytest_invocations(run: str) -> list[list[str]]:
    """The argv AFTER the `pytest` token, for every pytest invocation in a run script."""
    return _invocations(run, lambda tok: tok == "pytest" or tok.endswith("/pytest"))


_CHECKER = "check_shell_suite_outcomes.py"


def _checker_invocations(run: str) -> list[list[str]]:
    return _invocations(run, lambda tok: posixpath.basename(tok) == _CHECKER)


_JUNIT_FLAGS = ("--junitxml", "--junit-xml")


def _junit_paths(args: list[str]) -> list[str | None]:
    """Every report path a pytest argv passes (`--junitxml=P`, `--junitxml P`, `--junit-xml ...`),
    in order; None for a flag left without a value."""
    return [val for opt, val in _options(args)[1] if opt in _JUNIT_FLAGS]


def _junit_path(args: list[str]) -> str | None:
    """The FIRST report path a pytest argv passes. A repeated flag is refused by name in
    outcome_check_violations, so which one pytest honours is never a question here."""
    paths = _junit_paths(args)
    return paths[0] if paths else None


def _override_ini_pairs(args: list[str]) -> list[tuple[str, str]]:
    """pytest ini overrides on the command line (`-o k=v`, `-ok=v`, `-qo k=v`, `--override-ini k=v`,
    `--override-ini=k=v`), in order, as (key, value)."""
    found: list[tuple[str, str]] = []
    for opt, value in _options(args)[1]:
        # `-o=k=v` reads as key `=k` here (as before T-4367): an accepted fail-closed false red.
        if opt in ("-o", "--override-ini") and value and "=" in value:
            key, val = value.split("=", 1)
            found.append((key.strip(), val.strip()))
    return found


def _override_ini(args: list[str]) -> dict[str, str]:
    """The ini overrides as a dict (the last of a repeated key wins, as in pytest)."""
    return dict(_override_ini_pairs(args))


def _argv_ambiguities(args: list[str]) -> list[str]:
    """Tokens _options reads one way and a looser reading another (T-4367 belt, fail closed): an
    option-looking token after `--` (pytest takes it as a PATH), and an option VALUE that itself
    looks like an option (`--ignore --co`, `-p -k`: pytest refuses the argv). Before T-4367 the
    wiring checks scanned such tokens as options; refusing them keeps every old refusal."""
    positionals, opts = _options(args)
    why = [f"passes `{p}` after `--`, where pytest reads it as a path, not an option (fail closed)"
           for p in positionals if p.startswith("-") and len(p) > 1]
    why += [f"gives `{opt}` the value `{val}`, which looks like an option (pytest refuses it) (fail closed)"
            for opt, val in opts if val and val.startswith("-") and len(val) > 1]
    return why


def _job_problems(job: dict) -> list[str]:
    """The job that holds the pytest and checker steps must not be skippable or forgivable with
    one key (N-20): the same edit as on the checker step, one level up, neutralises both."""
    why: list[str] = []
    if job.get("continue-on-error") not in (None, False, "false"):
        why.append("has job-level continue-on-error, so a red check would not fail the run")
    if "if" in job:
        why.append("has a job-level `if:` condition, so the whole job (pytest and the check) can be skipped")
    return why


def outcome_check_violations(docs: dict[str, dict]) -> list[str]:
    """The PRIMARY net's wiring: the whole-tree pytest step must write a junit report (with
    `-o junit_family=xunit1`), and a LATER step in the SAME job must run
    tools/check_shell_suite_outcomes.py on that same path (it fails unless every tools/test_*.sh
    PASSED in the report). [] = wired. Which step is "the" whole-tree step, and that there is
    exactly one, is violations()'s job, not this one's."""
    out: list[str] = []
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if any(_is_whole_tree(args) for step in steps for args in _pytest_invocations(step.get("run") or "")):
                out += [f"{name}: job {job_name!r} {why}" for why in _job_problems(job)]
            for i, step in enumerate(steps):
                for args in _pytest_invocations(step.get("run") or ""):
                    if not _is_whole_tree(args):
                        continue
                    label = f"{name}: job {job_name!r} step {step.get('name')!r}"
                    out += [f"{label} {w}" for w in raw_expression_problems(step.get("run"))]  # T-4385 r3
                    out += [f"{label} {w}" for w in _argv_ambiguities(args)]  # T-4367 belt
                    if len(_junit_paths(args)) > 1:
                        out.append(f"{label} passes the junit report flag more than once "
                                   f"({_junit_paths(args)!r}): which one pytest writes must never be a "
                                   f"question (T-4367) (fail closed)")
                    if [k for k, _ in _override_ini_pairs(args)].count("junit_family") > 1:
                        out.append(f"{label} overrides `junit_family` more than once: which one pytest uses "
                                   f"must never be a question (T-4367) (fail closed)")
                    path = _junit_path(args)
                    if not path:
                        out.append(f"{label} must write a report (--junitxml=<path>) so the outcome check "
                                   f"can read what the shell suites did")
                        continue
                    if _override_ini(args).get("junit_family") != "xunit1":
                        out.append(f"{label} must pass `-o junit_family=xunit1`: record_property is incompatible "
                                   f"with the default xunit2 (pytest warns), and an unpinned pytest may stop "
                                   f"writing the per-testcase properties the checker reads")
                    checkers = [later for later in steps[i + 1:]
                                if any(path in inv for inv in _checker_invocations(later.get("run") or ""))]
                    if not checkers:
                        out.append(f"{label} writes {path!r} but no LATER step in the same job runs "
                                   f"{_CHECKER} on that path: nothing checks that the shell suites passed")
                    for later in checkers:
                        out += [f"{name}: job {job_name!r} step {later.get('name')!r} {why}"
                                for why in _checker_step_problems(later, path, doc, job)]
    return out


def _checker_step_problems(step: dict, path: str, doc: dict, job: dict) -> list[str]:
    """The checker step is the PRIMARY net, so its exit status must reach the job untouched
    (N-17): the `run:` is exactly `<python> tools/check_shell_suite_outcomes.py <report>` (no
    pipe, `||`, `;`, `&&`, redirect, env prefix, extra flag or trailing command), and the step
    has no `if:` and no truthy `continue-on-error`. The general step-level class is
    tools/test_ci_integrity.py's (T-4333); this pins the one step everything now rests on. The
    keys that switch the step off WITHOUT touching `run:` (`shell:`, `working-directory`, step
    `env`) are test_ci_integrity.backstop_step_problems, shared with the T-4372 floor step
    (imported here, lazily: test_ci_integrity imports this module at load time)."""
    from tools.test_ci_integrity import backstop_step_problems
    why: list[str] = backstop_step_problems(doc, job, step)
    if step.get("continue-on-error") not in (None, False, "false"):
        why.append("has continue-on-error, so a red check would not fail the job")
    if "if" in step:
        why.append("has an `if:` condition, so the check can be skipped")
    code = _code(step.get("run") or "").strip()
    try:
        tokens = shlex.split(code, comments=True)
    except ValueError:
        tokens = []
    exact = (len(tokens) == 3 and len(code.splitlines()) == 1
             and posixpath.basename(tokens[0]).startswith("python")
             and posixpath.basename(tokens[1]) == _CHECKER and tokens[2] == path)
    if not exact:
        why.append(f"must run exactly `python tools/{_CHECKER} {path}` and nothing else (a pipe, `||`, `;`, "
                   f"`&&`, redirect, env prefix, extra flag or trailing command can mask its exit status)")
    return why


def _is_whole_tree(args: list[str]) -> bool:
    """True iff the whole tools directory (`tools`, `tools/`, `./tools`) is a positional target, or
    (fail closed, T-4367) the value of any option other than an --ignore/--ignore-glob/--deselect.
    A single file or a subdirectory is NOT: it stays legal, and does not count toward the one step.
    A step that names the tree ONLY as an option value is refused by name in violations().
    Also fail closed (T-4367 r1 M1): an argv the parser reads ambiguously counts as whole-tree when
    ANY raw token is the tree, so `-k --ignore --ignore tools` (the second --ignore swallowing
    `tools`) still counts, and the ambiguity belt then refuses it."""
    positionals, opts = _options(args)
    values = [val for opt, val in opts if val is not None and opt not in _IGNORE_LIKE]
    if any(posixpath.normpath(p) == "tools" for p in positionals + values):
        return True
    return bool(_argv_ambiguities(args)) and any(posixpath.normpath(t) == "tools" for t in args)


def _targets_tree(args: list[str]) -> bool:
    """True iff the whole tools directory is a POSITIONAL target (what pytest actually collects)."""
    return any(posixpath.normpath(p) == "tools" for p in _options(args)[0])


def _targets_wrapper(value: str) -> bool:
    path = value.split("::")[0]
    return ("test_shell_suites" in value
            or posixpath.normpath(path) == "tools"
            or fnmatch.fnmatch(_WRAPPER_REL, path))


def _wrapper_exclusions(args: list[str]) -> list[str]:
    """Reasons this pytest argv can silently drop tools/test_shell_suites.py (or any
    shell suite). `--ignore`/`--deselect` of an UNRELATED test is deliberately fine;
    `-k`/`-m` are always flagged because an expression can drop anything unseen."""
    why: list[str] = []
    _, opts = _options(args)
    for flag, value in opts:
        if flag in _IGNORE_LIKE and value is not None and _targets_wrapper(value):
            why.append(f"{flag} {value}")
    for arg in args:  # a raw-token belt: `--co` anywhere, even where _options reads it as a value
        if arg in ("--collect-only", "--co"):
            why.append(arg)
    for flag, value in opts:
        if flag in ("-k", "-m"):
            why.append(f"{flag} {value} (a selection expression can drop the shell suites unseen)")
    return why + _argv_ambiguities(args)


def _code(run: str) -> str:
    """A run script without its `#` comment lines (a comment naming the wrapper is not a re-run)."""
    return "\n".join(line for line in run.replace("\\\n", " ").splitlines()
                     if not line.lstrip().startswith("#"))


# The one `${{ }}` expression ci.yml's `run:` scripts use, exactly as written there (T-4385 r3).
_RUN_EXPRESSION = "${{ runner.temp }}"


def raw_expression_problems(run: Any) -> list[str]:
    """A `${{` left in the RAW `run:` text once every exact `${{ runner.temp }}` is removed (T-4385 r3
    MF-A). GitHub substitutes `${{ }}` everywhere in `run:`, `#` comment lines included, BEFORE bash
    runs, while _code() drops those lines before any check here: `# ${{ fromJSON('"\\nexit 0\\n"') }}`
    injects new bash lines (reviewer runtime proof: pytest never ran and the step exited 0). Read on
    the raw text, so no comment, quote or continuation handling can hide one; any other spelling,
    `${{runner.temp}}` included, is an accepted false red. [] = none."""
    if not isinstance(run, str) or "${{" not in run.replace(_RUN_EXPRESSION, ""):
        return []
    return [f"holds a `${{{{` expression other than the literal `{_RUN_EXPRESSION}` in its raw `run:` text: "
            f"GitHub substitutes it everywhere, `#` comment lines included, before bash runs, so "
            f"`# ${{{{ fromJSON('\"\\nexit 0\\n\"') }}}}` injects lines this guard never reads (T-4385 r3) "
            f"(fail closed)"]


def whole_tree_pytest_steps(doc: dict) -> list[dict]:
    """Steps that run pytest over the whole tools/ directory. One YAML step is ONE step
    however many matrix legs run it (the nightly 3.11 + 3.12 pair is not a duplicate)."""
    return [s for s in _steps(doc)
            if any(_is_whole_tree(args) for args in _pytest_invocations(s.get("run") or ""))]


def violations(docs: dict[str, dict]) -> list[str]:
    """Everything wrong with how the given workflows execute the shell suites. [] = fine."""
    out: list[str] = []
    whole = [(name, s) for name, doc in docs.items() for s in whole_tree_pytest_steps(doc)]
    if len(whole) != 1:
        out.append(f"expected exactly 1 whole-tree pytest step (it collects {_WRAPPER_REL}, the only "
                   f"path that runs the shell suites), found {len(whole)}: "
                   f"{[(n, s.get('name')) for n, s in whole]}")
    for name, step in whole:
        for args in _pytest_invocations(step.get("run") or ""):
            if _is_whole_tree(args) and not _targets_tree(args):
                out.append(f"{name}: step {step.get('name')!r} names the tools tree only as an option value, "
                           f"not as a target pytest collects (T-4367) (fail closed)")
            if _is_whole_tree(args):
                out += [f"{name}: step {step.get('name')!r} can drop the shell suites: {w}"
                        for w in _wrapper_exclusions(args)]
    for name, doc in docs.items():
        for step in _steps(doc):
            if _RERUN.search(_code(step.get("run") or "")):
                out.append(f"{name}: step {step.get('name')!r} re-runs the shell suites; they already run "
                           f"once via {_WRAPPER_REL} (T-4280: running them twice cost ~218s per CI run)")
    return out


def _on_block(doc: dict) -> dict:
    """The trigger block. PyYAML (YAML 1.1) parses the bare key `on` as boolean True."""
    for key in (True, "on"):
        if isinstance(doc.get(key), dict):
            return doc[key]
    return {}


def _gh_glob_matches(pattern: str, path: str) -> bool:
    """GitHub's path-filter globs: `**` crosses `/`, `*` and `?` do not. `**/` also matches
    zero directories (`**/*.sh` covers a top-level x.sh) — the conservative reading, since
    over-matching only makes the guard flag more."""
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
            continue
        ch = pattern[i]
        rx += "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
        i += 1
    return re.fullmatch(rx, path) is not None


# A paths-ignore that matches either of these stops CI running on a change to the wrapper
# or to a shell suite; one that matches neither (docs/**, *.md, tickets/**) is harmless.
_TOOLS_CHANGE_PROBES = ("tools/test_shell_suites.py", "tools/test_example.sh")


def _events_missing_tools_trigger(doc: dict) -> list[str]:
    """Triggers that no longer run the suites. Judged on the PARSED trigger block per event,
    so no event can mask another's removal and a comment cannot satisfy it.

    pull_request must fire on a tools/ change; push is optional since T-5001 (the post-merge
    re-run was dropped to spare pve2) but, if present, must fire on tools/ too; schedule must
    exist with at least one cron, because the nightly is now what covers main. An event with
    no filter at all fires on everything; an event with `paths-ignore` fires on everything
    EXCEPT what it ignores (N-8: `paths-ignore: [tools/**]` is not "unfiltered")."""
    on = _on_block(doc)
    missing = []
    for event in ("push", "pull_request"):
        if event not in on:
            if event == "pull_request":
                missing.append(event)
            continue
        cfg = on[event] or {}
        if "paths" in cfg and "tools/**" not in (cfg["paths"] or []):
            missing.append(event)
        elif "paths" not in cfg and any(
                _gh_glob_matches(pat, probe)
                for pat in (cfg.get("paths-ignore") or []) for probe in _TOOLS_CHANGE_PROBES):
            missing.append(event)
    sched = on.get("schedule")
    if not (isinstance(sched, list) and any(isinstance(e, dict) and e.get("cron") for e in sched)):
        missing.append("schedule")
    return missing


_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})
_IGNORE_NAMES = frozenset({"collect_ignore", "collect_ignore_glob"})


def _marks(obj: Any) -> list:
    found = getattr(obj, "pytestmark", [])
    return list(found) if isinstance(found, (list, tuple)) else [found]


def _ignore_entry_hits_wrapper(entry: str, conftest_dir_rel: str) -> bool:
    """collect_ignore entries are relative to the conftest's directory."""
    full = posixpath.normpath(posixpath.join(conftest_dir_rel, entry))
    return full == "tools" or fnmatch.fnmatch(_WRAPPER_REL, full)


def _ignore_strings(node: ast.AST) -> tuple[list[str], bool]:
    """(string constants under `node`, whether `node` is a plain literal string / list of them)."""
    strings = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return strings, True
    literal = (isinstance(node, (ast.List, ast.Tuple, ast.Set))
               and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts))
    return strings, literal


def _conftest_neutering(conf: pathlib.Path, repo: pathlib.Path) -> list[str]:
    label = conf.relative_to(repo).as_posix()  # tools/conftest.py vs conftest.py
    try:
        tree = ast.parse(conf.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{label}: cannot parse ({exc}); cannot prove it spares the wrapper"]
    rel = conf.parent.relative_to(repo).as_posix()
    out: list[str] = []

    def check(value: ast.AST, how: str) -> None:
        strings, literal = _ignore_strings(value)
        if not literal:
            out.append(f"{label}: {how} is not a literal list of strings, so it cannot be proven to "
                       f"spare {WRAPPER.name} (fail closed)")
        out.extend(f"{label}: {how} drops {WRAPPER.name} via {s!r}"
                   for s in strings if _ignore_entry_hits_wrapper(s, rel))

    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets, value = [node.target], node.value
        if value is not None and any(isinstance(t, ast.Name) and t.id in _IGNORE_NAMES for t in targets):
            check(value, "an assignment to collect_ignore*")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in _IGNORE_NAMES
                and node.func.attr in ("append", "extend", "insert")):
            payload = node.args[1:] if node.func.attr == "insert" else node.args
            for arg in payload:
                check(arg, f"{node.func.value.id}.{node.func.attr}()")
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_ignore_collect":
            out.append(f"{label}: defines pytest_ignore_collect, which can drop {WRAPPER.name} "
                       f"unseen (extend this guard consciously if you need one)")
    return out


def wrapper_neutering(wrapper: pathlib.Path, conftests: list[pathlib.Path],
                      repo: pathlib.Path = REPO) -> list[str]:
    """Early warning for the in-tree ways found to switch tools/test_shell_suites.py off.

    NOT the primary net (that is the outcome check, tools/check_shell_suite_outcomes.py, which
    reads what pytest reported and so does not depend on the spelling). This is the fast local
    feedback that fails the edit on a developer's machine. It catches the plain forms; it
    exercises one passing input, so it is not a complete contract. Checked
    from OUTSIDE the wrapper (a module-level skip in it would skip its own guard): import it
    by path and read the marks on its test function and module (skip / skipif / xfail, any
    condition: a condition cannot be judged here), and scan the given conftests for
    collect_ignore* (literal entries resolved against the conftest's directory; a
    non-literal value fails closed) and a pytest_ignore_collect hook. [] = intact."""
    spec = importlib.util.spec_from_file_location("_shell_suites_under_guard", wrapper)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # the wrapper's @dataclass resolves its module via sys.modules
    try:
        spec.loader.exec_module(mod)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # incl. pytest's Skipped: a module-level pytest.skip() must be red HERE
        return [f"cannot load {wrapper.name} to inspect it: {exc!r}"]
    finally:
        sys.modules.pop(spec.name, None)
    out: list[str] = []
    fn = getattr(mod, "test_shell_suite_passes", None)
    if fn is None:
        out.append(f"{wrapper.name} no longer defines test_shell_suite_passes")
    else:
        found = sorted({m.name for m in _marks(fn) + _marks(mod)} & _SKIP_MARKS)
        if found:
            out.append(f"{wrapper.name} carries {found} on the module or test_shell_suite_passes: "
                       f"the shell suites would not run")
        # The imperative twin (R-2): `pytest.skip()` / `pytest.xfail()` / a conditional or a
        # raise in the BODY leaves no mark to find. Run the function once on a FAKE passing
        # suite; it must return normally. A skip/xfail surfaces here as an outcome exception
        # (a BaseException, which is also why the wrapper's own fast tests report `skipped`
        # rather than failing). run_suite is swapped out, so no real suite ever runs.
        real_run_suite = getattr(mod, "run_suite", None)
        recorded: list[tuple[str, Any]] = []
        takes_recorder = "record_property" in inspect.signature(fn).parameters
        kwargs = {"record_property": lambda name, value: recorded.append((name, value))} if takes_recorder else {}
        try:
            mod.run_suite = lambda script, **kw: mod.SuiteResult(
                returncode=0, output="", killed_reason=None, elapsed=0.0)
            fn(pathlib.Path("x.sh"), **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            out.append(f"{wrapper.name}: test_shell_suite_passes did not return normally for a passing "
                       f"suite ({type(exc).__name__}: {exc}); a skip, xfail or raise in its body means "
                       f"the shell suites do not run")
        else:
            # The outcome check judges each suite from the properties this records; without
            # them it can only say "never ran". Early warning that the two lines are still there.
            if not takes_recorder or dict(recorded) != {"suite_returncode": 0, "suite_killed": False}:
                out.append(f"{wrapper.name}: test_shell_suite_passes did not record the suite's real result "
                           f"(record_property suite_returncode / suite_killed; recorded {recorded}); the "
                           f"outcome check needs both")
        finally:
            if real_run_suite is not None:
                mod.run_suite = real_run_suite
    for conf in conftests:
        if conf.is_file():
            out += _conftest_neutering(conf, repo)
    return out


def _load_workflows() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) or {} for p in sorted(WORKFLOWS.glob("*.y*ml"))}


def _wf(*runs: str, matrix: Any = None) -> dict:
    job: dict[str, Any] = {"steps": [{"name": f"s{i}", "run": r} for i, r in enumerate(runs)]}
    if matrix is not None:
        job["strategy"] = {"matrix": {"python-version": matrix}}
    return {"jobs": {"test": job}}


class TheSurvivingPathIsWiredOnce(unittest.TestCase):
    """Live tree: what CI actually does today."""

    def test_there_are_shell_suites_to_run(self):
        found = sorted(p.name for p in TOOLS.glob("test_*.sh"))
        self.assertTrue(found, "no tools/test_*.sh found at all")

    def test_the_wrapper_file_exists(self):
        # Collectability by pytest (default `test_*.py`) is NOT asserted here: a constant
        # fnmatch cannot fail. It is proven for real by the one-whole-tree-step tests and
        # by wrapper_neutering below (which imports the file and reads its marks).
        self.assertTrue(WRAPPER.is_file(), f"{WRAPPER} is missing — the shell suites are unexecuted again")

    def test_the_wrapper_still_refuses_a_vacuous_pass(self):
        """`test_every_shell_suite_is_run` is what turns an empty glob into a red run
        instead of a zero-length parametrize that reports all-green. Name-only is not
        enough (a body of `pass` keeps the name): it must still contain an `assert` that
        reads SHELL_SUITES."""
        tree = ast.parse(WRAPPER.read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "test_every_shell_suite_is_run"]
        self.assertEqual(len(fns), 1, "test_every_shell_suite_is_run is missing or defined twice")
        asserts = [a for a in ast.walk(fns[0]) if isinstance(a, ast.Assert)]
        self.assertTrue(asserts, "test_every_shell_suite_is_run no longer asserts anything")
        self.assertTrue(
            any(isinstance(n, ast.Name) and n.id == "SHELL_SUITES" for a in asserts for n in ast.walk(a.test)),
            "test_every_shell_suite_is_run no longer asserts on SHELL_SUITES (the discovered set)")

    def test_real_workflows_run_the_shell_suites_exactly_once_and_cannot_drop_them(self):
        self.assertEqual(violations(_load_workflows()), [])

    def test_the_one_whole_tree_pytest_step_is_in_ci_yml(self):
        docs = _load_workflows()
        homes = [name for name, doc in docs.items() if whole_tree_pytest_steps(doc)]
        self.assertEqual(homes, ["ci.yml"])

    def test_the_outcome_check_is_wired_after_the_pytest_step(self):
        """The PRIMARY net (T-4280 re-brief): pytest writes a junit report and a LATER step in
        the same job fails unless every shell suite PASSED in it. The source guards below are
        early warning; this reads what actually ran."""
        self.assertEqual(outcome_check_violations(_load_workflows()), [])

    def test_the_outcome_checker_exists(self):
        self.assertTrue((TOOLS / _CHECKER).is_file(), f"tools/{_CHECKER} is missing: CI's outcome step would fail")

    def test_nothing_in_tree_can_skip_xfail_or_ignore_the_wrapper(self):
        """Early warning (the outcome check is the primary net): the plain in-tree ways to
        skip, xfail or not collect the wrapper. It lives in THIS file on purpose: a
        module-level skip in the wrapper would skip its own guard."""
        self.assertEqual(
            wrapper_neutering(WRAPPER, [REPO / "conftest.py", TOOLS / "conftest.py"]), [])

    def test_ci_path_filter_still_covers_the_suites(self):
        """ci.yml only runs on changes under tools/** (T-1627). Parsed, per trigger: a
        narrowed pull_request filter, or a lost nightly schedule (T-5001: main's coverage),
        would silently stop running the suites without touching a line of test code."""
        doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
        self.assertEqual(_events_missing_tools_trigger(doc), [],
                         "ci.yml no longer triggers on tools/** for the listed event(s)")


class TheGuardLogic(unittest.TestCase):
    """Fixtures: the guard itself, so it cannot go vacuous or brittle."""

    OK = "python -m pytest tools/ -q -n auto"

    def test_the_real_shape_passes(self):
        self.assertEqual(violations({"ci.yml": _wf("pip install pytest", self.OK)}), [])

    def test_the_nightly_two_leg_matrix_is_one_step_not_two(self):
        doc = _wf(self.OK, matrix=["3.11", "3.12"])
        self.assertEqual(len(whole_tree_pytest_steps(doc)), 1)
        self.assertEqual(violations({"ci.yml": doc}), [])

    def test_a_single_file_pytest_step_stays_legal(self):
        doc = _wf(self.OK, "python -m pytest tools/test_other.py -q")
        self.assertEqual(len(whole_tree_pytest_steps(doc)), 1)
        self.assertEqual(violations({"ci.yml": doc}), [])

    def test_whole_tree_spellings_all_count(self):
        for target in ("tools", "tools/", "./tools", "./tools/"):
            with self.subTest(target=target):
                self.assertEqual(len(whole_tree_pytest_steps(_wf(f"pytest {target} -q"))), 1)

    def test_a_single_file_or_subdir_is_not_a_whole_tree_step(self):
        for target in ("tools/test_x.py", "tools/hooks", "tools/test_x.py::TestY"):
            with self.subTest(target=target):
                self.assertEqual(whole_tree_pytest_steps(_wf(f"pytest {target} -q")), [])

    def test_a_second_whole_tree_step_is_the_duplicate_and_fails(self):
        for docs in (
            {"ci.yml": _wf(self.OK, "pytest tools -q")},
            {"ci.yml": _wf(self.OK), "other.yml": _wf(self.OK)},
        ):
            with self.subTest(docs=list(docs)):
                self.assertTrue(any("exactly 1" in v for v in violations(docs)))

    def test_no_whole_tree_step_at_all_fails(self):
        self.assertTrue(any("exactly 1" in v for v in violations({"ci.yml": _wf("pytest tools/test_x.py")})))

    def test_anything_that_can_drop_the_wrapper_from_the_one_step_fails(self):
        for flags in (
            "--ignore=tools/test_shell_suites.py",
            "--ignore tools/test_shell_suites.py",
            "--ignore-glob=tools/test_shell*",
            "--ignore-glob=tools/test_*.py",
            "--ignore=tools",
            "--deselect tools/test_shell_suites.py::test_shell_suite_passes[test_x.sh]",
            "-k 'not shell'",
            "-m 'not slow'",
            "--collect-only",
            # combined short flags: -k/-m ride at the end of a cluster
            "-qk 'not shell'",
            "-qm 'not slow'",
            "-xvk 'not shell'",
            "-sm 'not slow'",
        ):
            with self.subTest(flags=flags):
                self.assertTrue(violations({"ci.yml": _wf(f"python -m pytest tools/ {flags} -q")}),
                                f"{flags!r} can drop the shell suites and was not flagged")

    def test_excluding_an_unrelated_file_is_not_brittle(self):
        for flags in (
            "--ignore=tools/test_unrelated.py", "--deselect tools/test_other.py::test_slow", "-x", "-n 4",
            # short flags that merely CONTAIN k or m in their VALUE are not selectors
            "-n4", "-vv", "-rsx", "-Wignore:message:DeprecationWarning", "-p no:cacheprovider",
            "-o junit_family=xunit1", "--override-ini=junit_family=xunit1", "-ojunit_family=xunit1",
        ):
            with self.subTest(flags=flags):
                self.assertEqual(violations({"ci.yml": _wf(f"python -m pytest tools/ {flags} -q")}), [])

    def test_a_comment_line_naming_the_wrapper_is_not_a_rerun(self):
        commented = self.OK + "\n# tools/test_shell_suites.py runs every tools/test_*.sh inside this step"
        self.assertEqual(violations({"ci.yml": _wf(commented)}), [])
        self.assertEqual(violations({"ci.yml": _wf(self.OK, "# not tools/test_shell_suites.py again\necho ok")}), [])

    def test_every_route_that_reruns_the_suites_fails(self):
        for rerun in (
            "bash tools/run_shell_suites.sh",
            "for s in tools/test_*.sh; do bash \"$s\"; done",
            "bash tools/test_handoff_and_restart.sh",
            "python -m pytest tools/test_shell_suites.py -q",
        ):
            with self.subTest(rerun=rerun):
                self.assertTrue(any("re-runs the shell suites" in v for v in
                                    violations({"ci.yml": _wf(self.OK, rerun)})))
                # ...and also when it hides in a workflow file of its own.
                self.assertTrue(any("re-runs the shell suites" in v for v in
                                    violations({"ci.yml": _wf(self.OK), "shell.yml": _wf(rerun)})))

    def test_a_line_continued_or_chained_invocation_is_still_seen(self):
        chained = "pip install -q pytest && python -m pytest tools/ \\\n  --ignore=tools/test_shell_suites.py"
        self.assertTrue(violations({"ci.yml": _wf(chained)}))

    def test_argv_the_shared_parser_reads_ambiguously_fails_closed(self):
        """T-4367 belts: before the parsers were unified these were refused by a raw-token scan;
        they stay refused (pytest itself rejects or misreads each one)."""
        for flags in ("--ignore --co", "-p -k x", "--junitxml --collect-only", "-- -k x", "-- --co"):
            with self.subTest(flags=flags):
                self.assertTrue(violations({"ci.yml": _wf(f"python -m pytest tools/ {flags}")}),
                                f"{flags!r} was not flagged")

    def test_the_tree_named_only_as_an_option_value_is_refused(self):
        """`--rootdir tools` is not a collection target (the shared parser reads it as a value), but
        a step naming the tree only that way is refused by name, never read as the one step."""
        for run in ("pytest --rootdir tools tools/test_x.py", "pytest --basetemp=tools -q"):
            with self.subTest(run=run):
                self.assertTrue(any("only as an option value" in v for v in violations({"ci.yml": _wf(run)})))
        # ...and a real target beside such a value stays the one legal step.
        self.assertEqual(violations({"ci.yml": _wf("pytest tools/ --rootdir tools")}), [])

    def test_a_tree_token_swallowed_by_a_repeated_exclusion_flag_still_counts(self):
        """T-4367 r1 M1: the parser reads the first --ignore as the value of -k/-p/--junitxml, and the
        second swallows `tools`. Such an extra step must still be refused, as before T-4367."""
        for extra in ("python -m pytest -k --ignore --ignore tools",
                      "python -m pytest --junitxml --ignore --ignore tools",
                      "python -m pytest -p --deselect --deselect tools"):
            with self.subTest(extra=extra):
                doc = _wf(self.OK, extra)
                self.assertEqual(len(whole_tree_pytest_steps(doc)), 2)
                self.assertTrue(any("exactly 1" in v for v in violations({"ci.yml": doc})))
                self.assertTrue(outcome_check_violations({"ci.yml": doc}))

    def test_the_guards_share_one_argv_parser(self):
        """T-4367: every guard reads argv through the ONE parser, _options, which lives in this module
        and which test_ci_integrity imports, so a value-taking flag is taught once. Pinned by
        identity, not by re-implementation."""
        from tools import test_ci_integrity
        self.assertIs(test_ci_integrity._options, _options)
        for name in ("_split_value_flags", "_short_cluster_selects", "_SHORT_VALUE_FLAGS"):
            self.assertFalse(hasattr(sys.modules[__name__], name), f"{name} is a second argv parser")


class TheOutcomeWiringLogic(unittest.TestCase):
    """Fixtures for outcome_check_violations: the wiring must survive a workflow refactor."""

    PYTEST = ('python -m pytest tools/ -q -n auto -o junit_family=xunit1 '
              '--junitxml="${{ runner.temp }}/r.xml"')
    CHECK = 'python tools/check_shell_suite_outcomes.py "${{ runner.temp }}/r.xml"'

    def found(self, *runs: str, matrix: Any = None) -> list[str]:
        return outcome_check_violations({"ci.yml": _wf(*runs, matrix=matrix)})

    def test_the_real_shape_passes(self):
        self.assertEqual(self.found("pip install pytest", self.PYTEST, self.CHECK), [])

    def test_the_two_leg_matrix_is_still_one_wired_step(self):
        self.assertEqual(self.found(self.PYTEST, self.CHECK, matrix=["3.11", "3.12"]), [])

    def test_spellings_of_the_same_wiring_are_accepted(self):
        x1 = "-o junit_family=xunit1"
        for pytest_run, check_run in (
            (f"pytest tools {x1} --junitxml=r.xml", "python3 ./tools/check_shell_suite_outcomes.py r.xml"),
            (f"pytest tools {x1} --junitxml r.xml", "python tools/check_shell_suite_outcomes.py r.xml"),
            (f"pytest tools {x1} --junit-xml=r.xml", "python tools/check_shell_suite_outcomes.py r.xml"),
            (f"python -m pytest tools -q {x1}\\\n  --junitxml=r.xml",
             "python tools/check_shell_suite_outcomes.py \\\n  r.xml"),
        ):
            with self.subTest(pytest_run=pytest_run):
                self.assertEqual(self.found(pytest_run, check_run), [])

    def test_the_xunit1_family_flag_in_every_spelling_is_accepted(self):
        for flag in ("-o junit_family=xunit1", "-ojunit_family=xunit1", "--override-ini junit_family=xunit1",
                     "--override-ini=junit_family=xunit1"):
            with self.subTest(flag=flag):
                self.assertEqual(self.found(f"pytest tools {flag} --junitxml=r.xml", self.CHECK.replace(
                    '"${{ runner.temp }}/r.xml"', "r.xml")), [])

    def test_a_pytest_step_without_the_xunit1_family_is_caught(self):
        """N-21: record_property is flagged incompatible under the default xunit2 and an unpinned
        pytest may stop writing per-testcase properties there, which would turn every run red."""
        check = self.CHECK.replace('"${{ runner.temp }}/r.xml"', "r.xml")
        for run in ("pytest tools --junitxml=r.xml",
                    "pytest tools -o junit_family=xunit2 --junitxml=r.xml",
                    "pytest tools -o junit_family=legacy --junitxml=r.xml",
                    "pytest tools -o junit_duration_report=call --junitxml=r.xml"):
            with self.subTest(run=run):
                found = self.found(run, check)
                self.assertTrue(any("junit_family=xunit1" in v for v in found), found)

    def test_the_xunit1_family_flag_in_a_short_cluster_is_accepted(self):
        """`-qo k=v` is -q plus -o k=v (the shared parser walks clusters, T-4367)."""
        self.assertEqual(self.found("pytest tools -qo junit_family=xunit1 --junitxml=r.xml", self.CHECK.replace(
            '"${{ runner.temp }}/r.xml"', "r.xml")), [])

    def test_a_repeated_report_flag_or_family_override_is_refused(self):
        """Which of two values pytest honours must never be a question (T-4367, fail closed)."""
        check = self.CHECK.replace('"${{ runner.temp }}/r.xml"', "r.xml")
        for run in ("pytest tools -o junit_family=xunit1 --junitxml=r.xml --junitxml=other.xml",
                    "pytest tools -o junit_family=xunit1 --junitxml=r.xml --junit-xml r.xml",
                    "pytest tools -o junit_family=xunit1 -o junit_family=xunit1 --junitxml=r.xml"):
            with self.subTest(run=run):
                found = self.found(run, check)
                self.assertTrue(any("more than once" in v for v in found), found)

    def test_a_pytest_step_that_writes_no_report_is_caught(self):
        found = self.found("python -m pytest tools/ -q -n auto", self.CHECK)
        self.assertTrue(any("junitxml" in v for v in found), found)

    def test_no_checker_step_is_caught(self):
        found = self.found(self.PYTEST)
        self.assertTrue(any(_CHECKER in v for v in found), found)

    def test_a_checker_that_runs_before_pytest_is_caught(self):
        self.assertTrue(self.found(self.CHECK, self.PYTEST))

    def test_a_checker_pointed_at_a_different_report_is_caught(self):
        self.assertTrue(self.found(self.PYTEST, 'python tools/check_shell_suite_outcomes.py "${{ runner.temp }}/other.xml"'))

    def test_a_checker_in_another_job_is_caught(self):
        doc = {"jobs": {"test": {"steps": [{"name": "pytest", "run": self.PYTEST}]},
                        "check": {"steps": [{"name": "check", "run": self.CHECK}]}}}
        self.assertTrue(outcome_check_violations({"ci.yml": doc}))

    def test_a_checker_that_is_only_mentioned_in_a_comment_is_caught(self):
        self.assertTrue(self.found(self.PYTEST, "# python tools/check_shell_suite_outcomes.py " + '"${{ runner.temp }}/r.xml"'))

    def test_an_expression_on_a_comment_line_of_the_pytest_or_checker_step_is_caught(self):
        """T-4385 r3 MF-A: GitHub substitutes `${{ }}` on a comment line before bash runs, so it can
        inject `exit 0` into either step; _code() drops the line before every other check."""
        for comment in ("# ${{ fromJSON('\"\\nexit 0\\n\"') }}", "  # ${{ github.event.head_commit.message }}"):
            for runs in ((f"{comment}\n{self.PYTEST}", self.CHECK), (self.PYTEST, f"{comment}\n{self.CHECK}")):
                with self.subTest(comment=comment, runs=runs):
                    found = self.found(*runs)
                    self.assertTrue(any("(T-4385 r3)" in v for v in found), found)

    @staticmethod
    def _doc(check_run: str, **step_keys: Any) -> dict:
        return {"jobs": {"test": {"steps": [
            {"name": "pytest", "run": TheOutcomeWiringLogic.PYTEST},
            {"name": "check", "run": check_run, **step_keys},
        ]}}}

    def test_the_checker_step_must_be_exactly_the_invocation(self):
        """N-17: the checker is the primary net and one shell line; a logging edit that masks
        its exit status (`| tee`, `; echo`, `|| exit 0`) would silently disarm it."""
        report = '"${{ runner.temp }}/r.xml"'
        for check_run in (
            self.CHECK + " || true",
            self.CHECK + " || exit 0",
            self.CHECK + "; echo done",
            self.CHECK + " && echo ok",
            self.CHECK + ' | tee "${{ runner.temp }}/check.log"',
            self.CHECK + "\necho after",
            self.CHECK + " --tools-dir tools/hooks",
            self.CHECK + " 2>&1",
            "FOO=1 " + self.CHECK,
            "true && " + self.CHECK,
            f"python -u tools/check_shell_suite_outcomes.py {report}",
            f"sh -c 'python tools/check_shell_suite_outcomes.py {report}'",
        ):
            with self.subTest(check_run=check_run):
                found = outcome_check_violations({"ci.yml": self._doc(check_run)})
                self.assertTrue(found, f"{check_run!r} can mask the checker's exit status and was not flagged")

    def test_the_exact_invocation_in_its_plain_spellings_is_fine(self):
        report = '"${{ runner.temp }}/r.xml"'
        for check_run in (self.CHECK, f"python3 ./tools/check_shell_suite_outcomes.py {report}",
                          f"python tools/check_shell_suite_outcomes.py \\\n  {report}",
                          f"/usr/bin/python3 tools/check_shell_suite_outcomes.py {report}\n"):
            with self.subTest(check_run=check_run):
                self.assertEqual(outcome_check_violations({"ci.yml": self._doc(check_run)}), [])

    def test_a_checker_step_that_can_be_skipped_or_forgiven_is_caught(self):
        for keys in ({"continue-on-error": True}, {"continue-on-error": "true"}, {"if": "always()"},
                     {"if": "false"}, {"if": "${{ github.event_name == 'push' }}"}):
            with self.subTest(keys=keys):
                self.assertTrue(outcome_check_violations({"ci.yml": self._doc(self.CHECK, **keys)}))

    def test_a_checker_step_that_explicitly_does_not_forgive_is_fine(self):
        self.assertEqual(
            outcome_check_violations({"ci.yml": self._doc(self.CHECK, **{"continue-on-error": False})}), [])

    def test_step_keys_that_switch_the_checker_off_without_touching_run_are_caught(self):
        """T-4372 r1 M2 / reviewer F1: `shell:`, `working-directory` and step `env` disarm the
        checker while its `run:` stays exact (shared rule: test_ci_integrity.backstop_step_problems)."""
        for keys in ({"shell": "bash -c true {0}"}, {"shell": "python {0}"}, {"shell": "bash -n {0}"},
                     {"working-directory": "tools/hooks"}, {"env": {"PYTHONPATH": "tools/shim"}},
                     {"env": {"BASH_ENV": "tools/x.sh"}}, {"env": {"SHELLOPTS": "noexec"}},
                     {"env": {"BASH_FUNC_python%%": "() { :; }"}}, {"env": "${{ fromJSON(vars.E) }}"}):
            with self.subTest(keys=keys):
                self.assertTrue(outcome_check_violations({"ci.yml": self._doc(self.CHECK, **keys)}), keys)

    def test_an_inherited_default_shell_or_directory_is_caught(self):
        for level in ("job", "workflow"):
            for run in ({"shell": "bash -c true {0}"}, {"working-directory": "sub"}):
                with self.subTest(level=level, run=run):
                    doc = self._doc(self.CHECK)
                    (doc["jobs"]["test"] if level == "job" else doc)["defaults"] = {"run": run}
                    self.assertTrue(outcome_check_violations({"ci.yml": doc}))

    def test_benign_step_keys_on_the_checker_are_fine(self):
        for keys in ({"shell": "bash"}, {"shell": "bash --noprofile --norc -eo pipefail {0}"},
                     {"working-directory": "."}, {"env": {"LANG": "C.UTF-8"}}):
            with self.subTest(keys=keys):
                self.assertEqual(outcome_check_violations({"ci.yml": self._doc(self.CHECK, **keys)}), [])

    def test_a_job_that_can_be_skipped_or_forgiven_is_caught(self):
        """N-20: the same one-key edit as on the checker step, one level up, neutralises pytest
        and the checker together."""
        for keys in ({"continue-on-error": True}, {"continue-on-error": "true"}, {"if": "false"},
                     {"if": "always()"}, {"if": "${{ github.event_name == 'push' }}"}):
            with self.subTest(keys=keys):
                doc = self._doc(self.CHECK)
                doc["jobs"]["test"].update(keys)
                found = outcome_check_violations({"ci.yml": doc})
                self.assertTrue(any("job 'test'" in v and ("continue-on-error" in v or "`if:`" in v) for v in found), found)

    def test_a_job_that_explicitly_does_not_forgive_or_skip_is_fine(self):
        doc = self._doc(self.CHECK)
        doc["jobs"]["test"]["continue-on-error"] = False
        self.assertEqual(outcome_check_violations({"ci.yml": doc}), [])

    def test_job_level_keys_on_an_unrelated_job_are_not_flagged(self):
        doc = self._doc(self.CHECK)
        doc["jobs"]["docs"] = {"if": "false", "continue-on-error": True, "steps": [{"run": "echo hi"}]}
        self.assertEqual(outcome_check_violations({"ci.yml": doc}), [])

    def test_a_workflow_without_the_whole_tree_step_has_nothing_to_wire(self):
        """The 'exactly one whole-tree pytest step' guard owns that failure; this one must not
        double-report it or crash."""
        self.assertEqual(self.found("pytest tools/test_x.py -q"), [])


def _on(push=("tools/**",), pull_request=("tools/**",), key: Any = True,
        schedule=("0 9 * * *",)) -> dict:
    """A parsed workflow's trigger block. PyYAML (YAML 1.1) reads the bare key `on` as
    boolean True, which is what safe_load really returns for ci.yml."""
    block: dict[str, Any] = {}
    if push is not None:
        block["push"] = {"branches": ["main"], "paths": list(push)}
    if pull_request is not None:
        block["pull_request"] = {"paths": list(pull_request)}
    if schedule is not None:
        block["schedule"] = [{"cron": c} for c in schedule]
    return {key: block, "jobs": {}}


class TheTriggerGuardLogic(unittest.TestCase):
    def test_both_triggers_covering_tools_is_fine(self):
        self.assertEqual(_events_missing_tools_trigger(_on()), [])
        self.assertEqual(_events_missing_tools_trigger(_on(key="on")), [])

    def test_dropping_tools_from_either_trigger_alone_is_caught(self):
        self.assertEqual(_events_missing_tools_trigger(_on(push=("bugs/**",))), ["push"])
        self.assertEqual(_events_missing_tools_trigger(_on(pull_request=("bugs/**",))), ["pull_request"])
        self.assertEqual(_events_missing_tools_trigger(_on(push=(), pull_request=())), ["push", "pull_request"])

    def test_a_trigger_that_is_gone_entirely_is_caught(self):
        self.assertEqual(_events_missing_tools_trigger(_on(pull_request=None)), ["pull_request"])

    def test_t5001_no_push_trigger_is_fine_but_the_nightly_is_required(self):
        self.assertEqual(_events_missing_tools_trigger(_on(push=None)), [])
        self.assertEqual(_events_missing_tools_trigger(_on(push=None, schedule=None)), ["schedule"])
        self.assertEqual(_events_missing_tools_trigger(_on(push=None, schedule=())), ["schedule"])

    def test_an_unfiltered_trigger_runs_on_everything_so_it_covers_tools(self):
        doc = {True: {"push": {"branches": ["main"]}, "pull_request": None,
                      "schedule": [{"cron": "0 9 * * *"}]}, "jobs": {}}
        self.assertEqual(_events_missing_tools_trigger(doc), [])

    @staticmethod
    def _ignoring(*patterns: str, event: str = "pull_request") -> dict:
        doc = _on()
        doc[True][event] = {"paths-ignore": list(patterns)}
        return doc

    def test_a_paths_ignore_that_swallows_tools_is_caught_not_read_as_unfiltered(self):
        """`paths-ignore: [tools/**]` is not "no filter": PR/push CI would stop running on
        tools changes (N-8). Judged by GitHub's glob rules (`**` crosses `/`, `*` does not)."""
        for pattern in ("tools/**", "**", "tools/*", "tools/test_shell_suites.py", "**/*.sh", "tools/**/*"):
            for event in ("push", "pull_request"):
                with self.subTest(pattern=pattern, event=event):
                    self.assertEqual(_events_missing_tools_trigger(self._ignoring(pattern, event=event)), [event])

    def test_a_paths_ignore_of_unrelated_files_is_not_brittle(self):
        for patterns in (("docs/**", "*.md"), ("tickets/**", "ledger/**", "knowledge/**"),
                         ("tools/docs/**", "**/*.md"), ("tools/*/README.md",)):
            with self.subTest(patterns=patterns):
                self.assertEqual(_events_missing_tools_trigger(self._ignoring(*patterns)), [])


_CLEAN_WRAPPER = (
    "import pytest\n"
    "@pytest.mark.parametrize('script', [1, 2])\n"
    "def test_shell_suite_passes(script, record_property):\n"
    "    record_property('suite_returncode', 0)\n"
    "    record_property('suite_killed', False)\n"
)

# The real wrapper's shape: a dataclass result, a module-level run_suite the test calls by
# global lookup, and pytest.fail on a bad result. Its run_suite raises so a guard that
# ran the real thing (instead of swapping in a fake) would be visibly wrong.
_REALISH_WRAPPER = '''\
from __future__ import annotations
import pytest
from dataclasses import dataclass


@dataclass
class SuiteResult:
    returncode: int | None
    output: str
    killed_reason: str | None
    elapsed: float


def run_suite(script, **kw):
    raise RuntimeError("the guard must swap this for a fake, never run it")


@pytest.mark.parametrize("script", [1, 2])
def test_shell_suite_passes(script, record_property):
    res = run_suite(script)
    record_property("suite_returncode", res.returncode)
    record_property("suite_killed", res.killed_reason is not None)
    if res.killed_reason is not None:
        pytest.fail("killed")
    if res.returncode != 0:
        pytest.fail("nonzero")
'''


class TheNeuteringGuardLogic(unittest.TestCase):
    """Fixtures: each in-tree way to switch the wrapper off without touching CI, so the
    guard is a standing test rather than a one-off probe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        (self.repo / "tools").mkdir()
        self.wrapper = self.repo / "tools" / "test_shell_suites.py"

    def check(self, wrapper_src: str = _CLEAN_WRAPPER, tools_conf: str | None = None,
              root_conf: str | None = None) -> list[str]:
        self.wrapper.write_text(wrapper_src, encoding="utf-8")
        confs = []
        for path, src in ((self.repo / "conftest.py", root_conf), (self.repo / "tools" / "conftest.py", tools_conf)):
            if src is not None:
                path.write_text(src, encoding="utf-8")
            confs.append(path)
        return wrapper_neutering(self.wrapper, confs, repo=self.repo)

    def test_a_clean_wrapper_and_no_conftests_pass(self):
        self.assertEqual(self.check(), [])

    def test_skip_skipif_xfail_on_the_test_function_are_caught(self):
        for mark in ("@pytest.mark.skip(reason='x')", "@pytest.mark.skipif(True, reason='x')",
                     "@pytest.mark.skipif(False, reason='not evaluated')", "@pytest.mark.xfail"):
            with self.subTest(mark=mark):
                self.assertTrue(self.check(_CLEAN_WRAPPER.replace("def test_shell", f"{mark}\ndef test_shell")))

    def test_a_module_level_skip_or_xfail_mark_is_caught(self):
        for line in ("pytestmark = pytest.mark.skip(reason='x')", "pytestmark = [pytest.mark.xfail]",
                     "pytestmark = [pytest.mark.skipif(True, reason='x')]"):
            with self.subTest(line=line):
                self.assertTrue(self.check(_CLEAN_WRAPPER.replace("@pytest", f"{line}\n@pytest", 1)))

    def test_a_module_level_pytest_skip_call_is_caught_not_silently_skipped(self):
        src = "import pytest\npytest.skip('x', allow_module_level=True)\n" + _CLEAN_WRAPPER
        self.assertTrue(any("cannot load" in v for v in self.check(src)))

    def test_a_wrapper_without_the_test_function_is_caught(self):
        self.assertTrue(self.check("import pytest\n"))

    def test_parametrize_and_other_marks_are_not_neutering(self):
        src = _CLEAN_WRAPPER.replace("def test_shell", "@pytest.mark.filterwarnings('ignore')\ndef test_shell")
        self.assertEqual(self.check(src), [])

    def test_the_realistic_wrapper_shape_passes_and_never_runs_a_real_suite(self):
        """_REALISH_WRAPPER's run_suite raises: if the guard ran it instead of swapping in a
        fake passing result, this would be red (and a real guard run would take minutes)."""
        self.assertEqual(self.check(_REALISH_WRAPPER), [])

    def test_an_imperative_skip_or_xfail_in_the_body_is_caught(self):
        """The imperative twin of the marks (R-2): no pytest mark exists to find, and the
        wrapper's own fast tests report `skipped` rather than failing, because Skipped is
        a BaseException. Only RUNNING the function once on a fake passing suite sees it."""
        for stmt in (
            "pytest.skip('too slow in CI')",
            "pytest.xfail('known flaky')",
            "import shutil\n    if not shutil.which('no-such-tool-xyz'):\n        pytest.skip('tool missing')",
            "raise RuntimeError('boom')",
        ):
            with self.subTest(stmt=stmt):
                src = _REALISH_WRAPPER.replace("    res = run_suite(script)", f"    {stmt}\n    res = run_suite(script)")
                found = self.check(src)
                self.assertTrue(any("did not return normally" in v for v in found), found)

    def test_a_wrapper_that_stops_recording_the_real_result_is_caught(self):
        """Early warning for the outcome check's input: without suite_returncode / suite_killed
        in the report the checker calls every suite 'never ran'."""
        dropped = _REALISH_WRAPPER.replace("    record_property(\"suite_returncode\", res.returncode)\n", "")
        self.assertNotEqual(dropped, _REALISH_WRAPPER)
        found = self.check(dropped)
        self.assertTrue(any("did not record" in v for v in found), found)
        no_param = _REALISH_WRAPPER.replace("(script, record_property)", "(script)").replace(
            '    record_property("suite_returncode", res.returncode)\n'
            '    record_property("suite_killed", res.killed_reason is not None)\n', "")
        self.assertTrue(any("record_property" in v for v in self.check(no_param)), self.check(no_param))

    def test_a_body_that_fails_a_passing_suite_is_caught_too(self):
        """A test that goes red on a PASSING fake is broken the other way round; the guard
        only ever feeds it a passing suite, so any exception is a finding."""
        found = self.check(_REALISH_WRAPPER.replace("!= 0", "!= 99"))
        self.assertTrue(any("did not return normally" in v for v in found), found)

    def test_collect_ignore_in_every_spelling_that_hits_the_wrapper_is_caught(self):
        for conf in (
            "collect_ignore = ['test_shell_suites.py']",
            "collect_ignore = ['hooks', 'test_shell_suites.py']",
            "collect_ignore_glob = ['test_shell*']",
            "collect_ignore_glob = ['*']",
            "collect_ignore = ['.']",
            "collect_ignore: list = ['test_shell_suites.py']",
            "collect_ignore = []\ncollect_ignore += ['test_shell_suites.py']",
            "collect_ignore = []\ncollect_ignore.append('test_shell_suites.py')",
            "collect_ignore = []\ncollect_ignore.extend(['hooks', 'test_shell_suites.py'])",
        ):
            with self.subTest(conf=conf):
                self.assertTrue(self.check(tools_conf=conf), f"{conf!r} drops the wrapper and was not flagged")

    def test_a_repo_root_conftest_is_covered_too(self):
        for conf in ("collect_ignore = ['tools']", "collect_ignore = ['tools/test_shell_suites.py']",
                     "collect_ignore_glob = ['tools/test_shell*']"):
            with self.subTest(conf=conf):
                self.assertTrue(self.check(root_conf=conf), f"{conf!r} drops the wrapper and was not flagged")

    def test_a_collect_ignore_that_cannot_be_read_statically_fails_closed(self):
        self.assertTrue(self.check(tools_conf="def _f(): return ['a']\ncollect_ignore = _f()"))

    def test_a_collect_ignore_hook_is_caught(self):
        self.assertTrue(self.check(tools_conf="def pytest_ignore_collect(collection_path):\n    return True"))

    def test_ignoring_unrelated_paths_is_not_brittle(self):
        self.assertEqual(self.check(tools_conf="collect_ignore = ['germinate', 'hooks/fixtures']"), [])
        self.assertEqual(self.check(root_conf="collect_ignore = ['docs', 'tools/germinate']"), [])
        self.assertEqual(self.check(tools_conf="import os\nX = 1\n"), [])


# T-4940: isolate the seat's state dir for plain-unittest runs (pytest's conftest
# _isolate_state_dir already does this per test). See tools/_seat_state_isolation.py.
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover - run from tools/
    import _seat_state_isolation  # type: ignore[no-redef]


def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


if __name__ == "__main__":
    unittest.main()
