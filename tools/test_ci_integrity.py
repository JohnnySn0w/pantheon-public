"""test_ci_integrity.py — CI cannot silently stop running the Python suite through step-level config (T-4333).

tools/test_shell_suite_coverage.py (T-2330, T-4280) guards that CI has exactly ONE whole-tree
`pytest tools/` step and that nothing in it drops the shell-suite wrapper; its outcome check
(tools/check_shell_suite_outcomes.py) reads what pytest REPORTED about the shell suites. Neither
sees the class this file guards: edits to the WORKFLOW STEP, its job, its trigger-free
surroundings or the pytest configuration that neuter EVERY Python test while every guard stays
green. The T-4280 review listed them (N-3, N-9); each is a one-line edit:

  A. the main step, its job, or any job it `needs:` can be skipped or forgiven:
     `continue-on-error` (truthy), or an `if:` that is not trivially true (`true`, `success()`,
     `always()`, `!cancelled()`, bare or wrapped in `${{ }}`). A skipped `needs:` job skips this one too.
     GitHub has no workflow-level `if:` / `continue-on-error`; the workflow-level knobs that do
     exist (`env`, `defaults.run`) are covered by B and C.
  B. the run script swallows or sidesteps pytest's exit status: pytest must be the step's ONLY
     substantive command (before it only `set` with -e / -u / -x / +e / -o pipefail: no `echo`,
     `printf -v PATH`, `exit 0`, `cd`, `pushd`, function shim such as `python() { :; }`,
     install), with no redirect anywhere in the step except to an fd (`>&2`, `2>&1`) or
     /dev/null (a redirect can write a conftest, pytest.ini or test file first) and no command
     substitution (backtick or `$(`, quoted or not), the LAST command (nothing
     after it: no `|| true`, `| tee`, `&& echo`, `&`, later line), not reached only through `||`,
     not negated with `!`, no `trap` in the step; the step runs under bare bash/sh or bash/sh
     with only --noprofile / --norc / -e / -x / -o pipefail and one `{0}` (`bash -n {0}`,
     `bash -c true {0}`, `sh -c : {0}` never run the script and exit 0), at step, job-defaults
     and workflow-defaults level; with NO `working-directory` at any level of the main job (T-4385
     r2 MF-2: the runner resolves it PHYSICALLY, so with a committed symlink `lnk -> projects/sub`
     the text `lnk/..` is projects/, not the root, and no textual normalisation can tell); and every env var
     that can change what bash executes or where `python` resolves is refused in env at every
     level and in any run-script mention (see _REFUSED_ENV): BASH_ENV and ENV (bash / sh
     source them before the script), SHELLOPTS (`noexec` runs nothing and exits 0, which at
     job or workflow level also neuters the outcome-check step), PATH (a committed `python`
     shim), any BASH_FUNC_* name (an exported-function shim such as `BASH_FUNC_python%%`),
     these four Python ones: PYTHONPATH and PYTHONUSERBASE (a sitecustomize.py,
     usercustomize.py or shadow module runs first), PYTHONHOME (swaps the standard library)
     and PYTHONSTARTUP (names a file for Python to execute); PYTHONOPTIMIZE (strips `assert`
     in modules pytest does not rewrite, so a failing check reports "passed", which the
     executed-count floor cannot see); and the committed-file shims LD_PRELOAD and
     LD_LIBRARY_PATH (a committed .so) and HOME (moves the user site, so a committed
     usercustomize.py runs) (T-4381). This list is not exhaustive: the class is open, and the
     executed-count floor is the outcome backstop. Any mention of GITHUB_PATH in any run
     script of the main job is refused too: an earlier step appending a committed directory
     to $GITHUB_PATH puts a `python` shim first on PATH for later steps, and that write is
     not a PATH mention (T-4381). The WHOLE launch of the main pytest is allowlisted
     (T-4381 r1 M-1, r2 M-2): optional `VAR=x` prefixes; the only wrapper is `timeout` with a
     literal duration and optionally `-k DUR` / `--kill-after=DUR`, `-s SIG` / `--signal=SIG`,
     `--preserve-status` (any other wrapper or flag, `env` in every form, `nice`, `command`,
     `exec`, ... fails closed: `env -S "python3 -O"` rebuilds `python -O` from one word);
     then a BARE `python` / `python3` / `python3.N` (no path: `tools/python` or `./python3`
     can be a committed shim) with only the interpreter flags `-m`, `-u`, `-X dev` and
     `-W <filter>` (`-O` / `-OO` strip asserts like PYTHONOPTIMIZE; `-S`, `-I`, `-E`, `-c` and
     the rest fail closed; a -W / -X value starting with `-` fails too: in `python -W -m pytest`
     the -W eats `-m`, so `pytest` runs as a FILE, T-4385 D4) and `-m pytest`, or a bare
     `pytest` / `py.test` token. Any other wrapper is refused, including `time python -m pytest`
     (an accepted false red). PS4 (`$(...)` runs on every `set -x` line) and
     ACTIONS_ALLOW_UNSECURE_COMMANDS (stdout `::set-env::` / `::add-path::`) joined _REFUSED_ENV
     in T-4385 D7. No `#` except on a whole comment line, quoted or not (midline_hash_problems,
     shared with the inert steps below): the tokenizer ends a word at a mid-word `#` and bash
     does not, so `--junit-prefix=a# || true` hid the `|| true` (T-4385). A real trailing
     comment (`pytest tools/ # note`) is refused too, an accepted false red.
     EVERY OTHER step of the main job, before or after it, must be INERT (T-4385 D2): a `uses:`
     step exactly `actions/checkout@v4` or `actions/setup-python@v5` (no ref-less or other ref),
     with only the keys name / uses / with (no `env`: `NODE_OPTIONS=--require ./tools/x.js` on the
     checkout step runs committed JS, which can write BASH_ENV to $GITHUB_ENV, T-4385 r2 MF-1) and
     only today's `with:` keys at today's exact values (_ACTIONS: checkout `fetch-depth: 0`,
     setup-python `python-version: ${{ matrix.python-version }}`; checkout `ref` / `repository` /
     `path` swap the tree); a `run:` step with only the keys name / run / shell (no `if`,
     `continue-on-error`, `env`, `working-directory`: a skipped or forgiven checkout leaves the
     persistent self-hosted workspace's stale tree), a string `run:` (`run: true` loads as a bool,
     T-4385 r2 N-a), under bare bash/sh, with no `$` / brace / glob word (except a literal
     `"${{ runner.temp }}/<path>"`), no command substitution, redirect, pipe or mid-line `#`, and
     only allowlisted command shapes (inert_step_problems), each EXACT, with no wildcard family
     (T-4385 r1): git config keys by exact list (`gc.*` allowed `gc.recentObjectsHook`, a shell
     command), pip arguments only the EXACT names ci.yml installs today (_PIP_NAMES: pip, pyyaml,
     pytest, pytest-xdist; optionally `==<digits>`), none naming a repo path (T-4385 r2: a name
     regex admitted any PyPI name with a pytest entry point), no pytest outside the main step. The shell
     at every level of every checked step is exactly `bash` / `sh`, no path. A run script that
     bash would read differently from this tokenizer (a backslash-newline continuation, which bash
     joins into one word, or a control / Unicode line-break character) is refused outright
     (bash_disagreement). The $GITHUB_ENV / $GITHUB_PATH files are random per-step paths that
     only the environment holds, so no write to them is expressible, however the name is spelled.
     This replaces trusting the name scan, which the T-4381 review evaded three ways (an `env | sed`
     indirect name, a `#`-line comment skip, `PYTEST_ADD""OPTS`); the scan stays as the earlier,
     more specific message.
  C. pytest is told to collect or run less: on the command line, in `-o addopts=...` /
     `--override-ini addopts=...`, or in PYTEST_ADDOPTS (step / job / workflow `env`, a command
     prefix, or any other mention in the job's run scripts such as `export` or a `$GITHUB_ENV`
     write): `-k`, `-m` (also inside short clusters like `-qk`), `--deselect`, `--ignore*`,
     `--lf` / `--sw` (cache-dependent subsets), a no-run mode (`--co`, `--setup-plan`,
     `--fixtures`, `--version`, ...), `-p <plugin>`, `-p no:<plugin>` (except the allowlisted
     `no:cacheprovider`; `-p no:unittest` alone drops most of the suite), `-c <other config>`,
     or an override of a collection key (python_files / python_classes / python_functions /
     norecursedirs). PYTEST_PLUGINS anywhere is refused. A value built from `$` / `${{ }}`
     cannot be judged here and fails closed, and so does any word of the pytest command
     holding a brace, glob or tilde character (`{`, `*`, `?`, `[`, `~`, quoted or not),
     which bash expands with no `$` (`{-k,nope}` becomes `-k nope`; `~` becomes a $HOME
     that env can set to `-ktest_ok`), and any `@file` argument, whose lines pytest reads
     as more arguments (T-4373).
  D. the collection scope is not what is intended: exactly one pytest invocation in all
     workflows collects the whole of tools/, and none collects the repo root instead (a bare
     `pytest`, `pytest .`, `pytest $GITHUB_WORKSPACE`), which would re-run everything (T-4280's
     serial-runner cost) or, as the main step, run a different tree.
  E. the TREE neuters collection: no conftest.py in the repo (and no module a conftest loads
     via `pytest_plugins`) defines a hook whose contract is to drop, skip or re-grade tests or
     rewrite the exit status (pytest_collection_modifyitems, pytest_ignore_collect,
     pytest_collection, pytest_pycollect_makeitem, pytest_collect_directory,
     pytest_runtest_protocol, pytest_runtest_setup, pytest_runtest_call,
     pytest_runtest_makereport, pytest_sessionfinish); and no pytest config file at the repo
     root or in tools/ (pytest.ini, .pytest.ini, pytest.toml, .pytest.toml, pyproject.toml,
     tox.ini, setup.cfg) narrows collection the way C describes. Any such hook FAILS CLOSED:
     whether a hook "only reorders" cannot be judged statically, so a real one is added to
     _SANCTIONED_HOOKS consciously, in review.
  F. the TREE shadows the launch: nothing importable sits at the repo root except the tools/
     package (and a conftest.py, which E reads). `python -m pytest` and every xdist worker have
     the repo root first on sys.path, so a committed pytest.py, _pytest/, pluggy.py or argparse.py
     runs instead of the real one. `-P` on the launch was rejected: it protects only the
     controller (runtime-proven, the workers still imported the shim) (T-4385 D3).
  G. the ENVELOPE of the main job reaches the main step's env and tree, so it is an ALLOWLIST
     (T-4385 r2, mason ruling; envelope_problems): the job's keys are exactly today's
     (_JOB_KEYS: name, runs-on, timeout-minutes, strategy, steps; `container`, `services`,
     `env`, `defaults`, `if`, `needs`, `permissions`, ... are refused), `runs-on` EQUALS
     `[self-hosted, linux, x64, pve2]`, `strategy` holds only fail-fast / matrix and the matrix
     only python-version; workflow-level and job-level `env` are absent or empty (they reach
     every step: GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n make every git call run a
     command, PIP_FIND_LINKS / PIP_NO_INDEX / PIP_CONFIG_FILE make the install step take a
     committed wheel, NODE_OPTIONS runs committed JS inside the actions); workflow `defaults` holds
     only `run.shell` (shell_ok). The main step's own keys are _MAIN_STEP_KEYS (name, run, env,
     shell, if, continue-on-error) and its `env` is exactly _MAIN_STEP_ENV (empty since T-4763
     retired the codex-config read-PAT secret), so no env name at all is left to a denylist. ci.yml is loaded by
     a strict loader (_StrictLoader) that refuses a duplicate mapping key (PyYAML keeps the LAST
     `run:` / `env:`, T-4385 r2 N-b) and a `<<` merge key.
     MAINTENANCE COST, deliberate: because these are allowlists, ANY legitimate edit to the main
     job's envelope (a new action or action ref, a new pip name, a runs-on label, a `with:` key,
     a job key, a main-step env var) goes red here until this file is edited in the same PR.
     Accepted false reds (each is a conscious edit here, never a silent green):
     actions/upload-artifact and actions/cache steps, a `safe.directory` git config, `echo` /
     `mkdir` / `cd` in any step, `pip install -r ...` / `-e ...` / version ranges (`>=`) / a
     capitalised `PyYAML`, `shell: /bin/bash`, `time python -m pytest`, any `working-directory`
     (even `.`) in the main job, a reordered runs-on list, and a checkout `fetch-depth` other than 0.

Non-brittle by the same rules as T-4280: only a WHOLE-DIRECTORY target makes a step "the" main
step (a single-file pytest step in another job stays legal), a matrix is one YAML step however many
legs it runs, and nothing pins the exact text of today's workflow: every rule reads the parsed
YAML and a tokenized run script, and each is proven on mutations of the real parsed workflow
and on synthetic fixtures below. Splitting the main step out into a reusable workflow (a
`uses: ./.github/workflows/x.yml` job, whose `with:` / `secrets:` this file does not read) is
closed by the pin test_the_real_main_step_is_found: the one main step must stay in ci.yml, job
`test`.

Residual, deliberately not closed here (forgery or beyond static reading, for code review):
arbitrary code in a hook or fixture (an autouse fixture that skips, `os._exit(0)` in any hook,
a plugin registered through `config.pluginmanager.register(...)`, monkeypatching pytest
internals); COMMITTED code that an inert step runs (tools/ci_fleet_account_reconcile.py and the
other `python tools/<file>.py` programs), which can write $GITHUB_ENV / $GITHUB_PATH or tools/conftest.py like
any conftest can; pip-installed PyPI code (a .pth in site-packages runs at every start; the
package list is exact and reviewed); the first-party actions' own env writes (setup-python
legitimately writes PATH, LD_LIBRARY_PATH, pythonLocation) and the code at their pinned MAJOR tag
(v4 / v5 are moving tags, a supply-chain matter); the self-hosted runner's persistent tool
cache and workspace, where a prior job can leave a .pth or .git/config (runner hygiene); the
matrix python-version VALUES (setup-python installs an interpreter, never a repo file); and
workflow keys other than `env` / `defaults` (name, on, concurrency, permissions, run-name reach
no step's env or tree; the triggers are test_shell_suite_coverage's). The interpreter pin
(`"${{ steps.<id>.outputs.python-path }}" -m pytest`) was considered and DEFERRED (T-4385 D1):
the runner resolves the step's `bash` itself through the $GITHUB_PATH prepends, so a pinned python
still runs under a shimmed bash, and every writer the inert rule leaves (the residuals above)
can equally write BASH_ENV to $GITHUB_ENV, which beats any pin. `-o pythonpath=` / `--confcutdir` are refused, but a shadow
module or conftest must still be committed and reviewed to matter; a third-party plugin that pip installs and autoloads; a pytest config file ABOVE the
repo root; an unknown value-taking long flag whose value reads as a positional; and a
`working-directory` / `-c` / `-p` / pytest_plugins that is legitimate, which fails red here
(never green) until this guard is extended consciously. The OUTCOME backstop for this whole
class, residual included, is tools/check_suite_outcome_floor.py (T-4372): a later CI step fails
if fewer tests executed, or more were skipped, than tools/ci_outcome_baseline.json allows.
"""
from __future__ import annotations

import ast
import configparser
import copy
import functools
import os
import pathlib
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from typing import Any

import yaml

# _options is the ONE pytest argv parser, shared with the T-4280 wiring checks (T-4367); it lives in
# test_shell_suite_coverage because that module cannot import this one at load time.
from tools.test_shell_suite_coverage import REPO, WORKFLOWS, _code, _options, raw_expression_problems


# --- strict YAML (T-4385 r2 N-b) ---------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that REFUSES a duplicate mapping key and a `<<` merge key. PyYAML keeps the LAST
    of two `run:` (or `env:`) keys silently, so the guard would judge a different step than the
    one a reader sees first (T-4385 r2 N-b); a `<<` merge is the same override by another name.
    stdlib + PyYAML only."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            seen: list[Any] = []
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.constructor.ConstructorError(
                        None, None, "a `<<` merge key overrides mapping keys unseen (T-4385 r2) (fail closed)",
                        key_node.start_mark)
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate mapping key {key!r}: PyYAML keeps only the LAST one, so a reader "
                                    f"and this guard would see different workflows (T-4385 r2) (fail closed)",
                        key_node.start_mark)
                seen.append(key)
        return super().construct_mapping(node, deep=deep)


def load_workflow_text(text: str) -> dict:
    """One workflow file, parsed by _StrictLoader (raises yaml.YAMLError on a duplicate key)."""
    return yaml.load(text, Loader=_StrictLoader) or {}


