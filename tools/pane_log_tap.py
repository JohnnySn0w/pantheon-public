#!/usr/bin/env python3
"""tools/pane_log_tap.py: the reader behind arm_pane_log.sh's pipe-pane tap (T-1636, T-4460).

Reads a tmux pane's raw output on stdin and appends readable, wall-clock-stamped
lines to one log file:

    [2026-09-30 18:40:02] Provider completion probe. Reply with exactly this marker

Why not the old `awk '{print strftime(...) $0; fflush()}'`:
  - tmux expands pipe-pane's shell-command as a format, and format expansion
    runs strftime over it, so the awk's "%Y-%m-%d %H:%M:%S" arrived as a
    literal arm-time string. Every line carried the same timestamp.
  - Claude Code's TUI draws with CR and cursor-movement escapes, not newlines
    (one live session: 12006 CRs vs 28 LFs). awk's record is a newline, so the
    whole session sat in awk's memory as ONE record and was written only when
    the pane died: an 819,936-byte line with one timestamp.

This reader instead:
  - stamps each output burst at the time it is READ (this process's clock);
  - treats CR, LF and cursor-positioning escapes as line breaks;
  - strips the other escape sequences (colour, modes, OSC titles);
  - drops blank lines and lines it wrote recently, because a TUI re-paints the
    same rows on every frame;
  - holds back only an unfinished escape sequence and the unfinished current
    row (each capped), so memory stays flat for any session length.

Contract (same as arm_pane_log.sh): observability only. It opens the log once
(so it holds the inode across the Chronicle's archive-then-unlink step), flushes
after every burst, and exits 0 on EOF. It never writes to the pane.
"""

from __future__ import annotations

import codecs
import collections
import os
import re
import sys
import time
from typing import Callable, Iterable, TextIO

READ_SIZE = 65536
MAX_LINE_CHARS = 4096
RECENT_LINES = 256
# An escape sequence split across two reads is carried over, but never more
# than this many bytes (a stray ESC must not make us hold output forever).
MAX_CARRY_CHARS = 256

# CSI final bytes that move the cursor to another row or column: treated as a
# line break, so text painted at different screen positions doesn't glue together.
_CURSOR_MOVES = set("ABEFGHdf")
_CSI = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])")
MAX_CURSOR_FORWARD = 200
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OTHER_ESC = re.compile(r"\x1b[@-Z\\-_]|\x1b[()][0-9A-Za-z]")
_INCOMPLETE_ESC = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*|[()]?)?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BREAKS = re.compile(r"[\r\n]+")


def _csi_replacement(match: re.Match[str]) -> str:
    params, final = match.group(1), match.group(2)
    if final in _CURSOR_MOVES:
        return "\n"
    if final == "C":  # cursor forward N columns: the TUI uses it as indentation
        return " " * min(int(params) if params.isdigit() else 1, MAX_CURSOR_FORWARD)
    return ""


def normalize(text: str) -> str:
    """Strip terminal control sequences; cursor-positioning moves become line breaks."""
    text = _OSC.sub("", text)
    text = _CSI.sub(_csi_replacement, text)
    text = _OTHER_ESC.sub("", text)
    return _CONTROL.sub("", text)


class PaneLogTap:
    def __init__(self, out: TextIO, *, clock: Callable[[], float] = time.time) -> None:
        self._out = out
        self._clock = clock
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry = ""  # an escape sequence cut off at the end of a read
        self._pending = ""  # text after the last line break: the row may continue in the next read
        self._recent: collections.OrderedDict[str, None] = collections.OrderedDict()

    def _seen_recently(self, line: str) -> bool:
        if line in self._recent:
            self._recent.move_to_end(line)
            return True
        self._recent[line] = None
        if len(self._recent) > RECENT_LINES:
            self._recent.popitem(last=False)
        return False

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        text = self._carry + self._decoder.decode(chunk, final=final)
        self._carry = ""
        if not final:
            tail = _INCOMPLETE_ESC.search(text)
            if tail and len(text) - tail.start() <= MAX_CARRY_CHARS:
                self._carry = text[tail.start():]
                text = text[: tail.start()]
        segments = _BREAKS.split(self._pending + normalize(text))
        # The last segment has no break after it yet. Hold it for the next read,
        # unless this is EOF or it has already reached the line cap.
        self._pending = segments.pop()
        if final or len(self._pending) >= MAX_LINE_CHARS:
            segments.append(self._pending)
            self._pending = ""
        lines = []
        for segment in segments:
            line = segment.rstrip()[:MAX_LINE_CHARS]
            if line.strip() and not self._seen_recently(line):
                lines.append(line)
        if not lines:
            return
        stamp = time.strftime("[%Y-%m-%d %H:%M:%S] ", time.localtime(self._clock()))
        self._out.write("".join(stamp + line + "\n" for line in lines))
        self._out.flush()

    def run(self, chunks: Iterable[bytes]) -> None:
        for chunk in chunks:
            self.feed(chunk)
        self.feed(b"", final=True)


def _read_chunks(fd: int) -> Iterable[bytes]:
    while True:
        chunk = os.read(fd, READ_SIZE)
        if not chunk:
            return
        yield chunk


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: pane_log_tap.py <log-file>", file=sys.stderr)
        return 0
    try:
        out = open(argv[1], "a", encoding="utf-8")
    except OSError as exc:
        print(f"[pane-log-tap] cannot open {argv[1]}: {exc} (non-fatal)", file=sys.stderr)
        return 0
    with out:
        try:
            PaneLogTap(out).run(_read_chunks(sys.stdin.fileno()))
        except (OSError, KeyboardInterrupt):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
