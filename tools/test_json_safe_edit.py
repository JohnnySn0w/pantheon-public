"""T-2329: prove edit_json prevents the lost update, with a CONTROL that fails.

The control is the point. A concurrency test that passes proves nothing unless the
same harness, run against the unsafe idiom, actually loses an update — otherwise
the test may simply never be racing. `test_control_unsafe_idiom_loses_updates`
runs the existing tmp+os.replace-without-locking shape and asserts it DOES lose
writes; if that control ever starts passing, this suite has stopped racing and its
green is meaningless.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Without this the suite only imports when run from inside tools/, while the
# repo's documented invocation is `python3 -m unittest tools.test_<X>` from the
# repo root (tools/README.md). Ten of these tests errored on import that way.
sys.path.insert(0, str(HERE))

WRITERS = 8
KEYS_PER_WRITER = 1

_SAFE = """
import json, sys, time
sys.path.insert(0, {tools!r})
from json_safe_edit import edit_json
i = int(sys.argv[1])
time.sleep(0.05)                      # bunch the writers up so they actually race
with edit_json({path!r}) as cfg:
    entries = cfg.setdefault("entries", [])
    time.sleep(0.02)                  # widen the read-modify-write window
    entries.append(i)
"""

_UNSAFE = """
import json, os, sys, time
i = int(sys.argv[1])
p = {path!r}
time.sleep(0.05)
try:
    cfg = json.loads(open(p).read())
except FileNotFoundError:
    cfg = {{}}
entries = cfg.setdefault("entries", [])
time.sleep(0.02)
entries.append(i)
tmp = p + ".tmp." + str(os.getpid())
with open(tmp, "w") as fh:
    json.dump(cfg, fh)
os.replace(tmp, p)                    # atomic write, unsynchronised read-modify-write
"""


def _run_writers(script_src: str, path: Path) -> list:
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "w.py"
        script.write_text(textwrap.dedent(script_src).format(tools=str(HERE), path=str(path)))
        procs = [subprocess.Popen([sys.executable, str(script), str(i)])
                 for i in range(WRITERS)]
        for p in procs:
            p.wait()
    return json.loads(path.read_text()).get("entries", [])


class TestJsonSafeEdit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        self.path.write_text(json.dumps({"entries": []}))

    def tearDown(self):
        self.dir.cleanup()

    def test_concurrent_writers_all_survive(self):
        got = _run_writers(_SAFE, self.path)
        self.assertEqual(sorted(got), list(range(WRITERS)),
                         f"lost update under edit_json: got {sorted(got)}")

    def test_control_unsafe_idiom_loses_updates(self):
        """CONTROL — if this ever FAILS, the suite has stopped racing.

        Passing means the unsafe path DID lose updates, i.e. the harness is
        genuinely racing and the safe-path green above is meaningful. Failing
        means nothing was lost, so nothing was concurrent, and every other green
        in this file is void.

        (Was inverted here and inherited verbatim into
        bugs/instrument-whose-form-cannot-express-the-failure.md — caught by
        specialist seat d772ea96, 2026-07-29. The assertLess message three lines
        below stated it correctly the whole time, which is exactly why the
        docstring read as checked.)
        """
        got = _run_writers(_UNSAFE, self.path)
        self.assertLess(len(got), WRITERS,
                        "control did not lose an update — the test is no longer "
                        "racing, so the safe-path green above proves nothing")

    def test_exception_in_block_leaves_file_untouched(self):
        before = self.path.read_text()
        from json_safe_edit import edit_json
        with self.assertRaises(ValueError):
            with edit_json(self.path) as cfg:
                cfg["entries"].append("should-not-persist")
                raise ValueError("abort")
        self.assertEqual(self.path.read_text(), before)

    def test_lock_is_a_sidecar_not_the_config_inode(self):
        """The lock object must survive os.replace() swapping the config's inode."""
        from json_safe_edit import edit_json
        before_ino = os.stat(self.path).st_ino
        with edit_json(self.path) as cfg:
            cfg["entries"].append(1)
        self.assertNotEqual(os.stat(self.path).st_ino, before_ino,
                            "config was not atomically replaced")
        self.assertTrue(Path(str(self.path) + ".lock").exists(),
                        "sidecar lock missing — a lock on the config inode would "
                        "have been orphaned by the replace above")

    def test_missing_file_is_created(self):
        from json_safe_edit import edit_json
        p = Path(self.dir.name) / "new.json"
        with edit_json(p) as cfg:
            cfg["k"] = "v"
        self.assertEqual(json.loads(p.read_text()), {"k": "v"})


_LOCK_HOLDER = textwrap.dedent(
    """
    import sys, time, fcntl, os
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o660)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.stderr.write("held\\n"); sys.stderr.flush()
    time.sleep(120)
    """
)