def _load_workflows() -> dict[str, dict]:
    """Every workflow, strictly parsed. Deliberately shadows test_shell_suite_coverage's
    safe_load-based loader for every rule in this file."""
    return {p.name: load_workflow_text(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.y*ml"))}


def _run_of(step: Any) -> str:
    """A step's `run:` script when it is a string, else "" (a non-string is refused by name in
    ci_integrity_violations / inert_step_problems, never fed to the tokenizer: T-4385 r2 N-a)."""
    run = step.get("run") if isinstance(step, dict) else None
    return run if isinstance(run, str) else ""

# --- pytest argv -------------------------------------------------------------------------

_SELECTORS = frozenset({"-k", "-m"})
# --confcutdir stops conftest loading at a directory, so it can exclude tools/conftest.py (T-4333 r3).
_EXCLUDERS = frozenset({"--deselect", "--ignore", "--ignore-glob", "--confcutdir"})
_CACHED_SUBSET = frozenset({"--lf", "--last-failed", "--sw", "--stepwise", "--sw-skip", "--stepwise-skip"})
_NO_RUN = frozenset({"--collect-only", "--co", "--setup-only", "--setup-plan", "--fixtures", "--funcargs",
                     "--fixtures-per-test", "--markers", "--help", "--version", "-h", "-V"})
_CONFIG_SWAP = frozenset({"-c", "--config-file", "--inifile"})
_OVERRIDE = frozenset({"-o", "--override-ini"})
# ini keys that decide WHAT is collected or what it imports (pythonpath can put a shadow
# module first on sys.path); any override of them narrows or can narrow.
_NARROWING_INI_KEYS = frozenset({"python_files", "python_classes", "python_functions", "norecursedirs", "pythonpath"})
# `-p no:X` values known not to change what is collected or how it is graded. Anything else
# fails closed: `-p no:unittest` alone collects 471 of 12675 tests and exits 0 (T-4333 r1 M2).
_HARMLESS_DISABLES = frozenset({"cacheprovider"})


def _dynamic(value: str) -> bool:
    return "$" in value


def _addopts_problems(value: Any, label: str) -> list[str]:
    """What an addopts / PYTEST_ADDOPTS value does to collection. Unjudgeable fails closed."""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    value = str(value if value is not None else "")
    if _dynamic(value):
        return [f"{label} is built at run time ({value!r}), so what it selects cannot be judged (fail closed)"]
    try:
        tokens = shlex.split(value)
    except ValueError:
        return [f"{label} cannot be parsed ({value!r}) (fail closed)"]
    return [f"{label} {why}" for why in narrowing(tokens)]


def narrowing(args: list[str]) -> list[str]:
    """Reasons a pytest argv would collect or run LESS than its targets hold. [] = runs it all."""
    why: list[str] = []
    # pytest expands an `@file` argument into the file's lines (argparse fromfile), so a
    # committed file can hold `-k test_ok`, unread here, like `-c <config>` (fail closed;
    # T-4333 follow-up r1 N-1).
    why += [f"passes `{a}`, an @file whose committed lines pytest reads as more arguments, unread "
            f"here (fail closed)" for a in args if a.startswith("@")]
    for opt, val in _options(args)[1]:
        if opt in _SELECTORS:
            why.append(f"selects with `{opt} {val}`: an expression can drop any test unseen")
        elif opt in _EXCLUDERS:
            why.append(f"excludes with `{opt} {val}`")
        elif opt in _CACHED_SUBSET:
            why.append(f"passes `{opt}`, which runs only a cache-dependent subset")
        elif opt in _NO_RUN:
            why.append(f"passes `{opt}`, which runs no tests and exits 0")
        elif opt == "-p" and val is not None and val.startswith("no:"):
            if val[3:] not in _HARMLESS_DISABLES:
                why.append(f"disables plugin `-p {val}`: switching off a builtin such as `unittest` or "
                           f"`python` drops whole classes of tests and exits 0 (fail closed)")
        elif opt == "-p" and val is not None:
            why.append(f"loads plugin `-p {val}`, whose hooks this guard cannot see (fail closed)")
        elif opt in _CONFIG_SWAP:
            why.append(f"points pytest at another config file (`{opt} {val}`), unread here (fail closed)")
        elif opt in _OVERRIDE and val:
            key, _, value = val.partition("=")
            key = key.strip()
            if key == "addopts":
                why += _addopts_problems(value, f"`{opt} addopts=...`")
            elif key in _NARROWING_INI_KEYS:
                why.append(f"overrides the collection key `{key}` (`{opt} {val}`)")
    return why


# --- run scripts -------------------------------------------------------------------------

_REDIRECT = re.compile(r"\d*(?:[<>]+&?|&>>?|<&|>&)")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words RECOGNISED as launching pytest (so a wrapped pytest is still found and then judged);
# for the main step only `timeout` is ACCEPTED, by launch_structure_problems() (T-4381 r2).
_WRAPPERS = frozenset({"env", "exec", "time", "nohup", "command", "builtin", "timeout", "xvfb-run", "!", "nice",
                       "ionice", "stdbuf", "setsid", "taskset", "chrt", "sudo", "doas", "unbuffer", "catchsegv"})
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# Characters bash expands without any `$`: brace expansion (`{-k,nope}` becomes `-k nope`)
# and pathname globs (`-[p]` or `--c?` can match a committed file named `-p` or `--co`), and
# tilde expansion (`~` becomes $HOME, which step / job / workflow env can set to `-ktest_ok`;
# bash also expands it after `=` / `:` as in `-o addopts=~`). Refused anywhere in a word
# rather than modelling where bash expands it (T-4333 follow-up r1 M-1).
# Extglob `@(...)` / `+(...)` / `!(...)` and process substitution `<(...)` are NOT listed:
# shlex(punctuation_chars=True) splits `(` off as an operator, so the pytest segment ends
# there and the "only pytest in the step" / "last command" rules refuse them; the test
# test_extglob_and_process_substitution_are_refused pins that, so a tokenizer change
# cannot silently reopen them.
_BRACE_OR_GLOB = re.compile(r"[{*?\[~]")


def _is_operator(tok: str) -> bool:
    return bool(tok) and set(tok) <= set("();<>|&") and not _REDIRECT.fullmatch(tok)


# Characters where this tokenizer and bash disagree about line or word boundaries (T-4385 r1 M-2):
# str.splitlines() ends a line at \r, \v, \f, \x1c-\x1e, \x85,  ,   and shlex treats \r as
# blank, while bash ends a line only at \n and keeps every other one inside the word. Refused
# outright rather than modelled.
_BASH_DISAGREES = re.compile("[\x00-\x08\x0b-\x1f\x7f\x85  ]")


def bash_disagreement(run: str) -> str | None:
    """Why this tokenizer would not read `run` the way bash does, or None. Checked on the RAW
    script, before _code() (which joins a backslash-newline with a SPACE where bash joins with
    nothing, so `pip install evil.w\\<newline>hl` read as two names while bash installed
    `evil.whl`, runtime-proven). A backslash-newline is refused anywhere, comment lines and
    quotes included (inside single quotes bash keeps it literal: an accepted false red); no real
    ci.yml run script uses a continuation (T-4385 r1 M-2)."""
    if re.search(r"\\[ \t]*(?:\n|$)", run):
        return ("holds a backslash-newline line continuation, which bash joins into ONE word (`evil.w\\` + "
                "newline + `hl` is `evil.whl`) and this tokenizer does not (T-4385) (fail closed)")
    if _BASH_DISAGREES.search(run):
        return ("holds a control or Unicode line-break character that bash keeps inside a word while this "
                "tokenizer splits on it (T-4385) (fail closed)")
    return None


def _script_tokens(run: str) -> list[str]:
    """Shell tokens of a run script, operators kept as tokens (quotes respected, so
    `-k 'a || b'` is one word), `;` between lines. Raises ValueError on unbalanced quotes and
    on any script bash would read differently (bash_disagreement, T-4385 r1 M-2)."""
    why = bash_disagreement(run)
    if why:
        raise ValueError(why)
    tokens: list[str] = []
    for line in _code(run).split("\n"):
        if not line.strip():
            continue
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens += list(lex) + [";"]
    return tokens


def _segments(tokens: list[str]) -> list[tuple[str | None, list[str], int]]:
    """[(operator before, words, index of the segment's last token)] for simple commands."""
    out: list[tuple[str | None, list[str], int]] = []
    words: list[str] = []
    before: str | None = None
    for i, tok in enumerate(tokens):
        if _is_operator(tok):
            if words:
                out.append((before, words, i - 1))
                words = []
            before = tok
        else:
            words.append(tok)
    if words:
        out.append((before, words, len(tokens) - 1))
    return out


def _strip_redirects(words: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if _REDIRECT.fullmatch(w):
            if out and out[-1].isdigit():
                out.pop()
            skip = True
            continue
        out.append(w)
    return out


def _is_pytest(tok: str) -> bool:
    return posixpath.basename(tok) in ("pytest", "py.test") or tok == "-mpytest"


def _launcher_prefix(prefix: list[str]) -> bool:
    """True when the words before the pytest token only LAUNCH it (`python -m`, `VAR=x`,
    `env`, `timeout 900`, ...), so `pip install pytest` is not mistaken for a pytest run."""
    # A `$` word (`timeout $T`, `$PYWRAP`) or a brace / glob word (`{timeout,true}`) is
    # accepted as launcher so the pytest is still found; expanded_words() and
    # globbing_words() then refuse it by name (T-4333 r3, T-4373).
    # The VALUE word after a short flag is accepted too (`python -X dev`, `timeout -s KILL`,
    # `env -u HOME`), so the pytest is still found; for the main step
    # launch_structure_problems() and interpreter_flag_problems() then judge it (T-4381).
    return all(_ASSIGN.match(w) or w in _WRAPPERS or w.startswith("-") or _DURATION.match(w)
               or "$" in w or _BRACE_OR_GLOB.search(w) or (i and prefix[i - 1].startswith("-") and not prefix[i - 1].startswith("--"))
               or posixpath.basename(w).startswith("python") for i, w in enumerate(prefix))


_INTERPRETER = re.compile(r"^python(3(\.\d+)?)?$")
_SIGNAL = re.compile(r"^(SIG)?[A-Z]+[0-9]*$|^\d+$")
_WRAPPER_WHY = ("only an optional `timeout [-k DUR] [-s SIG] [--preserve-status] DUR` may wrap the launch (`env -S` "
                "rebuilds `python -O` from one word; `env`, `nice`, `command`, `exec` and other wrappers change what "
                "runs) (fail closed)")


def launch_structure_problems(prefix: list[str], word: str) -> list[str]:
    """The WHOLE launch of the main pytest, by allowlist (T-4381 r2 M-2):
    `[VAR=x ...] [!] [timeout [-k DUR | --kill-after=DUR] [-s SIG | --signal=SIG] [--preserve-status] DUR]`
    then either a bare `python` / `python3` / `python3.N` (flags judged by
    interpreter_flag_problems) and `-m pytest` / `-mpytest`, or a bare `pytest` / `py.test`.
    Any other wrapper word or flag (`env` in any form, `nice`, `stdbuf`, `setsid`, `command`,
    `exec`, `nohup`, ...) and any path-qualified interpreter or pytest token (`tools/python`,
    `./python3`, `/usr/bin/python3`, `tools/pytest`: a committed shim) fails closed, naming it.
    `$` / brace / glob words are named by expanded_words() / globbing_words() and are named
    here too; `!` has its own message."""
    bad: list[str] = []
    i, n = 0, len(prefix)
    while i < n and (_ASSIGN.match(prefix[i]) or prefix[i] == "!"):
        i += 1
    if i < n and prefix[i] == "timeout":
        i += 1
        while i < n and prefix[i].startswith("-"):
            opt = prefix[i]
            if opt == "--preserve-status":
                i += 1
            elif opt in ("-k", "-s") and i + 1 < n:
                ok = (_DURATION if opt == "-k" else _SIGNAL).match(prefix[i + 1])
                if not ok:
                    bad.append(f"launches pytest through `timeout {opt} {prefix[i + 1]}`: {_WRAPPER_WHY}")
                i += 2
            elif opt.startswith("--kill-after=") and _DURATION.match(opt.split("=", 1)[1]) \
                    or opt.startswith("--signal=") and _SIGNAL.match(opt.split("=", 1)[1]):
                i += 1
            else:
                bad.append(f"launches pytest through `timeout {opt}`: {_WRAPPER_WHY}")
                i += 1
        if i < n and _DURATION.match(prefix[i]):
            i += 1
        else:
            bad.append(f"launches pytest through `timeout` without a literal duration: {_WRAPPER_WHY}")
    if i < n:
        head = prefix[i]
        if _INTERPRETER.match(head):
            if not (word == "-mpytest" or word == "pytest" and n - 1 > i and re.fullmatch(r"-u*m", prefix[-1])):
                bad.append(f"runs pytest token `{word}` after `{head}` without `-m`: only `-m pytest` / `-mpytest` is "
                           f"accepted (`python pytest` runs a FILE named pytest) (fail closed)")
            return bad
        if posixpath.basename(head).startswith("python"):
            bad.append(f"launches pytest with interpreter `{head}`: only a bare python, python3 or python3.N is "
                       f"accepted (a path such as `tools/python` can run a committed shim) (fail closed)")
        else:
            bad.append(f"launches pytest through `{head}`: {_WRAPPER_WHY}")
        return bad
    if word not in ("pytest", "py.test"):
        bad.append(f"runs pytest token `{word}`: only a bare `pytest` / `py.test` (or `python -m pytest`) is "
                   f"accepted (a path such as `tools/pytest` can run a committed shim) (fail closed)")
    return bad


def interpreter_flag_problems(prefix: list[str]) -> list[str]:
    """Words after the `python` interpreter in a pytest launch prefix that are not on the
    ALLOWLIST: `-m` (ends a cluster; `-m pytest`, `-Om pytest`, `-um pytest`), `-u`, `-X dev`
    / `-Xdev` and `-W <filter>` / `-W<filter>`, which only make a run stricter or unbuffered.
    Everything else fails closed (T-4381 M-1): `-O` / `-OO` strip `assert` in modules pytest
    does not rewrite, so a failing check reports "passed"; `-S`, `-I`, `-E`, `-s`, `-B`, `-c`,
    another `-X` option or any non-flag word change what runs. A launcher-prefix word
    starting with `-` was accepted blindly before, so `python -O -m pytest` was green."""
    first = next((i for i, w in enumerate(prefix) if posixpath.basename(w).startswith("python")), None)
    if first is None:
        return []
    words, bad, i = prefix[first + 1:], [], 0
    while i < len(words):
        w = words[i]
        ok = w.startswith("-") and not w.startswith("--") and len(w) > 1
        j, cluster = 0, w[1:]
        while ok and j < len(cluster):
            c = cluster[j]
            if c == "u":
                j += 1
            elif c == "m":
                ok = j + 1 == len(cluster)  # the module is the separate pytest token
                break
            elif c in "XW":
                value = cluster[j + 1:]
                if not value:
                    i += 1
                    value = words[i] if i < len(words) else ""
                    w = f"{w} {value}".rstrip()
                # T-4385 D4: a value starting with `-` is never a warning filter or an -X option;
                # in `python -W -m pytest` the -W eats `-m`, so `pytest` then runs as a FILE path.
                ok = bool(value) and not value.startswith("-") and (c == "W" or value == "dev")
                break
            else:
                ok = False
        if not ok:
            bad.append(w)
        i += 1
    return bad


class Invocation:
    """One pytest command inside a run script."""

    def __init__(self, prefix: list[str], args: list[str], before: str | None, last: bool, end: int = -1,
                 word: str = "pytest"):
        self.prefix, self.args, self.before, self.last, self.end = prefix, args, before, last, end
        self.word = word  # the pytest token itself (`pytest`, `/usr/bin/pytest`, `-mpytest`)
        self.positionals, self.opts = _options(args)

    @staticmethod
    def _target(p: str) -> str:
        for ws in ("${{ github.workspace }}", "${GITHUB_WORKSPACE}", "$GITHUB_WORKSPACE"):
            if p.startswith(ws):
                p = "." + p[len(ws):]
        return posixpath.normpath(p)

    @property
    def whole_tree(self) -> bool:
        return any(self._target(p) == "tools" for p in self.positionals)

    @property
    def unscoped(self) -> bool:
        """Collects the repo root rather than tools/: bare `pytest`, `pytest .`, the workspace."""
        if any(opt in _NO_RUN for opt, _ in self.opts):
            return False
        return not self.positionals or any(self._target(p) == "." for p in self.positionals)


def invocations(run: str) -> list[Invocation]:
    """Every pytest command in a run script. Raises ValueError when it cannot be tokenized."""
    tokens = _script_tokens(run)
    found: list[Invocation] = []
    for before, words, end in _segments(tokens):
        words = _strip_redirects(words)
        for i, w in enumerate(words):
            if _is_pytest(w) and _launcher_prefix(words[:i]):
                last = all(t == ";" for t in tokens[end + 1:])
                found.append(Invocation(words[:i], words[i + 1:], before, last, end, w))
                break
    return found


def _safe_invocations(run: str) -> list[Invocation]:
    try:
        return invocations(run)
    except ValueError:
        return []


# --- workflow structure ------------------------------------------------------------------

def _unwrap(expr: Any) -> Any:
    if isinstance(expr, str):
        s = expr.strip()
        m = re.fullmatch(r"\$\{\{(.*)\}\}", s, re.S)
        return (m.group(1) if m else s).strip()
    return expr


def trivially_true(cond: Any) -> bool:
    return cond is True or _unwrap(cond) in ("true", "success()", "always()", "!cancelled()")


def _is_root_dir(value: Any) -> bool:
    """`working-directory` that is LITERALLY the repo root: exactly `.`, `./` or
    `${{ github.workspace }}`. No normalisation (T-4385 r2 MF-2): the runner resolves the path
    physically, so with a committed symlink `lnk -> projects/sub`, `lnk/..` is projects/ although
    posixpath.normpath says `.`. Used for the backstop steps' shared rule; inside the main job
    envelope_problems / inert_step_problems / _step_problems refuse ANY working-directory."""
    return isinstance(value, str) and value.strip() in (".", "./", "${{ github.workspace }}")


def _may_be_root(value: Any) -> bool:
    """For rule D (a bare pytest in ANOTHER job): True unless `value` is certainly not the repo
    root. Fails closed on an expression, an absolute path, a `..` component, a textual root, or a
    path that resolves to the root in the checked-out tree (a committed symlink) (T-4385 r2 MF-2)."""
    s = str(value).strip()
    if "$" in s or s.startswith("/") or ".." in s.split("/") or posixpath.normpath(s) == ".":
        return True
    return os.path.realpath(REPO / s) == os.path.realpath(REPO)


# Custom `shell:` words GitHub's own bash template uses; anything else (`-c`, `-n`, a second
# positional) can make the script never run and still exit 0 (T-4333 r1 M1).
_SHELL_LONG_FLAGS = frozenset({"--noprofile", "--norc"})


def shell_ok(value: Any) -> bool:
    """True for bare `bash` / `sh`, or bash/sh with only --noprofile, --norc, -e, -x and
    `-o pipefail` (clusters like `-eo pipefail` included) and exactly one trailing `{0}`.
    The first word is exactly `bash` or `sh`, no path: `tools/bash {0}` runs a committed shell
    (T-4385 r1 M-5; `/bin/bash` is an accepted false red)."""
    if value is None:
        return True
    try:
        words = shlex.split(str(value))
    except ValueError:
        return False
    if not words or words[0] not in ("bash", "sh"):
        return False
    if len(words) == 1:
        return True
    if words[-1] != "{0}" or words.count("{0}") != 1:
        return False
    rest = words[1:-1]
    i = 0
    while i < len(rest):
        w = rest[i]
        if w in _SHELL_LONG_FLAGS:
            i += 1
            continue
        if not re.fullmatch(r"-[ex]*o?", w) or w == "-":
            return False
        if w.endswith("o"):
            if i + 1 >= len(rest) or rest[i + 1] != "pipefail":
                return False
            i += 1
        i += 1
    return True


def _harmless_leader(words: list[str]) -> bool:
    """The ONLY command allowed before pytest in the main step: `set` with flags from -e, -u,
    -x, +e and `-o pipefail` (clusters such as `-euo pipefail` included). T-4333 r2: every
    richer leader (echo, printf, mkdir) was a hole, because a redirect or `printf -v PATH` on
    it changes pytest's input or environment before pytest runs. Adding a leader back is a
    one-line conscious guard edit."""
    if words[0] != "set" or len(words) == 1:
        return False
    rest = words[1:]
    i = 0
    while i < len(rest):
        w = rest[i]
        if w == "+e":
            i += 1
            continue
        if not re.fullmatch(r"-[eux]*o?", w) or w == "-":
            return False
        if w.endswith("o"):
            if i + 1 >= len(rest) or rest[i + 1] != "pipefail":
                return False
            i += 1
        i += 1
    return True


def _redirect_problems(tokens: list[str]) -> list[str]:
    """Any redirect in the step whose target is not an fd (`>&2`, `2>&1`) or /dev/null: it can
    write a conftest, a pytest.ini or a test file before pytest runs (T-4333 r2 R2-M1)."""
    why: list[str] = []
    for i, tok in enumerate(tokens):
        if not _REDIRECT.fullmatch(tok):
            continue
        target = tokens[i + 1] if i + 1 < len(tokens) else ""
        if target == "/dev/null" or (tok.endswith("&") and target.isdigit()):
            continue
        why.append(f"redirects `{tok} {target}`: only an fd (`>&2`, `2>&1`) or /dev/null is accepted, because a "
                   f"redirect can write a conftest, pytest.ini or test file before pytest runs (fail closed)")
    return why


_JUNIT_FLAGS = ("--junitxml", "--junit-xml")
# Exactly today's literal form (T-4333 round-4 ruling): `$RUNNER_TEMP` is an env var a
# step can override, and any wider spelling is the next hole.
_RUNNER_TEMP = "${{ runner.temp }}/"


def _runner_temp_path(value: str) -> bool:
    """A report path under the runner's temp dir with no other expansion in it."""
    return value.startswith(_RUNNER_TEMP) and "$" not in value[len(_RUNNER_TEMP):]


def _command_words(inv: "Invocation", exempt: Any) -> list[str]:
    """Every word of the pytest command (launch prefix, the pytest token, argv) except a
    --junitxml / --junit-xml VALUE for which `exempt(value)` holds."""
    found = [*inv.prefix, inv.word]
    skip_next = False
    for arg in inv.args:
        if skip_next:
            skip_next = False
            if exempt(arg):
                continue
        flag, eq, value = arg.partition("=")
        if flag in _JUNIT_FLAGS:
            if not eq:
                skip_next = True
                continue
            if exempt(value):
                continue
        found.append(arg)
    return found


def expanded_words(inv: "Invocation") -> list[str]:
    """Words of the pytest command (launch prefix, pytest token and argv) that hold a `$`
    expansion or a `${{ }}` expression: their run-time value can inject any flag unseen
    (`$EXTRA` holding `-p no:unittest`, `${{ vars.X }}`, `${X:--k nope}`, `$SHIM/pytest`). The
    one exception is the VALUE of --junitxml / --junit-xml when it starts with the literal
    `${{ runner.temp }}/` and holds no other expansion (T-4333 r3, narrowed by the round-4
    ruling; the pytest token joined in T-4373)."""
    return [w for w in _command_words(inv, _runner_temp_path) if "$" in w]


def _runner_temp_path_unglobbed(value: str) -> bool:
    """The exempt junit value with no brace or glob character after the literal prefix,
    whose own braces GitHub substitutes before bash runs (T-4373)."""
    return _runner_temp_path(value) and not _BRACE_OR_GLOB.search(value[len(_RUNNER_TEMP):])


def globbing_words(inv: "Invocation") -> list[str]:
    """Words of the pytest command (launch prefix, pytest token and argv) holding a `{`, `*`,
    `?`, `[` or `~`, which bash can expand into extra words or flags with no `$` at all:
    `{-k,nope}` becomes `-k nope`, `--{deselect,deselect}=x` becomes two --deselect flags,
    `{/tmp/shim,/usr}/bin/pytest` runs a shim, `-[p]` / `--c?` match a committed file named
    `-p` / `--co`. The tokenizer strips quotes, so a quoted `{` or `*` is refused too (fail
    closed); the one exception is the exempt junit value (T-4373)."""
    return [w for w in _command_words(inv, _runner_temp_path_unglobbed) if _BRACE_OR_GLOB.search(w)]


def _forgives(value: Any) -> bool:
    return value is not None and value is not False and _unwrap(value) != "false"


def _needs_closure(jobs: dict, name: str) -> list[str]:
    seen: list[str] = []
    stack = [name]
    while stack:
        needs = (jobs.get(stack.pop()) or {}).get("needs") or []
        for dep in [needs] if isinstance(needs, str) else needs:
            if dep not in seen:
                seen.append(dep)
                stack.append(dep)
    return seen


def _run_defaults(block: dict) -> dict:
    """`defaults.run` of a workflow or job, {} when absent or not a mapping (a malformed one is
    refused by envelope_problems, never crashed on)."""
    defaults = block.get("defaults") if isinstance(block, dict) else None
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return run if isinstance(run, dict) else {}


# Env vars refused at every level and in any run-script mention (export, prefix, $GITHUB_ENV).
# These are env vars that can change what bash executes or where `python` resolves
# (T-4333 follow-up r2 M-2 added SHELLOPTS, PATH and the BASH_FUNC_ prefix below), or
# that make failing tests report "passed" (T-4381 F-1: PYTHONOPTIMIZE strips `assert` in
# modules pytest does not rewrite, which the executed-count floor cannot see because the
# tests still ran). T-4381 F-2 added the committed-file shims: LD_PRELOAD / LD_LIBRARY_PATH
# (a committed .so) and HOME (moves the user site, so a committed usercustomize.py runs;
# the `~` argv route is refused separately). A run-script mention is refused whether it
# writes or only READS the name (`mkdir -p "$HOME/.cache"` goes red too): deliberately, a
# cheap fail-closed rather than telling reads from writes. The class is open by nature: the list is not
# exhaustive, and the executed-count floor is the outcome backstop.
_REFUSED_ENV = {
    "PYTEST_PLUGINS": "loads plugins whose hooks this guard cannot see",
    "BASH_ENV": "is sourced by bash before the script runs, so it can shim `python` and exit 0",
    "ENV": "is sourced by sh before the script runs, so it can shim `python` and exit 0",
    "PYTHONPATH": "puts a directory first on sys.path, so a sitecustomize.py or shadow module there runs first",
    "PYTHONHOME": "swaps the Python standard library pytest runs on",
    "PYTHONUSERBASE": "moves user site-packages, so a usercustomize.py or shadow module there runs first",
    "PYTHONSTARTUP": "names a file for Python to execute (fail closed)",
    "SHELLOPTS": "sets bash options at startup, and `noexec` runs nothing and exits 0 (at job or workflow "
                 "level the outcome-check step too)",
    "PATH": "decides where `python` resolves, so a committed shim can run instead (fail closed)",
    "PYTHONOPTIMIZE": "strips `assert` statements outside pytest-rewritten modules, so a failing check in a "
                      "helper module reports passed, which the executed-count floor cannot see",
    "LD_PRELOAD": "loads a shared object into every process first, so a committed .so can shim `python` "
                  "or pytest (fail closed)",
    "LD_LIBRARY_PATH": "puts a directory first on the library search path, so a committed .so can replace "
                       "one `python` loads (fail closed)",
    "HOME": "moves the user site-packages, so a committed usercustomize.py or shadow module there runs "
            "first (fail closed)",
    # T-4385 D7: bash expands PS4 (command substitution included) on every xtrace line, and `set -x`
    # / `bash -x` are accepted, so `PS4: '$(...)'` runs arbitrary code before pytest.
    "PS4": "is expanded by bash on every `set -x` trace line, so a `$(...)` in it runs arbitrary code before "
           "pytest (fail closed)",
    # T-4385 D7: re-enables the stdout `::set-env::` / `::add-path::` workflow commands, so committed
    # test OUTPUT could set BASH_ENV or PATH for the outcome steps that follow pytest.
    "ACTIONS_ALLOW_UNSECURE_COMMANDS": "re-enables the `::set-env::` / `::add-path::` stdout commands, so "
                                       "anything a step prints can set env or PATH for later steps (fail closed)",
    # T-4385 r2 (item 7): named for a specific message; the allowlists (empty job / workflow env,
    # no env on a non-main step, _MAIN_STEP_ENV exact) are what refuse them structurally.
    "NODE_OPTIONS": "makes every node process (the checkout / setup-python actions) `--require` committed JS "
                    "first, which can write BASH_ENV to $GITHUB_ENV (T-4385 r2) (fail closed)",
    "GIT_CONFIG": "names a git config file, so a committed one can make git run a command (T-4385 r2) (fail closed)",
}
# Refused by NAME PREFIX, same treatment: bash imports `BASH_FUNC_<name>%%` as a function,
# so `BASH_FUNC_python%%` shims `python`. Whether GitHub accepts `%%` in a key does not
# matter: fail closed on the prefix.
_REFUSED_ENV_PREFIXES = {
    "BASH_FUNC_": "imports an exported bash function, which can shim `python` and exit 0",
    # GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n (and GIT_CONFIG_PARAMETERS,
    # GIT_CONFIG_GLOBAL) inject git config, e.g. core.fsmonitor or an alias that runs a command.
    "GIT_CONFIG_": "injects git config (core.fsmonitor, core.hooksPath, alias.*), which makes git run a "
                   "command (T-4385 r2) (fail closed)",
    # PIP_FIND_LINKS / PIP_NO_INDEX / PIP_CONFIG_FILE / PIP_INDEX_URL make `pip install pytest` take a
    # committed or foreign wheel.
    "PIP_": "reconfigures pip (PIP_FIND_LINKS, PIP_NO_INDEX, PIP_CONFIG_FILE, PIP_INDEX_URL), so the install "
            "step can take a committed or foreign wheel (T-4385 r2) (fail closed)",
}
# Refused on ANY mention in any `run:` script of the main pytest job (not an env key, so
# not checked in `env` blocks): a step that appends a directory to $GITHUB_PATH puts it
# first on PATH for every LATER step, so a committed `python` there shims the main step,
# and that write is not a PATH mention (T-4381 F-2). The same step-mention scan as above.
_REFUSED_SCRIPT_MENTIONS = {
    "GITHUB_PATH": "(a $GITHUB_PATH write puts a directory first on PATH for later steps, so a committed "
                   "`python` there can shim the main step) (fail closed)",
}


def _mentions(code: str, var: str, prefix: bool = False) -> int:
    tail = "" if prefix else "(?![A-Za-z0-9_])"
    return len(re.findall(rf"(?<![A-Za-z0-9_]){var}{tail}", code))


def _env_problems(env: Any, where: str) -> list[str]:
    if env is None:
        return []
    if not isinstance(env, dict):
        return [f"{where} `env` is an expression ({env!r}), so PYTEST_ADDOPTS cannot be ruled out (fail closed)"]
    why: list[str] = []
    if "PYTEST_ADDOPTS" in env:
        why += _addopts_problems(env["PYTEST_ADDOPTS"], f"{where} env PYTEST_ADDOPTS")
    for var in _REFUSED_ENV:
        if var in env:
            why.append(f"{where} env {var} {_REFUSED_ENV[var]}")
    for key in env:
        for pre, reason in _REFUSED_ENV_PREFIXES.items():
            if str(key).startswith(pre):
                why.append(f"{where} env {key} {reason}")
    return why


def backstop_step_problems(doc: dict, job: dict, step: dict) -> list[str]:
    """Step-level keys that switch off an OUTCOME step reading pytest's report after the main step
    (T-4280's check_shell_suite_outcomes.py, T-4372's check_suite_outcome_floor.py) without
    touching its `run:` line, which each owner pins exactly (T-4372 r1 M2, reviewer F1).

    Shared here, not copied, because this file owns the step-level rules (shell_ok, _is_root_dir,
    _env_problems) and both wiring guards call it:
      - `shell:` on the step, or inherited from job / workflow `defaults.run`, must pass shell_ok
        (`bash -c true {0}` never runs the script and exits 0);
      - `working-directory` on the step (or inherited) must be the repo root (elsewhere,
        `tools/<checker>.py` names another tree's checker);
      - the step's own `env` must hold no refused var (PYTHONPATH, BASH_ENV, SHELLOPTS, ... can
        run other code first). Job and workflow `env` are already refused for this job by the
        main step's rules, since the backstops must sit in the main step's job."""
    why: list[str] = []
    for label, value in (("step", step.get("shell")), ("job defaults", _run_defaults(job).get("shell")),
                         ("workflow defaults", _run_defaults(doc).get("shell"))):
        if not shell_ok(value):
            why.append(f"runs under {label} shell {value!r}: only bare bash/sh or bash/sh with --noprofile, "
                       f"--norc, -e, -x, -o pipefail and one `{{0}}` is accepted (another shell or `-c` can "
                       f"skip the check and exit 0) (fail closed)")
    for label, value in (("step", step.get("working-directory")),
                         ("job defaults", _run_defaults(job).get("working-directory")),
                         ("workflow defaults", _run_defaults(doc).get("working-directory"))):
        if value is not None and not _is_root_dir(value):
            why.append(f"has {label} working-directory {value!r}: `tools/` would name another tree's checker")
    why += _env_problems(step.get("env"), "step")
    why += raw_expression_problems(step.get("run"))  # T-4385 r3 MF-A: the exact-`run:` check reads _code()
    return why


# --- T-4385 D2: every OTHER step of the main job is inert, by allowlist of shape ----------
# The $GITHUB_ENV / $GITHUB_PATH files are per-step random paths that only the environment holds.
# Bash text reaches them only through a `$` expansion, a command substitution, or a program that
# reads its own environment (python -c, sh -c, printenv | xargs, a git alias `!cmd`, pip
# installing a local path). A step with no `$`, no substitution, no redirect or pipe, whose
# every command is one of the shapes below, cannot express such a write, however the name is
# spelled; the name scan in _step_problems stays only as the earlier, more specific message.
# Applied to the steps AFTER the main step too: a later `BASH_ENV` write neuters the outcome steps.
# T-4385 r2: the EXACT `uses:` values ci.yml's main job uses today, each with its exact `with:` keys and
# values (type included: `fetch-depth: false` equals 0 in Python). A ref-less or other ref (N-c), any
# other `with:` key (checkout `ref` / `repository` / `path` swap the tree; setup-python
# `python-version-file` reads a committed file) and any step `env` (MF-1) are refused.
_ACTIONS: dict[str, dict[str, Any]] = {
    "actions/checkout@v4": {"fetch-depth": 0},
    "actions/setup-python@v5": {"python-version": "${{ matrix.python-version }}"},
}
_USES_STEP_KEYS = frozenset({"name", "uses", "with"})
_RUN_STEP_KEYS = frozenset({"name", "run", "shell"})
# The main job envelope (T-4385 r2, mason ruling): exactly the keys ci.yml's `test` job has today.
_JOB_KEYS = frozenset({"name", "runs-on", "timeout-minutes", "strategy", "steps"})
_RUNS_ON = ["self-hosted", "linux", "x64", "pve2"]
_STRATEGY_KEYS = frozenset({"fail-fast", "matrix"})
_MATRIX_KEYS = frozenset({"python-version"})
# The main step: its keys, and its env EXACTLY (no denylist of names left to extend).
_MAIN_STEP_KEYS = frozenset({"name", "run", "env", "shell", "if", "continue-on-error"})
# T-4763: empty. The T-3613 codex-config read PAT was its only entry; the codex-config fixture
# now reads this repo's history, so the main step carries no secret at all.
_MAIN_STEP_ENV: dict[str, str] = {}
_R2 = "(T-4385 r2) (fail closed)"
_R3 = "(T-4385 r3) (fail closed)"
# T-4385 r3 MF-B: the two action steps the main job REQUIRES, once each, in this order before the main step.
_CHECKOUT = "actions/checkout@v4"
_SETUP_PYTHON = "actions/setup-python@v5"
# Other keys (core.pager, alias.*, core.hooksPath, include.path, core.fsmonitor, ...) make git run a command.
_GIT_CONFIG_KEYS = frozenset({"user.email", "user.name", "init.defaultBranch"})
# `git -c KEY=VALUE ... gc`: an EXACT key list with a value shape per key, no `gc.*` family
# (T-4385 r1 M-1: `gc.recentObjectsHook='touch marker'` runs a shell command during gc). These are
# the three ci.yml's "Keep the workspace repo packed" step uses; a new key is a conscious edit.
_GIT_GC_KEYS = {
    "gc.auto": re.compile(r"^\d+$"),
    "gc.autoPackLimit": re.compile(r"^\d+$"),
    "gc.autoDetach": re.compile(r"^(?:true|false)$"),
}
_PIP_FLAGS = frozenset({"--upgrade", "-U", "-q", "--quiet"})
# A package argument is one of the EXACT names ci.yml installs today, optionally
# `==<digits>(.<digits>)*` (T-4385 r2; r1 M-3 had a name regex, which admitted any PyPI name,
# including a plugin with a pytest11 entry point that autoloads into the main step). No dot, path,
# URL or VCS form can match; an argument naming an existing path in the repo is refused too.
_PIP_NAMES = frozenset({"pip", "pyyaml", "pytest", "pytest-xdist"})
_PKG = re.compile(r"^(?P<name>[a-z0-9-]+)(?:==\d+(?:\.\d+)*)?$")
_TOOLS_SCRIPT = re.compile(r"^(?:\./)?tools/[A-Za-z0-9_]+\.py$")
_INERT_WHY = ("which is not an allowlisted inert command (`set` flags, `true`, `git config --global` user.email / "
              "user.name / init.defaultBranch <value>, `git [-c gc.auto=N | gc.autoPackLimit=N | gc.autoDetach=BOOL] "
              "gc [--auto] [--quiet]`, `pip install` of pip / pyyaml / pytest / pytest-xdist, `python tools/<file>.py`; "
              "pytest runs only in the "
              "main step): any other command can write $GITHUB_ENV / $GITHUB_PATH and change what later steps run "
              "(T-4385) (fail closed)")


def midline_hash_problems(code: str) -> list[str]:
    """A `#` anywhere but a whole comment line (`code` is already _code()-stripped of those).
    shlex ends a word at a mid-word `#` and drops the rest of the line as a comment, while bash
    keeps `a#` as a word and runs what follows: `--junit-prefix=a# || true` hid the `|| true`
    and a failing suite exited 0 (runtime-proven). Refused even inside quotes, where bash starts
    no comment (`-k 'a#b'`): quoting is not visible after tokenizing (the brace/glob rule's
    stance), and a second, quote-aware lexer is exactly the bash-vs-tokenizer disagreement this
    closes. No real run line needs a `#` (T-4385)."""
    if "#" not in code:
        return []
    return ["holds a `#` outside a whole comment line: this tokenizer ends a word at a mid-word `#` and drops the "
            "rest of the line, bash does not (`--junit-prefix=a# || true` hides the `|| true`), and quoting is not "
            "visible here (T-4385) (fail closed)"]


def _pip_install_ok(args: list[str], repo: pathlib.Path = REPO) -> bool:
    """Only allowlisted flags and at least one name from _PIP_NAMES (N-3: a bare `pip install
    --upgrade` installs nothing and is refused; T-4385 r2: exact names), none of which names a path
    in the repo (the step runs from the repo root, T-4385 r1 M-3 / M-4)."""
    names = [a for a in args if a not in _PIP_FLAGS]
    matches = [_PKG.match(a) for a in names]
    return bool(names) and all(m and m["name"] in _PIP_NAMES and not os.path.lexists(repo / m["name"])
                               for m in matches)


def _git_ok(rest: list[str]) -> bool:
    """`git config --global <key> <value>` for an exact key and a value not starting with `-`
    (N-1), or `git [-c <exact gc key>=<shaped value>]... gc [--auto] [--quiet]` (M-1)."""
    if rest[:2] == ["config", "--global"]:
        return len(rest) == 4 and rest[2] in _GIT_CONFIG_KEYS and not rest[3].startswith("-")
    i = 0
    while i < len(rest) and rest[i] == "-c":
        key, eq, value = (rest[i + 1] if i + 1 < len(rest) else "").partition("=")
        if not eq or key not in _GIT_GC_KEYS or not _GIT_GC_KEYS[key].match(value):
            return False
        i += 2
    return rest[i:i + 1] == ["gc"] and all(w in ("--auto", "--quiet") for w in rest[i + 1:])


def _inert_command(words: list[str]) -> bool:
    """One simple command of a non-main step of the main job matches an allowlisted shape. No
    pytest: ci.yml runs none outside the main step, and any other pytest run in the main job loads
    every conftest and plugin before the main step (T-4385 r1 N-2)."""
    head, rest = words[0], words[1:]
    if head in ("true", ":"):
        return not rest
    if head == "set":
        return _harmless_leader(words)
    if head == "git":
        return _git_ok(rest)
    if head == "pip":
        return rest[:1] == ["install"] and _pip_install_ok(rest[1:])
    if _INTERPRETER.match(head):
        if rest[:3] == ["-m", "pip", "install"]:
            return _pip_install_ok(rest[3:])
        return bool(rest) and bool(_TOOLS_SCRIPT.match(rest[0]))  # a committed program (residual)
    return False


def _working_directory_problems(doc: dict, job: dict, step: dict, who: str) -> list[str]:
    """ANY `working-directory` at step, job-defaults or workflow-defaults level of a step of the
    main job (T-4385 r2 MF-2). The runner resolves it PHYSICALLY: with a committed symlink
    `lnk -> projects/sub`, `lnk/..` is projects/, which posixpath.normpath calls `.`. ci.yml sets
    none, so absent is the only accepted value (even `.` is an accepted false red)."""
    return [f"has {label} working-directory {value!r}: {who} takes none (the runner resolves it physically, so "
            f"through a committed symlink `lnk/..` is not the repo root, and `tools/` would name another tree) {_R2}"
            for label, value in (("step", step.get("working-directory")),
                                 ("job defaults", _run_defaults(job).get("working-directory")),
                                 ("workflow defaults", _run_defaults(doc).get("working-directory")))
            if value is not None]


def _uses_step_problems(step: dict) -> list[str]:
    """A `uses:` step of the main job (T-4385 r2): exactly one of _ACTIONS, only name / uses / with,
    and only that action's `with:` keys at their exact values."""
    why: list[str] = []
    uses = step.get("uses")
    for k in step:
        if k == "env":
            why.append(f"sets `env` on the `uses: {uses}` step: an action step of the main job may set none "
                       f"(`NODE_OPTIONS: --require ./tools/x.js` runs committed JS inside the action, which can write "
                       f"BASH_ENV to $GITHUB_ENV, T-4385 r2 MF-1) {_R2}")
            why += _env_problems(step.get("env"), "step")
        elif k not in _USES_STEP_KEYS:
            why.append(f"has step key `{k}` on the `uses: {uses}` step: an action step of the main job may have only "
                       f"name / uses / with (a skipped or forgiven checkout leaves the persistent workspace's stale "
                       f"tree) {_R2}")
    if not isinstance(uses, str) or uses not in _ACTIONS:
        why.append(f"uses `{uses}`: only {sorted(_ACTIONS)} may run in the main job, at exactly that ref (a `uses:` "
                   f"action can write $GITHUB_ENV / $GITHUB_PATH unseen; a ref-less or other ref is other code) {_R2}")
        return why
    allowed = _ACTIONS[uses]
    with_ = step.get("with")
    if with_ is None:
        return why
    if not isinstance(with_, dict):
        return why + [f"has a non-mapping `with:` ({with_!r}) on `{uses}` {_R2}"]
    for k, v in with_.items():
        if k not in allowed:
            why.append(f"passes `with: {k}` to `{uses}`: only {sorted(allowed)} is accepted (checkout ref / repository "
                       f"/ path swap the tree the main step tests; other inputs change what the action does) {_R2}")
        elif type(v) is not type(allowed[k]) or v != allowed[k]:
            why.append(f"passes `with: {k}: {v!r}` to `{uses}`: only the exact value {allowed[k]!r} is accepted {_R2}")
    return why


def envelope_problems(doc: dict, job: dict) -> list[str]:
    """The main job's ENVELOPE, by allowlist (T-4385 r2, mason ruling): what reaches the main step's
    env and tree other than its steps. Job keys exactly _JOB_KEYS (`env` only when empty); runs-on
    equal to _RUNS_ON; strategy / matrix keys by allowlist; workflow `env` absent or empty;
    workflow `defaults` only `run.shell` (shell_ok is judged per step, working-directory refused
    per step)."""
    why: list[str] = []
    wenv = doc.get("env")
    if wenv:
        why.append(f"sits in a workflow with a non-empty workflow-level `env` ({wenv!r}): it reaches every step of the "
                   f"main job (GIT_CONFIG_*, PIP_*, NODE_OPTIONS, BASH_ENV, ...), so it must be absent or empty {_R2}")
    wdef = doc.get("defaults")
    if wdef is not None:
        run = wdef.get("run") if isinstance(wdef, dict) else None
        if not isinstance(wdef, dict) or set(wdef) - {"run"} or not isinstance(run, dict) or set(run) - {"shell"}:
            why.append(f"sits in a workflow whose `defaults` ({wdef!r}) holds more than `run.shell` {_R2}")
    for k in job:
        if k == "env":
            if job["env"]:
                why.append(f"has a non-empty job-level `env` ({job['env']!r}): it reaches every step of the main job "
                           f"(GIT_CONFIG_*, PIP_*, NODE_OPTIONS, BASH_ENV, ...), so it must be absent or empty {_R2}")
        elif k not in _JOB_KEYS:
            why.append(f"has job key `{k}`: the main job may have only {sorted(_JOB_KEYS)} (`container` / `services` "
                       f"swap the runtime, `defaults` / `env` reach every step, `if` / `needs` can skip it) {_R2}")
    if job.get("runs-on") != _RUNS_ON:
        why.append(f"runs on {job.get('runs-on')!r}: the main job runs on exactly {_RUNS_ON} (another runner is "
                   f"another environment and tool cache) {_R2}")
    strategy = job.get("strategy")
    if strategy is not None:
        if not isinstance(strategy, dict):
            why.append(f"has a non-mapping `strategy` ({strategy!r}) {_R2}")
        else:
            why += [f"has strategy key `{k}`: only {sorted(_STRATEGY_KEYS)} {_R2}" for k in strategy
                    if k not in _STRATEGY_KEYS]
            matrix = strategy.get("matrix")
            if matrix is not None and not isinstance(matrix, dict):
                why.append(f"has a non-mapping matrix ({matrix!r}) {_R2}")
            elif matrix:
                why += [f"has matrix key `{k}`: only {sorted(_MATRIX_KEYS)} (`include` adds legs and variables) {_R2}"
                        for k in matrix if k not in _MATRIX_KEYS]
    if not isinstance(job.get("steps"), list):
        why.append(f"has no `steps` list {_R2}")
    return why


def inert_step_problems(doc: dict, job: dict, step: dict) -> list[str]:
    """Why a step of the main job OTHER than the main step could write $GITHUB_ENV / $GITHUB_PATH
    or change the tree (T-4385 D2, r2). [] = inert. A `uses:` step: _uses_step_problems. A
    `run:` step: only name / run / shell, a string run; shell_ok at every level (`shell: python {0}`
    writes the file with no `$`); no working-directory; no step
    `env` (a step BASH_ENV or PS4 runs other code); no command substitution, no `#` except on a
    whole comment line (the tokenizer, unlike bash, ends a word at a mid-word `#`), no redirect,
    only `;` / `&&` between commands, no `$` / brace / glob / tilde word except the literal
    `"${{ runner.temp }}/<path>"` form; and every command one of the shapes in _inert_command."""
    if not isinstance(step, dict):
        return [f"is not a mapping ({step!r}), so it cannot be shown inert {_R2}"]
    if "uses" in step:
        return _uses_step_problems(step)
    if "run" not in step:
        return [f"has neither `run:` nor `uses:` (keys {sorted(map(str, step))}), so it cannot be shown inert {_R2}"]
    run = step["run"]
    if not isinstance(run, str):
        return [f"has a {type(run).__name__} `run:` ({run!r}), not a string: YAML reads `run: true` / `run: yes` as "
                f"a bool, which GitHub renders back to text, so what runs is not what this guard reads {_R2}"]
    why: list[str] = [
        f"has step key `{k}`: a non-main `run:` step of the main job may have only name / run / shell (`if` or "
        f"`continue-on-error` can skip or forgive an install and leave the persistent workspace's stale state) {_R2}"
        for k in step if k not in _RUN_STEP_KEYS and k not in ("env", "working-directory")]
    for label, value in (("step", step.get("shell")), ("job defaults", _run_defaults(job).get("shell")),
                         ("workflow defaults", _run_defaults(doc).get("shell"))):
        if not shell_ok(value):
            why.append(f"runs under {label} shell {value!r}: a non-main step of the main job must run under bare "
                       f"bash/sh (another interpreter can write $GITHUB_ENV / $GITHUB_PATH with no `$`) (T-4385) "
                       f"(fail closed)")
    # T-4385 r1 M-4: rule F reads only the repo root, so `python -m pip` from `projects/` ran a committed
    # projects/pip/__main__.py. r2 MF-2: the runner resolves the path physically (`lnk/..` through a
    # committed symlink is not the root), so ANY working-directory is refused, at every level.
    why += _working_directory_problems(doc, job, step, "a non-main step of the main job")
    if "env" in step:
        why.append("sets step `env`: a non-main step of the main job may set none (a BASH_ENV, ENV or PS4 there runs "
                   "other code that can write $GITHUB_ENV / $GITHUB_PATH) (T-4385) (fail closed)")
        why += _env_problems(step.get("env"), "step")  # the specific name, when there is one
    code = _code(run)
    if "`" in code or "$(" in code:
        why.append("uses command substitution (a backtick or `$(`), which can write $GITHUB_ENV / $GITHUB_PATH "
                   "(T-4385) (fail closed)")
    why += midline_hash_problems(code)
    try:
        tokens = _script_tokens(run)
    except ValueError as exc:
        return why + [f"run script cannot be tokenized ({exc}), so it cannot be shown inert (T-4385) (fail closed)"]
    for i, tok in enumerate(tokens):
        if _REDIRECT.fullmatch(tok):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            why.append(f"redirects `{tok} {target}`: a non-main step of the main job may not redirect (a redirect "
                       f"to $GITHUB_ENV / $GITHUB_PATH changes what later steps run) (T-4385) (fail closed)")
        elif _is_operator(tok) and tok not in (";", "&&"):
            why.append(f"uses the operator `{tok}`: a non-main step of the main job may join commands only with `;`, "
                       f"`&&` or a new line (a pipe feeds another program) (T-4385) (fail closed)")
    for _, words, _ in _segments(tokens):
        words = _strip_redirects(words)
        if not words:
            continue
        expanding = [w for w in words if ("$" in w or _BRACE_OR_GLOB.search(w)) and not _runner_temp_path_unglobbed(w)]
        if expanding:
            why.append(f"runs `{' '.join(words)}` with the expanding word(s) {expanding}: a `$`, brace, glob or tilde "
                       f"can reach $GITHUB_ENV / $GITHUB_PATH (only a literal `${{{{ runner.temp }}}}/<path>` may "
                       f"expand) (T-4385) (fail closed)")
        elif not _inert_command(words):
            why.append(f"runs `{' '.join(words)}`, {_INERT_WHY}")
    return why


def step_order_problems(job: dict, main_index: int) -> list[str]:
    """The main job's required steps (T-4385 r3 MF-B): EXACTLY ONE `actions/checkout@v4`, as the FIRST
    step, and EXACTLY ONE `actions/setup-python@v5`, after it and before the main step. The workspace
    is self-hosted and persistent: without a checkout first, the suite (and every step before it) runs
    on whatever tree the previous job left, and a python not set up by this job is the runner's."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []  # refused by envelope_problems
    uses = [s.get("uses") if isinstance(s, dict) else None for s in steps]
    checkouts = [i for i, u in enumerate(uses) if u == _CHECKOUT]
    setups = [i for i, u in enumerate(uses) if u == _SETUP_PYTHON]
    why: list[str] = []
    if checkouts != [0]:
        why.append(f"has `{_CHECKOUT}` at step index(es) {checkouts} (main step at {main_index}): the main job needs "
                   f"exactly one, as its FIRST step, or the persistent self-hosted workspace's stale tree is what "
                   f"runs {_R3}")
    if len(setups) != 1 or not (checkouts[:1] or [-1])[0] < setups[0] < main_index:
        why.append(f"has `{_SETUP_PYTHON}` at step index(es) {setups} (main step at {main_index}): the main job needs "
                   f"exactly one, after the checkout and before the main step, or the suite runs on whatever "
                   f"python the runner has {_R3}")
    return why


def _main_steps(docs: dict[str, dict]):
    """(workflow, doc, job name, job, step index, step) for every step with a whole-tree pytest."""
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            for i, step in enumerate((job or {}).get("steps") or []):
                if any(inv.whole_tree for inv in _safe_invocations(_run_of(step))):
                    yield name, doc, job_name, job, i, step


def _step_problems(doc: dict, job: dict, step: dict) -> list[str]:
    why: list[str] = []
    jobs = doc.get("jobs") or {}
    # A. skipped or forgiven, at step or job level, or via a `needs:` job that can be skipped.
    for label, block in (("step", step), ("job", job)):
        if _forgives(block.get("continue-on-error")):
            why.append(f"has {label}-level continue-on-error ({block['continue-on-error']!r}): a red suite would not fail the run")
        if "if" in block and not trivially_true(block["if"]):
            why.append(f"has a {label}-level `if: {block['if']}`: the suite can be skipped")
    job_name = next((n for n, j in jobs.items() if j is job), None)
    for dep in _needs_closure(jobs, job_name) if job_name else []:
        cond = (jobs.get(dep) or {}).get("if")
        if cond is not None and not trivially_true(cond):
            why.append(f"needs job {dep!r}, whose `if: {cond}` can skip it, and a skipped dependency skips this job")
    # B. shell and working directory.
    for label, value in (("step", step.get("shell")), ("job defaults", _run_defaults(job).get("shell")),
                         ("workflow defaults", _run_defaults(doc).get("shell"))):
        if not shell_ok(value):
            why.append(f"runs under {label} shell {value!r}: only bare bash/sh or bash/sh with --noprofile, "
                       f"--norc, -e, -x, -o pipefail and one `{{0}}` is accepted (`-c`, `-n` or an extra "
                       f"argument can skip the script and exit 0) (fail closed)")
        elif value is not None and shlex.split(str(value))[0] != "bash":
            # T-4385 r3 nit: ci.yml's main step runs under the default bash; `sh` is dash, which this
            # bash-modelled tokenizer does not read and which has no `pipefail`.
            why.append(f"runs under {label} shell {value!r}: the main step runs under bash only (`sh` is another "
                       f"shell than the one this guard models) {_R3}")
    why += _working_directory_problems(doc, job, step, "the main step")  # T-4385 r2 MF-2
    # G. the main step's own keys and env, by allowlist (T-4385 r2; the job envelope is
    # envelope_problems, reported per job by ci_integrity_violations).
    why += [f"has step key `{k}`: the main step may have only {sorted(_MAIN_STEP_KEYS)} {_R2}"
            for k in step if k not in _MAIN_STEP_KEYS and k != "working-directory"]
    senv = step.get("env")
    if senv is not None:
        if not isinstance(senv, dict):
            why.append(f"has a non-mapping step `env` ({senv!r}) {_R2}")
        else:
            why += [f"sets step env {k}={v!r}: the main step's env is exactly {_MAIN_STEP_ENV} (any other name can "
                    f"change what bash, python, pip, git or node run; no denylist is complete) {_R2}"
                    for k, v in senv.items() if _MAIN_STEP_ENV.get(k, object()) != v]
    # C. env at every level (the specific name, next to the allowlist messages above).
    why += _env_problems(doc.get("env"), "workflow")
    why += _env_problems(job.get("env"), "job")
    why += _env_problems(step.get("env"), "step")
    # B + C. the run script itself.
    run = _run_of(step)
    try:
        tokens = _script_tokens(run)
        invs = invocations(run)
    except ValueError as exc:
        return why + [f"run script cannot be tokenized ({exc}) (fail closed)"]
    code = _code(run)
    if "`" in code or "$(" in code:
        why.append("uses command substitution (a backtick or `$(`), which runs arbitrary commands inside "
                   "an argument, quoted or not (fail closed)")
    why += midline_hash_problems(code)  # T-4385: same rule as the inert steps
    why += _redirect_problems(tokens)
    segments = _segments(tokens)
    if any(words[0] == "trap" for _, words, _ in segments):
        why.append("sets a `trap`, which can rewrite the step's exit status")
    main_ends = {inv.end for inv in invs if inv.whole_tree}
    for _, words, end in segments:
        words = _strip_redirects(words)
        if end in main_ends or not words or words[0] == "trap":
            continue
        if not _harmless_leader(words):
            why.append(f"runs `{' '.join(words)}` in the same step as pytest: only pytest itself, optionally "
                       f"after `set` with -e / -u / -x / +e / -o pipefail, is accepted (a function shim, `cd`, "
                       f"`exit 0`, `printf -v PATH`, an install can change or skip what runs) (fail closed)")
    for inv in invs:
        if not inv.whole_tree:
            continue
        if not inv.last:
            why.append("runs something after pytest (`|| true`, `| tee`, `&& ...`, `&`, a later line): "
                       "pytest must be the step's last command so its exit status is the step's")
        if inv.before is not None and "||" in inv.before:
            why.append("reaches pytest only through `||`, so it runs only when the command before it fails")
        if "!" in inv.prefix:
            why.append("negates pytest with `!`")
        why += launch_structure_problems(inv.prefix, inv.word)
        why += [f"launches pytest with interpreter flag `{w}`: only -m, -u, -X dev and -W <filter> are "
                f"accepted after `python` (`-O` / `-OO` strip asserts so failing checks pass; `-S`, `-I`, "
                f"`-E` and the rest change what runs) (fail closed)"
                + (" (the flag consumed a `-` word as its value, so `-m` no longer introduces the module and "
                   "`pytest` runs as a FILE path, e.g. a committed ./pytest)" if " -" in w else "")
                for w in interpreter_flag_problems(inv.prefix)]
        for w in inv.prefix:
            if w.startswith("PYTEST_ADDOPTS="):
                why += _addopts_problems(w.split("=", 1)[1], "command-prefix PYTEST_ADDOPTS")
        why += narrowing(inv.args)
        # T-4385 r2 MF-2, the same class in the argv: a `..` path component in a target is resolved
        # through whatever a committed symlink points at by anything that does not normalise textually.
        why += [f"passes the target `{p}` with a `..` component: a committed symlink makes it name another tree "
                f"{_R2}" for p in inv.positionals if ".." in p.split("/")]
        why += [f"passes `{w}`, a shell expansion or `${{{{ }}}}` expression in the pytest command whose run-time "
                f"value can inject any flag unseen (only a --junitxml path starting with the literal "
                f"`${{{{ runner.temp }}}}/` may expand) (fail closed)"
                for w in expanded_words(inv)]
        why += [f"passes `{w}`, which holds a brace, glob or tilde character (`{{`, `*`, `?`, `[`, `~`) in the "
                f"pytest command: bash can expand it into extra flags with no `$` (`{{-k,nope}}` becomes "
                f"`-k nope`; `-[p]` can match a committed file named `-p`; `~` becomes $HOME, which env can "
                f"set to `-ktest_ok`), and quoting is not visible here (fail closed)"
                for w in globbing_words(inv)]
    # C. PYTEST_ADDOPTS / refused env vars set any other way in this job's scripts.
    prefixed = {w.split("=", 1)[0] for inv in invs for w in inv.prefix if _ASSIGN.match(w)}
    for other in (job.get("steps") or []):
        code = _code(_run_of(other))
        for var in ("PYTEST_ADDOPTS", *_REFUSED_ENV):
            analysed = other is step and var == "PYTEST_ADDOPTS" and var in prefixed
            hits = _mentions(code, var) - (1 if analysed else 0)
            if hits > 0:
                why.append(f"step {other.get('name')!r} mentions {var} outside the analysed command prefix "
                           f"(an `export` or a $GITHUB_ENV write reaches pytest unseen) (fail closed)")
        for pre in _REFUSED_ENV_PREFIXES:
            if _mentions(code, pre, prefix=True):
                why.append(f"step {other.get('name')!r} mentions {pre}* outside the analysed command prefix "
                           f"(an `export` or a $GITHUB_ENV write reaches pytest unseen) (fail closed)")
        # T-4385 r3 MF-A: on the RAW text of EVERY step (the main one included), before any comment stripping.
        why += [f"step {other.get('name')!r} {w}" for w in raw_expression_problems(_run_of(other))]
        for var, reason in _REFUSED_SCRIPT_MENTIONS.items():
            if _mentions(code, var):
                why.append(f"step {other.get('name')!r} mentions {var} {reason}")
        # T-4385 D2: the name scan above is a speed bump; this is the wall.
        if other is not step:
            why += [f"step {other.get('name')!r} {w}" for w in inert_step_problems(doc, job, other)]
    return why


def ci_integrity_violations(docs: dict[str, dict]) -> list[str]:
    """Everything that lets CI stop running the Python suite while staying green. [] = fine."""
    out: list[str] = []
    # A step that mentions pytest but that bash would read differently (T-4385 r1 M-2: a
    # backslash-newline continuation) or that cannot be tokenized is not seen by _main_steps, so
    # it could be the main step in disguise: name it rather than only miscounting the main steps.
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                # T-4385 r2 N-a: `run: true` / `run: yes` load as a bool, which crashed the tokenizer
                # (TypeError). Any non-string `run:` anywhere is a named violation instead.
                if isinstance(step, dict) and "run" in step and not isinstance(step["run"], str):
                    out.append(f"{name}: job {job_name!r} step {step.get('name')!r} has a {type(step['run']).__name__} "
                               f"`run:` ({step['run']!r}), not a string (YAML reads `run: true` / `run: yes` as a bool) "
                               f"{_R2}")
                run = _run_of(step)
                if "pytest" in run or "py.test" in run:
                    try:
                        _script_tokens(run)
                    except ValueError as exc:
                        out.append(f"{name}: job {job_name!r} step {step.get('name')!r} mentions pytest but its run "
                                   f"script cannot be read the way bash reads it ({exc}), so what it runs cannot "
                                   f"be judged (fail closed)")
    mains = list(_main_steps(docs))
    if len(mains) != 1:
        out.append(f"expected exactly 1 step running pytest on the whole tools/ tree, found {len(mains)}: "
                   f"{[(n, s.get('name')) for n, _, _, _, _, s in mains]}")
    for name, doc, job_name, job, index, step in mains:
        out += [f"{name}: job {job_name!r} {why}" for why in envelope_problems(doc, job)]  # G (T-4385 r2)
        out += [f"{name}: job {job_name!r} {why}" for why in step_order_problems(job, index)]  # T-4385 r3 MF-B
        out += [f"{name}: job {job_name!r} step {step.get('name')!r} {why}" for why in _step_problems(doc, job, step)]
    # D. no pytest run over the repo root anywhere (the main step included).
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                wd = step.get("working-directory") or _run_defaults(job).get("working-directory") \
                    or _run_defaults(doc).get("working-directory")
                if wd is not None and not _may_be_root(wd):
                    continue  # a bare pytest in another directory collects that directory, not tools/
                if any(inv.unscoped for inv in _safe_invocations(_run_of(step))):
                    out.append(f"{name}: job {job_name!r} step {step.get('name')!r} runs pytest over the repo "
                               f"root (bare `pytest` / `pytest .`): a second full run, or a different tree than tools/")
    return out


# --- the tree: conftests, plugins, config files ------------------------------------------

_NEUTERING_HOOKS = frozenset({
    "pytest_collection_modifyitems", "pytest_ignore_collect", "pytest_collection",
    "pytest_pycollect_makeitem", "pytest_collect_directory", "pytest_runtest_protocol",
    "pytest_runtest_setup", "pytest_runtest_call", "pytest_runtest_makereport", "pytest_sessionfinish",
})
# (repo-relative path, hook) pairs a reviewer has read and accepted. Empty today: no conftest
# in the repo defines any of the hooks above.
_SANCTIONED_HOOKS: frozenset[tuple[str, str]] = frozenset()
_PRUNE = frozenset({"node_modules", "venv", "build", "dist", "__pycache__"})


def _conftests(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every conftest.py pytest could load from the repo (dot-dirs pruned, as pytest's own
    norecursedirs does; that also keeps .claude/worktrees out)."""
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _PRUNE)
        if "conftest.py" in files:
            found.append(pathlib.Path(root) / "conftest.py")
    return found


def _module_file(repo: pathlib.Path, dotted: str) -> pathlib.Path | None:
    base = repo.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _hook_problems(path: pathlib.Path, repo: pathlib.Path, seen: set[pathlib.Path]) -> list[str]:
    if path in seen:
        return []
    seen.add(path)
    label = path.relative_to(repo).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{label}: cannot parse ({exc}); cannot prove it defines no collection hook (fail closed)"]
    out: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
        for n in names:
            if n in _NEUTERING_HOOKS and (label, n) not in _SANCTIONED_HOOKS:
                out.append(f"{label}: defines {n}, which can drop, skip or re-grade tests or rewrite the exit "
                           f"status unseen (read it, then add it to _SANCTIONED_HOOKS consciously)")
            if n == "pytest_plugins":
                value = node.value
                strings = [value.value] if isinstance(value, ast.Constant) and isinstance(value.value, str) else (
                    [e.value for e in value.elts] if isinstance(value, (ast.List, ast.Tuple))
                    and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts) else None)
                if strings is None:
                    out.append(f"{label}: pytest_plugins is not a literal, so its hooks cannot be read (fail closed)")
                    continue
                for dotted in strings:
                    mod = _module_file(repo, dotted)
                    if mod is None:
                        out.append(f"{label}: pytest_plugins loads {dotted!r}, not found in the repo, so its "
                                   f"hooks cannot be read (fail closed)")
                    else:
                        out += _hook_problems(mod, repo, seen)
    return out


_CONFIG_FILES = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml", "tox.ini", "setup.cfg")


def _config_options(path: pathlib.Path) -> dict[str, Any] | None:
    """The pytest options a config file holds; None when it holds no pytest section."""
    if path.suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if path.name == "pyproject.toml":
            tool = data.get("tool", {}).get("pytest")
            if not isinstance(tool, dict):
                return None
            return tool.get("ini_options", tool)
        return data.get("pytest")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8"))
    section = {"setup.cfg": "tool:pytest"}.get(path.name, "pytest")
    if parser.has_section(section):
        return dict(parser[section])
    return {} if path.name in ("pytest.ini", ".pytest.ini") else None


# T-4385 D3: the ONLY importable top-level names at the repo root. `python -m pytest` puts the cwd
# (the repo root) first on sys.path, and so does every xdist worker (execnet starts it with
# `python -c`), so a committed root pytest.py, _pytest/, pluggy.py, or a lazily imported stdlib
# name such as argparse.py runs instead of the real one. `-P` on the launch protects only the
# controller (runtime-proven: every worker still imported a committed pytest.py). An allowlist,
# not a name list. A directory WITHOUT __init__ is a namespace portion and loses to an installed
# regular package, so it is not refused.
_ROOT_IMPORTABLE_OK = frozenset({("tools", "dir"), ("conftest.py", "file")})  # conftest.py: read by rule E
_MODULE_SUFFIXES = frozenset({".py", ".pyc", ".pyw", ".so", ".pyd"})


def root_module_problems(repo: pathlib.Path = REPO) -> list[str]:
    """F: importable top-level Python names at the repo root other than tools/ (T-4385 D3)."""
    out: list[str] = []
    for p in sorted(repo.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            kind, importable = "dir", (p / "__init__.py").is_file() or (p / "__init__.pyc").is_file()
        else:
            kind, importable = "file", p.suffix in _MODULE_SUFFIXES
        if importable and (p.name, kind) not in _ROOT_IMPORTABLE_OK:
            out.append(f"{p.name}{'/' if kind == 'dir' else ''}: an importable module at the repo root, which is first "
                       f"on sys.path for `python -m pytest` and every xdist worker, so it can shadow pytest, a plugin "
                       f"or a stdlib module (only tools/ is allowed there) (T-4385) (fail closed)")
    return out


def tree_violations(repo: pathlib.Path = REPO) -> list[str]:
    """E: conftest / plugin collection hooks and narrowing pytest config files; F: no importable
    module at the repo root but tools/. [] = fine."""
    out: list[str] = root_module_problems(repo)
    seen: set[pathlib.Path] = set()
    for conf in _conftests(repo):
        out += _hook_problems(conf, repo, seen)
    for d in (repo, repo / "tools"):
        for fname in _CONFIG_FILES:
            path = d / fname
            if not path.is_file():
                continue
            label = path.relative_to(repo).as_posix()
            try:
                options = _config_options(path)
            except (tomllib.TOMLDecodeError, configparser.Error, UnicodeDecodeError) as exc:
                out.append(f"{label}: cannot parse ({exc}) (fail closed)")
                continue
            for key, value in (options or {}).items():
                if key == "addopts":
                    out += _addopts_problems(value, f"{label}: addopts")
                elif key in _NARROWING_INI_KEYS:
                    out.append(f"{label}: sets the collection key {key} = {value!r}")
    return out


# --- tests -------------------------------------------------------------------------------

class TheRealCiRunsTheWholeSuite(unittest.TestCase):
    """Live tree: what CI and the repo actually do today."""

    def test_real_workflows_cannot_neuter_the_suite_at_step_level(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])

    def test_real_tree_has_no_collection_hook_or_narrowing_config(self):
        self.assertEqual(tree_violations(REPO), [])

    def test_the_real_main_step_is_found(self):
        mains = list(_main_steps(_load_workflows()))
        self.assertEqual([(m[0], m[2]) for m in mains], [("ci.yml", "test")])


def _before_pytest(step: dict, text: str) -> None:
    """Insert `text` right before the pytest COMMAND (not before the whole script), so a
    prefix-style mutation targets exactly what it names (T-4333 r2)."""
    old = step["run"]
    step["run"] = old.replace("python -m pytest", text + "python -m pytest", 1)
    assert step["run"] != old, "mutation anchor `python -m pytest` not found in the real main step"


@functools.lru_cache(maxsize=1)
def _parsed_real() -> dict[str, dict]:
    return _load_workflows()


def _real() -> tuple[dict, dict, dict]:
    """A deep copy of the parsed real workflows, plus the real main job and step inside it."""
    docs = copy.deepcopy(_parsed_real())
    (name, doc, _, job, _, step), = list(_main_steps(docs))
    return docs, job, step


class MutationsOfTheRealWorkflowAreCaught(unittest.TestCase):
    """Each hole, applied to the PARSED real workflow (no text pinning): must go red."""

    def assertCaught(self, docs: dict, fragment: str = "") -> None:
        found = ci_integrity_violations(docs)
        self.assertTrue(found, "mutation was not flagged")
        if fragment:
            self.assertTrue(any(fragment in v for v in found), found)

    def test_continue_on_error_at_step_or_job(self):
        for level in ("step", "job"):
            for value in (True, "true", "${{ matrix.python-version == '3.11' }}"):
                with self.subTest(level=level, value=value):
                    docs, job, step = _real()
                    (step if level == "step" else job)["continue-on-error"] = value
                    self.assertCaught(docs, "continue-on-error")

    def test_a_non_trivial_if_at_step_or_job(self):
        for level in ("step", "job"):
            for cond in (False, "false", "${{ false }}", "github.event_name == 'schedule'", "${{ vars.RUN_TESTS }}"):
                with self.subTest(level=level, cond=cond):
                    docs, job, step = _real()
                    (step if level == "step" else job)["if"] = cond
                    self.assertCaught(docs, "`if:")

    def test_a_skippable_needs_job(self):
        docs, job, _ = _real()
        docs["ci.yml"]["jobs"]["gate"] = {"if": "false", "runs-on": "x", "steps": [{"run": "true"}]}
        job["needs"] = "gate"
        self.assertCaught(docs, "needs job 'gate'")

    def test_the_exit_status_is_swallowed(self):
        for suffix in (" || true", " || :", " || exit 0", " | tee log.txt", " && echo ok", " &",
                       "\necho done", "; exit 0"):
            with self.subTest(suffix=suffix):
                docs, _, step = _real()
                step["run"] = step["run"].rstrip("\n") + suffix
                self.assertCaught(docs, "last command")

    def test_pytest_reached_only_through_or_negated_or_trapped(self):
        for wrap in ("true || {}", "! {}", "trap 'exit 0' EXIT\n{}"):
            with self.subTest(wrap=wrap):
                docs, _, step = _real()
                pre, _, post = wrap.partition("{}")
                _before_pytest(step, pre)
                step["run"] += post
                self.assertCaught(docs)

    def test_pytest_addopts_in_env_at_every_level(self):
        for level in ("workflow", "job", "step"):
            for value in ("-k 'not slow'", "-m smoke", "--deselect tools/test_x.py::t", "-qk nope", "${{ vars.OPTS }}"):
                with self.subTest(level=level, value=value):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})["PYTEST_ADDOPTS"] = value
                    self.assertCaught(docs, "PYTEST_ADDOPTS")

    def test_pytest_addopts_as_a_prefix_export_or_github_env_write(self):
        for mutate in (
            lambda job, step: _before_pytest(step, "PYTEST_ADDOPTS='-k nope' "),
            lambda job, step: _before_pytest(step, "env PYTEST_ADDOPTS='-m smoke' "),
            lambda job, step: _before_pytest(step, "export PYTEST_ADDOPTS='-k nope'\n"),
            lambda job, step: job["steps"].insert(0, {"name": "sneak", "run": 'echo "PYTEST_ADDOPTS=-k nope" >> "$GITHUB_ENV"'}),
            lambda job, step: step.setdefault("env", {}).update(PYTEST_PLUGINS="tools.evil"),
        ):
            with self.subTest(mutate=mutate):
                docs, job, step = _real()
                mutate(job, step)
                self.assertCaught(docs, "PYTEST_")

    def test_override_ini_addopts_and_collection_keys(self):
        for flags in ("-o addopts='-k nope'", "--override-ini=addopts='-m smoke'", "-oaddopts=--deselect=tools/test_x.py",
                      "--override-ini 'addopts=--ignore=tools/hooks'", "-o python_files=test_a*.py",
                      "-o norecursedirs=germinate", "-o addopts=$EXTRA"):
            with self.subTest(flags=flags):
                docs, _, step = _real()
                step["run"] = step["run"].replace("pytest tools/", f"pytest tools/ {flags}", 1)
                self.assertNotEqual(step["run"], _real()[2]["run"])
                self.assertCaught(docs)

    def test_narrowing_flags_on_the_command_line(self):
        for flags in ("-k 'not slow'", "-qm smoke", "--deselect tools/test_x.py::t", "--ignore=tools/hooks",
                      "--ignore-glob='tools/test_a*'", "--lf", "--sw", "--co", "--setup-plan", "--fixtures",
                      "-p tools.evil_plugin", "-c other.ini"):
            with self.subTest(flags=flags):
                docs, _, step = _real()
                step["run"] = step["run"].replace("pytest tools/", f"pytest tools/ {flags}", 1)
                self.assertCaught(docs)

    def test_bare_or_dot_pytest_as_a_second_step_or_as_the_main_step(self):
        for run in ("pytest", "python -m pytest -q -n auto", "pytest .", "py.test ./ -q", "pytest $GITHUB_WORKSPACE -q"):
            with self.subTest(second=run):
                docs, job, _ = _real()
                job["steps"].append({"name": "again", "run": run})
                self.assertCaught(docs, "repo root")
            with self.subTest(main=run):
                docs, _, step = _real()
                step["run"] = run
                self.assertCaught(docs, "exactly 1")

    def test_shell_or_working_directory_changes(self):
        for mutate in (
            lambda d, j, s: s.update(shell="python {0}"),
            lambda d, j, s: j.update(defaults={"run": {"shell": "pwsh"}}),
            lambda d, j, s: s.update({"working-directory": "projects/demo"}),
            lambda d, j, s: d["ci.yml"].update(defaults={"run": {"working-directory": "organism"}}),
            lambda d, j, s: s.update({"working-directory": "${{ github.workspace }}/projects/demo"}),
        ):
            with self.subTest(mutate=mutate):
                docs, job, step = _real()
                mutate(docs, job, step)
                self.assertCaught(docs)


