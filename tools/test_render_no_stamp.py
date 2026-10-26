"""T-3434: render.py --no-stamp (artifacts-only) and the inbox_consumer skill refresh."""
from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import inbox_consumer
from tools.inbox_consumer import SkipCycle, main, refresh_skills_or_warn
from tools.persona_orientation import render

REPO_ROOT = Path(__file__).resolve().parents[1]
BINDINGS = (
    REPO_ROOT / "rulebook" / "persona-orientation" / "source-bindings.yaml",
    REPO_ROOT / "tools" / "persona_orientation" / "sop-skill-bindings.yaml",
)


class NoStampRender(unittest.TestCase):
    def test_artifacts_written_bindings_untouched_check_passes(self):
        before = [p.read_bytes() for p in BINDINGS]
        with tempfile.TemporaryDirectory() as tmp:
            home, codex = Path(tmp) / "home", Path(tmp) / "codex"
            home.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = render.run(REPO_ROOT, home, "write", "all", codex, stamp=False)
            self.assertEqual(rc, 0)
            self.assertTrue(list((home / ".claude" / "skills").glob("*/SKILL.md")))
            self.assertTrue(list((codex / "skills").glob("*/SKILL.md")))
            self.assertEqual(before, [p.read_bytes() for p in BINDINGS])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(render.run(REPO_ROOT, home, "check", "all", codex), 0)

    def test_flag_parsed(self):
        with patch.object(render, "run", return_value=0) as run:
            render.main(["--no-stamp", "--harness", "all"])
        self.assertIs(run.call_args.kwargs["stamp"], False)
        with patch.object(render, "run", return_value=0) as run:
            render.main([])
        self.assertIs(run.call_args.kwargs["stamp"], True)


def _cp(rc):
    return subprocess.CompletedProcess([], rc, "", "")


class RefreshSkills(unittest.TestCase):
    def _run(self, effect):
        err = io.StringIO()
        with patch("tools.inbox_consumer.subprocess.run", side_effect=effect) as run, \
                contextlib.redirect_stderr(err):
            refresh_skills_or_warn("recovery")
        return run, err.getvalue()

    def test_fresh_does_not_render(self):
        run, err = self._run([_cp(0)])
        self.assertEqual(run.call_count, 1)
        self.assertIn("--check", run.call_args.args[0])
        self.assertEqual(err, "")

    def test_stale_renders_no_stamp(self):
        run, err = self._run([_cp(2), _cp(0)])
        self.assertEqual(run.call_count, 2)
        cmd = run.call_args_list[1].args[0]
        self.assertIn("--no-stamp", cmd)
        self.assertEqual(cmd[cmd.index("--harness") + 1], "all")
        self.assertEqual(run.call_args_list[1].kwargs["cwd"], str(inbox_consumer_root()))
        self.assertIn("re-rendered", err)

    def test_stale_render_failure_logged(self):
        run, err = self._run([_cp(2), _cp(1)])
        self.assertIn("re-render exited 1", err)

    def test_check_error_does_not_render(self):
        run, err = self._run([_cp(1)])
        self.assertEqual(run.call_count, 1)
        self.assertIn("exited 1", err)

    def test_timeout(self):
        _, err = self._run(subprocess.TimeoutExpired("render", 120))
        self.assertIn("timed out", err)

    def test_exception(self):
        _, err = self._run(OSError("boom"))
        self.assertIn("failed", err)


def inbox_consumer_root():
    from tools._git_utils import REPO_ROOT as root
    return root


class CycleContinues(unittest.TestCase):
    def test_cycle_continues_in_every_case(self):
        cases = [[_cp(0)], [_cp(2), _cp(0)], [_cp(1)],
                 subprocess.TimeoutExpired("r", 1), OSError("x")]
        for effect in cases:
            with self.subTest(effect=effect), tempfile.TemporaryDirectory() as tmp:
                manager = MagicMock()
                with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "log"), \
                     patch("tools.inbox_consumer.check_seat_intake"), \
                     patch("tools.inbox_consumer.check_cap_hit"), \
                     patch("tools.inbox_consumer.pull_repo_or_warn"), \
                     patch("tools.inbox_consumer.subprocess.run", side_effect=effect), \
                     patch("tools.inbox_consumer.check_dispatch_locked", manager.dispatch), \
                     patch("tools.inbox_consumer.check_recent_inbound", manager.recent), \
                     patch("tools.inbox_consumer.list_eligible_tickets",
                           side_effect=SkipCycle("empty_inbox")), \
                     patch("tools.inbox_consumer.surface_active_ticket", return_value=None), \
                     contextlib.redirect_stderr(io.StringIO()):
                    main("recovery")
                manager.dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
