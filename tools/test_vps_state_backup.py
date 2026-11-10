"""Tests for the permission hardening in vps_state_backup.sh (T-4546).

The root-run backup copies every seat's memory pins, HANDOFF.md files and kino.db into
one checkout. Left at root's default umask 022 that checkout was world-readable and
flattened the 0750 home cordon (T-4546 / ORG-012 sweep 2026-10-04 item B5). The fix is a
`umask 077` plus a self-healing `chmod 0700` of the checkout and its parent on every run.

WHY THIS SHAPE, not an end-to-end run of the script. The script reads every account's
home, /etc/kino, /etc/systemd/system and the kino DB, so a real run is non-hermetic: it
would copy whatever seat state the test host holds into a temp dir, which is the exact
class of leak this ticket closes, and its result would differ per host. The perms setup is
instead fenced between two marker comments in the script; these tests (a) run exactly
that fenced block in a bash subshell against a temp REPO_DIR and assert the resulting
modes, and (b) statically pin that the block, and the call that applies it, sit before the
script's first filesystem-mutating command. The script body is never executed, so nothing
is copied, committed or pushed, and /var/lib/vps-backup is never touched.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "vps_state_backup.sh"
BEGIN = "# --- T-4546 perms preamble: begin ---"
END = "# --- T-4546 perms preamble: end ---"
HARDEN_FN = "harden_backup_perms"

# A command that creates, copies, deletes or commits something. `[ -d ... ]` tests,
# `command -v sqlite3` and `echo` are deliberately not in this set.
_MUTATOR = re.compile(
    r"\b(?:mkdir|cp|rm)\s+-"
    r"|\bsqlite3\s+[/-]"
    r"|\bgit\s+(?:add|-c|push|commit)\b"
    r"|^\s*find\s"
)


def _script_lines() -> list[str]:
    return SCRIPT.read_text().splitlines()


def _preamble() -> str:
    """The fenced perms block, verbatim. Fails loudly if the markers are gone."""
    lines = _script_lines()
    begins = [i for i, ln in enumerate(lines) if ln.strip() == BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == END]
    assert len(begins) == 1 and len(ends) == 1, (
        f"expected exactly one preamble fence in {SCRIPT.name}, "
        f"found begin={begins} end={ends}"
    )
    assert begins[0] < ends[0], "preamble end marker precedes begin marker"
    return "\n".join(lines[begins[0] + 1 : ends[0]])


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _run_bash(program: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, timeout=30
    )


class ScriptShape(unittest.TestCase):
    def test_script_parses(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_preamble_is_fenced_and_defines_the_hardener(self):
        block = _preamble()
        self.assertRegex(block, r"(?m)^umask 077\s*$")
        self.assertRegex(block, rf"(?m)^{HARDEN_FN}\(\)\s*\{{")

    def test_umask_and_hardening_precede_every_mutating_command(self):
        lines = _script_lines()
        code = [
            (i, ln) for i, ln in enumerate(lines) if not ln.lstrip().startswith("#")
        ]
        end_idx = next(i for i, ln in enumerate(lines) if ln.strip() == END)
        first_mut = next(i for i, ln in code if _MUTATOR.search(ln))
        call_idx = [
            i for i, ln in code if ln.strip() == HARDEN_FN and i > end_idx
        ]
        self.assertEqual(len(call_idx), 1, f"expected one {HARDEN_FN} call, got {call_idx}")
        self.assertLess(end_idx, first_mut, "umask preamble must precede the first mutator")
        self.assertLess(call_idx[0], first_mut, "chmod hardening must precede the first mutator")


class PreambleBehaviour(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.root.chmod(0o755)
        self.parent = self.root / "vps-backup"
        self.repo = self.parent / "pantheon-vps-backup"
        (self.repo / ".git").mkdir(parents=True)

    def _preamble_program(self, tail: str, umask: str = "022") -> str:
        # `umask` set to root's default BEFORE the block, so only the block can change it.
        return (
            f"set -euo pipefail\numask {umask}\n"
            f"REPO_DIR={self.repo!s}\n{_preamble()}\n{tail}\n"
        )

    def test_block_sets_umask_077_over_a_022_start(self):
        r = _run_bash(
            self._preamble_program(
                f'mkdir "{self.repo}/newdir"; touch "{self.repo}/newfile"'
            )
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_mode(self.repo / "newdir"), 0o700)
        self.assertEqual(_mode(self.repo / "newfile"), 0o600)

    def test_hardener_heals_a_loosened_parent_and_checkout(self):
        self.parent.chmod(0o755)
        self.repo.chmod(0o755)
        r = _run_bash(self._preamble_program(HARDEN_FN))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_mode(self.parent), 0o700)
        self.assertEqual(_mode(self.repo), 0o700)

    def test_hardener_touches_only_the_parent_and_the_checkout(self):
        grandparent_before = _mode(self.root)
        inner = self.repo / "hosts"
        inner.mkdir()
        inner.chmod(0o755)
        kept = self.repo / "kept.txt"
        kept.write_text("x")
        kept.chmod(0o644)
        r = _run_bash(self._preamble_program(HARDEN_FN))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_mode(self.root), grandparent_before)
        # Modes preserved by `cp -a` inside are intentionally left alone: the 0700
        # checkout root is the barrier, and a recursive chmod would hide drift.
        self.assertEqual(_mode(inner), 0o755)
        self.assertEqual(_mode(kept), 0o644)

    def test_hardener_is_idempotent(self):
        self.parent.chmod(0o700)
        self.repo.chmod(0o700)
        r = _run_bash(self._preamble_program(f"{HARDEN_FN}\n{HARDEN_FN}"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_mode(self.parent), 0o700)
        self.assertEqual(_mode(self.repo), 0o700)

    def test_trailing_slash_override_still_hardens_the_right_parent(self):
        self.parent.chmod(0o755)
        self.repo.chmod(0o755)
        prog = self._preamble_program(HARDEN_FN).replace(
            f"REPO_DIR={self.repo!s}\n", f"REPO_DIR={self.repo!s}/\n"
        )
        r = _run_bash(prog)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_mode(self.parent), 0o700)
        self.assertEqual(_mode(self.repo), 0o700)
        self.assertEqual(_mode(self.root), 0o755)


STRIP_BEGIN = "# --- T-4871 nested-git strip: begin ---"
STRIP_END = "# --- T-4871 nested-git strip: end ---"


def _strip_block() -> str:
    lines = _script_lines()
    b = [i for i, ln in enumerate(lines) if ln.strip() == STRIP_BEGIN]
    e = [i for i, ln in enumerate(lines) if ln.strip() == STRIP_END]
    assert len(b) == 1 and len(e) == 1 and b[0] < e[0], (
        f"expected one nested-git strip fence in {SCRIPT.name}, found begin={b} end={e}"
    )
    return "\n".join(lines[b[0] + 1 : e[0]])


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )


class NestedGitStrip(unittest.TestCase):
    """T-4871. A linked worktree's `.git` is a FILE (`gitdir: <live repo>/.git/worktrees/X`).
    The old strip removed only `.git` DIRECTORIES, so a copied worktree kept its pointer
    back into the live seat repo, and root's `git add -A` in the backup checkout refreshed
    that live worktree's index as root:root 0600. The seat's own hourly snapshot then failed
    with EACCES (archivist-state-snapshot, 2026-10-11..12)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        # The LIVE seat repo with a linked worktree INSIDE it (as in the specimen).
        self.live = root / "live-memory"
        self.live.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.live)
        (self.live / "pin.md").write_text("x\n")
        _git("add", "pin.md", cwd=self.live)
        _git("commit", "-qm", "init", cwd=self.live)
        wt = self.live / ".claude" / "worktrees" / "agent-x"
        r = _git("worktree", "add", "-q", "-b", "wt", str(wt), cwd=self.live)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.live_index = self.live / ".git" / "worktrees" / "agent-x" / "index"
        # The BACKUP checkout, holding a `cp -a` of the live dir (as the script does).
        self.repo = root / "backup"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        dst = self.repo / "hosts" / "exocortex" / "memory" / "slug"
        dst.mkdir(parents=True)
        subprocess.run(["cp", "-a", f"{self.live}/.", str(dst)], check=True)
        self.copied_wt_git = dst / ".claude" / "worktrees" / "agent-x" / ".git"
        self.assertTrue(self.copied_wt_git.is_file(), "fixture: worktree .git must be a FILE")

    def _run_strip(self):
        r = _run_bash(f"set -euo pipefail\nREPO_DIR={self.repo!s}\n{_strip_block()}")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_strip_removes_nested_git_files_and_dirs_but_keeps_top_level(self):
        self._run_strip()
        left = [p for p in self.repo.rglob(".git") if p != self.repo / ".git"]
        self.assertEqual(left, [], f"nested .git entries survived: {left}")
        self.assertTrue((self.repo / ".git").is_dir(), "backup repo's own .git must survive")

    def test_git_add_after_strip_never_touches_the_live_worktree(self):
        before = self.live_index.stat().st_mtime_ns
        os.utime(self.live_index, ns=(before - 10**9, before - 10**9))
        pinned = self.live_index.stat().st_mtime_ns
        self._run_strip()
        r = _git("add", "-A", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        _git("status", "--porcelain=2", cwd=self.repo)
        self.assertEqual(self.live_index.stat().st_mtime_ns, pinned,
                         "backup git touched the LIVE worktree index")
        staged = _git("ls-files", "-s", cwd=self.repo).stdout
        self.assertNotIn("160000 ", staged, "a gitlink was recorded instead of content")


MIRROR_BEGIN = "# --- T-5993 mirror leg: begin ---"
MIRROR_END = "# --- T-5993 mirror leg: end ---"

_STUB_OK = (
    "import os, pathlib\n"
    "def mirror_push_after_origin(cwd, refspec):\n"
    "    pathlib.Path(os.environ['STUB_LOG']).write_text(f'{cwd}|{refspec}')\n"
    "    return 'mirror pushed'\n"
)


def _mirror_block() -> str:
    lines = _script_lines()
    b = [i for i, ln in enumerate(lines) if ln.strip() == MIRROR_BEGIN]
    e = [i for i, ln in enumerate(lines) if ln.strip() == MIRROR_END]
    assert len(b) == 1 and len(e) == 1 and b[0] < e[0], (
        f"expected one mirror-leg fence in {SCRIPT.name}, found begin={b} end={e}"
    )
    return "\n".join(lines[b[0] + 1 : e[0]])


class MirrorLeg(unittest.TestCase):
    """T-5993: the ORG-101 dual-write leg for root's backup push. Root must never import
    code from a claude-writable checkout, so the leg imports _git_utils only from a lib
    dir that the running uid owns and nobody else can write. The fenced block (the
    mirror_leg function plus the push branch) runs against a temp checkout with a bare
    origin and a STUB _git_utils; nothing real is pushed and no lib is installed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        _git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.repo = root / "backup"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        (self.repo / "f").write_text("x\n")
        _git("add", "f", cwd=self.repo)
        _git("commit", "-qm", "c", cwd=self.repo)
        self.lib = root / "lib"
        (self.lib / "tools").mkdir(parents=True)
        os.chmod(self.lib, 0o755)
        os.chmod(self.lib / "tools", 0o755)
        self._stub(_STUB_OK)
        self.log = root / "stub.log"

    def _stub(self, body):
        f = self.lib / "tools" / "_git_utils.py"
        f.write_text(body)
        os.chmod(f, 0o644)

    def _run(self, *, no_push="0", lib=None):
        prog = (f"set -euo pipefail\nREPO_DIR={self.repo!s}\ncd \"$REPO_DIR\"\n"
                f"export VPS_BACKUP_NO_PUSH={no_push}\n"
                f"export VPS_BACKUP_MIRROR_TOOLS_DIR={lib or self.lib}\n"
                f"export STUB_LOG={self.log}\n{_mirror_block()}\n")
        return _run_bash(prog)

    def test_good_push_calls_the_leg_with_main_main_and_exits_0(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(), f"{self.repo}|main:main")
        self.assertIn("vps_state_backup: mirror leg: mirror pushed", r.stdout)

    def test_no_push_makes_no_call(self):
        r = self._run(no_push="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.log.exists())

    def test_broken_origin_exits_non_zero_and_never_calls_the_leg(self):
        _git("remote", "set-url", "origin", "/nonexistent/origin.git", cwd=self.repo)
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_a_raising_stub_still_exits_0(self):
        self._stub("def mirror_push_after_origin(cwd, refspec):\n    raise RuntimeError('boom')\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vps_state_backup: mirror leg: error", r.stdout)

    def test_a_stub_that_exits_non_zero_still_exits_0(self):
        self._stub("import sys\ndef mirror_push_after_origin(cwd, refspec):\n    sys.exit(3)\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vps_state_backup: mirror leg: error", r.stdout)

    def test_inert_mirror_says_inert(self):
        self._stub("def mirror_push_after_origin(cwd, refspec):\n    return None\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vps_state_backup: mirror leg: inert", r.stdout)

    def test_missing_lib_dir_prints_skipped(self):
        r = self._run(lib=Path(self._tmp.name) / "nope")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vps_state_backup: mirror leg: skipped", r.stdout)

    def test_group_writable_lib_file_is_refused(self):
        os.chmod(self.lib / "tools" / "_git_utils.py", 0o664)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vps_state_backup: mirror leg: skipped (unsafe", r.stdout)
        self.assertFalse(self.log.exists())

    def test_world_writable_lib_dir_is_refused(self):
        os.chmod(self.lib, 0o757)
        r = self._run()
        self.assertIn("skipped (unsafe", r.stdout)
        self.assertFalse(self.log.exists())

    def test_symlink_in_lib_is_refused(self):
        os.symlink("/etc/hostname", self.lib / "tools" / "link")
        r = self._run()
        self.assertIn("skipped (unsafe", r.stdout)
        self.assertFalse(self.log.exists())

    def test_pythonpath_cannot_shadow_the_lib(self):
        evil = Path(self._tmp.name) / "evil"
        evil.mkdir()
        (evil / "_git_utils.py").write_text(
            "import os, pathlib\ndef mirror_push_after_origin(c, r):\n"
            "    pathlib.Path(os.environ['STUB_LOG']).write_text('EVIL')\n    return 'evil'\n")
        prog_env = dict(os.environ, PYTHONPATH=str(evil))
        prog = (f"set -euo pipefail\nREPO_DIR={self.repo!s}\ncd \"$REPO_DIR\"\n"
                f"export VPS_BACKUP_NO_PUSH=0 VPS_BACKUP_MIRROR_TOOLS_DIR={self.lib} STUB_LOG={self.log}\n"
                f"{_mirror_block()}\n")
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30, env=prog_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(), f"{self.repo}|main:main")


class UnitEnvironment(unittest.TestCase):
    """T-5993: the system unit has no User=, so HOME is unset unless pinned, git never
    reads /root/.gitconfig, and the mirror leg's credential helper is invisible (the
    first live run failed 'could not read Password')."""

    def test_unit_pins_home_to_root(self):
        unit = (SCRIPT.parent / "systemd" / "pantheon-vps-state-backup.service").read_text()
        self.assertIn("\nEnvironment=HOME=/root\n", unit)


if __name__ == "__main__":
    unittest.main()