def _wf(run: str, matrix: Any = None, **step_keys: Any) -> dict[str, dict]:
    """A synthetic main job inside the REAL job envelope (T-4385 r2: the envelope is allowlisted, so a
    fixture with an invented `runs-on` would be red for the envelope, not for what it tests)."""
    _, real_job, _ = _real()
    job = {k: v for k, v in real_job.items() if k != "steps"}
    # T-4385 r3 MF-B: the checkout (first) and setup-python steps are required before the main step.
    job["steps"] = [{"uses": "actions/checkout@v4"}, {"uses": "actions/setup-python@v5"},
                    {"name": "setup", "run": "pip install pytest pytest-xdist"}, {"name": "main", "run": run, **step_keys}]
    if matrix is not None:
        job["strategy"] = {**job.get("strategy", {}), "matrix": {"python-version": matrix}}
    return {"ci.yml": {"jobs": {"test": job}}}


class ReviewRoundOneHolesAreCaught(unittest.TestCase):
    """T-4333 review r1 (M1-M3, S1): each was GREEN on a copy of ci.yml before the fix."""

    assertCaught = MutationsOfTheRealWorkflowAreCaught.assertCaught

    def test_m1_a_bash_shell_that_never_runs_the_script(self):
        for level, shell in (("step", "bash -n {0}"), ("step", "bash -c true {0}"), ("job", "sh -c : {0}"),
                             ("workflow", "bash -c 'exit 0' {0}"), ("step", "bash {0} extra"),
                             ("step", "bash --noprofile {0} {0}"), ("step", "bash -o errexit {0}"),
                             ("step", "bash --rcfile /tmp/shim {0}"), ("step", "bash -"), ("step", "bash -s")):
            with self.subTest(level=level, shell=shell):
                docs, job, step = _real()
                if level == "step":
                    step["shell"] = shell
                else:
                    ({"job": job, "workflow": docs["ci.yml"]}[level]).setdefault("defaults", {})["run"] = {"shell": shell}
                self.assertCaught(docs, "shell")

    def test_m2_disabling_a_builtin_plugin(self):
        for where in ("cmd-space", "cmd-joined", "step-env", "prefix"):
            with self.subTest(where=where):
                docs, _, step = _real()
                if where == "cmd-space":
                    step["run"] = step["run"].replace("pytest tools/", "pytest tools/ -p no:unittest", 1)
                elif where == "cmd-joined":
                    step["run"] = step["run"].replace("pytest tools/", "pytest tools/ -pno:unittest", 1)
                elif where == "step-env":
                    step.setdefault("env", {})["PYTEST_ADDOPTS"] = "-p no:unittest"
                else:
                    _before_pytest(step, "PYTEST_ADDOPTS='-p no:python' ")
                self.assertCaught(docs, "disables plugin")

    def test_m3_bash_env_or_env_at_any_level_or_via_github_env(self):
        for var in ("BASH_ENV", "ENV"):
            for level in ("workflow", "job", "step"):
                with self.subTest(var=var, level=level):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})[var] = "/tmp/shim.sh"
                    self.assertCaught(docs, var)
            with self.subTest(var=var, via="GITHUB_ENV"):
                docs, job, _ = _real()
                job["steps"].insert(0, {"name": "sneak", "run": f'echo "{var}=/tmp/shim.sh" >> "$GITHUB_ENV"'})
                self.assertCaught(docs, var)

    def test_s1_commands_before_pytest_in_the_same_step(self):
        for lead in ("exit 0\n", "cd /tmp/decoy && ", "pushd /tmp/decoy\n", "python() { :; }\n",
                     "function python { :; }\n", "alias python=true\n", "source /tmp/shim.sh\n",
                     "pip install evil-plugin && ", "true; ", "echo $(exit 0); cd /tmp; "):
            with self.subTest(lead=lead):
                docs, _, step = _real()
                _before_pytest(step, lead)
                self.assertCaught(docs, "same step as pytest")


