"""Tests for backup_staleness_check.py (T-2886) — the pure age logic and the
main() verdict paths, with the network call stubbed."""
import datetime as dt
import unittest
from unittest import mock

from tools import backup_staleness_check as bsc

NOW = dt.datetime(2026, 8, 13, 12, 0, 0, tzinfo=dt.timezone.utc)


class AgeHours(unittest.TestCase):
    def test_z_suffix_utc(self):
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T00:00:00Z", NOW), 12.0)

    def test_explicit_offset(self):
        # 06:00 at -04:00 == 10:00 UTC -> 2h before NOW
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T06:00:00-04:00", NOW), 2.0)

    def test_naive_treated_as_utc(self):
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T11:00:00", NOW), 1.0)

    def test_future_commit_is_negative_age_not_stale(self):
        self.assertLess(bsc.age_hours("2026-08-13T13:00:00Z", NOW), 0.0)


class MainVerdicts(unittest.TestCase):
    def _run(self, iso=None, err=None, max_age="48"):
        patcher = (
            mock.patch.object(bsc, "latest_commit_date", side_effect=err)
            if err else
            mock.patch.object(bsc, "latest_commit_date", return_value=iso)
        )
        with patcher, mock.patch.object(bsc.time, "sleep"):  # no real retry waits
            return bsc.main(["--max-age-hours", max_age])

    def test_fresh_exits_zero(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=recent), 0)

    def test_stale_exits_one(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=old), 1)

    def test_api_error_fails_closed(self):
        self.assertEqual(self._run(err=RuntimeError("gh api failed")), 1)


if __name__ == "__main__":
    unittest.main()
