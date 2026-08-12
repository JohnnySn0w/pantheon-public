from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.heartbeat_write import (
    HeartbeatWriteError,
    build_heartbeat,
    main,
    resolve_persona,
    write_heartbeat,
)

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


class HeartbeatSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.heartbeat_dir = Path(self.temporary.name) / "heartbeat"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_write_heartbeat_produces_exact_schema_keys_and_types(self) -> None:
        path = write_heartbeat(
            agent="specialist",
            source="stop-hook",
            heartbeat_dir=self.heartbeat_dir,
            user=None,
            now=NOW,
            pid=4242,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            set(payload), {"agent", "ts", "epoch", "pid", "source", "schema_version"}
        )
        self.assertEqual(payload["agent"], "specialist")
        self.assertIsInstance(payload["agent"], str)
        self.assertEqual(payload["ts"], "2026-08-11T12:00:00Z")
        self.assertIsInstance(payload["ts"], str)
        self.assertEqual(payload["epoch"], int(NOW.timestamp()))
        self.assertIsInstance(payload["epoch"], int)
        self.assertEqual(payload["pid"], 4242)
        self.assertIsInstance(payload["pid"], int)
        self.assertEqual(payload["source"], "stop-hook")
        self.assertIsInstance(payload["source"], str)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIsInstance(payload["schema_version"], int)

    def test_build_heartbeat_normalizes_naive_datetime_to_utc_z_suffixed(self) -> None:
        naive = datetime(2026, 8, 11, 12, 0, 0)
        payload = build_heartbeat("orchestrator", source="manual", now=naive, pid=1)
        self.assertEqual(payload["ts"], "2026-08-11T12:00:00Z")


class HeartbeatAtomicityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.heartbeat_dir = Path(self.temporary.name) / "heartbeat"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_write_leaves_only_the_target_file_no_stray_temp(self) -> None:
        write_heartbeat(
            agent="recovery",
            source="manual",
            heartbeat_dir=self.heartbeat_dir,
            user=None,
            now=NOW,
            pid=1,
        )
        entries = sorted(p.name for p in self.heartbeat_dir.iterdir())
        self.assertEqual(entries, ["recovery.json"])

    def test_second_write_overwrites_cleanly_with_no_residue(self) -> None:
        write_heartbeat(
            agent="recovery", source="manual", heartbeat_dir=self.heartbeat_dir,
            user=None, now=NOW, pid=1,
        )
        second = NOW.replace(minute=5)
        write_heartbeat(
            agent="recovery", source="manual", heartbeat_dir=self.heartbeat_dir,
            user=None, now=second, pid=2,
        )
        entries = sorted(p.name for p in self.heartbeat_dir.iterdir())
        self.assertEqual(entries, ["recovery.json"])
        payload = json.loads((self.heartbeat_dir / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], 2)

    def test_heartbeat_dir_created_with_mode_0700(self) -> None:
        write_heartbeat(
            agent="recovery", source="manual", heartbeat_dir=self.heartbeat_dir,
            user=None, now=NOW, pid=1,
        )
        self.assertEqual(self.heartbeat_dir.stat().st_mode & 0o777, 0o700)


class HeartbeatResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.heartbeat_dir = Path(self.temporary.name) / "heartbeat"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_agent_wins_over_disagreeing_user(self) -> None:
        persona = resolve_persona("specialist", user="claude")
        self.assertEqual(persona, "specialist")

    def test_user_derived_resolution_via_account_persona(self) -> None:
        persona = resolve_persona(None, user="claude-research")
        self.assertEqual(persona, "specialist")

    def test_write_heartbeat_resolves_from_user_end_to_end(self) -> None:
        path = write_heartbeat(
            agent=None,
            source="stop-hook",
            heartbeat_dir=self.heartbeat_dir,
            user="claude-research",
            now=NOW,
            pid=7,
        )
        self.assertEqual(path.name, "specialist.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["agent"], "specialist")

    def test_unknown_agent_raises_and_writes_nothing(self) -> None:
        with self.assertRaisesRegex(HeartbeatWriteError, "unknown --agent"):
            write_heartbeat(
                agent="not-a-real-persona",
                source="manual",
                heartbeat_dir=self.heartbeat_dir,
                user=None,
                now=NOW,
                pid=1,
            )
        self.assertFalse(self.heartbeat_dir.exists())

    def test_unmapped_user_raises_and_writes_nothing(self) -> None:
        with self.assertRaisesRegex(HeartbeatWriteError, "unmapped \\$USER"):
            write_heartbeat(
                agent=None,
                source="manual",
                heartbeat_dir=self.heartbeat_dir,
                user="nobody",
                now=NOW,
                pid=1,
            )
        self.assertFalse(self.heartbeat_dir.exists())

    def test_unset_user_and_no_agent_raises(self) -> None:
        with self.assertRaisesRegex(HeartbeatWriteError, r"\$USER is unset"):
            resolve_persona(None, user=None)


class HeartbeatMainCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _expected_path(self, persona: str) -> Path:
        return self.home / ".local" / "state" / "pantheon" / "heartbeat" / f"{persona}.json"

    def test_main_with_explicit_agent_writes_default_path_and_returns_zero(self) -> None:
        import os

        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            rc = main(["--agent", "orchestrator", "--source", "stop-hook"])
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                del os.environ["HOME"]

        self.assertEqual(rc, 0)
        payload = json.loads(self._expected_path("orchestrator").read_text(encoding="utf-8"))
        self.assertEqual(payload["agent"], "orchestrator")
        self.assertEqual(payload["source"], "stop-hook")

    def test_main_with_unknown_agent_returns_nonzero_and_writes_nothing(self) -> None:
        import os

        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            rc = main(["--agent", "not-a-real-persona"])
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                del os.environ["HOME"]

        self.assertNotEqual(rc, 0)
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