class ReviewRoundTwoHolesAreCaught(unittest.TestCase):
    """T-4333 review r2: each returned [] from ci_integrity_violations() before the fix. Every
    mutation lands next to the pytest command and asserts the rule it is meant to trip."""

    assertCaught = MutationsOfTheRealWorkflowAreCaught.assertCaught

    def test_r2_m1_a_redirect_writes_files_before_pytest(self):
        for lead in ("echo 'def pytest_sessionfinish(session, exitstatus): session.exitstatus = 0' >> tools/conftest.py\n",
                     "printf '[pytest]\\naddopts = -p no:unittest\\n' > pytest.ini\n",
                     "echo '' > tools/test_ci_integrity.py\n",
                     "set -e 2> pytest.ini\n",  # the one allowed leader, still refused for its redirect
                     "set -e < /tmp/x\n"):
            with self.subTest(lead=lead):
                docs, _, step = _real()
                _before_pytest(step, lead)
                self.assertCaught(docs, "redirects `")
        for tail in (" > tools/conftest.py", " 2> pytest.ini", " &> out.log"):
            with self.subTest(tail=tail):
                docs, _, step = _real()
                step["run"] = step["run"].rstrip("\n") + tail
                self.assertCaught(docs, "redirects `")

    def test_r2_m2_command_substitution_anywhere_in_the_step(self):
        for lead in ("echo `rm -f tools/test_*.py`\n", "printf '%s' \"$(rm -f tools/test_x.py)\"\n"):
            with self.subTest(lead=lead):
                docs, _, step = _real()
                _before_pytest(step, lead)
                self.assertCaught(docs, "command substitution")
        for arg in (' --junit-prefix="$(rm -f tools/test_x.py)"', " --junit-prefix=`id`"):
            with self.subTest(arg=arg):
                docs, _, step = _real()
                step["run"] = step["run"].replace("pytest tools/", "pytest tools/" + arg, 1)
                self.assertCaught(docs, "command substitution")

    def test_r2_m3_printf_v_path_and_python_env(self):
        docs, _, step = _real()
        _before_pytest(step, "printf -v PATH '%s' /tmp/shim\n")
        self.assertCaught(docs, "`printf -v PATH %s /tmp/shim` in the same step")
        for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"):
            for level in ("workflow", "job", "step"):
                with self.subTest(var=var, level=level):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})[var] = "tools/shim"
                    self.assertCaught(docs, f"env {var}")
            with self.subTest(var=var, via="GITHUB_ENV"):
                docs, job, _ = _real()
                job["steps"].insert(0, {"name": "sneak", "run": f'echo "{var}=tools/shim" >> "$GITHUB_ENV"'})
                self.assertCaught(docs, f"mentions {var}")

    def test_r2_leaders_other_than_set_are_refused(self):
        for lead in ("echo start\n", "printf 'x'\n", "mkdir -p out && ", "export FOO=1\n", "ulimit -n 4096\n",
                     "set -o errexit\n", "set -- a b\n", "set\n"):
            with self.subTest(lead=lead):
                docs, _, step = _real()
                _before_pytest(step, lead)
                self.assertCaught(docs, "in the same step as pytest")


