#!/usr/bin/env python3
"""
Pantheon — usage monitor.

Walks Claude Code transcript JSONLs under ~/.claude/projects/, sums per-turn
token usage into rolling windows (1h / 5h / 24h / 7d), appends a snapshot to
state, and (optionally) Telegram-alerts when the 5h window crosses a
threshold.

Bootstrap-mode design:
  - Run on cron every 30 min with no --alert-tokens; it just collects data.
  - After ~7 days of state accumulation, run with --propose-threshold to
    print percentile-based suggestions.
  - Then re-deploy the cron with --alert-tokens N + --send-telegram set.

No credentials touched. Reads transcripts the user already owns; sends
alerts via the bot token at ~/.claude/channels/telegram/.env.
"""

from __future__ import annotations
import argparse
import json
import os
import statistics
import sys
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_FILE = STATE_DIR / "usage.jsonl"
ALERT_COOLDOWN_FILE = STATE_DIR / "alert_cooldown"
HOURLY_STATE_FILE = STATE_DIR / "usage_hourly.json"
TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"
OPERATOR_CHAT_ID = "424242424"  # from access.json allowFrom

WINDOWS_HOURS = (1, 5, 24, 168)  # 1h, 5h, 24h, 7d
ALERT_COOLDOWN_HOURS = 4

# T-1897: calendar-hour bucket retention for the hourly-anomaly detector. usage_hourly.json is
# a MUTABLE, PRUNED sidecar (JSON object, rewritten each run) — deliberately NOT append-only
# like usage.jsonl, so it stays bounded regardless of how long usage.jsonl grows.
HOURLY_RETENTION_HOURS = 24 * 30  # 30 days

# PROPOSED (T-1875, pending the operator's ratification — see rulebook/policies/cost-monitoring-thresholds.md):
# %-of-remaining-weekly-budget consumed by the current day's rate that triggers a pacing alert.
# No default exists for the weekly quota itself (--weekly-quota-tokens) — Anthropic's real weekly
# cap is undisclosed, so the feature is opt-in-only rather than shipping a fabricated number.
WEEKLY_PACING_PCT_DEFAULT = 20.0

# PROPOSED (T-1897, pending the operator's ratification — see rulebook/policies/cost-monitoring-thresholds.md):
# sigma-multiple over the non-zero hourly-token baseline that flags a burn anomaly.
ANOMALY_SIGMA_DEFAULT = 3.0
# n>=30 is the classic rule-of-thumb floor for a CLT-stable mean/stddev estimate.
ANOMALY_MIN_SAMPLES_DEFAULT = 30
# Requires 2 CONSECUTIVE fully-elapsed hours over threshold, not 1 — a single heavy-work hour is
# common and not itself the loop/injection-burn signature; a stuck loop burns *sustained* across
# hours, so consecutive-hours filters single-spike false positives while catching the real pattern.
ANOMALY_CONSECUTIVE_HOURS_DEFAULT = 2


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _env_float(name: str, default: float | None = None) -> float | None:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_bool(name: str) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Snapshot:
    ts: str  # ISO UTC of when the snapshot was taken
    tokens_by_window: dict[str, int]  # "1h" -> int
    sessions_by_window: dict[str, int]  # active sessions touched within window
    transcripts_scanned: int
    cache_creation_by_window: dict[str, int] = field(default_factory=dict)
    cache_read_by_window: dict[str, int] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except (ValueError, TypeError):
        return None


def turn_tokens(usage: dict) -> int:
    """All token classes summed. Cache reads count too — they hit Max plan limits."""
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
    )


