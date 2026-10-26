"""Tests for tools/pane_log_tap.py and the arm_pane_log.sh tap it backs (T-4460)."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools import pane_log_tap

REPO_ROOT = Path(__file__).resolve().parent.parent
ARM = REPO_ROOT / "tools" / "arm_pane_log.sh"
STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")


class FakeClock:
    def __init__(self, start: float = 1790793600.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _tap() -> tuple[pane_log_tap.PaneLogTap, io.StringIO, FakeClock]:
    out = io.StringIO()
    clock = FakeClock()
    return pane_log_tap.PaneLogTap(out, clock=clock), out, clock


def _lines(out: io.StringIO) -> list[tuple[str, str]]:
    parsed = []
    for line in out.getvalue().splitlines():
        match = STAMP.match(line)
        assert match, line
        parsed.append((match.group(1), match.group(2)))
    return parsed


class PaneLogTapTests(unittest.TestCase):
    def test_tui_drawing_without_newlines_becomes_separate_lines(self) -> None:
        # The shape of Claude Code's TUI output: cursor home/down moves and CRs, no LF.
        tap, out, _ = _tap()
        tap.feed(
            b"\x1b[?25l\x1b[H\r\x1b[2C\x1b[13B\x1b[48;5;237m\x1b[38;5;231mProvider probe\x1b[0m"
            b"\r\x1b[1B\x1b[2Cmarker line\r\x1b[3;1H\xe2\x9d\xaf prompt\x1b[5;1H"
        )
        self.assertEqual([text for _, text in _lines(out)], ["Provider probe", "  marker line", "\u276f prompt"])

    def test_each_burst_is_stamped_when_read_not_when_armed(self) -> None:
        tap, out, clock = _tap()
        tap.feed(b"first\r")
        clock.now += 61
        tap.feed(b"second\r")
        stamps = [stamp for stamp, _ in _lines(out)]
        self.assertEqual(len(stamps), 2)
        self.assertNotEqual(stamps[0], stamps[1])

    def test_repainted_rows_are_written_once_until_they_age_out(self) -> None:
        tap, out, _ = _tap()
        for _ in range(5):
            tap.feed(b"\x1b[Hstatus bar\r\x1b[2Bbody\r")
        self.assertEqual([text for _, text in _lines(out)], ["status bar", "body"])
        for n in range(pane_log_tap.RECENT_LINES):
            tap.feed(f"filler {n}\r".encode())
        tap.feed(b"status bar\r")
        self.assertEqual(_lines(out)[-1][1], "status bar")

    def test_escape_sequence_split_across_reads_is_not_leaked(self) -> None:
        tap, out, _ = _tap()
        tap.feed(b"alpha\r\x1b[38;5")
        tap.feed(b";231mbeta\r")
        self.assertEqual([text for _, text in _lines(out)], ["alpha", "beta"])

    def test_a_row_continued_in_the_next_read_stays_one_line(self) -> None:
        tap, out, _ = _tap()
        tap.feed(b"\x1b[Hpar")
        self.assertEqual(out.getvalue(), "")
        tap.feed(b"tial row\r")
        self.assertEqual([text for _, text in _lines(out)], ["partial row"])

    def test_utf8_split_across_reads_decodes(self) -> None:
        tap, out, _ = _tap()
        tap.feed("caf\u00e9 \u276f".encode()[:-2])
        tap.feed("caf\u00e9 \u276f".encode()[-2:] + b"\r")
        self.assertEqual([text for _, text in _lines(out)], ["caf\u00e9 \u276f"])

    def test_osc_titles_and_bells_are_stripped(self) -> None:
        tap, out, _ = _tap()
        tap.feed(b"\x1b]0;window title\x07visible\x07\r\x1b]8;;http://x\x1b\\link\r")
        self.assertEqual([text for _, text in _lines(out)], ["visible", "link"])

    def test_memory_stays_bounded(self) -> None:
        tap, out, _ = _tap()
        # An unterminated OSC must not be carried forever.
        tap.feed(b"\x1b]" + b"x" * 5000)
        tap.feed(b"after\r")
        self.assertLessEqual(len(tap._carry), pane_log_tap.MAX_CARRY_CHARS)
        self.assertIn("after", [text for _, text in _lines(out)])
        # One huge line is capped, not held.
        tap.feed(b"y" * (pane_log_tap.MAX_LINE_CHARS * 3) + b"\r")
        self.assertEqual(len(_lines(out)[-1][1]), pane_log_tap.MAX_LINE_CHARS)
        # An unbroken row that never ends is emitted at the cap instead of growing.
        for _ in range(10):
            tap.feed(b"z" * 1000)
            self.assertLess(len(tap._pending), pane_log_tap.MAX_LINE_CHARS)
        self.assertTrue(_lines(out)[-1][1].startswith("zzz"))

    def test_trailing_text_is_flushed_at_eof(self) -> None:
        tap, out, _ = _tap()
        tap.run([b"no newline at the end"])
        self.assertEqual([text for _, text in _lines(out)], ["no newline at the end"])

    def test_main_is_fail_open_on_bad_arguments_and_unwritable_log(self) -> None:
        self.assertEqual(pane_log_tap.main(["pane_log_tap.py"]), 0)
        self.assertEqual(pane_log_tap.main(["pane_log_tap.py", "/nonexistent-dir/x/y.log"]), 0)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class ArmPaneLogLiveTests(unittest.TestCase):
    """A real tap on a private tmux server (its own TMUX_TMPDIR, and TMUX unset so
    nothing can reach the live fleet server)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t4460-", dir="/tmp"))
        self.env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        self.env["TMUX_TMPDIR"] = str(self.tmp)
        # % and # in the path: tmux format expansion would rewrite them (strftime,
        # #{...}) if arm_pane_log.sh didn't escape the pipe-pane command.
        self.log_dir = self.tmp / "logs %Y-%m-%d #{pane_id} ##"
        self.env["PANE_LOG_DIR"] = str(self.log_dir)

    def tearDown(self) -> None:
        subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True, timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True, timeout=10)

    def test_live_tap_writes_advancing_timestamps_to_the_literal_path(self) -> None:
        # The pane prints only when the test releases it (go files), never on a
        # fixed timer: under full-suite load arming can take longer than any
        # sleep, and output printed before pipe-pane attaches is lost (T-4880).
        go1, go2 = self.tmp / "go1", self.tmp / "go2"
        script = (
            f"while [ ! -e '{go1}' ]; do sleep 0.1; done; printf 'first\\r\\033[2Bsecond\\r'; "
            f"while [ ! -e '{go2}' ]; do sleep 0.1; done; printf 'third\\r'; sleep 60"
        )
        self.assertEqual(self._tmux("new-session", "-d", "-s", "t4460", "-x", "80", "-y", "20", "sh", "-c", script).returncode, 0)
        armed = subprocess.run(["bash", str(ARM), "t4460:0.0", "probe"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        self.assertIn("armed:", armed.stderr)
        self.assertEqual(self._tmux("display-message", "-p", "-t", "t4460:0.0", "#{pane_pipe}").stdout.strip(), "1")
        log = self.log_dir / "pane-timed-probe.log"

        def wait_for(word: str) -> str:
            deadline = time.monotonic() + 30
            text = ""
            while time.monotonic() < deadline:
                text = log.read_text(encoding="utf-8") if log.exists() else ""
                if word in text:
                    break
                time.sleep(0.2)
            return text

        go1.touch()
        wait_for("second")
        # Stamps have one-second resolution: release "third" in a later second.
        time.sleep(1.1)
        go2.touch()
        text = wait_for("third")
        parsed = [STAMP.match(line) for line in text.splitlines()]
        self.assertTrue(parsed and all(parsed), text)
        body = [m.group(2) for m in parsed]
        for word in ("first", "second", "third"):
            self.assertTrue(any(word in line for line in body), (word, body))
        # Written while the pane is still alive, and not frozen at arm time.
        self.assertGreaterEqual(len({m.group(1) for m in parsed}), 2, text)
        # Arming again is a no-op (the -o toggle guard), not a disarm.
        again = subprocess.run(["bash", str(ARM), "t4460:0.0", "probe"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertIn("already piped", again.stderr)
        self.assertEqual(self._tmux("display-message", "-p", "-t", "t4460:0.0", "#{pane_pipe}").stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