class ReviewRoundThreeHolesAreCaught(unittest.TestCase):
    """T-4333 review r3: an expansion in the pytest argv injects flags `narrowing()` never
    reads. Each row stayed GREEN in both guards before the fix."""

    assertCaught = MutationsOfTheRealWorkflowAreCaught.assertCaught
    MSG = "a shell expansion or `${{ }}` expression in the pytest command"

    @staticmethod
    def _append(step: dict, text: str) -> None:
        old = step["run"]
        step["run"] = old.replace("python -m pytest tools/", "python -m pytest tools/ " + text, 1)
        assert step["run"] != old

    def test_r3_expansions_in_the_pytest_argv(self):
        rows = (
            ("${{ vars.PYTEST_ARGS }}", None),
            ("$EXTRA", ("step", {"EXTRA": "-p no:unittest"})),
            ("${EXTRA}", ("job", {"EXTRA": "-p no:unittest"})),
            ("${X:--k nope}", None),
            ("${X:=-p no:unittest}", None),
            ("${{ env.EXTRA }}", ("workflow", {"EXTRA": "-k nope"})),
            ("${{ matrix.extra }}", "matrix"),
            ("${X@P}", None),
            ("${a[X]}", None),
            ('"$EXTRA"', None),
        )
        for text, setup in rows:
            with self.subTest(text=text):
                docs, job, step = _real()
                self._append(step, text)
                if setup == "matrix":
                    job["strategy"]["matrix"]["include"] = [{"extra": "-p no:unittest"}]
                elif setup:
                    level, env = setup
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {}).update(env)
                self.assertCaught(docs, self.MSG)

    def test_r3_expansions_in_the_launch_prefix_or_a_junit_value_that_is_not_runner_temp(self):
        for lead in ("timeout $T ", "$PYWRAP ", "FOO=$X "):
            with self.subTest(lead=lead):
                docs, _, step = _real()
                _before_pytest(step, lead)
                self.assertCaught(docs, self.MSG)
        for junit in ("--junitxml=$RUNNER_TEMP/$X.xml", '--junitxml="$HOME/x.xml"', "--junit-xml ${{ vars.P }}/x.xml",
                      # Round-4 ruling: only the literal `${{ runner.temp }}/` prefix is exempt.
                      "--junitxml=$RUNNER_TEMP/x.xml", '--junit-xml="${RUNNER_TEMP}/x.xml"',
                      "--junitxml=${{ vars.X }}", '--junitxml="${{ runner.temp }}$X.xml"',
                      '--junitxml="${{ runner.temp }}/${{ vars.X }}.xml"', '--junitxml="${{runner.temp}}/x.xml"'):
            with self.subTest(junit=junit):
                docs, _, step = _real()
                self._append(step, junit)
                self.assertCaught(docs, self.MSG)

    def test_r3_confcutdir_and_pythonpath_override(self):
        for text in ("--confcutdir=tools/sub", "-o pythonpath=tools/shim"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                self.assertCaught(docs)

    def test_r3_a_runner_temp_report_path_stays_green(self):
        for junit in ('--junitxml="${{ runner.temp }}/x.xml"', '--junitxml "${{ runner.temp }}/x.xml"',
                      '--junit-xml="${{ runner.temp }}/pytest-tools.xml"'):
            with self.subTest(junit=junit):
                run = f"python -m pytest tools/ -q -n auto -o junit_family=xunit1 {junit}"
                self.assertEqual(ci_integrity_violations(_wf(run)), [])


class BraceAndGlobExpansionIsCaught(unittest.TestCase):
    """T-4373 (T-4333 round-4 reviewer): bash brace expansion and pathname globs inject
    pytest flags with no `$`, so the r3 `$` rule never saw them. Each row stayed GREEN in
    both guards before the fix (`pytest tools/ {-k,test_ok}` ran rc=0 over a failing test)."""

    assertCaught = MutationsOfTheRealWorkflowAreCaught.assertCaught
    _append = staticmethod(ReviewRoundThreeHolesAreCaught._append)
    MSG = "which holds a brace, glob or tilde character"

    def test_braces_in_the_pytest_argv(self):
        for text in ("{-k,nope}", "{-p,no:unittest}", "--{deselect,deselect}=tools/test_x.py", "-{k,}",
                     "{--co,}", "tools/{a,b}"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                self.assertCaught(docs, self.MSG)

    def test_globs_in_the_pytest_argv(self):
        # Unmatched, bash leaves the word as is; matched against a committed file named
        # `-p`, `--co` or `-k` it becomes that flag.
        for text in ("tools/*", "*", "-[p] no:unittest", "--c?", "-? nope", "tools/test_[a-z]*.py"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                self.assertCaught(docs, self.MSG)

    def test_braces_or_globs_in_the_launch_prefix_or_the_pytest_token(self):
        for old, new in (("python -m pytest", "{timeout,true} python -m pytest"),
                         ("python -m pytest", "python{,3} -m pytest"),
                         ("python -m pytest", "{/tmp/shim,/usr}/bin/pytest"),
                         ("python -m pytest", "/opt/py*/bin/pytest"),
                         ("python -m pytest", "./tool?/pytest")):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace(old, new, 1)
                self.assertNotEqual(step["run"], _real()[2]["run"])
                self.assertCaught(docs, self.MSG)

    def test_a_dollar_in_the_pytest_token_itself(self):
        # The r3 `$` rule read the prefix and argv but not the pytest token (T-4373 D-3).
        for new in ("$SHIM/pytest", "${X:-/tmp/shim}/bin/pytest"):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                self.assertCaught(docs, ReviewRoundThreeHolesAreCaught.MSG)

    def test_a_brace_or_glob_after_the_exempt_junit_prefix(self):
        for junit in ('--junitxml="${{ runner.temp }}/{a,b}.xml"', '--junitxml "${{ runner.temp }}/*.xml"',
                      '--junit-xml="${{ runner.temp }}/x?.xml"', "--junitxml=tools/{a,b}.xml"):
            with self.subTest(junit=junit):
                docs, _, step = _real()
                self._append(step, junit)
                self.assertCaught(docs, self.MSG)

    def test_quoting_is_not_visible_so_a_quoted_metacharacter_is_refused(self):
        # D-T4373-2: shlex(posix=True) strips quotes before the rule reads a word, so
        # `-W 'ignore:x*'` and an unquoted `-W ignore:x*` are the same word here. Refused
        # (fail closed); a legitimate need is a conscious guard edit. Accepted false reds
        # include a regex in `-o filterwarnings=...` and a quoted `~` (r1 nit).
        for text in ("-W 'ignore:x*'", "--junit-prefix='{a,b}'", "-k 'a or b'",
                     "-o 'filterwarnings=ignore:.*deprecated.*:DeprecationWarning'", "--junit-prefix='~x'"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                self.assertCaught(docs)
        docs, _, step = _real()
        self._append(step, "-W 'ignore:x*'")
        self.assertCaught(docs, self.MSG)

    def test_tilde_expands_to_a_home_that_env_can_set(self):
        # r1 M-1: `HOME: -ktest_ok` in env plus ` ~` ran rc=0 over a failing test; bash also
        # tilde-expands after `=` / `:` in assignment-like words (`-o addopts=~`).
        for level in ("step", "job", "workflow"):
            for text in ("~", "~/x", "-o addopts=~", "--rootdir=x:~"):
                with self.subTest(level=level, text=text):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})["HOME"] = "-ktest_ok"
                    self._append(step, text)
                    self.assertCaught(docs, self.MSG)
        for old, new in (("python -m pytest", "~/bin/pytest"), ("python -m pytest", "timeout ~ python -m pytest"),
                         ('"${{ runner.temp }}/', '"${{ runner.temp }}/~')):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace(old, new, 1)
                self.assertNotEqual(step["run"], _real()[2]["run"])
                self.assertCaught(docs, self.MSG)

    def test_an_at_file_argument(self):
        # r1 N-1: `pytest tools/ @tools/args.txt` with a committed `-k test_ok` ran rc=0.
        for text in ("@tools/args.txt", "-q @args", "-o addopts=@tools/args.txt"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                self.assertCaught(docs, "an @file whose committed lines")
        docs, _, step = _real()
        step.setdefault("env", {})["PYTEST_ADDOPTS"] = "@tools/args.txt"
        self.assertCaught(docs, "an @file whose committed lines")

    def test_extglob_and_process_substitution_are_refused(self):
        # Not in _BRACE_OR_GLOB on purpose: shlex splits `(` off as an operator, so the
        # pytest segment ends there and the step-shape rules refuse it (r1 nit). If a
        # tokenizer change ever keeps `@(x)` as one word, this goes red.
        for text in ("@(-k|x) nope", "+(-p) no:unittest", "!(x)", "<(echo -k nope)", "-k <(echo x)"):
            with self.subTest(text=text):
                docs, _, step = _real()
                self._append(step, text)
                found = ci_integrity_violations(docs)
                self.assertTrue(any("last command" in v or "same step as pytest" in v or "cannot be tokenized" in v
                                    for v in found), found)

    def test_env_that_changes_what_bash_executes_or_where_python_resolves(self):
        # r2 M-2: `SHELLOPTS: noexec` ran nothing and exited 0 (at job / workflow level the
        # outcome-check step too); PATH and BASH_FUNC_python%% shim `python`.
        for var, value in (("SHELLOPTS", "noexec"), ("SHELLOPTS", "errexit:noexec"), ("PATH", "tools/shim:/usr/bin"),
                           ("BASH_FUNC_python%%", "() { :; }"), ("BASH_FUNC_python", "() { :; }")):
            for level in ("step", "job", "workflow"):
                with self.subTest(var=var, value=value, level=level):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})[var] = value
                    self.assertCaught(docs, f"{level} env {var} ")
        for line, frag in (('echo "SHELLOPTS=noexec" >> "$GITHUB_ENV"', "mentions SHELLOPTS"),
                           ('echo "PATH=tools/shim:$PATH" >> "$GITHUB_ENV"', "mentions PATH"),
                           ("echo 'BASH_FUNC_python%%=() { :; }' >> \"$GITHUB_ENV\"", "mentions BASH_FUNC_*")):
            with self.subTest(line=line):
                docs, job, _ = _real()
                job["steps"].insert(0, {"name": "sneak", "run": line})
                self.assertCaught(docs, frag)

    def test_t4381_pythonoptimize_and_committed_file_shims_at_every_level(self):
        # F-1: PYTHONOPTIMIZE=1 turned a failing assert in a non-test helper module from
        # "1 failed" into "1 passed". F-2: LD_PRELOAD / LD_LIBRARY_PATH load a committed .so;
        # HOME moves the user site so a committed usercustomize.py runs.
        for var, value, frag in (("PYTHONOPTIMIZE", "1", "strips `assert`"), ("PYTHONOPTIMIZE", "2", "strips `assert`"),
                                 ("LD_PRELOAD", "tools/shim.so", "committed .so"),
                                 ("LD_LIBRARY_PATH", "tools/lib", "committed .so"),
                                 ("HOME", "tools/fakehome", "usercustomize.py")):
            for level in ("step", "job", "workflow"):
                with self.subTest(var=var, value=value, level=level):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})[var] = value
                    found = ci_integrity_violations(docs)
                    self.assertTrue(any(f"{level} env {var} " in v and frag in v for v in found), found)

    def test_t4381_pythonoptimize_via_github_env_and_a_github_path_write(self):
        for line, frag in (('echo "PYTHONOPTIMIZE=1" >> "$GITHUB_ENV"', "mentions PYTHONOPTIMIZE"),
                           ('echo "LD_PRELOAD=$PWD/tools/shim.so" >> "$GITHUB_ENV"', "mentions LD_PRELOAD"),
                           ('echo "HOME=$PWD/tools/fakehome" >> "$GITHUB_ENV"', "mentions HOME"),
                           ('echo "$PWD/tools/shim" >> "$GITHUB_PATH"', "mentions GITHUB_PATH (a $GITHUB_PATH write"),
                           ("echo tools/shim >> $GITHUB_PATH", "mentions GITHUB_PATH (a $GITHUB_PATH write"),
                           ('echo "${{ github.workspace }}/tools/shim" | tee -a "${GITHUB_PATH}"', "mentions GITHUB_PATH")):
            with self.subTest(line=line):
                docs, job, _ = _real()
                job["steps"].insert(0, {"name": "sneak", "run": line})
                found = ci_integrity_violations(docs)
                self.assertTrue(any("step 'sneak' " + frag in v for v in found), found)

    def test_t4381_a_github_path_mention_only_in_another_job_or_a_comment_stays_green(self):
        docs = copy.deepcopy(_parsed_real())
        docs["ci.yml"]["jobs"]["other"] = {"runs-on": "x", "steps": [{"name": "o", "run": 'echo x >> "$GITHUB_PATH"'}]}
        self.assertEqual(ci_integrity_violations(docs), [])
        docs, job, _ = _real()
        job["steps"].insert(1, {"name": "note", "run": "# never write to GITHUB_PATH here\ntrue"})  # r3: checkout first
        self.assertEqual(ci_integrity_violations(docs), [])

    def test_t4381_m1_interpreter_flags_outside_the_allowlist(self):
        # `python -O -m pytest` stripped a helper-module assert: "1 passed" (review r1 M-1).
        for old, new, flag in (("python -m pytest", "python -O -m pytest", "`-O`"),
                               ("python -m pytest", "python -OO -m pytest", "`-OO`"),
                               ("python -m pytest", "python -Om pytest", "`-Om`"),
                               ("python -m pytest", "python3 -O -m pytest", "`-O`"),
                               ("python -m pytest", "python -S -m pytest", "`-S`"),
                               ("python -m pytest", "python -I -m pytest", "`-I`"),
                               ("python -m pytest", "python -u -O -m pytest", "`-O`"),
                               ("python -m pytest", "python -uO -m pytest", "`-uO`"),
                               ("python -m pytest", "python -X importtime -m pytest", "`-X importtime`"),
                               ("python -m pytest", "python -Xfrozen_modules=off -m pytest", "`-Xfrozen_modules=off`"),
                               ("python -m pytest", "python -O -mpytest", "`-O`")):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace(old, new, 1)
                self.assertNotEqual(step["run"], _real()[2]["run"])
                found = ci_integrity_violations(docs)
                self.assertTrue(any(f"interpreter flag {flag}:" in v for v in found), found)

    def test_t4381_m1_allowlisted_interpreter_flags_and_other_pytest_tokens_stay_green(self):
        for new in ("python -u -m pytest", "python -um pytest", "python -X dev -m pytest", "python -Xdev -m pytest",
                    "python -W error -m pytest", "python -Werror -m pytest", "python -u -X dev -W error -mpytest",
                    "python3 -m pytest", "pytest", "py.test", "timeout 900 python -m pytest"):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                self.assertEqual(ci_integrity_violations(docs), [])

    def test_t4381_m2_env_split_string_rebuilds_python_o(self):
        # r2 M-2: each ran a failing helper-module assert as "1 passed" (rc=0).
        for new in ('env -S "python3 -O" -m pytest', 'env --split-string="python3 -O" -m pytest',
                    'env -S"python3 -O" -m pytest', "env -S 'python3\\_-O' -m pytest"):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                found = ci_integrity_violations(docs)
                self.assertTrue(any("launches pytest through `env`: only an optional `timeout" in v for v in found), found)

    def test_t4381_m2_other_wrappers_and_non_bare_interpreters(self):
        for new, frag in (("env -i python -m pytest", "through `env`:"),
                          ("env -u HOME python -m pytest", "through `env`:"),
                          ("nice python -m pytest", "through `nice`:"),
                          ("command -p python -m pytest", "through `command`:"),
                          ("stdbuf -oL python -m pytest", "through `stdbuf`:"),
                          ("timeout --foreground 900 python -m pytest", "through `timeout --foreground`:"),
                          ("timeout 900 env python -m pytest", "through `env`:"),
                          ("tools/python -m pytest", "interpreter `tools/python`:"),
                          ("./python3 -m pytest", "interpreter `./python3`:"),
                          ("/usr/bin/python3 -m pytest", "interpreter `/usr/bin/python3`:"),
                          ("python3-dbg -m pytest", "interpreter `python3-dbg`:"),
                          ("tools/pytest", "pytest token `tools/pytest`:"),
                          ("python pytest", "without `-m`")):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                found = ci_integrity_violations(docs)
                self.assertTrue(any(frag in v for v in found), found)

    def test_t4381_m2_accepted_launch_shapes_stay_green(self):
        for new in ("timeout 25m python -m pytest", "timeout -k 30 1500 python -m pytest",
                    "timeout --preserve-status 900 python -m pytest", "timeout --kill-after=30 -s KILL 900 python -m pytest",
                    "timeout --signal=TERM 25m pytest", "python3.11 -m pytest", "python -m pytest",
                    "FOO=1 python3 -um pytest", "py.test"):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                self.assertEqual(ci_integrity_violations(docs), [])
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])

    def test_today_and_the_exempt_junit_forms_stay_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        for junit in ('--junitxml="${{ runner.temp }}/x.xml"', '--junitxml "${{ runner.temp }}/x.xml"',
                      '--junit-xml="${{ runner.temp }}/pytest-tools.xml"'):
            with self.subTest(junit=junit):
                run = f"python -m pytest tools/ -q -n auto -o junit_family=xunit1 {junit}"
                self.assertEqual(ci_integrity_violations(_wf(run)), [])

    def test_t4385_d4_a_flag_that_eats_the_m(self):
        # `python -W -m pytest`: -W takes `-m` as its filter, so `pytest` runs as a FILE (rc=0 with a
        # committed ./pytest, runtime-proven). The launch rule saw prefix[-1] == '-m' and was green.
        for new, flag in (("python -W -m pytest", "`-W -m`"), ("python -u -W -m pytest", "`-W -m`"),
                          ("python -W -um pytest", "`-W -um`"), ("python -X -m pytest", "`-X -m`"),
                          ("python -W-m pytest", "`-W-m`")):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                found = ci_integrity_violations(docs)
                self.assertTrue(any(f"interpreter flag {flag}:" in v for v in found), found)
                if " -" in flag:
                    self.assertTrue(any("runs as a FILE path" in v for v in found), found)
        for new in ("python -W ignore::DeprecationWarning -m pytest", "python -Wd -m pytest", "python -We -m pytest"):
            with self.subTest(new=new):
                docs, _, step = _real()
                step["run"] = step["run"].replace("python -m pytest", new, 1)
                self.assertEqual(ci_integrity_violations(docs), [])

    def test_t4385_d7_ps4_and_unsecure_commands_at_every_level_and_via_github_env(self):
        for var, value, frag in (("PS4", "$(touch tools/conftest.py)+ ", "`set -x` trace line"),
                                 ("ACTIONS_ALLOW_UNSECURE_COMMANDS", "true", "`::set-env::`")):
            for level in ("step", "job", "workflow"):
                with self.subTest(var=var, level=level):
                    docs, job, step = _real()
                    block = {"workflow": docs["ci.yml"], "job": job, "step": step}[level]
                    block.setdefault("env", {})[var] = value
                    found = ci_integrity_violations(docs)
                    self.assertTrue(any(f"{level} env {var} " in v and frag in v for v in found), found)
            with self.subTest(var=var, via="GITHUB_ENV"):
                docs, job, _ = _real()
                job["steps"].insert(0, {"name": "sneak", "run": f'echo "{var}={value}" >> "$GITHUB_ENV"'})
                self.assertTrue(any(f"step 'sneak' mentions {var} " in v for v in ci_integrity_violations(docs)))


