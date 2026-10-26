"""The harness workflow's contract (T-4761, ORG-091 phase 4).

.github/workflows/harness.yml runs the claude and codex harness suites that T-4755 imported
into harness/. The rules every workflow meets (lane label, bounded timeout, repack step,
no repo-root pytest, no second whole-tree tools/ run) are swept over it by
test_workflow_runner_lanes.py, test_workflow_timeouts.py and test_ci_integrity.py. This
file pins what is specific to it, parsed rather than byte-pinned:

- path-scoped triggers: harness/** and this workflow file, plus the three instance files the
  harness suites read (pinned-rules/codex.md, pinned-rules/orchestrator.md, instance.yaml), on PRs,
  plus a nightly schedule that covers main (T-5001 dropped the merge-push re-run);
- one job per harness, each running its whole tests/ tree from its own directory, with
  full history for the codex job (its fixtures read pinned codex-config revisions);
- the `mcp` SDK is a test-only dependency of the claude job alone, at an exact version
  (T-4750 orchestrator ruling 5), and every DIRECT install is an exact pin into a job venv, never
  into the persistent runner's shared setup-python interpreter. Transitive dependencies
  (mcp's) are not locked; they resolve within the pinned packages' own ranges (T-4763);
- least-privilege token;
- outside that one seat pin, mcp reaches a workflow ONLY as the ORG-052 adapter runtime: one
  `pip install -r tools/host_mcp_adapter/dist/requirements.lock` (mcp 2.x, hash-locked, wheels
  only, no deps) in mcp-adapter-runtime.yml's mcp2-tiers job (T-4858). Every pip install in every
  workflow is read by a full pip-argv parser (requirements and constraints files included, since
  a `-r` install names no package on the command line) and any other mcp install is refused;
- a third job, the release-mirror leak gate (T-4768 PR4): it builds BOTH mirrors at HEAD
  through tools/build_harness_mirror.py (whose build always runs the gate), on the light
  lane, and the builder + gate sources trigger the workflow.

It replaces harness/codex/tests/test_ci_workflow.py, which byte-pinned codex-config's own
workflow; that workflow is inert in a subdirectory and was removed with it.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
HARNESS = WORKFLOWS / "harness.yml"
JOB_DIRS = {"claude-harness": "harness/claude", "codex-harness": "harness/codex"}
MCP_JOB = "claude-harness"
VENV_PYTHON = '"$RUNNER_TEMP/harness-venv/bin/python"'
INSTANCE_TRIGGERS = ("pinned-rules/codex.md", "pinned-rules/orchestrator.md", "instance.yaml")
LEAK_GATE_JOB = "mirror-leak-gate"
LEAK_GATE_TRIGGERS = ("tools/build_harness_mirror.py", "tools/mirror_leak_gate.py",
                      "tools/neutralize_seed.py", "tools/verify_seed.py",
                      "tools/agent_role_map.py", "tools/_git_utils.py")
_EXACT_PIN = re.compile(r"[A-Za-z0-9._-]+==\d+(\.\d+)*")
# T-4858: the one sanctioned mcp install outside the seat pin.
ADAPTER_LOCK = "tools/host_mcp_adapter/dist/requirements.lock"
ADAPTER_WORKFLOW = "mcp-adapter-runtime.yml"
ADAPTER_JOB = "mcp2-tiers"
ADAPTER_LOCK_FLAGS = ("--require-hashes", "--no-deps")


def _doc(path: Path = HARNESS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on(doc: dict) -> dict:
    # PyYAML (YAML 1.1) loads a bare `on:` key as True.
    return doc.get("on", doc.get(True)) or {}


def _runs(job: dict) -> list[str]:
    return [str(s["run"]) for s in job.get("steps", []) if "run" in s]


def _pip_installs(run: str) -> list[list[str]]:
    """The argument lists of every `... -m pip install ...` command line in a run script."""
    out = []
    for line in run.splitlines():
        words = shlex.split(line, comments=True)
        for i in range(len(words) - 2):
            if words[i:i + 3] == ["-m", "pip", "install"]:
                out.append([w for w in words[i + 3:] if not w.startswith("-")])
    return out


# --- T-4858: the pip-argv reader behind the mcp install guard ---------------------------------

# pip options that take a value, so the value is never read as a package name.
_PIP_VALUE_OPTS = {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-f", "--find-links",
    "-i", "--index-url", "--extra-index-url", "-d", "--dest", "-t", "--target", "--prefix",
    "--root", "--src", "--only-binary", "--no-binary", "--platform", "--python-version",
    "--implementation", "--abi", "--progress-bar", "--cache-dir", "--log", "--trusted-host",
    "--retries", "--timeout", "--upgrade-strategy", "--report", "--python", "--config-settings",
    "-C", "--global-option", "--proxy", "--cert", "--client-cert", "--exists-action",
    "--keyring-provider", "--root-user-action",
}
# pip GLOBAL options that take a value; they may sit between `pip` and `install`.
_PIP_GLOBAL_VALUE_OPTS = {
    "--log", "--log-file", "--cache-dir", "--python", "--proxy", "--timeout", "--retries",
    "--exists-action", "--cert", "--client-cert", "--trusted-host", "--keyring-provider",
    "--use-feature", "--use-deprecated", "--src",
}
_SHELL_OPS = {"&&", "||", ";", "|", "&", "(", ")", "((", "))", ";;", "|&", "<", ">", ">>", "<<", "&>"}
_CONTINUATION = re.compile(r"\\\r?\n")


def _shell_words(line: str) -> list[str]:
    """A line as shell words, with operators ( ) ; && || | split off as their own tokens even
    when they touch a word (`(pip`, `x;pip`). Raises ValueError on an unbalanced quote."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _pip_install_starts(words: list[str]) -> list[int]:
    """Index just past `install` for each pip install in a word list: `-m pip install`,
    `pip install` / `pip3 install` / `<path>/pip install`, `uv pip install`, with any pip
    GLOBAL options between `pip` and `install` (`pip --quiet install`, `-m pip --isolated
    install`)."""
    starts = []
    for i, word in enumerate(words):
        base = word.rsplit("/", 1)[-1]
        if not (base in ("pip", "pip3") or re.fullmatch(r"pip3\.\d+", base)):
            continue
        j = i + 1
        while j < len(words) and words[j].startswith("-") and words[j] not in _SHELL_OPS:
            j += 2 if words[j] in _PIP_GLOBAL_VALUE_OPTS else 1
        if j < len(words) and words[j] == "install":
            starts.append(j + 1)
    return starts


