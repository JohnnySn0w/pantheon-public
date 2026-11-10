#!/usr/bin/env python3
"""Tests for the UserPromptSubmit capture hook (T-3241).

Two tiers, mirroring test_recall_feedback.py house style:
  * in-process — the pure helper functions, imported via importlib (hyphenated
    module name);
  * subprocess — the REAL process contract (stdin/stdout/stderr/exit code),
    hermetic via HOME + UPS_CAPTURE_DIR pointed at a tempdir.

The hook's contract is fail-open: exit 0 ALWAYS, silent on stdout+stderr in
default mode, one capture file per fire. These tests are the repo-side half of
the evidence; the live-seat captures (wired by HO, out of this ticket's scope)
are the other half.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "ups-capture.py"
_spec = importlib.util.spec_from_file_location("ups_capture", _HOOK)
ups = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ups)

# Repo root on sys.path so the allowlist tests can import the monitor module.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def _future_until(hours: float = 1.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# In-process: emit gating
# ---------------------------------------------------------------------------


class TestEmitDecision(unittest.TestCase):
    def test_emit_decision_unset_off(self) -> None:
        d = ups.emit_decision({}, _NOW)
        self.assertFalse(d["enabled"])
        self.assertIn("unset", d["reason"])
        self.assertIsNone(d["shape"])

    def test_emit_decision_valid_future_on(self) -> None:
        env = {"UPS_CAPTURE_EMIT_UNTIL": "2026-08-25T13:00:00Z"}
        d = ups.emit_decision(env, _NOW)
        self.assertTrue(d["enabled"])
        self.assertEqual(d["shape"], "json")  # default shape
        # explicit plain shape honoured
        env["UPS_CAPTURE_EMIT_SHAPE"] = "plain"
        d2 = ups.emit_decision(env, _NOW)
        self.assertTrue(d2["enabled"])
        self.assertEqual(d2["shape"], "plain")
        # unknown shape => OFF (never emit a shape we can't name)
        env["UPS_CAPTURE_EMIT_SHAPE"] = "xml"
        d3 = ups.emit_decision(env, _NOW)
        self.assertFalse(d3["enabled"])

    def test_emit_decision_expired_off(self) -> None:
        env = {"UPS_CAPTURE_EMIT_UNTIL": "2026-08-25T11:00:00Z"}
        d = ups.emit_decision(env, _NOW)
        self.assertFalse(d["enabled"])
        self.assertTrue(d["reason"])

    def test_emit_decision_beyond_max_window_off(self) -> None:
        env = {"UPS_CAPTURE_EMIT_UNTIL": "2026-08-27T13:00:00Z"}  # 49h ahead
        d = ups.emit_decision(env, _NOW)
        self.assertFalse(d["enabled"])
        self.assertTrue(d["reason"])

    def test_emit_decision_garbage_off(self) -> None:
        for bad in ("not-a-date", "2026-08-25T13:00:00", "2026-13-45T99:99:99Z", ""):
            d = ups.emit_decision({"UPS_CAPTURE_EMIT_UNTIL": bad}, _NOW)
            self.assertFalse(d["enabled"], f"should be OFF for {bad!r}")
            self.assertTrue(d["reason"])


# ---------------------------------------------------------------------------
# In-process: env snapshot redaction
# ---------------------------------------------------------------------------


class TestEnvSnapshot(unittest.TestCase):
    def test_env_snapshot_redacts_secret_named_values_keeps_names(self) -> None:
        environ = {
            "MY_TOKEN": "s3cretvalue999",
            "GITHUB_API_KEY": "k" * 12,
            "PLAIN": "hello world",
            # secret value embedded under an innocent name — must be scrubbed
            "INNOCENT_URL": "https://x.example/?q=s3cretvalue999&r=1",
        }
        snap, names = ups.env_snapshot(environ)
        # names survive; secret values do not
        self.assertIn("MY_TOKEN", snap)
        self.assertIn("GITHUB_API_KEY", snap)
        blob = json.dumps(snap)
        self.assertNotIn("s3cretvalue999", blob)
        self.assertNotIn("k" * 12, blob)
        # redaction placeholder carries the length
        self.assertIn("len=14", snap["MY_TOKEN"])
        # non-secret survives verbatim
        self.assertEqual(snap["PLAIN"], "hello world")
        # touched names reported, sorted
        self.assertIn("MY_TOKEN", names)
        self.assertIn("INNOCENT_URL", names)
        self.assertEqual(names, sorted(names))


# ---------------------------------------------------------------------------
# In-process: filename / label / sid
# ---------------------------------------------------------------------------


class TestFilenameAndLabel(unittest.TestCase):
    def test_filename_sorts_by_monotonic_and_carries_sid8(self) -> None:
        sid = ups.session_short_id({"session_id": "sess-abc-123-def"})
        self.assertEqual(sid, "sess-abc")
        self.assertEqual(ups.session_short_id("not a dict"), "nosid")
        self.assertEqual(ups.session_short_id({}), "nosid")
        self.assertEqual(ups.session_short_id({"session_id": 7}), "nosid")

        utc = "20260825T120000.000001Z"
        n_a = ups.capture_filename(utc, 99, sid, None, 41)
        n_b = ups.capture_filename(utc, 100, sid, None, 42)
        # zero-padded monotonic => lexical order == fire order
        self.assertEqual(sorted([n_b, n_a]), [n_a, n_b])
        self.assertIn("sess-abc", n_a)
        self.assertTrue(n_a.startswith("ups_"))
        self.assertTrue(n_a.endswith("_41.json"))
        # label variant carries the label
        n_l = ups.capture_filename(utc, 100, sid, "B", 42)
        self.assertIn("_B_", n_l)

    def test_label_sanitised_and_never_fatal(self) -> None:
        self.assertEqual(ups.parse_label(["--label", "A"]), "A")
        self.assertEqual(ups.parse_label(["--label=B-2_x"]), "B-2_x")
        # shell-hostile chars stripped
        self.assertEqual(ups.parse_label(["--label", "we!rd$(x)"]), "werdx")
        # capped at 32
        self.assertEqual(len(ups.parse_label(["--label", "a" * 99])), 32)
        # absent / dangling / all-stripped => None, never an exception
        self.assertIsNone(ups.parse_label([]))
        self.assertIsNone(ups.parse_label(["--label"]))
        self.assertIsNone(ups.parse_label(["--label", "!!!"]))
        self.assertIsNone(ups.parse_label(["/path/ups-capture.py"]))


# ---------------------------------------------------------------------------
# In-process: capture write cap + entry fail-open + bounded stdin read
# ---------------------------------------------------------------------------


class TestWriteAndEntry(unittest.TestCase):
    def test_capture_cap_skips_write_at_max(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "caps"
            p1 = ups.write_capture(d, "ups_a.json", {"n": 1}, max_files=2)
            p2 = ups.write_capture(d, "ups_b.json", {"n": 2}, max_files=2)
            self.assertIsNotNone(p1)
            self.assertIsNotNone(p2)
            # at the cap: silent skip, None returned, nothing written
            p3 = ups.write_capture(d, "ups_c.json", {"n": 3}, max_files=2)
            self.assertIsNone(p3)
            self.assertEqual(len(list(d.glob("ups_*.json"))), 2)
            # modes: dir 0700, files 0600
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(p1.stat().st_mode), 0o600)
            # no leftover tmp files
            self.assertEqual(list(d.glob("*.tmp")), [])

    def test_entry_swallows_internal_exception_and_exits_zero(self) -> None:
        codes: list[int] = []
        orig_main = ups.main
        try:
            def _boom(*a, **k):  # noqa: ANN002, ANN003
                raise RuntimeError("internal failure")

            ups.main = _boom
            ups.entry(exit_fn=codes.append)
        finally:
            ups.main = orig_main
        self.assertEqual(codes, [0])

    def test_bounded_stdin_read_returns_on_deadline(self) -> None:
        r, w = os.pipe()
        try:
            os.write(w, b"partial-payload")
            t0 = time.monotonic()
            data, hit = ups.read_stdin_bounded(r, deadline_s=0.2)
            elapsed = time.monotonic() - t0
            self.assertEqual(data, b"partial-payload")
            self.assertTrue(hit)
            self.assertLess(elapsed, 1.5)  # returned at ~deadline, not at 30s
        finally:
            os.close(r)
            os.close(w)
        # closed-at-startup stdin => empty, no deadline hit, no exception
        self.assertEqual(ups.read_stdin_bounded(None), (b"", False))

    def test_bounded_stdin_read_with_fd_above_1024(self) -> None:
        # T-5872: select.select() raises ValueError for fd >= 1024, which the
        # old code swallowed into an empty read (silent payload loss).
        from tools._test_fixtures import fds_above_select_ceiling
        with fds_above_select_ceiling():
            r, w = os.pipe()
            try:
                self.assertGreaterEqual(r, 1024)
                os.write(w, b"high-fd-payload")
                os.close(w)
                w = -1
                data, hit = ups.read_stdin_bounded(r, deadline_s=2.0)
                self.assertEqual(data, b"high-fd-payload")
                self.assertFalse(hit)
            finally:
                os.close(r)
                if w >= 0:
                    os.close(w)

    def test_bounded_stdin_read_from_regular_file_and_devnull(self) -> None:
        # T-5872 REBOUND: epoll refuses regular files and /dev/null (EPERM);
        # `hook < file` / `hook < /dev/null` must read exactly as before.
        with tempfile.NamedTemporaryFile() as tf:
            tf.write(b"from-a-file")
            tf.flush()
            fd = os.open(tf.name, os.O_RDONLY)
            try:
                self.assertEqual(ups.read_stdin_bounded(fd, deadline_s=2.0), (b"from-a-file", False))
                self.assertIsNone(ups.LAST_READ_ERROR)
            finally:
                os.close(fd)
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            self.assertEqual(ups.read_stdin_bounded(fd, deadline_s=2.0), (b"", False))
            self.assertIsNone(ups.LAST_READ_ERROR)
        finally:
            os.close(fd)

    def test_read_error_is_surfaced_not_silent(self) -> None:
        # T-5872: a genuine read failure keeps partial data (fail-open) but is
        # visible: stderr line + a record field distinguishing it from empty.
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r, w = os.pipe()
            os.close(r)
            os.close(w)
            data, hit = ups.read_stdin_bounded(r)  # guaranteed-closed fd
        self.assertEqual((data, hit), (b"", False))
        self.assertIn("read error", err.getvalue())
        self.assertIsNotNone(ups.LAST_READ_ERROR)
        rec = ups.main(argv=["x"], environ={}, stdin_reader=lambda: (b"", False),
                       stdout=io.StringIO())
        self.assertIn("read_error", rec["stdin"])


# ---------------------------------------------------------------------------
# Subprocess: the real process contract
# ---------------------------------------------------------------------------


class _SubprocessBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.capdir = self.tmp / "captures"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_hook(
        self,
        stdin: bytes = b"",
        extra_env: dict | None = None,
        argv: tuple = (),
        capdir: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "UPS_CAPTURE_DIR": str(capdir if capdir is not None else self.capdir),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(_HOOK), *argv],
            input=stdin,
            capture_output=True,
            env=env,
            timeout=15,
        )

    def captures(self) -> list[Path]:
        if not self.capdir.is_dir():
            return []
        return sorted(self.capdir.glob("ups_*.json"))

    def sole_record(self) -> dict:
        files = self.captures()
        self.assertEqual(len(files), 1, f"expected exactly one capture, got {files}")
        return json.loads(files[0].read_text(encoding="utf-8"))


class TestSubprocessContract(_SubprocessBase):
    def test_capture_file_shape_and_verbatim_raw(self) -> None:
        # deliberately quirky spacing + non-ASCII to prove verbatim storage
        payload = '{"session_id": "sess-abc-123",  "prompt": "héllo wörld",\n "n": [1, 2]}'
        proc = self.run_hook(stdin=payload.encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        rec = self.sole_record()
        # schema + identity
        self.assertEqual(rec["capture_schema"], "ups-capture/1")
        self.assertEqual(rec["ticket"], "T-3241")
        self.assertIsNone(rec["label"])
        # clock block
        for k in ("utc", "wall_ns", "monotonic_ns", "boot_id"):
            self.assertIn(k, rec["clock"])
        # process block
        for k in ("pid", "ppid", "uid", "cwd", "argv", "python"):
            self.assertIn(k, rec["process"])
        # stdin block: VERBATIM raw + parsed convenience copy
        s = rec["stdin"]
        self.assertEqual(s["raw"], payload)
        self.assertEqual(s["bytes"], len(payload.encode("utf-8")))
        self.assertTrue(s["utf8"])
        self.assertIsNone(s["raw_b64"])
        self.assertEqual(s["json"]["prompt"], "héllo wörld")
        self.assertIsNone(s["json_error"])
        self.assertFalse(s["read_hit_deadline"])
        # emit off by default, with reason
        self.assertFalse(rec["emit"]["enabled"])
        self.assertIsNone(rec["emit"]["stdout"])
        # env + errors + timing present
        self.assertIn("env", rec)
        self.assertIn("env_redacted_names", rec)
        self.assertEqual(rec["errors"], [])
        self.assertIsInstance(rec["elapsed_ms"], (int, float))
        # filename carries sid8
        self.assertIn("sess-abc", self.captures()[0].name)

    def test_default_mode_silent_stdout_and_stderr_exit_zero(self) -> None:
        proc = self.run_hook(stdin=b'{"session_id": "s"}')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")

    def test_emit_mode_single_line_additional_context_and_recorded_stdout(self) -> None:
        proc = self.run_hook(
            stdin=b'{"session_id": "sess-emit-1"}',
            extra_env={"UPS_CAPTURE_EMIT_UNTIL": _future_until()},
            argv=("--label", "A"),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        out = proc.stdout.decode("utf-8")
        self.assertEqual(out.count("\n"), 1)  # exactly one line
        obj = json.loads(out)
        hso = obj["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        marker = hso["additionalContext"]
        self.assertTrue(marker.startswith("UPS-CAPTURE-PROBE"))
        self.assertIn("label=A", marker)
        rec = self.sole_record()
        # the exact emitted text is recorded for transcript reconciliation
        self.assertEqual(rec["emit"]["stdout"], out)
        self.assertTrue(rec["emit"]["enabled"])
        # the marker names the actual on-disk capture file
        self.assertIn(f"capture={self.captures()[0].name}", marker)
        # plain shape: bare marker on stdout
        self.capdir_plain = self.tmp / "captures2"
        proc2 = self.run_hook(
            stdin=b'{"session_id": "sess-emit-2"}',
            extra_env={
                "UPS_CAPTURE_EMIT_UNTIL": _future_until(),
                "UPS_CAPTURE_EMIT_SHAPE": "plain",
            },
            argv=("--label", "B"),
            capdir=str(self.capdir_plain),
        )
        self.assertEqual(proc2.returncode, 0)
        out2 = proc2.stdout.decode("utf-8")
        self.assertTrue(out2.startswith("UPS-CAPTURE-PROBE"))
        self.assertIn("label=B", out2)

    def test_malformed_stdin_fails_open_and_still_captures_raw(self) -> None:
        proc = self.run_hook(stdin=b"{this is not json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")
        rec = self.sole_record()
        self.assertEqual(rec["stdin"]["raw"], "{this is not json")
        self.assertIsNone(rec["stdin"]["json"])
        self.assertTrue(rec["stdin"]["json_error"])

    def test_empty_stdin_exit_zero(self) -> None:
        proc = self.run_hook(stdin=b"")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")
        rec = self.sole_record()
        self.assertEqual(rec["stdin"]["bytes"], 0)
        self.assertIn("nosid", self.captures()[0].name)

    def test_non_utf8_stdin_exit_zero_b64_recorded(self) -> None:
        blob = b"\xff\xfe\x01\x02ups"
        proc = self.run_hook(stdin=blob)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        rec = self.sole_record()
        self.assertFalse(rec["stdin"]["utf8"])
        self.assertIsNone(rec["stdin"]["raw"])
        self.assertEqual(base64.b64decode(rec["stdin"]["raw_b64"]), blob)

    def test_unwritable_capture_dir_exit_zero_silent(self) -> None:
        proc = self.run_hook(stdin=b'{"session_id": "s"}', capdir="/proc/nope/sub")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")

    def test_emit_mode_still_emits_when_capture_unwritable(self) -> None:
        proc = self.run_hook(
            stdin=b'{"session_id": "s"}',
            extra_env={"UPS_CAPTURE_EMIT_UNTIL": _future_until()},
            capdir="/proc/nope/sub",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        out = proc.stdout.decode("utf-8")
        obj = json.loads(out)
        marker = obj["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(marker.startswith("UPS-CAPTURE-PROBE"))
        # no capture file exists, so the marker must say so
        self.assertIn("capture=none", marker)

    def test_two_fires_same_session_correlate_and_order(self) -> None:
        payload = b'{"session_id": "sess-same-1", "prompt": "one"}'
        p1 = self.run_hook(stdin=payload)
        p2 = self.run_hook(stdin=b'{"session_id": "sess-same-1", "prompt": "two"}')
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        files = self.captures()
        self.assertEqual(len(files), 2)
        recs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
        # correlate: same payload session id in both, same boot id
        sids = [json.loads(r["stdin"]["raw"])["session_id"] for r in recs]
        self.assertEqual(sids[0], sids[1])
        self.assertEqual(recs[0]["clock"]["boot_id"], recs[1]["clock"]["boot_id"])
        # order: lexical filename order == monotonic order == fire order
        self.assertLess(recs[0]["clock"]["monotonic_ns"], recs[1]["clock"]["monotonic_ns"])
        self.assertEqual(recs[0]["stdin"]["json"]["prompt"], "one")
        self.assertEqual(recs[1]["stdin"]["json"]["prompt"], "two")

    def test_sub_second_budget(self) -> None:
        payload = json.dumps(
            {"session_id": "sess-big-1", "prompt": "x" * 200_000}
        ).encode("utf-8")
        t0 = time.monotonic()
        proc = self.run_hook(stdin=payload)
        wall = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0)
        rec = self.sole_record()
        self.assertEqual(rec["stdin"]["bytes"], len(payload))
        # hook-internal elapsed well under the 1s budget; wall includes
        # interpreter startup so gets a looser (still tight) bound
        self.assertLess(rec["elapsed_ms"], 1000)
        self.assertLess(wall, 3.0)

    def test_secret_value_absent_from_file_even_under_other_name(self) -> None:
        # The plan's added redaction test: a value under a secret-shaped name
        # must be ABSENT from the written file even when that same value also
        # appears somewhere else in the environment under an innocent name.
        secret = "hunter2hunter2hunter2"
        proc = self.run_hook(
            stdin=b'{"session_id": "s"}',
            extra_env={
                "DEPLOY_TOKEN": secret,
                "INNOCENT_NOTE": f"the value {secret} leaked into prose",
            },
        )
        self.assertEqual(proc.returncode, 0)
        text = self.captures()[0].read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        rec = json.loads(text)
        self.assertIn("DEPLOY_TOKEN", rec["env"])  # name + length survive
        self.assertIn("DEPLOY_TOKEN", rec["env_redacted_names"])
        self.assertIn("INNOCENT_NOTE", rec["env_redacted_names"])


# ---------------------------------------------------------------------------
# Allowlist pairing (context_fill_monitor)
# ---------------------------------------------------------------------------


def _ups_capture_is_wired() -> bool:
    """Reachability probe for the two allowlist-pairing tests below.
    ``ups-capture.py`` was pruned from ``HOOK_ALLOWLIST['orchestrator']`` in
    ``tools/context_fill_monitor.py`` on 2026-09-01 by recovery (leave-no-trace
    per T-3241 close + T-3006 needs-no-future-capture-window; see the
    comment at ``context_fill_monitor.py:568``). The hook file itself
    (``tools/hooks/ups-capture.py``) is preserved for future ORG-073
    capture windows — these two tests then become the audit gate that
    fires when someone re-adds the wire without also re-establishing
    the surrounding capture apparatus. The guard returns False on the
    intentionally-unwound state so both tests SKIP; if the wire is ever
    re-added, they run again automatically."""
    try:
        from tools.context_fill_monitor import HOOK_ALLOWLIST
    except ImportError:
        return False
    return "ups-capture.py" in HOOK_ALLOWLIST.get("orchestrator", set())


class TestAllowlistPairing(unittest.TestCase):
    @unittest.skipUnless(
        _ups_capture_is_wired(),
        "skips while ups-capture.py is intentionally NOT in HOOK_ALLOWLIST['orchestrator'] "
        "(pruned 2026-09-01 by recovery per T-3241 close + T-3006 needs-no-future-capture-window; "
        "context_fill_monitor.py:568 records the leave-no-trace rationale). The test remains "
        "as the audit gate for a future re-add — it will run again automatically once the "
        "wire is re-established. T-3547 slice 6.",
    )
    def test_orchestrator_allowlist_contains_ups_capture(self) -> None:
        from tools.context_fill_monitor import HOOK_ALLOWLIST

        self.assertIn("ups-capture.py", HOOK_ALLOWLIST["orchestrator"])

    @unittest.skipUnless(
        _ups_capture_is_wired(),
        "skips while ups-capture.py is intentionally NOT in HOOK_ALLOWLIST['orchestrator'] — "
        "see sibling test's skip reason. T-3547 slice 6.",
    )
    def test_wired_command_shapes_pass_command_is_allowlisted(self) -> None:
        from tools.context_fill_monitor import HOOK_ALLOWLIST, _command_is_allowlisted

        allow = HOOK_ALLOWLIST["orchestrator"]
        shapes = [
            "/home/claude/.claude/hooks/ups-capture.py",
            "/home/claude/.claude/hooks/ups-capture.py --label A",
            "/home/claude/.claude/hooks/ups-capture.py --label=B",
            "UPS_CAPTURE_EMIT_UNTIL=2026-08-26T02:00:00Z "
            "/home/claude/.claude/hooks/ups-capture.py --label A",
        ]
        for cmd in shapes:
            self.assertTrue(_command_is_allowlisted(cmd, allow), f"rejected: {cmd}")


if __name__ == "__main__":
    unittest.main()