class T4385MidLineHashInTheMainStep(unittest.TestCase):
    """shlex treats a mid-word `#` as a comment start and bash does not: `--junit-prefix=a# || true`
    was GREEN here while bash ran the `|| true` and a failing suite exited 0 (runtime-proven)."""

    FRAG = "holds a `#` outside a whole comment line"

    def test_a_mid_line_hash_is_red(self):
        for tail in (" --junit-prefix=a# || true", " -k x# ; true", " tools/# ; exit 0",
                     " -k 'a#b'", " --junit-prefix='a#b'", ' --junit-prefix="a#b"', " # trailing comment"):
            with self.subTest(tail=tail):
                docs, _, step = _real()
                step["run"] = step["run"].rstrip("\n") + tail
                found = ci_integrity_violations(docs)
                self.assertTrue(any("step 'Run tools test suite' " + self.FRAG in v for v in found), found)

    def test_whole_line_comments_and_todays_line_stay_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        docs, _, step = _real()
        self.assertNotIn("#", step["run"])
        step["run"] = "# why this runs\n  # indented note\n" + step["run"]
        self.assertEqual(ci_integrity_violations(docs), [])


def _insert_near_main(job: dict, step: dict, new: dict, after: bool = False) -> None:
    """Insert `new` right before (or right after) the main step in the real job."""
    i = next(k for k, s in enumerate(job["steps"]) if s is step)
    job["steps"].insert(i + 1 if after else i, new)


