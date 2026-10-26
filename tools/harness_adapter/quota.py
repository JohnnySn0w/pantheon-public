"""Pure quota-policy primitives for harness runtime switching."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


BalanceBand = Literal["ok", "low", "exhausted", "unknown"]


@dataclass(frozen=True)
class BalanceReading:
    """A prepaid provider balance (T-4924 plan D-7): no reset, so not a window.

    Amounts are ``Decimal`` parsed from the provider's string values; a reading
    is per currency, and the observer picks the one its thresholds name.
    """

    is_available: bool
    currency: str
    total: Decimal
    granted: Decimal
    topped_up: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class BalanceThresholds:
    """Absolute, per-currency thresholds from the manifest (plan §4.3)."""

    currency: str
    low_water: Decimal
    hard_floor: Decimal
    burn_alarm_24h: Decimal | None = None


@dataclass(frozen=True)
class QuotaSnapshot:
    profile: str
    windows: tuple[QuotaWindow, ...]
    observed_at: datetime
    freshness: QuotaFreshness
    evidence: Mapping[str, str] = field(default_factory=dict)
    # T-5005 (T-4924 H1): optional prepaid balance; None for windowed providers.
    balance: BalanceReading | None = None


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


def classify_balance(
    reading: BalanceReading | None,
    thresholds: BalanceThresholds | None,
) -> BalanceBand:
    """Band a prepaid balance against absolute thresholds (T-4924 plan §3.2 gap 1).

    exhausted: ``is_available`` false, or total at/below ``hard_floor``.
    low: below ``low_water``. ok: otherwise. unknown: no reading, no
    thresholds, a currency mismatch, or a non-finite/inconsistent amount.
    Pure: the H2 ``classify_snapshot`` balance branch maps the band to a
    usability verdict.
    """

    if reading is None or thresholds is None:
        return "unknown"
    if reading.currency != thresholds.currency:
        return "unknown"
    values = (reading.total, thresholds.low_water, thresholds.hard_floor)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
        return "unknown"
    if thresholds.hard_floor > thresholds.low_water:
        return "unknown"
    if not reading.is_available or reading.total <= thresholds.hard_floor:
        return "exhausted"
    if reading.total < thresholds.low_water:
        return "low"
    return "ok"


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


#: T-5011 PR-B (T-4924 plan §3.2 gap 2, artisan ruling 2): a prepaid-balance
#: harness has no weekly reset. Its cap ceiling is a re-probe interval, so a
#: top-up is noticed within it (a 402 every 15 min costs nothing). The usability
#: re-probe ceiling (subscription_usability.REPROBE_CEILING_SECONDS) uses the
#: same value.
PREPAID_REPROBE_CEILING_SECONDS: Mapping[HarnessName, float] = {"hermes": 15 * 60.0}


def cap_reset_ceiling(
    harness: HarnessName,
    *,
    now: datetime,
    schedule: Mapping[HarnessName, WeeklyResetSchedule] = HARNESS_WEEKLY_RESET_SCHEDULE,
) -> datetime:
    """The latest instant a subscription cap on ``harness`` can still hold.

    A scheduled harness (claude, codex) returns ``next_weekly_reset`` unchanged.
    A prepaid harness (hermes) returns ``now`` plus its re-probe ceiling. Any
    other harness raises ``KeyError``, as ``next_weekly_reset`` always did.
    """

    if harness in schedule:
        return next_weekly_reset(harness, now=now, schedule=schedule)
    if harness in PREPAID_REPROBE_CEILING_SECONDS:
        return _as_aware_utc(now) + timedelta(seconds=PREPAID_REPROBE_CEILING_SECONDS[harness])
    raise KeyError(harness)