def scan_transcripts(
    now: datetime,
) -> tuple[dict[int, int], dict[int, set[str]], int, dict[int, int], dict[int, int], dict[str, int]]:
    """Walk transcripts; return tokens/sessions/scanned-count/cache_creation/cache_read (all keyed
    per-window-hours except scanned-count), plus hourly_tokens: calendar-hour-bucketed token sums
    (T-1897) for every record within the same 168h/7d reach the file-level mtime prefilter already
    admits — a byproduct of the walk this function already does, at ZERO added transcript I/O."""
    cutoffs = {h: now - timedelta(hours=h) for h in WINDOWS_HOURS}
    tokens = {h: 0 for h in WINDOWS_HOURS}
    sessions = {h: set() for h in WINDOWS_HOURS}
    cache_creation = {h: 0 for h in WINDOWS_HOURS}
    cache_read = {h: 0 for h in WINDOWS_HOURS}
    hourly_tokens: dict[str, int] = {}
    earliest_cutoff = min(cutoffs.values())
    scanned = 0

    if not TRANSCRIPT_ROOT.exists():
        return (
            tokens,
            {h: set() for h in WINDOWS_HOURS},
            0,
            {h: 0 for h in WINDOWS_HOURS},
            {h: 0 for h in WINDOWS_HOURS},
            {},
        )

    for jsonl in TRANSCRIPT_ROOT.rglob("*.jsonl"):
        # Cheap mtime prefilter — if file hasn't been touched within longest window, skip it.
        try:
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < earliest_cutoff:
            continue
        scanned += 1
        session_id = jsonl.stem
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    # Prefer the message timestamp; fall back to record-level ts
                    ts_raw = rec.get("timestamp") or msg.get("created_at") or rec.get("ts")
                    ts = parse_ts(ts_raw) if isinstance(ts_raw, str) else None
                    if ts is None:
                        # Fall back to file mtime — coarse, but better than dropping the row
                        ts = mtime
                    toks = turn_tokens(usage)
                    if toks <= 0:
                        continue
                    creation_toks = int(usage.get("cache_creation_input_tokens") or 0)
                    read_toks = int(usage.get("cache_read_input_tokens") or 0)
                    for h, cutoff in cutoffs.items():
                        if ts >= cutoff:
                            tokens[h] += toks
                            sessions[h].add(session_id)
                            cache_creation[h] += creation_toks
                            cache_read[h] += read_toks
                    if ts >= earliest_cutoff:
                        hour_key = floor_hour(ts)
                        hourly_tokens[hour_key] = hourly_tokens.get(hour_key, 0) + toks
        except OSError:
            continue
    return tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def write_snapshot(snap: Snapshot) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(snap), separators=(",", ":")) + "\n")