class T4385NonMainStepsOfTheMainJobAreInert(unittest.TestCase):
    """D2: any other step of the main job is inert by allowlist of shape, so no spelling of a
    $GITHUB_ENV / $GITHUB_PATH write is expressible. The name scan was evadable (T-4381 F-A)."""

    # The three T-4381 review evasions, each GREEN before T-4385 (the second runtime-proven:
    # rc=0 and GITHUB_PATH received tools/shim).
    EVASIONS = (
        "echo tools/shim >> \"$(env | sed -n 's/^GITHUB_P[A]TH=//p')\"",
        'f="\n#$GITHUB_PATH"\necho tools/shim >> "${f#?#}"',
        'echo PYTEST_ADD""OPTS=-knope >> "$GITHUB_ENV"',
    )
    RED = (
        "python -c \"import os;open(os.environ['GITHUB_ENV'],'a').write('BASH_ENV=x')\"",
        "printenv GITHUB_PATH | xargs -I{} sh -c 'echo tools/shim >> {}'",
        "git -c alias.x='!true' x",
        "git config --global core.pager cat",
        "git config --global alias.st status",
        "git -c core.fsmonitor=tools/x gc",
        "pip install ./tools/evil",
        "python -m pip install -r tools/reqs.txt",
        "pip install evil.whl",
        "pip install --index-url https://example.invalid/simple y",
        "pip install pytest>=8",
        "python -O tools/ci_fleet_account_reconcile.py --check",
        "python tools/x.py a#; true",
        "FOO=1 python tools/x.py",
        "true | true",
        "true || true",
        "mkdir -p tools/shim",
        "echo hi",
    )

    def assertD2(self, docs: dict, name: str = "sneak") -> None:
        found = ci_integrity_violations(docs)
        self.assertTrue(any(v.startswith("ci.yml:") and f"step {name!r} " in v and "(T-4385" in v for v in found), found)

    def test_the_review_evasions_are_red_for_the_inert_rule(self):
        for run in self.EVASIONS:
            for after in (False, True):
                with self.subTest(run=run, after=after):
                    docs, job, step = _real()
                    sneak = {"name": "sneak", "run": run}
                    _insert_near_main(job, step, sneak, after=after)
                    self.assertTrue(inert_step_problems(docs["ci.yml"], job, sneak))
                    self.assertD2(docs)

    def test_other_writers_are_red(self):
        for run in self.RED:
            with self.subTest(run=run):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "run": run})
                self.assertD2(docs)

    def test_step_env_shell_and_untrusted_actions_are_red(self):
        for extra in ({"env": {"BASH_ENV": "tools/x.sh"}}, {"env": {"FOO": "1"}}, {"shell": "python {0}"},
                      {"shell": "bash -c true {0}"}):
            with self.subTest(extra=extra):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "run": "git gc --auto", **extra})
                self.assertD2(docs)
        for uses in ("someone/action@v1", "./.github/actions/x", "docker://alpine", "actions/cache@v4"):
            with self.subTest(uses=uses):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "uses": uses})
                self.assertD2(docs)

    def test_todays_shapes_and_their_spellings_stay_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        for run in ("set -e\ngit config --global user.name x", "git gc --auto", "git -c gc.auto=1 gc --quiet",
                    "pip install pyyaml==6.0.2", "python3 -m pip install -q pytest", "python ./tools/x.py --flag",
                    'python tools/check_suite_outcome_floor.py "${{ runner.temp }}/a.xml"', "true",
                    "# a note naming GITHUB_ENV\ntrue",
                    "git config --global user.email a@b && git config --global init.defaultBranch main"):
            with self.subTest(run=run):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "ok", "run": run})
                self.assertEqual(ci_integrity_violations(docs), [])
        # T-4385 r2: exactly today's refs (a @v5 / @v6 bump is a conscious edit of _ACTIONS). r3 MF-B: each
        # is required exactly once, so the row renames the real step instead of inserting a second one.
        for uses in ("actions/checkout@v4", "actions/setup-python@v5"):
            with self.subTest(uses=uses):
                docs, job, step = _real()
                next(s for s in job["steps"] if s.get("uses") == uses)["name"] = "ok"
                self.assertEqual(ci_integrity_violations(docs), [])

    def test_the_same_lines_in_another_job_stay_green(self):
        docs = copy.deepcopy(_parsed_real())
        docs["ci.yml"]["jobs"]["other"] = {"runs-on": "x", "steps": [
            {"name": "o", "run": r} for r in (*self.EVASIONS, *self.RED)] + [{"uses": "someone/action@v1"}]}
        self.assertEqual(ci_integrity_violations(docs), [])


class T4385R1ReboundHolesAreCaught(unittest.TestCase):
    """T-4385 review r1 (REBOUND): the inert allowlist had wildcard families (`gc.*`, pip names by
    suffix list) and the tokenizer did not read the script the way bash does (backslash-newline).
    Each M-/N- row was GREEN on 1edd17211 (M-1..M-4 runtime-proven by the reviewer)."""

    assertD2 = T4385NonMainStepsOfTheMainJobAreInert.assertD2

    def _sneak(self, run: str, **keys: Any) -> dict:
        docs, job, step = _real()
        _insert_near_main(job, step, {"name": "sneak", "run": run, **keys})
        return docs

    def test_m1_git_config_keys_are_an_exact_list(self):
        for run in ("git -c gc.recentObjectsHook='touch marker' gc --auto",
                    "git -c gc.pruneExpire=now gc --auto", "git -c gc.auto=x gc --auto",
                    "git -c gc.autoDetach=maybe gc", "git -c gc.autoPackLimit=-1 gc", "git -c gc.auto gc"):
            with self.subTest(run=run):
                self.assertD2(self._sneak(run))

    def test_m2_a_backslash_newline_continuation_is_refused(self):
        for run in ("pip install evil.w\\\nhl", "python -m pip install demo\\\n.tbz", "true \\\n",
                    "git gc --auto\r", "pip install pyyaml\x0bx"):
            with self.subTest(run=run):
                self.assertD2(self._sneak(run))
        # The main step: `-\` + newline + `knope` is `-knope` to bash.
        docs, _, step = _real()
        step["run"] = step["run"].rstrip("\n") + " -\\\nknope"
        found = ci_integrity_violations(docs)
        self.assertTrue(any("backslash-newline" in v for v in found), found)
        # An honest continuation in the main step is an accepted false red (it was green before r1).
        found = ci_integrity_violations(_wf("set -x\npython -m pytest tools/ \\\n  -q -n auto"))
        self.assertTrue(any("step 'main' mentions pytest" in v and "backslash-newline" in v for v in found), found)

    def test_m3_pip_arguments_must_be_plain_index_names(self):
        for arg in ("demo.tbz", "demo.txz", "demo.tlz", "demo.tar.lz", "demo.tar.lzma", "demo.tar.bz2",
                    "demo.tar.xz", "demo.tar.gz", "zope.interface", "bugs", "tools", "pyyaml==6.0rc1",
                    "demo-", "a@b"):
            for prefix in ("pip install", "python -m pip install --upgrade"):
                with self.subTest(run=f"{prefix} {arg}"):
                    self.assertD2(self._sneak(f"{prefix} {arg}"))

    def test_m4_a_non_main_step_runs_from_the_repo_root(self):
        self.assertD2(self._sneak("python -m pip install pyyaml", **{"working-directory": "projects"}))
        self.assertD2(self._sneak("python -m pip install pyyaml", **{"working-directory": "${{ github.workspace }}/x"}))

    def test_m5_the_shell_is_a_bare_bash_or_sh(self):
        for shell in ("tools/bash {0}", "./tools/bash", "tools/sh -e {0}", "/tmp/bash"):
            with self.subTest(shell=shell):
                self.assertD2(self._sneak("true", shell=shell))
        for shell in ("tools/bash {0}", "./tools/bash"):
            with self.subTest(job_default=shell):
                docs, job, _ = _real()
                job["defaults"] = {"run": {"shell": shell}}
                found = ci_integrity_violations(docs)
                self.assertTrue(any("step 'Run tools test suite' runs under job defaults shell" in v for v in found), found)
                self.assertTrue(any("(T-4385)" in v and "job defaults shell" in v for v in found), found)

    def test_n1_a_git_config_value_starting_with_a_dash(self):
        for run in ("git config --global user.name --get-all", "git config --global init.defaultBranch -x"):
            with self.subTest(run=run):
                self.assertD2(self._sneak(run))

    def test_n2_no_pytest_outside_the_main_step_of_the_main_job(self):
        for run in ("python -m pytest tools/test_x.py -q", "pytest tools/test_x.py", "py.test tools/test_x.py",
                    "python3 -m pytest --version"):
            with self.subTest(run=run):
                self.assertD2(self._sneak(run))

    def test_n3_pip_install_with_no_name(self):
        for run in ("pip install --upgrade", "python -m pip install -q"):
            with self.subTest(run=run):
                self.assertD2(self._sneak(run))

    def test_the_real_workflow_and_tight_shapes_stay_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        for run in ("git -c gc.auto=6700 -c gc.autoPackLimit=50 -c gc.autoDetach=false gc --auto --quiet",
                    "git -c gc.autoDetach=true gc", "pip install pytest-xdist pyyaml==6.0.2 -U",
                    "python3.12 -m pip install --upgrade pip pyyaml pytest pytest-xdist",
                    "git config --global user.email ci@pantheon.local"):
            # T-4385 r2 MF-2: no working-directory at all in the main job (`.` is an accepted false red).
            for keys in ({}, {"shell": "bash --noprofile --norc -eo pipefail {0}"}):
                with self.subTest(run=run, keys=keys):
                    self.assertEqual(ci_integrity_violations(self._sneak(run, **keys)), [])


class T4385R2EnvelopeIsAllowlisted(unittest.TestCase):
    """T-4385 review r2 (REBOUND, reviewer-runtime-proven MF-1 / MF-2 / N-a / N-b / N-c) and the mason
    ruling that the main job's ENVELOPE is in scope. Every red row is a mutation of the real parsed
    ci.yml (or, for N-b, of its text) and asserts a `(T-4385 r2)` message, so each was GREEN or
    CRASHED on e663f3971."""

    R2 = "(T-4385 r2)"
    assertD2 = T4385NonMainStepsOfTheMainJobAreInert.assertD2

    def assertR2(self, docs: dict, fragment: str = "") -> None:
        found = ci_integrity_violations(docs)
        self.assertTrue(any(self.R2 in v and fragment in v for v in found), found)

    @staticmethod
    def _action(job: dict, prefix: str) -> dict:
        return next(s for s in job["steps"] if str(s.get("uses", "")).startswith(prefix))

    # --- MF-1: no env on an action step --------------------------------------------------------

    def test_mf1_env_on_an_action_step(self):
        for action in ("actions/checkout", "actions/setup-python"):
            for env in ({"NODE_OPTIONS": "--require ./tools/x.js"}, {"FOO": "1"}, {}):
                with self.subTest(action=action, env=env):
                    docs, job, _ = _real()
                    self._action(job, action)["env"] = env
                    self.assertR2(docs, "sets `env` on the `uses:")
        docs, job, _ = _real()
        self._action(job, "actions/checkout")["env"] = {"NODE_OPTIONS": "--require ./tools/x.js"}
        self.assertR2(docs, "step env NODE_OPTIONS ")

    # --- MF-2: no working-directory anywhere in the main job -----------------------------------

    def test_mf2_any_working_directory_at_any_level_of_the_main_job(self):
        for value in ("lnk/..", "lnk/../", "projects/..", ".", "./", "${{ github.workspace }}"):
            for level in ("main step", "step before", "step after", "job defaults", "workflow defaults"):
                with self.subTest(value=value, level=level):
                    docs, job, step = _real()
                    if level == "main step":
                        step["working-directory"] = value
                    elif level.startswith("step"):
                        _insert_near_main(job, step, {"name": "sneak", "run": "true", "working-directory": value},
                                          after=level == "step after")
                    elif level == "job defaults":
                        job["defaults"] = {"run": {"working-directory": value}}
                    else:
                        docs["ci.yml"]["defaults"] = {"run": {"working-directory": value}}
                    self.assertR2(docs, f"working-directory {value!r}")

    def test_mf2_the_path_resolves_physically_so_no_textual_root_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "projects" / "sub").mkdir(parents=True)
            (root / "lnk").symlink_to("projects/sub")
            self.assertEqual(posixpath.normpath("lnk/.."), ".")
            cwd = subprocess.run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=f"{tmp}/lnk/..",
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(os.path.realpath(cwd), os.path.realpath(root / "projects"))
        self.assertFalse(_is_root_dir("lnk/.."))
        self.assertFalse(_is_root_dir("tools/.."))
        for value in (".", "./", "${{ github.workspace }}"):
            self.assertTrue(_is_root_dir(value), value)  # the backstop steps' shared rule, other files

    def test_mf2_a_dot_dot_target_of_the_main_pytest(self):
        docs, _, step = _real()
        step["run"] = step["run"].replace("pytest tools/", "pytest lnk/../tools/", 1)
        self.assertR2(docs, "`..` component")

    # --- N-a / N-b / N-c ------------------------------------------------------------------------

    def test_na_a_non_string_run_is_a_named_violation_not_a_crash(self):
        for value in (True, False, 1, None, ["python -m pytest tools/"], {"a": 1}):
            for where in ("step before", "step after", "main step", "other job"):
                with self.subTest(value=value, where=where):
                    docs, job, step = _real()
                    if where.startswith("step"):
                        _insert_near_main(job, step, {"name": "sneak", "run": value}, after=where == "step after")
                    elif where == "main step":
                        step["run"] = value
                    else:
                        docs["ci.yml"]["jobs"]["other"] = {"runs-on": "x", "steps": [{"name": "sneak", "run": value}]}
                    self.assertR2(docs, "not a string")
        steps = load_workflow_text("jobs:\n  t:\n    steps:\n      - run: yes\n      - run: true\n")["jobs"]["t"]["steps"]
        self.assertEqual([s["run"] for s in steps], [True, True])

    def test_nb_duplicate_or_merge_keys_are_refused_by_the_loader(self):
        text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        real_run = "        run: python -m pytest tools/"
        rows = (
            ("second run", real_run, '        run: "true"\n' + real_run),
            # T-4763: the main step has no env of its own any more, so the duplicate is two inserted ones.
            ("second env", real_run, "        env:\n          PYTEST_ADDOPTS: -k nope\n        env: {}\n" + real_run),
            ("second runs-on", "    runs-on: [self-hosted", "    runs-on: ubuntu-latest\n    runs-on: [self-hosted"),
            ("second jobs", "\njobs:\n", "\njobs: {}\njobs:\n"),
            ("merge key", real_run, '        <<: {run: "true"}\n' + real_run),
        )
        for label, old, new in rows:
            with self.subTest(label):
                mutated = text.replace(old, new, 1)
                self.assertNotEqual(mutated, text)
                with self.assertRaises(yaml.YAMLError) as caught:
                    load_workflow_text(mutated)
                self.assertIn(self.R2, str(caught.exception))
        # Why: plain safe_load keeps the LAST key silently, so the guard judged the real step.
        silent = yaml.safe_load(text.replace(real_run, '        run: "true"\n' + real_run, 1))
        self.assertEqual(silent, yaml.safe_load(text))
        self.assertEqual(_load_workflows()["ci.yml"], yaml.safe_load(text))  # the real file loads strictly

    def test_nc_and_with_exact_action_refs_and_inputs(self):
        rows = (
            ("actions/checkout", {"uses": "actions/checkout"}), ("actions/checkout", {"uses": "actions/checkout@main"}),
            ("actions/checkout", {"uses": "actions/checkout@v5"}), ("actions/checkout", {"uses": "Actions/checkout@v4"}),
            ("actions/setup-python", {"uses": "actions/setup-python"}),
            ("actions/setup-python", {"uses": "actions/setup-python@v5.1.0"}),
            ("actions/checkout", {"with": {"fetch-depth": 0, "ref": "evil-branch"}}),
            ("actions/checkout", {"with": {"fetch-depth": 0, "repository": "someone/fork"}}),
            ("actions/checkout", {"with": {"fetch-depth": 0, "path": "projects/sub"}}),
            ("actions/checkout", {"with": {"fetch-depth": 1}}), ("actions/checkout", {"with": {"fetch-depth": False}}),
            ("actions/checkout", {"with": {"fetch-depth": "0"}}), ("actions/checkout", {"with": "fetch-depth=0"}),
            ("actions/setup-python", {"with": {"python-version": "3.12"}}),
            ("actions/setup-python", {"with": {"python-version-file": "tools/.python-version"}}),
            ("actions/setup-python", {"with": {"python-version": "${{ matrix.python-version }}", "cache": "pip"}}),
            ("actions/setup-python", {"with": {"python-version": "${{ matrix.python-version }}",
                                              "update-environment": False}}),
        )
        for action, patch in rows:
            with self.subTest(action=action, patch=patch):
                docs, job, _ = _real()
                self._action(job, action).update(patch)
                self.assertR2(docs)
        for uses in ("actions/checkout", "actions/checkout@v5", "actions/setup-python@main"):
            with self.subTest(inserted=uses):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "uses": uses})
                self.assertR2(docs, f"uses `{uses}`")

    def test_step_keys_that_skip_or_forgive_a_non_main_step(self):
        for key, value in (("if", "false"), ("continue-on-error", True), ("id", "x"), ("timeout-minutes", 1)):
            for action in ("actions/checkout", "actions/setup-python"):
                with self.subTest(key=key, action=action):
                    docs, job, _ = _real()
                    self._action(job, action)[key] = value
                    self.assertR2(docs, f"has step key `{key}`")
            with self.subTest(key=key, step="run"):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "run": "pip install pyyaml", key: value})
                self.assertR2(docs, f"has step key `{key}`")
        docs, job, step = _real()
        _insert_near_main(job, step, {"name": "sneak"})
        self.assertR2(docs, "neither `run:` nor `uses:`")

    # --- the job / workflow envelope ------------------------------------------------------------

    def test_job_keys_outside_the_allowlist(self):
        for key, value in (("container", "python:3.12"), ("services", {"db": {"image": "postgres"}}),
                           ("defaults", {"run": {"shell": "bash"}}), ("permissions", {"contents": "read"}),
                           ("environment", "prod"), ("needs", []), ("if", "true"), ("continue-on-error", False),
                           ("outputs", {"a": "b"}), ("concurrency", "x")):
            with self.subTest(key=key):
                docs, job, _ = _real()
                job[key] = value
                self.assertR2(docs, f"has job key `{key}`")
        for env in ({"LANG": "C.UTF-8"}, {"GIT_CONFIG_COUNT": "1"}, "${{ fromJson(vars.E) }}"):
            with self.subTest(env=env):
                docs, job, _ = _real()
                job["env"] = env
                self.assertR2(docs, "job-level `env`")

    def test_runs_on_is_exact(self):
        for value in ("ubuntu-latest", "self-hosted", ["self-hosted", "linux", "x64"],
                      ["self-hosted", "linux", "x64", "pve2", "gpu"], ["pve2", "self-hosted", "linux", "x64"],
                      "${{ vars.RUNNER }}", None):
            with self.subTest(value=value):
                docs, job, _ = _real()
                job["runs-on"] = value
                self.assertR2(docs, "runs on")

    def test_strategy_and_matrix_keys(self):
        for mutate, frag in (
            (lambda s: s.update({"max-parallel": 1}), "strategy key `max-parallel`"),
            (lambda s: s["matrix"].update(include=[{"python-version": "3.12", "extra": "-k nope"}]), "matrix key `include`"),
            (lambda s: s["matrix"].update(os=["a"]), "matrix key `os`"),
            (lambda s: s.update(matrix="${{ fromJson(vars.M) }}"), "non-mapping matrix"),
        ):
            with self.subTest(frag=frag):
                docs, job, _ = _real()
                mutate(job["strategy"])
                self.assertR2(docs, frag)

    def test_workflow_env_and_defaults(self):
        for env in ({"LANG": "C.UTF-8"}, {"PIP_NO_INDEX": "1"}):
            with self.subTest(env=env):
                docs, _, _ = _real()
                docs["ci.yml"]["env"] = env
                self.assertR2(docs, "workflow-level `env`")
        for defaults in ({"run": {"shell": "bash"}, "other": 1}, {"run": {"shell": "bash", "x": 1}}, "bash"):
            with self.subTest(defaults=defaults):
                docs, _, _ = _real()
                docs["ci.yml"]["defaults"] = defaults
                self.assertR2(docs, "holds more than `run.shell`")

    ITEM7 = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "tools/x.sh",
             "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='tools/x.sh'", "GIT_CONFIG_GLOBAL": "tools/gitconfig",
             "GIT_CONFIG": "tools/gitconfig", "PIP_FIND_LINKS": "tools/wheels", "PIP_NO_INDEX": "1",
             "PIP_CONFIG_FILE": "tools/pip.conf", "PIP_INDEX_URL": "https://example.invalid/simple",
             "NODE_OPTIONS": "--require ./tools/x.js"}

    def test_item7_git_pip_and_node_env_at_every_level(self):
        for var, value in self.ITEM7.items():
            for level in ("workflow", "job", "main step", "run step", "checkout step"):
                with self.subTest(var=var, level=level):
                    docs, job, step = _real()
                    env = {var: value}
                    if level == "workflow":
                        docs["ci.yml"]["env"] = env
                    elif level == "job":
                        job["env"] = env
                    elif level == "main step":
                        step["env"] = {**step.get("env", {}), **env}
                    elif level == "run step":
                        _insert_near_main(job, step, {"name": "sneak", "run": "pip install pyyaml", "env": env})
                    else:
                        self._action(job, "actions/checkout")["env"] = env
                    self.assertR2(docs, f"env {var} ")

    def test_the_main_step_env_and_keys_are_exact(self):
        for env in ({"LANG": "C.UTF-8"}, {"PYTEST_ADDOPTS": "-p no:cacheprovider"},
                    # T-4763: a secret (even the retired codex-config read PAT's shape) is refused too.
                    {"READ_PAT": "${{ secrets.READ_PAT }}"}, {"READ_PAT": "literal"}):
            with self.subTest(env=env):
                docs, _, step = _real()
                step["env"] = {**step.get("env", {}), **env}
                self.assertR2(docs, "the main step's env is exactly")
        docs, _, step = _real()
        step["env"] = "${{ fromJson(vars.E) }}"
        self.assertR2(docs, "non-mapping step `env`")
        for key, value in (("id", "x"), ("timeout-minutes", 1), ("with", {"a": 1})):
            with self.subTest(key=key):
                docs, _, step = _real()
                step[key] = value
                self.assertR2(docs, f"has step key `{key}`: the main step may have only")

    def test_pip_names_are_an_exact_list(self):
        for run in ("pip install pytest-randomly", "pip install evil-plugin", "pip install PyYAML",
                    "python -m pip install --upgrade pip pyyaml pytest pytest-xdist pytest-custom-exit-code",
                    "pip install pytest-xdist==3.6.1 hypothesis"):
            with self.subTest(run=run):
                docs, job, step = _real()
                _insert_near_main(job, step, {"name": "sneak", "run": run})
                self.assertD2(docs)

    def test_todays_envelope_and_its_benign_spellings_stay_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        for label, mutate in (
            ("empty job env", lambda d, j, s: j.update(env={})),
            ("empty workflow env", lambda d, j, s: d["ci.yml"].update(env={})),
            ("workflow default shell", lambda d, j, s: d["ci.yml"].update(defaults={"run": {"shell": "bash"}})),
            ("no timeout", lambda d, j, s: j.pop("timeout-minutes")),
            ("no strategy", lambda d, j, s: j.pop("strategy")),
            ("checkout without with", lambda d, j, s: self._action(j, "actions/checkout").pop("with")),
            ("pinned pip", lambda d, j, s: _insert_near_main(j, s, {"name": "p", "run": "pip install pytest-xdist==3.6.1"})),
            ("main step if", lambda d, j, s: s.update({"if": "success()"})),
        ):
            with self.subTest(label):
                docs, job, step = _real()
                mutate(docs, job, step)
                self.assertEqual(ci_integrity_violations(docs), [])