def pip_install_commands(run: str) -> list[dict]:
    """Every pip install in a run script, parsed: packages (positional names), requirements and
    constraints files, editable targets, the bare flags, and the value of each value-taking
    option. Backslash-newline continuations are joined FIRST (T-4858 r1 B1), so a command split
    over lines is read whole. A line shlex cannot parse (an unbalanced quote) yields
    {"unparseable": line}, which the guard records as a violation: fail closed, never a
    whitespace-split guess."""
    out = []
    for line in _CONTINUATION.sub(" ", run).splitlines():
        try:
            words = _shell_words(line)
        except ValueError:
            if re.search(r"\bpip", line):
                out.append({"unparseable": line.strip()})
            continue
        for start in _pip_install_starts(words):
            cmd = {"packages": [], "requirements": [], "constraints": [], "editables": [],
                   "flags": set(), "values": {}}
            i = start
            while i < len(words) and words[i] not in _SHELL_OPS:
                word = words[i]
                if word.startswith("--") and "=" in word:
                    opt, value = word.split("=", 1)
                elif word in _PIP_VALUE_OPTS:
                    opt, value = word, (words[i + 1] if i + 1 < len(words) else "")
                    i += 1
                elif word[:2] in ("-r", "-c", "-e") and len(word) > 2:
                    opt, value = word[:2], word[2:]
                elif word.startswith("-"):
                    cmd["flags"].add(word)
                    i += 1
                    continue
                else:
                    cmd["packages"].append(word)
                    i += 1
                    continue
                cmd["values"].setdefault(opt, []).append(value)
                if opt in ("-r", "--requirement"):
                    cmd["requirements"].append(value)
                elif opt in ("-c", "--constraint"):
                    cmd["constraints"].append(value)
                elif opt in ("-e", "--editable"):
                    cmd["editables"].append(value)
                i += 1
            out.append(cmd)
    return out