def read_telegram_token() -> str | None:
    if not TELEGRAM_ENV.exists():
        return None
    for line in TELEGRAM_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def send_telegram(text: str) -> bool:
    """POST to the Telegram Bot API. Returns True on 2xx."""
    token = read_telegram_token()
    if not token:
        print("usage_monitor: no telegram token; alert NOT sent", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": OPERATOR_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as e:
        print(f"usage_monitor: telegram send failed: {e}", file=sys.stderr)
        return False


def cooldown_file(kind: str) -> Path:
    """Per-alert-kind cooldown file. "tokens" keeps the original path (zero migration for
    the already-deployed state file); other kinds get their own file so, e.g., a cache-health
    alert firing doesn't suppress an independent weekly-quota alert for ALERT_COOLDOWN_HOURS."""
    if kind == "tokens":
        return ALERT_COOLDOWN_FILE
    return STATE_DIR / f"alert_cooldown_{kind}"


def alert_in_cooldown(now: datetime, kind: str = "tokens") -> bool:
    f = cooldown_file(kind)
    if not f.exists():
        return False
    try:
        last = parse_ts(f.read_text().strip())
    except OSError:
        return False
    if last is None:
        return False
    return (now - last) < timedelta(hours=ALERT_COOLDOWN_HOURS)


def stamp_cooldown(now: datetime, kind: str = "tokens") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cooldown_file(kind).write_text(now.isoformat())


def cache_health_ratio(creation: int, read: int) -> float | None:
    """cache_creation:cache_read ratio. None = no cache activity at all (nothing to evaluate,
    avoids alert noise on idle windows). inf = writes with zero reads back (worst case, avoids
    a ZeroDivisionError) — a healthy cache has many reads per creation, so low ratio is good."""
    if creation == 0 and read == 0:
        return None
    if read == 0:
        return float("inf")
    return creation / read


def cache_health_alert_needed(ratio: float | None, threshold: float) -> bool:
    return ratio is not None and ratio > threshold


def build_cache_health_alert_text(
    creation: int, read: int, ratio: float, threshold: float, window_label: str
) -> str:
    ratio_str = "inf (no cache reads)" if ratio == float("inf") else f"{ratio:.2f}"
    return (
        f"Pantheon usage alert — cache-health degraded ({window_label} window).\n"
        f"cache_creation: {fmt_tokens(creation)} | cache_read: {fmt_tokens(read)} | "
        f"ratio: {ratio_str} (threshold {threshold:.2f})\n"
        f"High creation:read means the cache is being rewritten more than reused — check for "
        f"a TTL regression or a broken prefix.\n"
        f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another cache-health alert."
    )


def weekly_pacing_status(tokens_7d: int, daily_rate: int, quota: int) -> dict:
    """Evaluate the 7d rolling total against a weekly-quota estimate, using the last 24h as
    the current daily burn rate (no fixed week-start exists for a rolling window, so pacing is
    framed as: at today's rate, how fast would the remaining budget go?)."""
    remaining = quota - tokens_7d
    if remaining <= 0:
        return {"remaining": remaining, "pct": None, "reason": "quota_exceeded"}
    if daily_rate <= 0:
        return {"remaining": remaining, "pct": 0.0, "reason": None}
    return {"remaining": remaining, "pct": (daily_rate / remaining) * 100, "reason": None}


def weekly_pacing_alert_needed(status: dict, pct_threshold: float) -> bool:
    if status["reason"] == "quota_exceeded":
        return True
    return status["pct"] is not None and status["pct"] > pct_threshold


def build_weekly_pacing_alert_text(
    tokens_7d: int, daily_rate: int, quota: int, status: dict
) -> str:
    if status["reason"] == "quota_exceeded":
        return (
            f"Pantheon usage alert — 7d rolling total EXCEEDS weekly quota estimate.\n"
            f"7d: {fmt_tokens(tokens_7d)} tokens vs quota {fmt_tokens(quota)} "
            f"({fmt_tokens(-status['remaining'])} over)\n"
            f"24h burn rate: {fmt_tokens(daily_rate)}/day\n"
            f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another weekly-pacing alert."
        )
    return (
        f"Pantheon usage alert — weekly-quota pacing: current burn would exhaust the "
        f"remaining budget early.\n"
        f"7d: {fmt_tokens(tokens_7d)} tokens vs quota {fmt_tokens(quota)} "
        f"(remaining {fmt_tokens(status['remaining'])})\n"
        f"24h burn rate: {fmt_tokens(daily_rate)}/day = {status['pct']:.0f}% of remaining "
        f"budget consumed per day\n"
        f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another weekly-pacing alert."
    )


def _percentiles(values: list[int]) -> dict[str, int]:
    values = sorted(values)
    n = len(values)
    return {
        "p50": values[n // 2],
        "p90": values[int(n * 0.9)],
        "p95": values[int(n * 0.95)],
        "p99": values[min(n - 1, int(n * 0.99))],
    }


def propose_threshold() -> int:
    """Print a token-threshold proposal from accumulated state."""
    if not STATE_FILE.exists():
        print("No state yet. Run with no flags on a cron for a week, then come back.")
        return 1
    rows = []
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    five_h_values = [v for v in (r.get("tokens_by_window", {}).get("5h") for r in rows) if isinstance(v, int) and v > 0]
    if len(five_h_values) < 14:  # arbitrary "enough" floor
        print(f"Only {len(five_h_values)} non-zero 5h snapshots. Want >=14. Keep collecting.")
        return 1
    p = _percentiles(five_h_values)
    print(f"5h-window token consumption ({len(five_h_values)} snapshots):")
    print(f"  p50: {fmt_tokens(p['p50'])}")
    print(f"  p90: {fmt_tokens(p['p90'])}")
    print(f"  p95: {fmt_tokens(p['p95'])}")
    print(f"  p99: {fmt_tokens(p['p99'])}")
    print(f"")
    print(f"Suggested --alert-tokens: {p['p95']} (p95 — alerts on the top 5% of 5h windows)")

    # T-1875: weekly-quota calibration aid. NOT an auto-suggested --weekly-quota-tokens value —
    # Anthropic's real weekly cap is undisclosed, so this is only a distribution reference.
    seven_d_values = [v for v in (r.get("tokens_by_window", {}).get("168h") for r in rows) if isinstance(v, int) and v > 0]
    if len(seven_d_values) >= 14:
        p7 = _percentiles(seven_d_values)
        print(f"")
        print(f"7d-window token consumption ({len(seven_d_values)} snapshots) — calibration reference for --weekly-quota-tokens, NOT a suggested value (the real weekly cap is undisclosed):")
        print(f"  p50: {fmt_tokens(p7['p50'])}")
        print(f"  p90: {fmt_tokens(p7['p90'])}")
        print(f"  p95: {fmt_tokens(p7['p95'])}")
        print(f"  p99: {fmt_tokens(p7['p99'])}")

    # T-1875: cache-ratio calibration aid — empty until snapshots carry the new fields.
    cache_ratios = []
    for r in rows:
        creation = r.get("cache_creation_by_window", {}).get("24h")
        read = r.get("cache_read_by_window", {}).get("24h")
        if isinstance(creation, int) and isinstance(read, int) and read > 0:
            cache_ratios.append(creation / read)
    if len(cache_ratios) >= 14:
        cache_ratios.sort()
        n = len(cache_ratios)
        print(f"")
        print(f"24h cache_creation:cache_read ratio ({n} snapshots) — calibration reference for --cache-ratio-threshold:")
        print(f"  p50: {cache_ratios[n // 2]:.2f}")
        print(f"  p90: {cache_ratios[int(n * 0.9)]:.2f}")
        print(f"  p95: {cache_ratios[int(n * 0.95)]:.2f}")
        print(f"  p99: {cache_ratios[min(n - 1, int(n * 0.99))]:.2f}")

    # T-1897: hourly-anomaly calibration aid — empty until usage_hourly.json accumulates data.
    # 14-sample floor matches the module's existing "enough to print a rough picture" bar used
    # above; it's deliberately looser than ANOMALY_MIN_SAMPLES_DEFAULT (30), which gates LIVE
    # alerting validity rather than a one-off printed reference.
    hourly_buckets = load_hourly_state()
    hourly_stats = hourly_baseline_stats(hourly_buckets)
    if hourly_stats is not None and hourly_stats[2] >= 14:
        mean, stddev, n = hourly_stats
        nonzero_values = sorted(v for v in hourly_buckets.values() if isinstance(v, int) and v > 0)
        p = _percentiles(nonzero_values)
        print(f"")
        print(f"Hourly token-burn distribution ({n} non-zero hourly buckets, up to {HOURLY_RETENTION_HOURS // 24}d) — calibration reference for --anomaly-sigma / --anomaly-min-samples:")
        print(f"  mean: {fmt_tokens(int(mean))}  stddev: {fmt_tokens(int(stddev))}")
        print(f"  p50: {fmt_tokens(p['p50'])}  p90: {fmt_tokens(p['p90'])}  p95: {fmt_tokens(p['p95'])}  p99: {fmt_tokens(p['p99'])}")
        print(f"  {ANOMALY_SIGMA_DEFAULT:.1f}-sigma threshold at current sample: {fmt_tokens(int(mean + ANOMALY_SIGMA_DEFAULT * stddev))}")

    return 0


def floor_hour(dt: datetime) -> str:
    """Calendar-hour bucket key, UTC. Cron ticks (*/30) land on hour boundaries, so this is
    trivially comparable across days without interpolation."""
    return dt.strftime("%Y-%m-%dT%H")


def _hour_key_to_dt(key: str) -> datetime | None:
    try:
        return datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def merge_hourly_buckets(
    existing: dict[str, int], fresh: dict[str, int], now: datetime, retention_hours: int = HOURLY_RETENTION_HOURS
) -> dict[str, int]:
    """Overwrite existing bucket values with this run's fresh (self-correcting) recompute, then
    prune anything older than retention_hours. Malformed keys are dropped rather than raising —
    state is self-healing across runs rather than a crash-on-corruption liability on a 30-min cron."""
    merged = dict(existing)
    merged.update(fresh)
    cutoff = now - timedelta(hours=retention_hours)
    pruned = {}
    for key, value in merged.items():
        dt = _hour_key_to_dt(key)
        if dt is not None and dt >= cutoff:
            pruned[key] = value
    return pruned


def hourly_baseline_stats(buckets: dict[str, int]) -> tuple[float, float, int] | None:
    """Mean/stddev/n over NON-ZERO hourly values only — mirrors the module's existing percentile
    philosophy (propose_threshold excludes zero 5h windows) so idle hours don't distort the
    baseline. None when there's no non-zero data at all."""
    values = [v for v in buckets.values() if isinstance(v, int) and v > 0]
    if not values:
        return None
    return statistics.mean(values), statistics.pstdev(values), len(values)


def anomaly_hours_to_check(now: datetime, count: int = ANOMALY_CONSECUTIVE_HOURS_DEFAULT) -> list[str]:
    """The `count` most-recently-COMPLETE calendar hours, most-recent first. Deliberately excludes
    the in-progress current hour — comparing a partial running sum against full-hour baselines
    would systematically under-flag."""
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    return [floor_hour(current_hour_start - timedelta(hours=i)) for i in range(1, count + 1)]


def anomaly_alert_needed(
    buckets: dict[str, int],
    now: datetime,
    sigma: float = ANOMALY_SIGMA_DEFAULT,
    min_samples: int = ANOMALY_MIN_SAMPLES_DEFAULT,
    consecutive: int = ANOMALY_CONSECUTIVE_HOURS_DEFAULT,
) -> tuple[bool, dict]:
    """3(or N)-sigma-over-non-zero-baseline anomaly check, requiring `consecutive` fully-elapsed
    hours to all exceed threshold before firing (see ANOMALY_CONSECUTIVE_HOURS_DEFAULT rationale)."""
    stats = hourly_baseline_stats(buckets)
    if stats is None:
        return False, {"reason": "no_data"}
    mean, stddev, n = stats
    if n < min_samples:
        return False, {"reason": "insufficient_samples", "n": n, "min_samples": min_samples}
    if stddev <= 0:
        # Zero observed variance -> can't confidently place a value in "the tail" of a
        # distribution that has no spread yet. Fails closed (no alert) rather than treating any
        # deviation from a flat constant as automatically anomalous.
        return False, {"reason": "zero_variance", "mean": mean, "n": n}
    threshold_value = mean + sigma * stddev
    hour_keys = anomaly_hours_to_check(now, count=consecutive)
    hour_values = [buckets.get(k, 0) for k in hour_keys]
    if any(v <= threshold_value for v in hour_values):
        return False, {"reason": "not_anomalous", "threshold": threshold_value, "hour_keys": hour_keys, "hour_values": hour_values}
    return True, {
        "reason": "anomalous",
        "hour_keys": hour_keys,
        "hour_values": hour_values,
        "mean": mean,
        "stddev": stddev,
        "sigma": sigma,
        "threshold": threshold_value,
        "n": n,
    }


def build_anomaly_alert_text(details: dict) -> str:
    hour_keys = details["hour_keys"]
    hour_values = details["hour_values"]
    per_hour = "; ".join(f"{k}: {fmt_tokens(v)}" for k, v in zip(hour_keys, hour_values))
    return (
        f"Pantheon usage — hourly burn anomaly, investigate.\n"
        f"{len(hour_keys)} consecutive hours over {details['sigma']:.1f}-sigma "
        f"(mean {fmt_tokens(int(details['mean']))}, stddev {fmt_tokens(int(details['stddev']))}, "
        f"threshold {fmt_tokens(int(details['threshold']))}, n={details['n']} baseline hours):\n"
        f"{per_hour}\n"
        f"Investigate-tier signal, not an alarm — a genuine heavy work arc can look identical to a "
        f"stuck loop from token counts alone; check what's running.\n"
        f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another anomaly alert."
    )


def load_hourly_state() -> dict[str, int]:
    """Reads usage_hourly.json. Missing file or any malformed shape fails open to {} — a fresh
    deployment (or a corrupted sidecar) must never crash the 30-min cron; it just re-bootstraps."""
    if not HOURLY_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(HOURLY_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    buckets = data.get("buckets") if isinstance(data, dict) else None
    if not isinstance(buckets, dict):
        return {}
    return {k: v for k, v in buckets.items() if isinstance(v, int)}


def write_hourly_state(buckets: dict[str, int]) -> None:
    """Atomic same-dir tmp-write + os.replace (the shape spine_index_groom._save_cache used
    before T-2184 removed that cache; this is now the pattern's only live instance) — a
    crash mid-write on the 30-min fleet-wide cron must never truncate this file. A plain
    write_text would leave a half-written file that load_hourly_state's fail-open would silently
    treat as corrupt-and-empty, discarding up to 30 days of baseline and restarting cold-start."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HOURLY_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"buckets": buckets}, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, HOURLY_STATE_FILE)


def build_alert_text(tokens: dict[int, int], threshold: int) -> str:
    five_h = tokens[5]
    pct_over = ((five_h - threshold) / threshold) * 100 if threshold else 0
    return (
        f"Pantheon usage alert — 5h window crossed threshold.\n"
        f"5h: {fmt_tokens(five_h)} tokens ({pct_over:+.0f}% vs threshold {fmt_tokens(threshold)})\n"
        f"24h: {fmt_tokens(tokens[24])} | 7d: {fmt_tokens(tokens[168])} | 1h: {fmt_tokens(tokens[1])}\n"
        f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another alert."
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Pantheon usage monitor.")
    p.add_argument(
        "--alert-tokens",
        type=int,
        default=None,
        help="Alert if rolling 5h window exceeds this many tokens. Omit to just collect data.",
    )
    p.add_argument(
        "--send-telegram",
        action="store_true",
        help="Actually send the alert via Telegram (otherwise just prints to stderr).",
    )
    p.add_argument(
        "--print-summary",
        action="store_true",
        help="Print current rolling-window summary to stdout.",
    )
    p.add_argument(
        "--propose-threshold",
        action="store_true",
        help="Print percentile-based threshold proposals from accumulated state, then exit.",
    )
    p.add_argument(
        "--weekly-quota-tokens",
        type=int,
        default=_env_int("PANTHEON_WEEKLY_QUOTA_TOKENS"),
        help=(
            "PROPOSED feature, pending ratification. 7d-rolling-window quota estimate for "
            "pacing alerts. No default — the real weekly subscription cap is undisclosed; "
            "set explicitly (or via PANTHEON_WEEKLY_QUOTA_TOKENS) to enable. Omit to skip."
        ),
    )
    p.add_argument(
        "--weekly-pacing-pct",
        type=float,
        default=_env_float("PANTHEON_WEEKLY_PACING_PCT", WEEKLY_PACING_PCT_DEFAULT),
        help=(
            "PROPOSED default %.1f. Alert if the last 24h's burn rate would consume more "
            "than this pct of the REMAINING weekly budget per day. Only used when "
            "--weekly-quota-tokens is set." % WEEKLY_PACING_PCT_DEFAULT
        ),
    )
    p.add_argument(
        "--cache-ratio-threshold",
        type=float,
        default=_env_float("PANTHEON_CACHE_RATIO_THRESHOLD"),
        help=(
            "PROPOSED feature, pending ratification + historical calibration. Alert if "
            "cache_creation:cache_read exceeds this ratio over --cache-ratio-window. No "
            "default (or via PANTHEON_CACHE_RATIO_THRESHOLD) — omit to skip."
        ),
    )
    p.add_argument(
        "--cache-ratio-window",
        type=int,
        default=int(os.environ.get("PANTHEON_CACHE_RATIO_WINDOW", 24)),
        choices=list(WINDOWS_HOURS),
        help="Rolling window (hours) the cache-health ratio alert evaluates. Default 24h.",
    )
    p.add_argument(
        "--enable-anomaly-alert",
        action="store_true",
        default=_env_bool("PANTHEON_ENABLE_ANOMALY_ALERT"),
        help=(
            "PROPOSED feature, pending ratification (T-1897). Hourly-bucket COLLECTION always "
            "runs; this flag gates ALERTING on top of it. Off by default, matching the "
            "--alert-tokens bootstrap discipline: collect under a bare cron, ratify the "
            "sigma/min-samples/consecutive-hours constants via --propose-threshold, then flip."
        ),
    )
    p.add_argument(
        "--anomaly-sigma",
        type=float,
        default=_env_float("PANTHEON_ANOMALY_SIGMA", ANOMALY_SIGMA_DEFAULT),
        help="PROPOSED default %.1f. Sigma-multiple over the non-zero hourly baseline." % ANOMALY_SIGMA_DEFAULT,
    )
    p.add_argument(
        "--anomaly-min-samples",
        type=int,
        default=_env_int("PANTHEON_ANOMALY_MIN_SAMPLES") or ANOMALY_MIN_SAMPLES_DEFAULT,
        help="PROPOSED default %d. Minimum non-zero hourly samples before alerting (else collect-only)." % ANOMALY_MIN_SAMPLES_DEFAULT,
    )
    p.add_argument(
        "--anomaly-consecutive-hours",
        type=int,
        default=_env_int("PANTHEON_ANOMALY_CONSECUTIVE_HOURS") or ANOMALY_CONSECUTIVE_HOURS_DEFAULT,
        help="PROPOSED default %d. Fully-elapsed hours that must ALL exceed threshold to fire." % ANOMALY_CONSECUTIVE_HOURS_DEFAULT,
    )
    args = p.parse_args()

    if args.propose_threshold:
        return propose_threshold()

    now = utcnow()
    tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = scan_transcripts(now)

    snap = Snapshot(
        ts=now.isoformat(),
        tokens_by_window={f"{h}h": tokens[h] for h in WINDOWS_HOURS},
        sessions_by_window={f"{h}h": len(sessions[h]) for h in WINDOWS_HOURS},
        transcripts_scanned=scanned,
        cache_creation_by_window={f"{h}h": cache_creation[h] for h in WINDOWS_HOURS},
        cache_read_by_window={f"{h}h": cache_read[h] for h in WINDOWS_HOURS},
    )
    write_snapshot(snap)

    # T-1897: hourly-anomaly bucket collection is unconditional every run (foundational data
    # collection, same discipline as the snapshot above) — only ALERTING is flag-gated below.
    hourly_buckets = merge_hourly_buckets(load_hourly_state(), hourly_tokens, now)
    write_hourly_state(hourly_buckets)

    if args.print_summary:
        print(
            f"5h: {fmt_tokens(tokens[5])} tokens ({len(sessions[5])} sessions) | "
            f"24h: {fmt_tokens(tokens[24])} ({len(sessions[24])}) | "
            f"7d: {fmt_tokens(tokens[168])} ({len(sessions[168])}) | "
            f"1h: {fmt_tokens(tokens[1])} ({len(sessions[1])})"
        )
        ratio_24 = cache_health_ratio(cache_creation[24], cache_read[24])
        ratio_str = (
            "n/a" if ratio_24 is None else ("inf" if ratio_24 == float("inf") else f"{ratio_24:.2f}")
        )
        print(
            f"cache 24h creation:read ratio: {ratio_str} "
            f"(creation {fmt_tokens(cache_creation[24])}, read {fmt_tokens(cache_read[24])})"
        )

    rc = 0

    if args.alert_tokens is not None and tokens[5] > args.alert_tokens:
        if alert_in_cooldown(now):
            print(
                f"usage_monitor: 5h={tokens[5]} > {args.alert_tokens} but in cooldown",
                file=sys.stderr,
            )
        else:
            text = build_alert_text(tokens, args.alert_tokens)
            if args.send_telegram:
                if send_telegram(text):
                    stamp_cooldown(now)
                    print("usage_monitor: alert sent", file=sys.stderr)
                else:
                    print("usage_monitor: alert SEND FAILED", file=sys.stderr)
                    rc = 1
            else:
                print(f"--- ALERT (dry-run, no --send-telegram) ---\n{text}", file=sys.stderr)

    if args.weekly_quota_tokens:
        status = weekly_pacing_status(tokens[168], tokens[24], args.weekly_quota_tokens)
        if weekly_pacing_alert_needed(status, args.weekly_pacing_pct):
            if alert_in_cooldown(now, kind="weekly"):
                print("usage_monitor: weekly-pacing flagged but in cooldown", file=sys.stderr)
            else:
                text = build_weekly_pacing_alert_text(
                    tokens[168], tokens[24], args.weekly_quota_tokens, status
                )
                if args.send_telegram:
                    if send_telegram(text):
                        stamp_cooldown(now, kind="weekly")
                        print("usage_monitor: weekly-pacing alert sent", file=sys.stderr)
                    else:
                        print("usage_monitor: weekly-pacing alert SEND FAILED", file=sys.stderr)
                        rc = 1
                else:
                    print(
                        f"--- WEEKLY-PACING ALERT (dry-run, no --send-telegram) ---\n{text}",
                        file=sys.stderr,
                    )

    if args.cache_ratio_threshold is not None:
        w = args.cache_ratio_window
        ratio = cache_health_ratio(cache_creation[w], cache_read[w])
        if cache_health_alert_needed(ratio, args.cache_ratio_threshold):
            if alert_in_cooldown(now, kind="cache"):
                print("usage_monitor: cache-health flagged but in cooldown", file=sys.stderr)
            else:
                text = build_cache_health_alert_text(
                    cache_creation[w], cache_read[w], ratio, args.cache_ratio_threshold, f"{w}h"
                )
                if args.send_telegram:
                    if send_telegram(text):
                        stamp_cooldown(now, kind="cache")
                        print("usage_monitor: cache-health alert sent", file=sys.stderr)
                    else:
                        print("usage_monitor: cache-health alert SEND FAILED", file=sys.stderr)
                        rc = 1
                else:
                    print(
                        f"--- CACHE-HEALTH ALERT (dry-run, no --send-telegram) ---\n{text}",
                        file=sys.stderr,
                    )

    if args.enable_anomaly_alert:
        needed, details = anomaly_alert_needed(
            hourly_buckets,
            now,
            sigma=args.anomaly_sigma,
            min_samples=args.anomaly_min_samples,
            consecutive=args.anomaly_consecutive_hours,
        )
        if needed:
            if alert_in_cooldown(now, kind="anomaly"):
                print("usage_monitor: hourly-anomaly flagged but in cooldown", file=sys.stderr)
            else:
                text = build_anomaly_alert_text(details)
                if args.send_telegram:
                    if send_telegram(text):
                        stamp_cooldown(now, kind="anomaly")
                        print("usage_monitor: hourly-anomaly alert sent", file=sys.stderr)
                    else:
                        print("usage_monitor: hourly-anomaly alert SEND FAILED", file=sys.stderr)
                        rc = 1
                else:
                    print(
                        f"--- HOURLY-ANOMALY ALERT (dry-run, no --send-telegram) ---\n{text}",
                        file=sys.stderr,
                    )

    return rc


if __name__ == "__main__":
    sys.exit(main())
