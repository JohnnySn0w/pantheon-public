import hashlib
import json
import os
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.harness_adapter.provider_quota import (
    ACCEPTANCE_OVERLAY_SCHEMA,
    ClaudeAcceptanceQuotaOverlayObserver,
    ClaudeStatuslineQuotaObserver,
    CodexAppServerQuotaObserver,
    UnavailableQuotaObserver,
    parse_claude_statusline_observation,
    parse_codex_app_server_quota_observation,
    read_codex_app_server_rate_limits,
)
from tools.harness_adapter.quota import (
    FailoverPolicy,
    HARNESS_WEEKLY_RESET_SCHEDULE,
    WeeklyResetSchedule,
    decide_failover,
    next_weekly_reset,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_acceptance_overlay(
    root: Path,
    *,
    run_id: str = "20260803T120000Z",
    canonical_payload: bytes = b'{"canonical":true}\n',
    envelope_overrides: dict | None = None,
    payload: dict | None = None,
) -> tuple[Path, Path, Path]:
    state_dir = root / "state"
    canonical = state_dir / "quota" / "claude-statusline.json"
    overlay = state_dir / "acceptance" / run_id / "quota-overlay.json"
    canonical.parent.mkdir(parents=True)
    overlay.parent.mkdir(parents=True)
    canonical.write_bytes(canonical_payload)
    payload = payload or {
        "observed_at": _iso(NOW),
        "limits": {
            "weekly": {
                "remaining_percent": 80.0,
                "resets_at": _iso(NOW + timedelta(days=3)),
            }
        },
    }
    envelope = {
        "schema": ACCEPTANCE_OVERLAY_SCHEMA,
        "run_id": run_id,
        "created_at": _iso(NOW - timedelta(seconds=1)),
        "canonical_path": str(canonical.resolve()),
        "canonical_sha256_at_install": _sha256_bytes(canonical_payload),
        "payload": payload,
    }
    if envelope_overrides:
        envelope.update(envelope_overrides)
    overlay.write_text(json.dumps(envelope), encoding="utf-8")
    overlay.chmod(0o600)
    return state_dir, canonical, overlay


class ClaudeStatuslineQuotaObservationTest(unittest.TestCase):
    def test_explicit_provider_cap_is_authoritative(self) -> None:
        snapshot = parse_claude_statusline_observation(
            {
                "observed_at": _iso(NOW),
                "rate_limit_reached_type": "weekly",
                "limits": {},
            },
            profile="claude/interactive-subscription",
            now=NOW,
        )

        self.assertEqual(snapshot.evidence["authoritative_limit"], "true")

    def test_statusline_weekly_observation_normalizes_to_quota_snapshot(self) -> None:
        payload = {
            "observed_at": _iso(NOW),
            "statusline": {
                "limits": {
                    "five_hour": {
                        "remaining_percent": 62.5,
                        "resets_at": _iso(NOW + timedelta(hours=2)),
                    },
                    "weekly": {
                        "usage_percent": 95.0,
                        "reset_at": _iso(NOW + timedelta(days=2)),
                    },
                }
            },
        }

        snapshot = parse_claude_statusline_observation(
            payload,
            profile="claude/interactive-subscription",
            now=NOW,
        )

        self.assertEqual(snapshot.profile, "claude/interactive-subscription")
        self.assertEqual(snapshot.freshness, "fresh")
        self.assertEqual(snapshot.evidence["provider"], "claude")
        self.assertEqual(snapshot.evidence["source"], "statusline")
        self.assertEqual([window.name for window in snapshot.windows], ["5h", "weekly"])
        weekly = snapshot.windows[1]
        self.assertEqual(weekly.remaining_percent, 5.0)
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "planned-failover")

    def test_stale_statusline_evidence_is_unknown_not_zero(self) -> None:
        payload = {
            "observed_at": _iso(NOW - timedelta(minutes=16)),
            "limits": {
                "weekly": {
                    "remaining_percent": 0.0,
                    "resets_at": _iso(NOW + timedelta(days=1)),
                }
            },
        }

        snapshot = parse_claude_statusline_observation(
            payload,
            profile="claude/interactive-subscription",
            now=NOW,
            max_age=timedelta(minutes=15),
        )

        self.assertEqual(snapshot.freshness, "stale")
        self.assertEqual(snapshot.evidence["reason"], "stale")
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")

    def test_missing_or_invalid_statusline_file_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "quota.json"
            observer = ClaudeStatuslineQuotaObserver(
                missing,
                profile="claude/interactive-subscription",
                now=lambda: NOW,
            )

            snapshot = observer.read()

        self.assertEqual(snapshot.freshness, "unavailable")
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(snapshot.evidence["reason"], "missing-or-invalid-json")
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")

    def test_statusline_file_reader_preserves_path_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "quota.json"
            path.write_text(
                json.dumps(
                    {
                        "observed_at": _iso(NOW),
                        "limits": {
                            "weekly": {
                                "remaining_percent": 80.0,
                                "resets_at": _iso(NOW + timedelta(days=3)),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            observer = ClaudeStatuslineQuotaObserver(
                path,
                profile="claude/interactive-subscription",
                now=lambda: NOW,
            )

            snapshot = observer.read()

        self.assertEqual(snapshot.freshness, "fresh")
        self.assertEqual(snapshot.evidence["path"], str(path))
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "stay")


class UnavailableQuotaObserverTest(unittest.TestCase):
    def test_read_returns_aware_utc_snapshot_and_reason_takes_precedence(self) -> None:
        observer = UnavailableQuotaObserver(
            profile="claude/interactive-subscription",
            reason="invalid-acceptance-overlay-config",
            evidence={"provider": "claude", "reason": "caller-reason"},
            now=lambda: datetime(2026, 8, 3, 12, 0),
        )

        snapshot = observer.read()

        self.assertEqual(snapshot.freshness, "unavailable")
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(snapshot.observed_at, NOW)
        self.assertEqual(snapshot.evidence["provider"], "claude")
        self.assertEqual(snapshot.evidence["reason"], "invalid-acceptance-overlay-config")


class ClaudeAcceptanceQuotaOverlayObserverTest(unittest.TestCase):
    def _observer(
        self,
        state_dir: Path,
        canonical: Path,
        overlay: Path,
        *,
        run_id: str = "20260803T120000Z",
    ) -> ClaudeAcceptanceQuotaOverlayObserver:
        return ClaudeAcceptanceQuotaOverlayObserver(
            overlay,
            canonical_path=canonical,
            profile="claude/interactive-subscription",
            run_id=run_id,
            state_dir=state_dir,
            now=lambda: NOW,
        )

    def test_valid_overlay_fixture_uses_acceptance_evidence_without_mutating_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_dir, canonical, overlay = _write_acceptance_overlay(Path(root))
            before = canonical.read_bytes()
            snapshot = self._observer(state_dir, canonical, overlay).read()
            after = canonical.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual(snapshot.freshness, "fresh")
        self.assertEqual(snapshot.observed_at, NOW)
        self.assertEqual(snapshot.evidence["source"], "acceptance-overlay")
        self.assertEqual(snapshot.evidence["underlying_source"], "statusline")
        self.assertEqual(snapshot.evidence["fixture"], "true")
        self.assertEqual(snapshot.evidence["run_id"], "20260803T120000Z")
        self.assertEqual(snapshot.windows[0].name, "weekly")
        self.assertEqual(snapshot.windows[0].remaining_percent, 80.0)

    def test_overlay_violations_are_unavailable_without_canonical_fallback(self) -> None:
        cases = (
            ("wrong-run", {"run_id": "other-run"}, None),
            ("wrong-path", {"canonical_path": "/tmp/not-canonical"}, None),
            ("wrong-schema", {"schema": "wrong/v1"}, None),
            ("invalid-payload", {"payload": []}, None),
            ("hash-mismatch", {}, "mutate-canonical"),
            ("wrong-mode", {}, "chmod"),
            ("overlay-symlink", {}, "overlay-symlink"),
            ("canonical-symlink", {}, "canonical-symlink"),
            ("canonical-alias", {}, "canonical-alias"),
            ("canonical-hardlink", {}, "canonical-hardlink"),
            ("unexpected-overlay-path", {}, "unexpected-overlay-path"),
        )
        for name, overrides, mutation in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                state_dir, canonical, overlay = _write_acceptance_overlay(
                    Path(root),
                    envelope_overrides=overrides,
                )
                if mutation == "mutate-canonical":
                    canonical.write_bytes(b'{"changed":true}\n')
                elif mutation == "chmod":
                    overlay.chmod(0o644)
                elif mutation == "overlay-symlink":
                    target = overlay.with_name("real-overlay.json")
                    overlay.rename(target)
                    overlay.symlink_to(target)
                elif mutation == "canonical-symlink":
                    target = canonical.with_name("real-statusline.json")
                    canonical.rename(target)
                    canonical.symlink_to(target)
                elif mutation == "canonical-alias":
                    overlay = canonical
                elif mutation == "canonical-hardlink":
                    overlay.unlink()
                    os.link(canonical, overlay)
                    overlay.chmod(0o600)
                elif mutation == "unexpected-overlay-path":
                    unexpected = overlay.with_name("other-fixture.json")
                    overlay.rename(unexpected)
                    overlay = unexpected

                snapshot = self._observer(state_dir, canonical, overlay).read()

                self.assertEqual(snapshot.freshness, "unavailable")
                self.assertEqual(snapshot.windows, ())
                self.assertEqual(snapshot.evidence["source"], "acceptance-overlay")

    def test_direct_observer_rejects_parent_traversal_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_dir, canonical, valid_overlay = _write_acceptance_overlay(Path(root))
            envelope = json.loads(valid_overlay.read_text(encoding="utf-8"))
            envelope["run_id"] = ".."
            overlay = state_dir / "quota-overlay.json"
            overlay.write_text(json.dumps(envelope), encoding="utf-8")
            overlay.chmod(0o600)

            snapshot = self._observer(
                state_dir,
                canonical,
                overlay,
                run_id="..",
            ).read()

        self.assertEqual(snapshot.freshness, "unavailable")
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(snapshot.evidence["reason"], "invalid-run-id")
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")


class CodexAppServerQuotaObservationTest(unittest.TestCase):
    def _fake_codex_executable(self, root: Path, *, mode: str) -> tuple[Path, Path, Path]:
        executable = root / "fake_codex"
        record_path = root / "requests.json"
        pid_path = root / "pid"
        executable.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import select
                import sys
                import time
                from pathlib import Path

                MODE = {mode!r}
                RECORD_PATH = Path({str(record_path)!r})
                PID_PATH = Path({str(pid_path)!r})
                REQUESTS = []

                def save_requests():
                    RECORD_PATH.write_text(json.dumps(REQUESTS), encoding="utf-8")

                def write_stdout(payload):
                    os.write(1, json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\\n")

                def write_stderr(payload):
                    os.write(2, payload.encode("utf-8"))

                def fail(code):
                    save_requests()
                    sys.exit(code)

                def read_line(timeout=2.0):
                    fd = sys.stdin.fileno()
                    deadline = time.monotonic() + timeout
                    line = bytearray()
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            fail(10)
                        ready, _, _ = select.select([fd], [], [], remaining)
                        if not ready:
                            fail(11)
                        chunk = os.read(fd, 1)
                        if not chunk:
                            fail(12)
                        line.extend(chunk)
                        if chunk == b"\\n":
                            try:
                                payload = json.loads(line.decode("utf-8"))
                            except Exception:
                                fail(13)
                            REQUESTS.append(payload)
                            save_requests()
                            return payload

                PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
                if sys.argv[1:] != ["app-server", "--stdio"]:
                    fail(14)

                initialize = read_line()
                if initialize.get("id") != 1 or initialize.get("method") != "initialize":
                    fail(15)
                ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.05)
                if ready:
                    write_stdout({{"id": 1, "result": {{}}}})
                    fail(16)
                if MODE == "timeout":
                    time.sleep(60)
                write_stdout({{"method": "account/changed", "params": {{"source": "test"}}}})
                write_stderr("diagnostic before initialize response\\n")
                write_stdout({{"id": 1, "result": {{}}}})

                initialized = read_line()
                if initialized != {{"method": "initialized"}}:
                    fail(17)
                rate_limits = read_line()
                if rate_limits != {{"id": 2, "method": "account/rateLimits/read"}}:
                    fail(18)
                if MODE == "error":
                    write_stdout({{"id": 2, "error": {{"code": -32000, "message": "no quota"}}}})
                    time.sleep(60)
                write_stdout({{"method": "telemetry/event", "params": {{"ok": True}}}})
                write_stderr("diagnostic before rate limit response\\n")
                write_stdout(
                    {{
                        "id": 2,
                        "result": {{
                            "rateLimits": {{
                                "secondary": {{
                                    "usedPercent": 20,
                                    "windowDurationMins": 10080,
                                    "resetsAt": 1785600000,
                                }}
                            }}
                        }},
                    }}
                )
                time.sleep(60)
                """
            ),
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable, record_path, pid_path

    def _pid_is_alive(self, pid_path: Path) -> bool:
        try:
            pid = int(pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_explicit_app_server_cap_is_authoritative_but_percentage_is_not(self) -> None:
        capped = parse_codex_app_server_quota_observation(
            {
                "rateLimits": {
                    "rateLimitReachedType": "weekly",
                    "secondary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10080,
                        "resetsAt": int((NOW + timedelta(days=2)).timestamp()),
                    },
                }
            },
            profile="codex/interactive-chatgpt",
            observed_at=NOW,
            now=NOW,
        )
        percentage_only = parse_codex_app_server_quota_observation(
            {
                "rateLimits": {
                    "secondary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10080,
                        "resetsAt": int((NOW + timedelta(days=2)).timestamp()),
                    },
                }
            },
            profile="codex/interactive-chatgpt",
            observed_at=NOW,
            now=NOW,
        )

        self.assertEqual(capped.evidence["authoritative_limit"], "true")
        self.assertEqual(percentage_only.evidence["authoritative_limit"], "false")

    def test_exact_app_server_wire_shape_normalizes_primary_and_weekly(self) -> None:
        payload = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 300,
                    "resetsAt": int((NOW + timedelta(hours=1)).timestamp()),
                },
                "secondary": {
                    "usedPercent": 96,
                    "windowDurationMins": 10080,
                    "resetsAt": int((NOW + timedelta(days=2)).timestamp()),
                },
            }
        }
        snapshot = parse_codex_app_server_quota_observation(
            payload,
            profile="codex/interactive-chatgpt",
            observed_at=NOW,
            now=NOW,
        )
        self.assertEqual([item.name for item in snapshot.windows], ["5h", "weekly"])
        self.assertEqual(snapshot.windows[1].remaining_percent, 4.0)
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "planned-failover")

    def test_app_server_reader_stages_requests_and_reaps_process(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            executable, record_path, pid_path = self._fake_codex_executable(root, mode="success")
            payload = read_codex_app_server_rate_limits(
                executable,
                codex_home=Path("/home/test/.codex"),
            )

            self.assertEqual(
                payload,
                {
                    "rateLimits": {
                        "secondary": {
                            "usedPercent": 20,
                            "windowDurationMins": 10080,
                            "resetsAt": 1785600000,
                        }
                    }
                },
            )
            requests = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(requests[0]["method"], "initialize")
            self.assertEqual(requests[1], {"method": "initialized"})
            self.assertEqual(requests[2], {"id": 2, "method": "account/rateLimits/read"})
            self.assertNotIn("params", requests[2])
            self.assertFalse(self._pid_is_alive(pid_path))

    def test_app_server_reader_error_response_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            executable, record_path, _ = self._fake_codex_executable(root, mode="error")

            payload = read_codex_app_server_rate_limits(
                executable,
                codex_home=Path("/home/test/.codex"),
            )

            self.assertIsNone(payload)
            requests = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(requests[2], {"id": 2, "method": "account/rateLimits/read"})

    def test_app_server_reader_timeout_reaps_process(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            executable, _, pid_path = self._fake_codex_executable(root, mode="timeout")

            payload = read_codex_app_server_rate_limits(
                executable,
                codex_home=Path("/home/test/.codex"),
                timeout=0.2,
            )

            self.assertIsNone(payload)
            self.assertFalse(self._pid_is_alive(pid_path))

    def test_app_server_limit_counts_normalize_to_weekly_snapshot(self) -> None:
        calls = []

        def read_observation():
            calls.append("called")
            return {
                "account_rate_limits": [
                    {
                        "bucket": "chatgpt-weekly",
                        "limit": 1000,
                        "remaining": 49,
                        "reset_after_seconds": 7200,
                    },
                    {
                        "window": "5h",
                        "used_percent": "25%",
                        "reset_after_seconds": 1800,
                    },
                ]
            }

        observer = CodexAppServerQuotaObserver(
            read_observation,
            profile="codex/interactive-chatgpt",
            now=lambda: NOW,
        )

        snapshot = observer.read()

        self.assertEqual(calls, ["called"])
        self.assertEqual(snapshot.profile, "codex/interactive-chatgpt")
        self.assertEqual(snapshot.freshness, "fresh")
        self.assertEqual(snapshot.observed_at, NOW)
        self.assertEqual(snapshot.evidence["provider"], "codex")
        self.assertEqual(snapshot.evidence["source"], "app-server")
        self.assertEqual(snapshot.windows[0].name, "weekly")
        self.assertAlmostEqual(snapshot.windows[0].remaining_percent, 4.9)
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "planned-failover")

    def test_app_server_reader_failure_is_unavailable(self) -> None:
        def read_observation():
            raise RuntimeError("not authenticated")

        observer = CodexAppServerQuotaObserver(
            read_observation,
            profile="codex/interactive-chatgpt",
            now=lambda: NOW,
        )

        snapshot = observer.read()

        self.assertEqual(snapshot.freshness, "unavailable")
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(snapshot.evidence["reason"], "read-failed")
        self.assertEqual(snapshot.evidence["error_type"], "RuntimeError")
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")

    def test_invalid_app_server_percentages_are_unavailable_not_blocking(self) -> None:
        observer = CodexAppServerQuotaObserver(
            lambda: {
                "observed_at": _iso(NOW),
                "windows": [
                    {
                        "name": "weekly",
                        "remaining_percent": 101,
                        "resets_at": _iso(NOW + timedelta(days=1)),
                    }
                ],
            },
            profile="codex/interactive-chatgpt",
            now=lambda: NOW,
        )

        snapshot = observer.read()

        self.assertEqual(snapshot.freshness, "unavailable")
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(snapshot.evidence["reason"], "missing-valid-windows")
        self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")


# T-2584: next_weekly_reset -- the tz/DST-correct schedule computation that
# lets a forced-cap fallback learn when its primary next resets, since a
# forced cap (unlike a planned failover) has no provider-observed reset
# window to record at swap time.
class NextWeeklyResetTest(unittest.TestCase):
    def test_midweek_swap_returns_this_weeks_upcoming_sunday_noon_central(self) -> None:
        # Wednesday 2026-01-14 04:00 CST -> the SAME week's Sunday 12:00 CST.
        now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now)
        self.assertEqual(reset, datetime(2026, 1, 18, 18, 0, tzinfo=timezone.utc))

    def test_sunday_afternoon_swap_rolls_to_the_following_sunday(self) -> None:
        # Sunday 2026-01-18 13:00 CST is already past this week's noon
        # reset instant, so the next one is the FOLLOWING Sunday.
        now = datetime(2026, 1, 18, 19, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now)
        self.assertEqual(reset, datetime(2026, 1, 25, 18, 0, tzinfo=timezone.utc))

    def test_now_exactly_on_the_reset_instant_rolls_to_next_week(self) -> None:
        # Sunday 2026-01-18 12:00:00 CST exactly -- no reset instant left to
        # wait for this week, so this must roll forward, not stay put.
        now = datetime(2026, 1, 18, 18, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now)
        self.assertEqual(reset, datetime(2026, 1, 25, 18, 0, tzinfo=timezone.utc))

    def test_cst_winter_swap_computes_the_correct_absolute_utc_instant(self) -> None:
        now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now)
        # CST is UTC-6 in January -- 12:00 Central == 18:00 UTC.
        self.assertEqual(reset.utcoffset(), timedelta(0))
        self.assertEqual(reset, datetime(2026, 1, 18, 18, 0, tzinfo=timezone.utc))

    def test_cdt_summer_swap_computes_the_correct_absolute_utc_instant(self) -> None:
        now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now)
        # CDT is UTC-5 in July -- 12:00 Central == 17:00 UTC, one hour
        # earlier than the winter case above despite the same local wall
        # time: this is the DST-correctness the whole helper exists for.
        self.assertEqual(reset, datetime(2026, 7, 19, 17, 0, tzinfo=timezone.utc))

    def test_codex_schedule_is_independent_of_claude(self) -> None:
        # Saturday 14:15 Central, same Wednesday-in-January "now" as above.
        now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("codex", now=now)
        self.assertEqual(reset, datetime(2026, 1, 17, 20, 15, tzinfo=timezone.utc))

    def test_naive_now_is_treated_as_utc(self) -> None:
        now = datetime(2026, 1, 14, 10, 0)
        reset = next_weekly_reset("claude", now=now)
        self.assertEqual(reset, datetime(2026, 1, 18, 18, 0, tzinfo=timezone.utc))

    def test_schedule_is_overridable_per_call_not_just_the_module_default(self) -> None:
        custom = {
            "claude": WeeklyResetSchedule(weekday=0, hour=0, minute=0),  # Monday midnight
        }
        now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
        reset = next_weekly_reset("claude", now=now, schedule=custom)
        self.assertNotEqual(reset, next_weekly_reset("claude", now=now))

    def test_default_schedule_covers_both_harnesses(self) -> None:
        self.assertIn("claude", HARNESS_WEEKLY_RESET_SCHEDULE)
        self.assertIn("codex", HARNESS_WEEKLY_RESET_SCHEDULE)


if __name__ == "__main__":
    unittest.main()