def _dist_name(spec: str) -> str:
    name = re.split(r"[<>=!~;\[\s@]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


_MCP_LINE = re.compile(r"^\s*mcp\s*(?:[<>=!~\[;@]|$)", re.I | re.M)


def _mcp_pin(text: str) -> str | None:
    """The version a requirements text pins mcp to ('' if unversioned), None if it has none."""
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if _MCP_LINE.match(line):
            match = re.search(r"==\s*([0-9][^\s\\;]*)", line)
            return match.group(1) if match else ""
    return None


def _only_binary_all(cmd: dict) -> bool:
    return ":all:" in cmd["values"].get("--only-binary", [])


def mcp_install_violations(workflows: dict[str, dict], repo: Path = REPO) -> tuple[list[str], list[str]]:
    """(violations, sanctioned) over {workflow file name: parsed doc}.

    Sanctioned: (1) the seat pin, exactly `mcp==1.x` in harness.yml's claude job (T-1934 /
    orchestrator ruling 5, its shape pinned by test_mcp_is_installed_in_the_claude_job_only); (2) the
    ORG-052 adapter runtime, exactly `-r` ADAPTER_LOCK with --require-hashes, --no-deps and
    --only-binary :all:, in ADAPTER_WORKFLOW's ADAPTER_JOB, the lock pinning mcp 2.x. Anything
    else that installs mcp is a violation: a package argument naming mcp (any spelling or
    specifier), a requirements or constraints file that pins it, an unreadable requirements
    file (fail closed), a lock install out of place or without its flags, a second lock install.

    KNOWN OUT OF SCOPE (T-4858 r2; not statically resolvable from a run script, so neither
    refused nor proven absent): `xargs pip install` and other commands that build the pip argv
    at run time; PIP_REQUIREMENT / PIP_CONSTRAINT / any PIP_* environment variable (job, step or
    $GITHUB_ENV) that adds requirements or options; pip nested inside a quoted `bash -c '...'` /
    `sh -c` / `eval` string (one shell word here); `pipx install`; `uv add` and other
    project-manager installs. A reviewer, not this guard, catches those.
    """
    violations: list[str] = []
    sanctioned: list[str] = []
    lock_installs = []
    for wf in sorted(workflows):
        for job_name, job in (workflows[wf].get("jobs") or {}).items():
            where = f"{wf}:{job_name}"
            for run in _runs(job):
                for cmd in pip_install_commands(run):
                    if "unparseable" in cmd:
                        violations.append(f"{where}: unparseable run line, fail closed: {cmd['unparseable']!r}")
                        continue
                    # Statically unresolvable installs fail closed (T-4858 r1): an expansion
                    # could name mcp, and an editable install builds whatever source it points
                    # at. No workflow needs either today; a future one changes this guard.
                    for spec in cmd["packages"]:
                        if "$" in spec or "`" in spec:
                            violations.append(f"{where}: package word {spec!r} cannot be resolved here (fail closed)")
                        elif "mcp" in spec.lower() and (".whl" in spec.lower() or "://" in spec):
                            violations.append(f"{where}: installs an mcp wheel or URL directly ({spec})")
                    for target in cmd["editables"]:
                        violations.append(f"{where}: editable install {target!r} cannot be resolved here (fail closed)")
                    for spec in cmd["packages"]:
                        if _dist_name(spec) != "mcp":
                            continue
                        if (wf == HARNESS.name and job_name == MCP_JOB
                                and re.fullmatch(r"mcp==1\.\d+\.\d+", spec)):
                            sanctioned.append(f"{where}: seat pin {spec}")
                        else:
                            violations.append(f"{where}: installs mcp directly ({spec})")
                    for kind in ("requirements", "constraints"):
                        for rel in cmd[kind]:
                            if kind == "requirements" and rel == ADAPTER_LOCK:
                                lock_installs.append((wf, job_name, cmd))
                                continue
                            path = repo / rel
                            if "$" in rel or not path.is_file():
                                violations.append(f"{where}: {kind} file {rel!r} cannot be read here (fail closed)")
                            elif _mcp_pin(path.read_text(encoding="utf-8")) is not None:
                                violations.append(f"{where}: {kind} file {rel} pins mcp")
    lock_pin = _mcp_pin((repo / ADAPTER_LOCK).read_text(encoding="utf-8")) if (repo / ADAPTER_LOCK).is_file() else None
    for wf, job_name, cmd in lock_installs:
        where = f"{wf}:{job_name}"
        problems = []
        if (wf, job_name) != (ADAPTER_WORKFLOW, ADAPTER_JOB):
            problems.append(f"only {ADAPTER_WORKFLOW}:{ADAPTER_JOB} may install it")
        problems += [f"missing {flag}" for flag in ADAPTER_LOCK_FLAGS if flag not in cmd["flags"]]
        if not _only_binary_all(cmd):
            problems.append("missing --only-binary :all:")
        if not lock_pin or not lock_pin.startswith("2."):
            problems.append(f"the lock pins mcp {lock_pin!r}, not 2.x")
        if problems:
            violations.append(f"{where}: adapter lock install refused: {'; '.join(problems)}")
        else:
            sanctioned.append(f"{where}: adapter runtime -r {ADAPTER_LOCK} (mcp {lock_pin})")
    if len(lock_installs) > 1:
        violations.append(f"the adapter lock is installed {len(lock_installs)} times; exactly one is allowed")
    return violations, sanctioned


def _real_workflows() -> dict[str, dict]:
    return {path.name: _doc(path) for path in sorted(WORKFLOWS.glob("*.y*ml"))}


class HarnessWorkflowContract(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _doc()
        self.jobs = self.doc["jobs"]

    def test_triggers_are_path_scoped_to_the_harness(self) -> None:
        on = _on(self.doc)
        self.assertIn("workflow_dispatch", on)
        # T-5001: no merge-push re-run; a nightly covers main instead.
        self.assertNotIn("push", on)
        self.assertTrue([e for e in on.get("schedule") or [] if e.get("cron")])
        for event in ("pull_request",):
            with self.subTest(event=event):
                paths = on[event]["paths"]
                self.assertIn("harness/**", paths)
                self.assertIn(".github/workflows/harness.yml", paths)
                # T-4763 (C3 review): the instance files the harness suites read since T-4759
                # moved them out of harness/; an edit to one must run the suite that reads it.
                for instance_file in INSTANCE_TRIGGERS + LEAK_GATE_TRIGGERS:
                    self.assertIn(instance_file, paths)
                # Path-scoped: nothing that matches every commit (ORG-091 phase 4).
                self.assertFalse([p for p in paths if p in ("**", "*", "tickets/**", "ledger/**")])

    def test_one_job_per_harness_runs_its_whole_suite_from_its_own_directory(self) -> None:
        self.assertEqual(set(self.jobs), set(JOB_DIRS) | {LEAK_GATE_JOB})
        for name, directory in JOB_DIRS.items():
            with self.subTest(job=name):
                steps = [s for s in self.jobs[name]["steps"] if "pytest" in str(s.get("run", ""))
                         and "pip" not in str(s.get("run", ""))]
                self.assertEqual(len(steps), 1)
                step = steps[0]
                self.assertEqual(step.get("working-directory"), directory)
                self.assertTrue((REPO / directory / "tests").is_dir())
                words = shlex.split(step["run"])
                self.assertEqual(words[:3], [VENV_PYTHON.strip('"'), "-m", "pytest"])
                self.assertIn("tests/", words)
                for flag in ("-k", "-m", "--deselect", "-x", "--lf", "--sw"):
                    self.assertNotIn(flag, words[3:])
                self.assertFalse([w for w in words if w.startswith("--ignore")])
                self.assertNotIn("continue-on-error", step)
                self.assertNotIn("if", step)

    def test_codex_job_checks_out_full_history(self) -> None:
        checkout = [s for s in self.jobs["codex-harness"]["steps"]
                    if str(s.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0].get("with", {}).get("fetch-depth"), 0)

    def test_every_direct_install_is_an_exact_pin_into_the_job_venv(self) -> None:
        for name, job in self.jobs.items():
            installs = [(run, args) for run in _runs(job) for args in _pip_installs(run)]
            with self.subTest(job=name):
                self.assertEqual(len(installs), 1)
                run, args = installs[0]
                self.assertIn(f"{VENV_PYTHON} -m pip install", run)
                self.assertIn('python -m venv "$RUNNER_TEMP/harness-venv"', run)
                if name in JOB_DIRS:
                    self.assertIn("pytest==9.1.1", args)
                for arg in args:
                    self.assertRegex(arg, _EXACT_PIN)

    def test_mcp_is_installed_in_the_claude_job_only(self) -> None:
        # orchestrator ruling 5 (T-4750): CI/test-only, version-pinned, scoped to the claude job.
        for name, job in self.jobs.items():
            mcp = [a for run in _runs(job) for args in _pip_installs(run) for a in args
                   if a.split("==")[0].lower() == "mcp"]
            with self.subTest(job=name):
                if name == MCP_JOB:
                    self.assertEqual(len(mcp), 1)
                    self.assertRegex(mcp[0], r"^mcp==1\.\d+\.\d+$")
                else:
                    self.assertEqual(mcp, [])

    def test_no_other_workflow_installs_mcp(self) -> None:
        # T-4858: narrowed to exactly two sanctioned installs across the whole tree, the seat pin
        # here and the adapter lock in mcp-adapter-runtime.yml; see mcp_install_violations.
        violations, sanctioned = mcp_install_violations(_real_workflows())
        self.assertEqual(violations, [])
        self.assertEqual(len(sanctioned), 2, sanctioned)
        self.assertTrue(any(s.startswith(f"{HARNESS.name}:{MCP_JOB}: seat pin mcp==1.") for s in sanctioned))
        self.assertTrue(any(s.startswith(f"{ADAPTER_WORKFLOW}:{ADAPTER_JOB}: adapter runtime") for s in sanctioned))

    def test_leak_gate_job_builds_both_mirrors_through_the_builder(self) -> None:
        job = self.jobs[LEAK_GATE_JOB]
        self.assertEqual(job["runs-on"], ["self-hosted", "linux", "x64", "light"])
        self.assertNotIn("continue-on-error", job)
        installs = [a for run in _runs(job) for args in _pip_installs(run) for a in args]
        self.assertEqual(installs, ["pyyaml==6.0.3"])
        gate = [s for s in job["steps"] if "build_harness_mirror.py" in str(s.get("run", ""))]
        self.assertEqual(len(gate), 1)
        step = gate[0]
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if", step)
        run = step["run"]
        self.assertIn("set -e", run)
        self.assertIn("for mirror in claude codex; do", run)
        self.assertIn('tools/build_harness_mirror.py build --mirror "$mirror" --source-sha HEAD',
                      run)
        self.assertNotIn("||", run)

    def test_token_is_read_only(self) -> None:
        self.assertEqual(self.doc.get("permissions"), {"contents": "read"})

    def test_the_inert_subdirectory_workflows_are_gone(self) -> None:
        for directory in JOB_DIRS.values():
            with self.subTest(directory=directory):
                self.assertFalse((REPO / directory / ".github").exists())


def _wf(run: str, job: str = "j") -> dict:
    return {"jobs": {job: {"steps": [{"run": run}]}}}


LOCK_RUN = ('"$V/python" -m pip install --quiet --no-index --find-links "$W" --require-hashes --no-deps '
            f'--only-binary :all: -r {ADAPTER_LOCK}')


class McpInstallGuard(unittest.TestCase):
    """T-4858: the narrowed guard refuses every mcp install but the two sanctioned ones."""

    def _check(self, workflows: dict[str, dict]) -> tuple[list[str], list[str]]:
        return mcp_install_violations(workflows)

    def _base(self) -> dict[str, dict]:
        return {HARNESS.name: _doc(), ADAPTER_WORKFLOW: _wf(LOCK_RUN, ADAPTER_JOB)}

    def test_the_two_sanctioned_installs_pass(self) -> None:
        violations, sanctioned = self._check(self._base())
        self.assertEqual(violations, [])
        self.assertEqual(len(sanctioned), 2)

    def test_the_real_harness_seat_pin_still_passes(self) -> None:
        violations, sanctioned = self._check({HARNESS.name: _doc()})
        self.assertEqual(violations, [])
        self.assertEqual(len(sanctioned), 1)
        self.assertRegex(sanctioned[0], r"^harness\.yml:claude-harness: seat pin mcp==1\.\d+\.\d+$")

    def test_no_other_real_workflow_line_is_newly_flagged(self) -> None:
        # Every pip install in every real workflow parses, and nothing but the two sanctioned
        # mcp installs is flagged; e.g. ci.yml's suite install reads as exactly its four names.
        violations, _ = self._check(_real_workflows())
        self.assertEqual(violations, [])
        ci = [c for run in _runs(_doc(WORKFLOWS / "ci.yml")["jobs"]["test"]) for c in pip_install_commands(run)]
        self.assertEqual([sorted(c["packages"]) for c in ci],
                         [["pip", "pytest-xdist==3.8.0", "pytest==9.1.1", "pyyaml==6.0.3"]])
        self.assertEqual(ci[0]["flags"], {"--upgrade"})

    def test_a_direct_mcp_install_anywhere_else_fails(self) -> None:
        for spec in ("mcp", "mcp==2.3.0", "mcp==1.27.1", "MCP[cli]==2.3.0", "mcp>=2", "Mcp~=2.3"):
            for wf in ("ci.yml", ADAPTER_WORKFLOW, "new.yml"):
                with self.subTest(spec=spec, workflow=wf):
                    docs = self._base()
                    docs[wf] = _wf(f'python -m pip install pyyaml==6.0.3 "{spec}"', "other")
                    violations, _ = self._check(docs)
                    self.assertTrue(any("installs mcp directly" in v for v in violations), violations)

    def test_every_pip_spelling_is_read(self) -> None:
        for run in ("pip install mcp==2.3.0", "pip3 install mcp", "/opt/py/bin/pip install mcp",
                    "uv pip install mcp", "python3 -m pip install --quiet mcp==2.3.0",
                    "x && python -m pip install mcp",
                    # T-4858 r1 B1: a backslash-continued command is read whole.
                    "python -m pip install --quiet \\\n  pyyaml==6.0.3 \\\n  mcp==2.3.0",
                    "python -m pip install \\\r\n  mcp",
                    # T-4858 r1: pip global options before `install`, and a subshell.
                    "pip --quiet install mcp", "python -m pip --isolated install mcp",
                    "python -m pip --log /tmp/x.log install mcp", "(pip install mcp)",
                    "x;pip install mcp",
                    # an mcp wheel file or URL, an unresolvable expansion, an editable target
                    "python -m pip install ./wheels/mcp-2.3.0-py3-none-any.whl",
                    "python -m pip install git+https://github.com/example/mcp-sdk",
                    'python -m pip install "$PKG"', "python -m pip install `cat pkgs`",
                    "python -m pip install -e ./vendor/sdk", "python -m pip install --editable=.",
                    # an unparseable line (unbalanced quote) fails closed
                    "python -m pip install 'mcp==2.3.0"):
            with self.subTest(run=run):
                docs = self._base()
                docs["new.yml"] = _wf(run)
                violations, _ = self._check(docs)
                self.assertTrue(violations, run)

    def test_the_continuation_and_unparseable_cases_name_their_cause(self) -> None:
        docs = self._base()
        docs["new.yml"] = _wf("python -m pip install --quiet \\\n  pyyaml==6.0.3 \\\n  mcp==2.3.0")
        self.assertIn("new.yml:j: installs mcp directly (mcp==2.3.0)", self._check(docs)[0])
        docs["new.yml"] = _wf("python -m pip install 'mcp==2.3.0")
        violations, _ = self._check(docs)
        self.assertEqual(len(violations), 1)
        self.assertIn("unparseable run line, fail closed", violations[0])

    def test_benign_neighbours_stay_green(self) -> None:
        # Not installs, or installs of plain pins: none may be flagged.
        for run in ("python -m pip download --require-hashes -r x.lock -d w", "python -m pip check",
                    "pip --version", 'python -m pip install --find-links "$W" pyyaml==6.0.3',
                    "python -m pip install --quiet \\\n  pyyaml==6.0.3 \\\n  pytest==9.1.1",
                    "echo 'unbalanced", "python -m pip install mcp-types==2.3.0"):
            with self.subTest(run=run):
                docs = self._base()
                docs["new.yml"] = _wf(run)
                violations, _ = self._check(docs)
                self.assertEqual(violations, [], run)

    def test_the_seat_pin_drifting_to_2x_or_into_another_harness_job_fails(self) -> None:
        for job, spec in ((MCP_JOB, "mcp==2.3.0"), ("codex-harness", "mcp==1.27.1")):
            with self.subTest(job=job, spec=spec):
                docs = self._base()
                docs[HARNESS.name] = _wf(f'"$V/python" -m pip install --quiet pytest==9.1.1 {spec}', job)
                violations, _ = self._check(docs)
                self.assertTrue(any("installs mcp directly" in v for v in violations), violations)

    def test_the_lock_install_out_of_place_or_twice_fails(self) -> None:
        cases = {
            "another workflow": {"ci.yml": _wf(LOCK_RUN, "test")},
            "another job": {ADAPTER_WORKFLOW: {"jobs": {ADAPTER_JOB: {"steps": [{"run": LOCK_RUN}]},
                                                       "extra": {"steps": [{"run": LOCK_RUN}]}}}},
            "twice in the job": {ADAPTER_WORKFLOW: _wf(LOCK_RUN + "\n" + LOCK_RUN, ADAPTER_JOB)},
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                docs = self._base()
                docs.update(extra)
                violations, _ = self._check(docs)
                self.assertTrue(violations, label)

    def test_the_lock_install_without_its_flags_fails(self) -> None:
        for drop in ("--require-hashes ", "--no-deps ", "--only-binary :all: "):
            with self.subTest(dropped=drop):
                docs = self._base()
                docs[ADAPTER_WORKFLOW] = _wf(LOCK_RUN.replace(drop, ""), ADAPTER_JOB)
                violations, _ = self._check(docs)
                self.assertTrue(any("adapter lock install refused" in v for v in violations), violations)

    def test_the_requirement_option_spellings_are_all_read(self) -> None:
        for spelling in (f"-r {ADAPTER_LOCK}", f"--requirement {ADAPTER_LOCK}",
                         f"--requirement={ADAPTER_LOCK}", f"-r{ADAPTER_LOCK}"):
            with self.subTest(spelling=spelling):
                docs = self._base()
                docs["ci.yml"] = _wf(f"python -m pip install --require-hashes --no-deps "
                                     f"--only-binary=:all: {spelling}", "test")
                violations, _ = self._check(docs)
                self.assertTrue(any("ci.yml:test: adapter lock install refused" in v for v in violations),
                                violations)

    def test_a_requirements_or_constraints_file_that_pins_mcp_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="t4858-req-") as tmp:
            repo = Path(tmp)
            (repo / "tools/host_mcp_adapter/dist").mkdir(parents=True)
            (repo / ADAPTER_LOCK).write_text((REPO / ADAPTER_LOCK).read_text(encoding="utf-8"), encoding="utf-8")
            (repo / "other.txt").write_text("pyyaml==6.0.3\nmcp==1.27.1 \\\n  --hash=sha256:00\n", encoding="utf-8")
            (repo / "clean.txt").write_text("pyyaml==6.0.3\nmcp-types==2.3.0\n", encoding="utf-8")
            for run, bad in (("python -m pip install -r other.txt", True),
                             ("python -m pip install -c other.txt pyyaml==6.0.3", True),
                             ("python -m pip install -r clean.txt", False),
                             ("python -m pip install -r missing.txt", True),
                             ('python -m pip install -r "$X/req.txt"', True)):
                with self.subTest(run=run):
                    docs = {ADAPTER_WORKFLOW: _wf(LOCK_RUN, ADAPTER_JOB), "new.yml": _wf(run)}
                    violations, _ = mcp_install_violations(docs, repo)
                    self.assertEqual(bool(violations), bad, violations)

    def test_a_lock_that_stops_pinning_mcp_2x_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="t4858-lock-") as tmp:
            repo = Path(tmp)
            (repo / "tools/host_mcp_adapter/dist").mkdir(parents=True)
            (repo / ADAPTER_LOCK).write_text("mcp==1.27.1 \\\n    --hash=sha256:00\n", encoding="utf-8")
            violations, _ = mcp_install_violations({ADAPTER_WORKFLOW: _wf(LOCK_RUN, ADAPTER_JOB)}, repo)
            self.assertTrue(any("not 2.x" in v for v in violations), violations)


class PipInstallParser(unittest.TestCase):
    def test_reads_the_arguments_after_pip_install(self) -> None:
        self.assertEqual(_pip_installs('"$X/python" -m pip install --quiet a==1 b==2.0'),
                         [["a==1", "b==2.0"]])

    def test_finds_nothing_without_an_install(self) -> None:
        self.assertEqual(_pip_installs("python -m venv x\npython -m pytest tests/"), [])


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
