"""Pure quota-policy primitives for harness runtime switching."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Literal, Mapping, Protocol
from zoneinfo import ZoneInfo

from tools.harness_adapter.types import HarnessName


QuotaFreshness = Literal["fresh", "stale", "unavailable"]
FailoverDecision = Literal["stay", "planned-failover", "unknown"]
FallbackRecoveryDecision = Literal["stay-on-fallback", "return-to-primary"]


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: float
    remaining_percent: float
    resets_at: datetime


@dataclass(frozen=True)
class QuotaSnapshot:
    profile: str
    windows: tuple[QuotaWindow, ...]
    observed_at: datetime
    freshness: QuotaFreshness
    evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FailoverPolicy:
    weekly_remaining_percent: float = 5.0
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrimaryHealthProbe:
    healthy: bool
    observed_at: datetime
    freshness: QuotaFreshness
    evidence: Mapping[str, str] = field(default_factory=dict)


class QuotaObserver(Protocol):
    def read(self) -> QuotaSnapshot:
        ...


_WEEKLY_WINDOW_NAMES = frozenset(
    {
        "week",
        "weekly",
        "weekly-quota",
        "weekly_remaining",
        "weekly-remaining",
        "7d",
        "7-day",
        "seven-day",
    }
)


def decide_failover(
    snapshot: QuotaSnapshot,
    policy: FailoverPolicy,
) -> FailoverDecision:
    """Return the planned failover action from observed weekly quota only.

    Missing, stale, unavailable, non-finite, or non-weekly evidence is unknown.
    Above-threshold fresh weekly evidence stays; at or below the reserve starts a
    planned failover. This function deliberately has no prompt/model inputs.
    """

    if snapshot.freshness != "fresh":
        return "unknown"
    if not _finite_percent(policy.weekly_remaining_percent):
        return "unknown"

    weekly = _weekly_window(snapshot)
    if weekly is None or not _finite_percent(weekly.remaining_percent):
        return "unknown"
    if weekly.remaining_percent <= policy.weekly_remaining_percent:
        return "planned-failover"
    return "stay"


def decide_fallback_recovery(
    primary_snapshot: QuotaSnapshot,
    primary_probe: PrimaryHealthProbe,
    *,
    now: datetime | None = None,
) -> FallbackRecoveryDecision:
    """Keep a fallback active until reset passed and primary is freshly healthy."""

    weekly = _weekly_window(primary_snapshot)
    if primary_snapshot.freshness != "fresh" or weekly is None:
        return "stay-on-fallback"

    observed_now = _as_aware_utc(now or datetime.now(timezone.utc))
    if observed_now < _as_aware_utc(weekly.resets_at):
        return "stay-on-fallback"

    probe_observed_at = _as_aware_utc(primary_probe.observed_at)
    if (
        primary_probe.freshness == "fresh"
        and primary_probe.healthy
        and probe_observed_at >= _as_aware_utc(weekly.resets_at)
    ):
        return "return-to-primary"
    return "stay-on-fallback"


def _weekly_window(snapshot: QuotaSnapshot) -> QuotaWindow | None:
    for window in snapshot.windows:
        if _normalise_window_name(window.name) in _WEEKLY_WINDOW_NAMES:
            return window
    return None


def _normalise_window_name(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _finite_percent(value: float) -> bool:
    return isfinite(float(value)) and 0.0 <= float(value) <= 100.0


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class WeeklyResetSchedule:
    """A provider's advertised weekly quota-reset instant, in its own local time.

    ``weekday`` follows ``datetime.weekday()`` (0=Monday .. 6=Sunday). The
    instant is expressed as local wall-clock time in an IANA zone (``tz``)
    rather than a fixed UTC offset -- a zone that observes DST (like
    America/Chicago) drifts an hour twice a year under a fixed-offset model.
    """

    weekday: int
    hour: int
    minute: int
    tz: str = "America/Chicago"


# T-2584: a forced-cap failover has no provider-observed reset window to
# record at swap time (the cap already fired) -- unlike a planned failover,
# which records the live-observed reset from the quota probe. This schedule
# is the substitute source. Values are operator-OBSERVED for his account (TG
# 7039/7040, 2026-08-05), not a documented provider SLA -- verify against
# the live quota signal before trusting as exact. Config-driven and keyed
# by harness (not a magic constant baked into the computation) so a
# different account/plan can override without touching next_weekly_reset.
# If a schedule ever needs to vary per-account rather than just per-harness,
# that's a real extension, not a variant of this dict -- flag it.
HARNESS_WEEKLY_RESET_SCHEDULE: Mapping[HarnessName, WeeklyResetSchedule] = {
    "claude": WeeklyResetSchedule(weekday=6, hour=12, minute=0),  # Sunday 12:00 Central
    "codex": WeeklyResetSchedule(weekday=5, hour=14, minute=15),  # Saturday 14:15 Central
}


def next_weekly_reset(
    harness: HarnessName,
    *,
    now: datetime,
    schedule: Mapping[HarnessName, WeeklyResetSchedule] = HARNESS_WEEKLY_RESET_SCHEDULE,
) -> datetime:
    """Return ``harness``'s next weekly reset instant strictly after ``now``, in UTC.

    If ``now`` lands exactly on this week's reset instant (or past it),
    there's no instant left to wait for this week, so the following week's
    occurrence is returned.
    """

    entry = schedule[harness]
    zone = ZoneInfo(entry.tz)
    local_now = _as_aware_utc(now).astimezone(zone)
    candidate = local_now.replace(
        hour=entry.hour, minute=entry.minute, second=0, microsecond=0
    )
    candidate += timedelta(days=(entry.weekday - candidate.weekday()) % 7)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)
