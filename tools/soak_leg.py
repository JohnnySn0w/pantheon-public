#!/usr/bin/env python3
"""Pantheon suite-health soak LEG runner (T-3393): the run each account does in-session.

tools/soak_filer.py files one leg ticket per account each month (T-0991/T-0992 option b:
each account runs its OWN leg, no cross-account sudo). This is what the leg runs. Its job
is that any red it reports is real: the only structural red class left by 2026-10 was
pytest-absent (about 60 tools/ test modules import pytest; some accounts' system python3
lacks it), so the leg no longer runs on the system interpreter. It provisions its own
venv on CI's exact pins and runs the suite the way CI does, so the environment is CI's on
every account and expected structural red is zero.

Steps:
  0. Root: by default the leg does NOT run in the seat's live checkout, whose seat state
     (canonical path, local files) makes canonical-path tests red (T-5817). It runs
     `git fetch origin` (skip with --no-fetch), `git worktree add --detach <out>/repo
     <ref>` (--ref, default origin/main), soaks that, and removes the worktree with
     `git worktree remove --force` in a finally, keeping the out dir's logs and report.
     --in-place runs in the current checkout instead (debugging only).
  1. Pins: parse the `pkg==ver` tokens from the `run:` line of CI's "Install test
     dependencies" step in the soaked root's .github/workflows/ci.yml, so the leg cannot
     drift from CI. The parse is strict: any install token that is not a `pkg==ver` pin,
     --upgrade/-U or `pip` is an error, and a pytest== pin is required.
     No pins found is an error (exit 2), never an unpinned install.
  2. Venv: build or reuse one at ~/.cache/pantheon/soak-venv (--venv or
     $PANTHEON_SOAK_VENV), under an flock on <venv>.lock. The interpreter major.minor and
     the pins are recorded in a marker file inside it; a matching marker means reuse,
     anything else means (re)install. A venv from another minor, or whose pip does not run,
     is removed (only if it is exactly the configured path and holds pyvenv.cfg and the
     runner's own .soak-owned marker, so a venv you pass with --venv is never deleted) and
     rebuilt. The first run needs network for pip.
  3. Suite: `python -m pytest tools/` with CI's flags (-q, -o faulthandler_timeout=600,
     -o junit_family=xunit1, --junitxml and --basetemp under the leg's out dir), with
     PANTHEON_DISABLE_LIVE_TELEGRAM=1, seat-only env stripped (PYTEST_ADDOPTS, PYTHONPATH,
     VIRTUAL_ENV, TMUX, the repo-redirect GIT_* variables, ...), niced, cwd = the soaked
     root, a bounded -n (default 2: seats share a CPU budget). Full output goes to <out>/pytest.log; the basetemp is
     removed on GREEN (kept on RED for diagnosis). The seat's own git identity is used
     (CI sets one only because its runner has none). CI also sets the account-wide
     init.defaultBranch to main; the leg changes no git config (a GIT_CONFIG_* env trips
     pantheon_config_git's sandbox guard), it only reads the seat's value, warns when it
     is not main, and adds defbranch=<value|unset> to the summary.
  4. Outcome: tools/check_shell_suite_outcomes.py on the report (CI's next step), then the
     report's executed/skipped/failed counts via check_suite_outcome_floor.count (the same
     per-(classname, name) counting CI uses). CI's executed_min/skipped_max floor is NOT
     enforced: seat counts legitimately differ, so they are reported instead.
  5. One summary line, `SOAK LEG: sha=<soaked commit> py=X.Y executed=N skipped=S failed=F
     pytest_rc=R shell_outcomes_rc=Q -> GREEN|RED`, then the failing test ids when RED.

GREEN = pytest exit 0, shell outcomes exit 0, a readable report with at least one executed
test, and no failed/errored testcase in it. A missing or unreadable report, or one where
every testcase skipped, is RED. SIGTERM exits 143 after the worktree is removed.

CLI:
    python3 tools/soak_leg.py                       # the leg: origin/main, fresh worktree
    python3 tools/soak_leg.py --workers 4 --out-dir DIR --venv DIR --ref REF --no-fetch
    python3 tools/soak_leg.py --in-place            # debug: this checkout, not a soak result
    python3 tools/soak_leg.py --paths tools/test_soak_filer.py tools/oom/
        # smoke on a subset; the shell-outcomes check is skipped (the subset holds no
        # shell-suite cases) and the summary says scope=subset. Not a soak result.

Exit codes: 0 GREEN; 1 RED; 2 setup error (git fetch / worktree add failure, no pins in
ci.yml, venv or pip failure).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.check_suite_outcome_floor import InputError, count  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

CI_INSTALL_STEP = "Install test dependencies"
DEFAULT_VENV = Path.home() / ".cache" / "pantheon" / "soak-venv"
DEFAULT_OUT_ROOT = Path.home() / ".cache" / "pantheon" / "soak-leg"
VENV_ENV = "PANTHEON_SOAK_VENV"
PINS_MARKER = ".soak-pins"
OWNED_MARKER = ".soak-owned"  # written right after `python -m venv`: this runner built it
DEFAULT_WORKERS = 2
DEFAULT_PATHS = ("tools/",)
DEFAULT_REF = "origin/main"
# CI's "Run tools test suite" flags, minus -n (bounded here) and the report paths.
# test_soak_leg asserts each one still appears in ci.yml's pytest step.
CI_PYTEST_OPTIONS = ("-o", "faulthandler_timeout=600", "-o", "junit_family=xunit1")
_NAMED_FAILURES = 50

# A strict `name[extras]==version` pin: no quotes, no other comparators, no shell syntax.
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
# What may precede `install` (`python -m pip`, `pip3`, ...) and follow it besides pins.
_INSTALL_PREFIX_TOKENS = frozenset({"python", "python3", "-m", "pip", "pip3"})
_INSTALL_FLAGS = frozenset({"--upgrade", "-U", "pip"})
# Seat env CI's runner does not have; any of it can change what pytest collects or imports
# (Recovery's T-5031 note). PANTHEON_DISABLE_LIVE_TELEGRAM=1 is set after the strip.
STRIPPED_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "TMUX")
SIGTERM_EXIT = 143

Runner = Callable[..., "subprocess.CompletedProcess"]


class SetupError(Exception):
    """The leg could not be provisioned (no pins, venv or pip failure): exit 2."""


# ---------------------------------------------------------------------------
# 1. Pins.
# ---------------------------------------------------------------------------


def parse_ci_pins(ci_text: str, step_name: str = CI_INSTALL_STEP) -> tuple[str, ...]:
    """The `pkg==ver` pins of the named step's `run:` line in a workflow's text.

    Text-parsed on purpose: the system python3 this runs under may lack pyyaml (that is the
    class this tool exists for). Strict, never a silent drop: the run line must be one
    `[python -m] pip install` command whose arguments are only `pkg==ver` pins, `--upgrade`
    / `-U` and the bare `pip`; anything else (an unpinned package, `-r file`, a `>=` spec, a
    quoted pin, `&&`, `${VAR}`) raises SetupError naming the token, as does a missing step,
    run line, or `pytest==` pin.
    """
    lines = ci_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^\s*-\s*name:\s*['\"]?{re.escape(step_name)}['\"]?\s*$", line):
            start = i
            break
    if start is None:
        raise SetupError(f"no '{step_name}' step in the CI workflow")
    for line in lines[start + 1:]:
        stripped = line.strip()
        if re.match(r"^-\s*name:", stripped):
            break
        if stripped.startswith("run:"):
            return _parse_install_command(stripped[len("run:"):].split(), step_name)
    raise SetupError(f"the '{step_name}' step has no run line")


def _parse_install_command(tokens: list[str], step_name: str) -> tuple[str, ...]:
    if "install" not in tokens:
        raise SetupError(f"the '{step_name}' step's run line is not a pip install: {' '.join(tokens)!r}")
    at = tokens.index("install")
    prefix = tokens[:at]
    if not prefix or prefix[-1] not in ("pip", "pip3") or any(
            t not in _INSTALL_PREFIX_TOKENS for t in prefix):
        raise SetupError(f"the '{step_name}' step's run line is not a plain `[python -m] pip "
                         f"install`: {' '.join(tokens)!r}")
    pins: list[str] = []
    for tok in tokens[at + 1:]:
        if tok in _INSTALL_FLAGS:
            continue
        if not _PIN.match(tok):
            raise SetupError(f"the '{step_name}' step's install has a token that is not a "
                             f"`pkg==ver` pin, --upgrade/-U or pip: {tok!r}")
        pins.append(tok)
    if not pins:
        raise SetupError(f"the '{step_name}' step's run line pins nothing: {' '.join(tokens)!r}")
    if not any(pin.lower().startswith("pytest==") for pin in pins):
        raise SetupError(f"the '{step_name}' step's pins have no pytest== pin: {pins}")
    return tuple(pins)


def load_ci_pins(ci_path: Path = CI_WORKFLOW) -> tuple[str, ...]:
    try:
        text = Path(ci_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupError(f"cannot read the CI workflow {ci_path}: {exc}") from exc
    return parse_ci_pins(text)


# ---------------------------------------------------------------------------
# 2. Venv.
# ---------------------------------------------------------------------------


def venv_python(venv_dir: Path) -> Path:
    return Path(venv_dir) / "bin" / "python"


def python_tag() -> str:
    """The running interpreter's major.minor: the venv is built from it."""
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def _marker_text(pins: Sequence[str], py: str | None = None) -> str:
    return f"python={py or python_tag()}\n" + "\n".join(pins) + "\n"