class TestPermissions(unittest.TestCase):
    """os.replace swaps the inode, so the tmp file's mode BECOMES the config's.

    Regression guards for a measured defect in this module's first version: it
    wrote the tmp via plain open(), which silently republished a 0600 config as
    0664. A config editor that widens permissions on settings.json has broken
    something for a reader nobody enumerated.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        self.path.write_text(json.dumps({}))

    def tearDown(self):
        self.dir.cleanup()

    def test_existing_mode_is_preserved_not_widened(self):
        from json_safe_edit import edit_json
        os.chmod(self.path, 0o600)
        with edit_json(self.path) as cfg:
            cfg["x"] = 1
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_existing_group_writable_mode_is_preserved(self):
        from json_safe_edit import edit_json
        os.chmod(self.path, 0o664)
        with edit_json(self.path) as cfg:
            cfg["x"] = 1
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o664)

    def test_create_mode_beats_umask(self):
        from json_safe_edit import edit_json
        p = Path(self.dir.name) / "fresh.json"
        with edit_json(p, create_mode=0o664) as cfg:
            cfg["a"] = 1
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o664)

    def test_sidecar_is_group_writable_for_cross_account_contention(self):
        """A too-narrow sidecar locks the owning seat out of its own settings.

        REGRESSION, AND THIS TEST WAS ITSELF THE BUG (orchestrator, 2026-07-29). The
        first version asserted `mode & 0o060`, which is group-read OR group-write
        — so it passed on 0640, the exact defect it existed to catch. It also
        passed for an environmental reason: this seat's umask is 002, so
        `O_CREAT, 0o660` yielded 0660 here while orchestrator measured 0640 as root
        under umask 022. Too weak AND green by accident.

        So: assert group-WRITE specifically, under a umask that WOULD strip it —
        otherwise the test proves the environment rather than the code.

        NB the parenthetical "flock needs write access" in the original version of
        this docstring is FALSE; see
        `test_read_only_sidecar_is_still_acquirable`. Group-write is still the
        mode we intend and still worth asserting, but it is not what admits a
        peer.
        """
        from json_safe_edit import edit_json
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with edit_json(self.path) as cfg:
            cfg["a"] = 1
        mode = os.stat(str(self.path) + ".lock").st_mode & 0o777
        self.assertTrue(mode & 0o020,
                        f"sidecar not group-WRITABLE: {oct(mode)} — umask stripped "
                        "it at O_CREAT")

    def test_read_only_sidecar_is_still_acquirable(self):
        """flock does NOT need write access — so we must not open the sidecar O_RDWR.

        The cross-account lockout was diagnosed as "umask strips group-write and
        flock needs write access." The second half is false: `flock()` places a
        lock regardless of the fd's access mode. The EACCES on a root-created 0640
        sidecar came from `os.open(..., O_RDWR)` — flock was never reached.

        This matters to the CALLER CONTRACT: with an O_RDONLY open, a group peer
        needs only group-READ, so the 0640 that umask leaves behind is usable and
        a privileged creator does not strictly have to chown.

        A 0440 sidecar is the single-account proxy for that case — not writable
        even by its owner, so it fails under O_RDWR and succeeds under O_RDONLY,
        without needing a second unix account to run the test.
        """
        from json_safe_edit import edit_json
        lock = Path(str(self.path) + ".lock")
        lock.touch()
        os.chmod(lock, 0o440)
        with edit_json(self.path, timeout=2.0) as cfg:
            cfg["a"] = 1
        self.assertEqual(json.loads(self.path.read_text())["a"], 1)
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o440,
                         "a sidecar we did not create must not be re-moded")

    def test_sidecar_created_by_another_mode_is_not_re_moded(self):
        """We must never silently widen a sidecar someone else created."""
        from json_safe_edit import edit_json
        lock = Path(str(self.path) + ".lock")
        lock.touch()
        os.chmod(lock, 0o600)
        with edit_json(self.path) as cfg:
            cfg["a"] = 1
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600,
                         "pre-existing sidecar was re-moded; widening another "
                         "account's lock file is the caller's chown decision")


class TestFailureModes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        self.path.write_text(json.dumps({"entries": []}))
        self.lock = Path(str(self.path) + ".lock")

    def tearDown(self):
        self.dir.cleanup()

    def _hold(self):
        p = subprocess.Popen([sys.executable, "-c", _LOCK_HOLDER, str(self.lock)],
                             stderr=subprocess.PIPE)
        self.addCleanup(p.stderr.close)
        # Wait for the child to CONFIRM it holds the lock rather than sleeping a
        # guessed interval — D5: do not assume the precondition you are testing.
        self.assertEqual(p.stderr.readline(), b"held\n")
        return p

    def test_corrupt_json_raises_rather_than_clobbering(self):
        from json_safe_edit import edit_json
        self.path.write_text("{not json")
        with self.assertRaises(json.JSONDecodeError):
            with edit_json(self.path):
                pass
        self.assertEqual(self.path.read_text(), "{not json",
                         "a corrupt shared config is a finding; overwriting it "
                         "destroys the evidence of how it got that way")

    def test_timeout_raises_rather_than_proceeding_unlocked(self):
        from json_safe_edit import LockTimeout, edit_json
        holder = self._hold()
        try:
            with self.assertRaises(LockTimeout):
                with edit_json(self.path, timeout=0.5):
                    pass
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_lock_frees_when_holder_is_killed(self):
        """No reaper needed: the kernel drops flock when the process dies.

        Contrast T-2292, where a lockfile RECORD naming a pid had to be reaped
        and the reaping is what broke exclusion. There is no record here, so
        there is nothing to go stale and nothing to get wrong.
        """
        from json_safe_edit import LockTimeout, edit_json
        holder = self._hold()
        with self.assertRaises(LockTimeout):
            with edit_json(self.path, timeout=0.3):
                pass

        holder.kill()
        holder.wait(timeout=10)

        with edit_json(self.path, timeout=5.0) as cfg:
            cfg["after"] = True
        self.assertTrue(json.loads(self.path.read_text())["after"])


if __name__ == "__main__":
    unittest.main()