# T-4385 r3 MF-A: GitHub substitutes `${{ }}` on comment lines too, so this comment injects
# `exit 0` as a new bash line (reviewer runtime proof: pytest never ran, the step exited 0).
_R3_INJECT = "# ${{ fromJSON('\"\\nexit 0\\n\"') }}"


class T4385R3RawExpressionsAndStepOrder(unittest.TestCase):
    """T-4385 review r3 (REBOUND). MF-A: a `${{ }}` on a `#` comment line of any step of the main job
    (dropped by _code() before every check, substituted by GitHub before bash runs). MF-B: the checkout
    and setup-python steps are required, once each, in order before the main step. Nit: the main step
    runs under bash only. Every red row asserts a `(T-4385 r3)` message, so each was GREEN on 595f99e20."""

    R3 = "(T-4385 r3)"
    STEPS = ("Run tools test suite", "Configure git identity", "Keep the workspace repo packed",
             "Install test dependencies", "Check every shell test suite passed",
             "Check the Python suite ran its committed floor")
    COMMENTS = (_R3_INJECT, "# ${{ github.event.head_commit.message }}", "    " + _R3_INJECT,
                "#${{ github.event.pull_request.title }}", "# ${{runner.temp}}")

    def assertR3(self, docs: dict, fragment: str = "") -> None:
        found = ci_integrity_violations(docs)
        self.assertTrue(any(self.R3 in v and fragment in v for v in found), found)

    @staticmethod
    def _named(job: dict, name: str) -> dict:
        return next(s for s in job["steps"] if s.get("name") == name)

    # --- MF-A --------------------------------------------------------------------------------

    def test_mfa_an_expression_on_a_comment_line_of_any_step_of_the_main_job(self):
        for comment in self.COMMENTS:
            for name in self.STEPS:
                for where in ("before", "after"):
                    with self.subTest(comment=comment, step=name, where=where):
                        docs, job, _ = _real()
                        s = self._named(job, name)
                        s["run"] = f"{comment}\n{s['run']}" if where == "before" else f"{s['run']}\n{comment}"
                        self.assertR3(docs, "`${{`")

    def test_mfa_the_one_literal_expression_stays_green(self):
        self.assertEqual(ci_integrity_violations(_load_workflows()), [])
        docs, job, _ = _real()
        s = self._named(job, "Configure git identity")
        s["run"] = "# the report lands in ${{ runner.temp }}\n" + s["run"]
        self.assertEqual(ci_integrity_violations(docs), [])

    # --- MF-B --------------------------------------------------------------------------------

    def test_mfb_checkout_and_setup_python_once_each_in_order_before_the_main_step(self):
        def action(steps: list, prefix: str) -> dict:
            return next(s for s in steps if str(s.get("uses", "")).startswith(prefix))

        def move(steps: list, s: dict, to: int) -> None:
            steps.remove(s)
            steps.insert(to if to >= 0 else len(steps) + 1 + to, s)

        cases = {
            "drop_checkout": (lambda st, m: st.remove(action(st, "actions/checkout")), "actions/checkout@v4"),
            "checkout_after_main": (lambda st, m: move(st, action(st, "actions/checkout"), st.index(m)),
                                    "actions/checkout@v4"),
            "checkout_last": (lambda st, m: move(st, action(st, "actions/checkout"), -1), "actions/checkout@v4"),
            "checkout_not_first": (lambda st, m: move(st, action(st, "actions/checkout"), 1), "actions/checkout@v4"),
            "main_first": (lambda st, m: move(st, m, 0), "actions/checkout@v4"),
            "duplicate_checkouts": (lambda st, m: st.insert(2, copy.deepcopy(action(st, "actions/checkout"))),
                                    "actions/checkout@v4"),
            "drop_setup_python": (lambda st, m: st.remove(action(st, "actions/setup-python")),
                                  "actions/setup-python@v5"),
            "setup_python_after_main": (lambda st, m: move(st, action(st, "actions/setup-python"), -1),
                                        "actions/setup-python@v5"),
            "setup_python_first": (lambda st, m: move(st, action(st, "actions/setup-python"), 0),
                                   "actions/setup-python@v5"),
            "duplicate_setup_python": (lambda st, m: st.insert(st.index(m), copy.deepcopy(
                action(st, "actions/setup-python"))), "actions/setup-python@v5"),
        }
        for label, (mutate, fragment) in cases.items():
            with self.subTest(label):
                docs, job, step = _real()
                mutate(job["steps"], step)
                self.assertR3(docs, fragment)

    # --- nit: the main step's shell ------------------------------------------------------------

    def test_nit_the_main_step_runs_under_bash_not_sh(self):
        for shell in ("sh", "sh -e {0}", "sh --noprofile --norc {0}"):
            for level in ("step", "job", "workflow"):
                with self.subTest(shell=shell, level=level):
                    docs, job, step = _real()
                    if level == "step":
                        step["shell"] = shell
                    else:
                        ({"job": job, "workflow": docs["ci.yml"]}[level]).setdefault("defaults", {})["run"] = \
                            {"shell": shell}
                    self.assertR3(docs, "shell")
        for shell in ("bash", "bash -e {0}", "bash --noprofile --norc -eo pipefail {0}"):
            with self.subTest(green=shell):
                docs, _, step = _real()
                step["shell"] = shell
                self.assertEqual(ci_integrity_violations(docs), [])


class TheGuardIsNotBrittle(unittest.TestCase):
    """Legal refactors stay green: the rules read meaning, not today's text."""

    OK = 'python -m pytest tools/ -q -n auto -o junit_family=xunit1 --junitxml="${{ runner.temp }}/r.xml"'

    def test_the_real_shape_and_its_spellings_pass(self):
        for run in (self.OK, "pytest tools", "python3 -m pytest ./tools/ -x", "timeout 1500 python -m pytest tools/ -n 4",
                    "FOO=1 python -m pytest tools/ -q", "set -euo pipefail\npython -m pytest tools/ -q",
                    "set -e && python -m pytest tools/ -q", "set -euo pipefail; set +e; python -m pytest tools/ -q",
                    "set -x\npython -m pytest tools/ -q -n auto", "python -m pytest tools/ -q 2>&1",
                    "python -m pytest tools/ -q >&2", "python -m pytest tools/ -q > /dev/null 2>&1",
                    "python -m pytest tools/ -q -p no:cacheprovider -rsx -Wignore:x -vv --tb=short --durations=20",
                    "python -m pytest tools/ -o junit_family=xunit1 --override-ini=console_output_style=count"):
            with self.subTest(run=run):
                self.assertEqual(ci_integrity_violations(_wf(run)), [])

    def test_a_matrix_is_one_step(self):
        self.assertEqual(ci_integrity_violations(_wf(self.OK, matrix=["3.11", "3.12"])), [])

    def test_trivially_true_conditions_and_explicit_false_forgiveness_pass(self):
        for keys in ({"if": True}, {"if": "true"}, {"if": "success()"}, {"if": "${{ success() }}"}, {"if": "always()"},
                     {"continue-on-error": False}, {"continue-on-error": "false"}, {"continue-on-error": "${{ false }}"},
                     {"if": "!cancelled()"}, {"if": "${{ !cancelled() }}"},
                     # T-4385 r3 nit: `sh` on the main step is red (T4385R3RawExpressionsAndStepOrder).
                     {"shell": "bash"}, {"shell": "bash --noprofile --norc -eo pipefail {0}"},
                     {"shell": "bash {0}"}, {"shell": "bash -e {0}"}, {"shell": "bash -x -o pipefail {0}"},
                     # T-4385 r2: the main step's env is exactly _MAIN_STEP_ENV; a working-directory
                     # (even `.`) and any other env name are red (T4385R2EnvelopeIsAllowlisted).
                     {"env": {}}):
            with self.subTest(keys=keys):
                self.assertEqual(ci_integrity_violations(_wf(self.OK, **keys)), [])

    def test_other_pytest_runs_that_do_not_cover_the_root_stay_legal(self):
        # In ANOTHER job: inside the main job no step but the main one may run pytest (T-4385 r1 N-2).
        docs = _wf(self.OK)
        docs["ci.yml"]["jobs"]["other"] = {"runs-on": "x", "steps": [
            {"name": "one file", "run": "python -m pytest tools/test_x.py -q"},
            {"name": "version", "run": "python -m pytest --version"},
            {"name": "sub-project", "run": "pytest", "working-directory": "projects/demo"},
            {"name": "unrelated job step", "run": "pip install pytest"},
        ]}
        self.assertEqual(ci_integrity_violations(docs), [])

    def test_an_unrelated_job_may_use_if_and_continue_on_error(self):
        docs = _wf(self.OK)
        docs["ci.yml"]["jobs"]["docs"] = {"if": "false", "continue-on-error": True, "steps": [{"run": "echo hi"}]}
        self.assertEqual(ci_integrity_violations(docs), [])

    def test_a_needs_job_is_refused_even_with_a_trivial_condition(self):
        # T-4385 r2: `needs` is not one of the main job's allowlisted keys today (it was green before).
        docs = _wf(self.OK)
        docs["ci.yml"]["jobs"]["lint"] = {"steps": [{"run": "true"}]}
        docs["ci.yml"]["jobs"]["test"]["needs"] = ["lint"]
        found = ci_integrity_violations(docs)
        self.assertTrue(found and all("job key `needs`" in v for v in found), found)

    def test_quoted_operators_inside_arguments_are_not_operators(self):
        run = self.OK + " --junit-prefix='a||b'"
        self.assertEqual(ci_integrity_violations(_wf(run)), [])


class TheTreeGuard(unittest.TestCase):
    """Fixtures for E: conftest hooks, pytest_plugins, config files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        (self.repo / "tools" / "sub").mkdir(parents=True)

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_clean_tree_passes(self):
        self.write("tools/conftest.py", "import pytest\n@pytest.fixture(autouse=True)\ndef f():\n    yield\n")
        self.assertEqual(tree_violations(self.repo), [])

    def test_a_modifyitems_hook_anywhere_is_caught(self):
        hook = "def pytest_collection_modifyitems(config, items):\n    items[:] = []\n"
        for rel in ("conftest.py", "tools/conftest.py", "tools/sub/conftest.py", "projects/x/conftest.py"):
            with self.subTest(rel=rel):
                for p in self.repo.rglob("conftest.py"):
                    p.unlink()
                self.write(rel, hook)
                found = tree_violations(self.repo)
                self.assertTrue(any("pytest_collection_modifyitems" in v for v in found), found)

    def test_every_spelling_of_a_neutering_hook_is_caught(self):
        for src in (
            "def pytest_collection_modifyitems(config, items):\n    for i in items: i.add_marker('skip')\n",
            "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_collection_modifyitems(items):\n    config.hook.pytest_deselected(items=items)\n",
            "class P:\n    def pytest_collection_modifyitems(self, items):\n        pass\n",
            "def _h(items): pass\npytest_collection_modifyitems = _h\n",
            "def pytest_ignore_collect(collection_path):\n    return True\n",
            "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n",
            "import pytest\n@pytest.hookimpl(wrapper=True)\ndef pytest_runtest_makereport(item, call):\n    r = yield\n    r.outcome = 'passed'\n    return r\n",
            "def pytest_runtest_setup(item):\n    import pytest; pytest.skip('x')\n",
            "async def pytest_collection(session):\n    return True\n",
        ):
            with self.subTest(src=src):
                self.write("tools/conftest.py", src)
                self.assertTrue(tree_violations(self.repo))

    def test_a_hook_only_in_a_string_or_a_non_conftest_module_is_not_a_hook(self):
        self.write("tools/neutralize_x.py", "T = '''\\ndef pytest_collection_modifyitems(config, items):\\n    pass\\n'''\n"
                                            "def pytest_collection_modifyitems(config, items):\n    pass\n")
        self.assertEqual(tree_violations(self.repo), [])

    def test_a_conftest_under_a_dot_dir_is_not_collected_so_not_scanned(self):
        self.write(".claude/worktrees/w/tools/conftest.py", "def pytest_collection_modifyitems(items):\n    items.clear()\n")
        self.assertEqual(tree_violations(self.repo), [])

    def test_pytest_plugins_are_followed_or_fail_closed(self):
        self.write("tools/plug.py", "def pytest_collection_modifyitems(items):\n    items.clear()\n")
        for src, fragment in (("pytest_plugins = ['tools.plug']\n", "tools/plug.py"),
                              ("pytest_plugins = 'tools.plug'\n", "tools/plug.py"),
                              ("pytest_plugins = ['some_pip_plugin']\n", "not found"),
                              ("pytest_plugins = [x for x in ()]\n", "not a literal")):
            with self.subTest(src=src):
                self.write("conftest.py", src)
                found = tree_violations(self.repo)
                self.assertTrue(any(fragment in v for v in found), found)
        self.write("tools/plug.py", "X = 1\n")
        self.write("conftest.py", "pytest_plugins = ['tools.plug']\n")
        self.assertEqual(tree_violations(self.repo), [])

    def test_a_sanctioned_hook_passes(self):
        global _SANCTIONED_HOOKS
        self.write("tools/conftest.py", "def pytest_collection_modifyitems(items):\n    items.sort(key=str)\n")
        saved = _SANCTIONED_HOOKS
        _SANCTIONED_HOOKS = frozenset({("tools/conftest.py", "pytest_collection_modifyitems")})
        try:
            self.assertEqual(tree_violations(self.repo), [])
        finally:
            _SANCTIONED_HOOKS = saved

    def test_narrowing_config_files_are_caught(self):
        for rel, text in (
            ("pytest.ini", "[pytest]\naddopts = -k 'not slow'\n"),
            ("tools/pytest.ini", "[pytest]\naddopts = --deselect tools/test_x.py::t\n"),
            (".pytest.ini", "[pytest]\npython_files = test_a*.py\n"),
            ("tox.ini", "[pytest]\naddopts = -m smoke\n"),
            ("setup.cfg", "[tool:pytest]\nnorecursedirs = germinate\n"),
            ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = \"-qk nope\"\n"),
            ("pyproject.toml", "[tool.pytest]\naddopts = [\"--ignore=tools/hooks\"]\n"),
            ("pytest.toml", "[pytest]\naddopts = [\"-p\", \"tools.evil\"]\n"),
            ("pytest.ini", "[pytest]\naddopts = -p no:unittest\n"),
            ("pytest.ini", "[pytest\n"),
        ):
            with self.subTest(rel=rel, text=text):
                for name in ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "pytest.toml"):
                    for d in (self.repo, self.repo / "tools"):
                        (d / name).unlink(missing_ok=True)
                self.write(rel, text)
                self.assertTrue(tree_violations(self.repo), f"{rel}: {text!r} not flagged")

    def test_t4385_d3_an_importable_module_at_the_repo_root(self):
        for rel in ("pytest.py", "pytest/__init__.py", "_pytest/__init__.py", "argparse.py", "pluggy.pyc",
                    "xdist.cpython-312-x86_64-linux-gnu.so", "setup.py", "execnet/__init__.pyc"):
            with self.subTest(rel=rel):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = pathlib.Path(tmp)
                    (repo / "tools").mkdir()
                    (repo / "tools" / "__init__.py").write_text("", encoding="utf-8")
                    path = repo / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("", encoding="utf-8")
                    found = tree_violations(repo)
                    self.assertTrue(any(v.startswith(rel.split("/")[0]) and "repo root" in v for v in found), found)

    def test_t4385_d3_tools_a_root_conftest_and_non_modules_pass(self):
        self.write("tools/__init__.py", "")
        self.write("conftest.py", "X = 1\n")
        self.write("README.md", "x\n")
        self.write("pytest/data.txt", "a namespace portion loses to the installed package\n")
        self.write("docs/x.md", "x\n")
        self.write(".hidden/pytest.py", "")
        self.assertEqual(tree_violations(self.repo), [])

    def test_harmless_config_files_pass(self):
        self.write("pyproject.toml", "[project]\nname = 'x'\n[tool.ruff]\nline-length = 100\n")
        self.write("setup.cfg", "[flake8]\nmax-line-length = 100\n")
        self.write("pytest.ini", "[pytest]\naddopts = -p no:cacheprovider -rsx\nmarkers =\n    slow: slow\n")
        self.assertEqual(tree_violations(self.repo), [])


class TheHarnessWorkflowIsSwept(unittest.TestCase):
    """T-4761 (ORG-091 phase 4): harness.yml runs the harness suites with pytest in their own
    jobs. It is covered by the rules that hold for EVERY workflow, not exempted: its pytest
    steps must be visible to the tokenizer, must not be a second whole-tree tools/ run, and
    must not collect the repo root. Its own contract is tools/test_harness_workflow.py."""

    def _harness_pytest_steps(self, docs: dict) -> list[tuple[str, dict]]:
        return [(job_name, step) for job_name, job in docs["harness.yml"]["jobs"].items()
                for step in job["steps"] if _safe_invocations(_run_of(step))]

    def test_its_pytest_steps_are_seen_and_none_is_a_main_step_or_a_root_run(self):
        docs = copy.deepcopy(_parsed_real())
        steps = self._harness_pytest_steps(docs)
        self.assertEqual(sorted(j for j, _ in steps), ["claude-harness", "codex-harness"])
        for job_name, step in steps:
            with self.subTest(job=job_name):
                invs = invocations(_run_of(step))
                self.assertEqual(len(invs), 1)
                self.assertFalse(invs[0].whole_tree)
                self.assertFalse(invs[0].unscoped)
        self.assertEqual([m[0] for m in _main_steps(docs)], ["ci.yml"])

    def test_a_harness_step_retargeted_at_the_root_or_tools_is_caught(self):
        def to_root(step: dict) -> None:  # bare pytest from the repo root
            del step["working-directory"]
            step["run"] = step["run"].replace(" tests/", "")

        def to_tools(step: dict) -> None:  # a second whole-tree tools/ run
            step["run"] = step["run"].replace(" tests/", " tools/")

        for mutate, expect in ((to_root, "over the repo root"), (to_tools, "expected exactly 1")):
            for index in range(2):
                docs = copy.deepcopy(_parsed_real())
                _, step = self._harness_pytest_steps(docs)[index]
                mutate(step)
                with self.subTest(expect=expect, index=index):
                    found = ci_integrity_violations(docs)
                    self.assertTrue(any(expect in v for v in found), found)


class TheArgvParser(unittest.TestCase):
    def test_short_clusters_and_values(self):
        self.assertEqual(_options(["-qk", "x", "-n4", "-o", "a=b", "tools"]),
                         (["tools"], [("-q", None), ("-k", "x"), ("-n", "4"), ("-o", "a=b")]))

    def test_a_k_or_m_inside_a_value_is_not_a_selector(self):
        for args in (["-n", "auto"], ["-rsxkm"], ["-Wignore:mk"], ["-p", "no:cacheprovider"], ["--tb=short"]):
            with self.subTest(args=args):
                self.assertEqual(narrowing(["tools", *args]), [])

    def test_pip_install_pytest_is_not_a_pytest_run(self):
        self.assertEqual(invocations("python -m pip install --upgrade pip pyyaml pytest pytest-xdist"), [])
        self.assertEqual(invocations("pip install pytest"), [])


if __name__ == "__main__":
    unittest.main()