def venv_python_tag(venv_dir: Path) -> str | None:
    """The major.minor the venv was built from: its pyvenv.cfg `version`, else the marker's
    `python=` line, else None (unknown)."""
    try:
        for line in (Path(venv_dir) / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("version", "version_info"):
                parts = value.strip().split(".")
                if len(parts) >= 2:
                    return f"{parts[0]}.{parts[1]}"
    except OSError:
        pass
    try:
        first = (Path(venv_dir) / PINS_MARKER).read_text(encoding="utf-8").splitlines()[:1]
    except OSError:
        return None
    return first[0][len("python="):] if first and first[0].startswith("python=") else None


def venv_is_current(venv_dir: Path, pins: Sequence[str]) -> bool:
    """True when the venv's interpreter exists and its marker records exactly these pins on
    this interpreter's major.minor."""
    marker = Path(venv_dir) / PINS_MARKER
    if not venv_python(venv_dir).exists():
        return False
    try:
        return marker.read_text(encoding="utf-8") == _marker_text(pins)
    except OSError:
        return False


def lock_path(venv_dir: Path) -> Path:
    venv_dir = Path(venv_dir)
    return venv_dir.with_name(venv_dir.name + ".lock")


def remove_venv(target: Path, configured: Path) -> None:
    """Delete a venv this runner owns, refusing anything that is not exactly the configured
    venv path, is a symlink, does not look like a venv (no pyvenv.cfg), or was not built by
    this runner (no .soak-owned)."""
    target, configured = Path(target), Path(configured)
    if target.is_symlink() or target.resolve() != configured.resolve():
        raise SetupError(f"refusing to remove {target}: it is not the configured venv {configured}")
    if not (target / "pyvenv.cfg").is_file():
        raise SetupError(f"refusing to remove {target}: no pyvenv.cfg, so it is not a venv; "
                         f"remove it by hand or pass another --venv")
    if not (target / OWNED_MARKER).is_file():
        raise SetupError(f"refusing to remove {target}: no {OWNED_MARKER}, so soak_leg did not "
                         f"build it; remove it by hand or pass another --venv")
    shutil.rmtree(target)


def _pip_works(venv_dir: Path, runner: Runner) -> bool:
    r = runner([str(venv_python(venv_dir)), "-m", "pip", "--version"], capture_output=True, text=True)
    return r.returncode == 0


def ensure_venv(venv_dir: Path, pins: Sequence[str], *, runner: Runner = subprocess.run,
                base_python: str = sys.executable) -> str:
    """Build or reuse the soak venv. Returns 'reused', 'installed' or 'created'. Any
    OSError/ValueError on the way (an unusable path, a permission error) is a SetupError
    (exit 2), never a RED leg."""
    try:
        return _ensure_venv(venv_dir, pins, runner=runner, base_python=base_python)
    except (OSError, ValueError) as exc:
        raise SetupError(f"setting up the venv {venv_dir} failed: {exc}") from exc


def _ensure_venv(venv_dir: Path, pins: Sequence[str], *, runner: Runner,
                 base_python: str) -> str:
    """ensure_venv's body.

    Holds an flock on <venv>.lock while it creates or installs, so two legs on one account
    cannot build the same venv at once. A venv from another interpreter minor, or one whose
    pip does not run (a half-built venv), is removed and rebuilt (remove_venv refuses any
    other path). The marker is written only after pip succeeds, so a failed install is
    retried next run.
    """
    venv_dir = Path(venv_dir)
    if venv_is_current(venv_dir, pins):
        return "reused"
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path(venv_dir), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if venv_is_current(venv_dir, pins):  # another leg built it while we waited
            return "reused"
        # The dir the interpreter path lives in, re-checked against the configured path.
        built = venv_python(venv_dir).parent.parent
        if venv_python(venv_dir).exists():
            built_py = venv_python_tag(venv_dir)
            if (built_py is not None and built_py != python_tag()) or not _pip_works(venv_dir, runner):
                remove_venv(built, venv_dir)
        action = "installed"
        if not venv_python(venv_dir).exists():
            if venv_dir.exists():
                remove_venv(built, venv_dir)  # a dir without an interpreter: half-built
            r = runner([base_python, "-m", "venv", str(venv_dir)], capture_output=True, text=True)
            if r.returncode != 0:
                raise SetupError(
                    f"creating the venv {venv_dir} failed (rc {r.returncode}); on Debian/Ubuntu the "
                    f"system python needs the python3-venv package: {(r.stderr or '').strip()}")
            (venv_dir / OWNED_MARKER).write_text("built by tools/soak_leg.py (T-3393)\n",
                                                 encoding="utf-8")
            action = "created"
        r = runner([str(venv_python(venv_dir)), "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", *pins], capture_output=True, text=True)
        if r.returncode != 0:
            raise SetupError(f"pip install {' '.join(pins)} into {venv_dir} failed (rc {r.returncode}; "
                             f"the first run needs network): {(r.stderr or '').strip()}")
        (venv_dir / PINS_MARKER).write_text(_marker_text(pins), encoding="utf-8")
        return action


# ---------------------------------------------------------------------------
# 3. Suite.
# ---------------------------------------------------------------------------


def junit_path(out_dir: Path) -> Path:
    return Path(out_dir) / "pytest-tools.xml"


def build_pytest_argv(python: Path, out_dir: Path, workers: int,
                      paths: Sequence[str] = DEFAULT_PATHS) -> list[str]:
    out_dir = Path(out_dir)
    return ["nice", "-n", "10", str(python), "-m", "pytest", *paths, "-q",
            "-n", str(workers), *CI_PYTEST_OPTIONS,
            f"--junitxml={junit_path(out_dir)}", f"--basetemp={out_dir / 'pytest-basetemp'}"]


def repo_redirect_env(*, runner: Runner = subprocess.run) -> frozenset[str]:
    """The git variables that redirect a repo: git's own `git rev-parse --local-env-vars`
    plus GIT_NAMESPACE. That is the set pantheon_config_git.REPO_REDIRECT_ENV documents, whose
    sandbox guard refuses to run while any is set; test_soak_leg pins this as a superset of
    it. Asked of git, not imported: nothing outside tests may import that handler
    (test_nothing_outside_tests_imports_the_real_handler). SetupError if git cannot answer."""
    try:
        r = runner(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True)
    except OSError as exc:
        raise SetupError(f"git rev-parse --local-env-vars failed: {exc}") from exc
    names = (r.stdout or "").split()
    if r.returncode != 0 or not names:
        raise SetupError(f"git rev-parse --local-env-vars failed (rc {r.returncode}): "
                         f"{(r.stderr or '').strip()}")
    return frozenset(names) | {"GIT_NAMESPACE"}


def build_pytest_env(base: dict | None = None, *, redirect: frozenset[str] | None = None) -> dict:
    """The seat env minus what CI's runner lacks: STRIPPED_ENV and every repo-redirect git
    variable (repo_redirect_env; CI sets none), plus the mandatory telegram flag. It adds NO
    git variable: GIT_CONFIG_COUNT and its kin trip pantheon_config_git's sandbox guard (r2:
    6 failed + 57 errors in its tests). The seat's own git identity is kept."""
    env = dict(os.environ if base is None else base)
    for key in (*STRIPPED_ENV, *(repo_redirect_env() if redirect is None else redirect)):
        env.pop(key, None)
    env["PANTHEON_DISABLE_LIVE_TELEGRAM"] = "1"
    return env


DEFAULT_BRANCH_ARGV = ("git", "config", "--global", "--get", "init.defaultBranch")


def default_branch(*, runner: Runner = subprocess.run) -> str | None:
    """The seat's account-wide init.defaultBranch (CI sets main), or None when unset or
    unreadable. Read-only. The leg runs as the seat itself, so this is the value its tests
    see."""
    try:
        r = runner(list(DEFAULT_BRANCH_ARGV), capture_output=True, text=True)
    except OSError:
        return None
    value = (r.stdout or "").strip()
    return value if r.returncode == 0 and value else None


def run_pytest(argv: Sequence[str], out_dir: Path, *, runner: Runner = subprocess.run,
               repo_root: Path = REPO_ROOT, env: dict | None = None) -> int:
    log = Path(out_dir) / "pytest.log"
    with log.open("w", encoding="utf-8") as fh:
        r = runner(list(argv), cwd=str(repo_root), env=build_pytest_env() if env is None else env,
                   stdout=fh, stderr=subprocess.STDOUT)
    return r.returncode


def run_shell_outcomes(xml: Path, out_dir: Path, *, runner: Runner = subprocess.run,
                       repo_root: Path = REPO_ROOT) -> int:
    r = runner([sys.executable, str(repo_root / "tools" / "check_shell_suite_outcomes.py"), str(xml)],
               cwd=str(repo_root), capture_output=True, text=True)
    text = (r.stdout or "") + (r.stderr or "")
    (Path(out_dir) / "shell-outcomes.log").write_text(text, encoding="utf-8")
    if r.returncode != 0 and text.strip():
        print(text.rstrip(), file=sys.stderr)
    return r.returncode


# ---------------------------------------------------------------------------
# Soaked root: a fresh detached worktree of origin/main (T-5817), or --in-place.
# ---------------------------------------------------------------------------


def worktree_dir(out_dir: Path) -> Path:
    return Path(out_dir) / "repo"


def fetch_origin(repo_root: Path, *, runner: Runner = subprocess.run) -> None:
    r = runner(["git", "-C", str(repo_root), "fetch", "--quiet", "origin"],
               capture_output=True, text=True)
    if r.returncode != 0:
        raise SetupError(f"git fetch origin failed (rc {r.returncode}; pass --no-fetch to soak "
                         f"the last-fetched ref): {(r.stderr or '').strip()}")


def add_worktree(repo_root: Path, out_dir: Path, ref: str, *, runner: Runner = subprocess.run) -> Path:
    wt = worktree_dir(out_dir)
    r = runner(["git", "-C", str(repo_root), "worktree", "add", "--detach", str(wt), ref],
               capture_output=True, text=True)
    if r.returncode != 0:
        raise SetupError(f"git worktree add --detach {wt} {ref} failed (rc {r.returncode}): "
                         f"{(r.stderr or '').strip()}")
    return wt


def remove_worktree(repo_root: Path, wt: Path, *, runner: Runner = subprocess.run) -> int:
    r = runner(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt)],
               capture_output=True, text=True)
    if r.returncode != 0:
        print(f"soak_leg: WARNING: git worktree remove --force {wt} failed (rc {r.returncode}): "
              f"{(r.stderr or '').strip()}", file=sys.stderr)
    return r.returncode


def head_sha(root: Path, *, runner: Runner = subprocess.run) -> str:
    r = runner(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
               capture_output=True, text=True)
    sha = (r.stdout or "").strip()
    return sha if r.returncode == 0 and sha else "?"


# ---------------------------------------------------------------------------
# 4-5. Outcome.
# ---------------------------------------------------------------------------


def evaluate(xml: Path, pytest_rc: int, shell_rc: int | None, *, subset: bool = False,
             sha: str = "?", py: str | None = None,
             defbranch: str | None = "main") -> tuple[bool, list[str]]:
    """(green, lines): the summary line first, then the failing ids / reason when RED.
    shell_rc None means the shell-outcomes check was skipped (a --paths subset); sha is the
    soaked commit, py the venv's interpreter, defbranch the seat's account-wide
    init.defaultBranch (None = unset; shown only when it is not CI's main, and it never
    decides the verdict). Zero executed tests is RED: a report of only
    skips proves nothing ran."""
    try:
        c = count(xml)
        counts = f"executed={c['executed']} skipped={c['skipped']} failed={len(c['failed'])}"
        failed, reason = c["failed"], None
        if c["executed"] == 0:
            reason = "no test executed (every testcase skipped): nothing was soaked"
    except InputError as exc:
        counts = "executed=? skipped=? failed=?"
        failed, reason = [], f"junit report unusable: {exc}"
    shell = "skipped" if shell_rc is None else str(shell_rc)
    green = reason is None and not failed and pytest_rc == 0 and shell_rc in (0, None)
    scope = " scope=subset" if subset else ""
    branch = "" if defbranch == "main" else f" defbranch={defbranch or 'unset'}"
    lines = [f"SOAK LEG: sha={sha} py={py or python_tag()}{branch} {counts} pytest_rc={pytest_rc} "
             f"shell_outcomes_rc={shell}{scope} -> {'GREEN' if green else 'RED'}"]
    if reason:
        lines.append(f"  {reason}")
    for tid in failed[:_NAMED_FAILURES]:
        lines.append(f"  FAILED {tid}")
    if len(failed) > _NAMED_FAILURES:
        lines.append(f"  ... {len(failed) - _NAMED_FAILURES} more (see the junit report)")
    return green, lines


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _default_out_dir() -> Path:
    return DEFAULT_OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run this account's suite-health soak leg on CI's "
                                            "pinned pytest (T-3393).")
    p.add_argument("--venv", type=Path, default=None,
                   help=f"venv dir (default ${VENV_ENV}, else {DEFAULT_VENV})")
    p.add_argument("--out-dir", type=Path, default=None,
                   help=f"report/log dir (default {DEFAULT_OUT_ROOT}/<UTC timestamp>)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"pytest-xdist -n (default {DEFAULT_WORKERS}; seats share a CPU budget)")
    p.add_argument("--paths", nargs="+", default=None,
                   help="smoke only: run these test paths instead of tools/ (skips the "
                        "shell-outcomes check; the result is not a soak leg)")
    p.add_argument("--ref", default=DEFAULT_REF,
                   help=f"commit to soak in a fresh detached worktree (default {DEFAULT_REF})")
    p.add_argument("--no-fetch", action="store_true",
                   help="do not `git fetch origin` first (soak the last-fetched ref)")
    p.add_argument("--in-place", action="store_true",
                   help="debug only: run in this checkout instead of a fresh worktree (its "
                        "seat state can make canonical-path tests red; not a soak result)")
    p.add_argument("--ci-workflow", type=Path, default=None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be >= 1")
    return args


def _soak(args: argparse.Namespace, root: Path, out_dir: Path, venv_dir: Path,
          paths: tuple[str, ...], runner: Runner) -> int:
    subset = paths != DEFAULT_PATHS
    try:
        pins = load_ci_pins(args.ci_workflow or root / ".github" / "workflows" / "ci.yml")
        action = ensure_venv(venv_dir, pins, runner=runner)
        env = build_pytest_env(redirect=repo_redirect_env(runner=runner))
    except SetupError as exc:
        print(f"soak_leg: {exc}", file=sys.stderr)
        return 2
    print(f"soak_leg: venv {venv_dir} {action} ({' '.join(pins)})")
    defbranch = default_branch(runner=runner)
    if defbranch != "main":
        print(f"soak_leg: WARNING: init.defaultBranch is {defbranch or 'unset'}, not main as on "
              f"CI; a test that inits a repo may see another default branch", file=sys.stderr)
    sha = head_sha(root, runner=runner)
    xml = junit_path(out_dir)
    print(f"soak_leg: running pytest {' '.join(paths)} -n {args.workers} in {root} (sha {sha}); "
          f"log {out_dir / 'pytest.log'}", flush=True)
    pytest_rc = run_pytest(build_pytest_argv(venv_python(venv_dir), out_dir, args.workers, paths),
                           out_dir, runner=runner, repo_root=root, env=env)
    shell_rc = None if subset else run_shell_outcomes(xml, out_dir, runner=runner, repo_root=root)
    green, lines = evaluate(xml, pytest_rc, shell_rc, subset=subset, sha=sha, defbranch=defbranch)
    if green:
        # A full run's tmp_path dirs are large and only useful for diagnosing red; the
        # report and logs stay. Seats share one disk.
        shutil.rmtree(out_dir / "pytest-basetemp", ignore_errors=True)
    print("\n".join(lines))
    return 0 if green else 1


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise SystemExit(SIGTERM_EXIT)


def install_sigterm_handler() -> None:
    """SIGTERM becomes SystemExit(143): the worktree removal in main's `finally` runs, and
    subprocess.run kills its running child on the way out."""
    signal.signal(signal.SIGTERM, _on_sigterm)


def main(argv: list[str] | None = None, *, runner: Runner = subprocess.run,
         repo_root: Path = REPO_ROOT) -> int:
    args = parse_args(argv)
    install_sigterm_handler()
    # Absolute: git -C, pytest's cwd and the venv interpreter each resolve from a different dir.
    venv_dir = (args.venv or Path(os.environ.get(VENV_ENV) or DEFAULT_VENV)).resolve()
    out_dir = (args.out_dir or _default_out_dir()).resolve()
    paths = tuple(args.paths) if args.paths else DEFAULT_PATHS
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.in_place:
            wt = None
        else:
            if not args.no_fetch:
                fetch_origin(repo_root, runner=runner)
            wt = add_worktree(repo_root, out_dir, args.ref, runner=runner)
    except (OSError, ValueError) as exc:  # e.g. git missing, an unwritable out dir: setup, not RED
        print(f"soak_leg: setting up the leg failed: {exc}", file=sys.stderr)
        return 2
    except SetupError as exc:
        print(f"soak_leg: {exc}", file=sys.stderr)
        return 2
    if wt is None:
        return _soak(args, repo_root, out_dir, venv_dir, paths, runner)
    try:
        return _soak(args, wt, out_dir, venv_dir, paths, runner)
    finally:
        if remove_worktree(repo_root, wt, runner=runner) == 0:
            print(f"soak_leg: removed worktree {wt}")


if __name__ == "__main__":
    sys.exit(main())
