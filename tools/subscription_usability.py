"""Live subscription-usability probing and fallback selection (T-4862).

Intent: pick a fallback runtime only when its subscription is usable RIGHT NOW,
established by a live observation -- never by trusting an announced reset time.

Time semantics (the operator TG 11671-3): a ``cooldown_until``, ``primary_resets_at``
or a reset parsed from a pane banner is only a HINT for *when to look again*.
``reprobe_at = min(hint, observed_at + ceiling)`` with per-harness ceilings
(codex 30 min, claude 60 min, hermes 15 min). A probe result is trusted as follows:

* ``usable`` / ``near-cap`` are live for ``PROBE_FRESH_TTL`` (10 min) only.
* ``capped`` stays authoritative until its ``reprobe_at``. That is the TTL of a
  live OBSERVATION of a cap, bounded by the ceiling; it is not trust in any
  announced reset instant (the hint can only pull the re-look EARLIER).
* ``unknown`` is re-attempted after ``UNKNOWN_BACKOFF`` so a failing prober
  cannot be hammered.
* Not due and no live result -> ``unknown``; never asserts usable or capped.

Selection (``rank_and_select``): usable > near-cap > unknown; capped is always
excluded. While the primary is NOT confirmed capped, ``unknown`` is not
selectable (staying put is the reversible move). When the primary IS confirmed
capped, unknown is attempted last (the post-start pane classifier is the
backstop).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Literal, Mapping, Sequence

Verdict = Literal["usable", "near-cap", "capped", "unknown"]

USABILITY_SCHEMA = "pantheon-runtime-usability/v1"
PROBE_FRESH_TTL = timedelta(minutes=10)
UNKNOWN_BACKOFF = timedelta(minutes=5)
REPROBE_CEILING_SECONDS: Mapping[str, float] = {
    "codex": 30 * 60.0,
    "claude": 60 * 60.0,
    # T-5011 PR-B (plan §3.2 gap 2): a prepaid balance is re-read every 15 min;
    # the same value bounds a hermes cap (quota.cap_reset_ceiling).
    "hermes": 15 * 60.0,
}
DEFAULT_CEILING_SECONDS = 30 * 60.0
# Conservative cooldown when a cap is confirmed but nothing gives a reset hint.
CAP_DEFAULT_COOLDOWN_SECONDS = 4 * 3600.0
STATUSLINE_MAX_AGE = timedelta(minutes=15)

NO_VIABLE_ALERT_STAMP = "no-viable-target-alert.json"
ABANDON_ALERT_STAMP = "transition-abandoned-alert.json"


@dataclass(frozen=True)
class UsabilityProbe:
    runtime: str  # "<harness>/<profile>"
    verdict: Verdict
    source: str
    observed_at: datetime | None
    reprobe_at: datetime | None
    hint_until: datetime | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeDeps:
    """Injectable seams. ``None`` fields fall back to the real implementations."""

    codex_snapshot: Callable[[Any], Any] | None = None  # binding -> QuotaSnapshot
    claude_statusline: Callable[[Any], Any] | None = None  # binding -> QuotaSnapshot
    claude_pane_cap: Callable[[Any], bool | None] | None = None  # True=capped, None=n/a
    claude_pong: Callable[[Any, datetime], tuple[Any, str]] | None = None  # -> (snapshot|None, outcome kind)
    # T-5011 PR-B: any harness other than claude/codex (hermes: the provider
    # balance API) reads its adapter's quota_observer; never a spending probe.
    observer_snapshot: Callable[[Any], Any] | None = None  # binding -> QuotaSnapshot


@dataclass(frozen=True)
class SelectionDecision:
    chosen: Any | None
    decision: Literal["selected", "no-viable-target"]
    per_candidate: Mapping[str, str]  # runtime ref -> verdict | "held" | "runtime-blocked"


def _rc():
    from tools import runtime_controller

    return runtime_controller


def runtime_ref(binding: Any) -> str:
    return f"{binding.launch.harness}/{binding.launch.profile}"


def _slug(binding: Any) -> str:
    return f"{binding.launch.harness}--{binding.launch.profile}"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def usability_path(persona: str, binding: Any, *, state_root: Path | None) -> Path:
    rc = _rc()
    return rc._controller_persona_root(persona, state_root=state_root) / "runtimes" / _slug(binding) / "usability.json"


def load_usability(persona: str, binding: Any, *, state_root: Path | None) -> UsabilityProbe | None:
    try:
        raw = json.loads(usability_path(persona, binding, state_root=state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != USABILITY_SCHEMA:
        return None
    verdict = raw.get("verdict")
    if verdict not in ("usable", "near-cap", "capped", "unknown"):
        return None
    evidence = raw.get("evidence")
    return UsabilityProbe(
        runtime=str(raw.get("runtime") or runtime_ref(binding)),
        verdict=verdict,
        source=str(raw.get("source") or ""),
        observed_at=_parse(raw.get("observed_at")),
        reprobe_at=_parse(raw.get("reprobe_at")),
        hint_until=_parse(raw.get("hint_until")),
        evidence={str(k): str(v) for k, v in evidence.items()} if isinstance(evidence, dict) else {},
    )


def write_usability(persona: str, binding: Any, probe: UsabilityProbe, *, state_root: Path | None) -> None:
    path = usability_path(persona, binding, state_root=state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": USABILITY_SCHEMA,
        "runtime": probe.runtime,
        "verdict": probe.verdict,
        "source": probe.source,
        "observed_at": _iso(probe.observed_at),
        "reprobe_at": _iso(probe.reprobe_at),
        "hint_until": _iso(probe.hint_until),
        "evidence": dict(probe.evidence),
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usability-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def classify_snapshot(snapshot: Any, *, floor_percent: float) -> tuple[Verdict, dict[str, str], datetime | None]:
    """QuotaSnapshot -> (verdict, evidence, hint_until).

    capped: authoritative_limit true, or a FRESH window with remaining <= 0
    (used_percent >= 100 counts as 0). near-cap: positive weekly remaining
    below the reserve floor. usable: fresh and above the floor. Anything
    stale/unavailable/empty is unknown.
    """
    if snapshot is None:
        return "unknown", {"reason": "no-snapshot"}, None
    evidence = {str(k): str(v) for k, v in dict(getattr(snapshot, "evidence", {}) or {}).items()}
    freshness = getattr(snapshot, "freshness", "unavailable")
    if str(evidence.get("authoritative_limit", "false")).lower() == "true":
        return "capped", evidence | {"basis": "authoritative_limit"}, None
    if freshness != "fresh":
        return "unknown", evidence | {"reason": f"freshness={freshness}"}, None
    if getattr(snapshot, "balance", None) is not None:
        # T-5011 PR-B (plan §3.2 gap 1): a prepaid balance has no window and no
        # reset. Its observer bands it against the manifest thresholds
        # (quota.classify_balance); anything else is unknown.
        band = evidence.get("balance_band")
        verdict = _BALANCE_BAND_VERDICTS.get(band or "")
        if verdict is None:
            return "unknown", evidence | {"reason": f"balance_band={band or 'missing'}"}, None
        return verdict, evidence | {"basis": "prepaid-balance"}, None
    windows = tuple(getattr(snapshot, "windows", ()) or ())
    if not windows:
        return "unknown", evidence | {"reason": "no-windows"}, None

    def remaining(window: Any) -> float:
        value = float(window.remaining_percent)
        if float(window.used_percent) >= 100.0:
            return 0.0
        return value

    exhausted = [w for w in windows if remaining(w) <= 0.0]
    if exhausted:
        hints = [_aware(w.resets_at) for w in exhausted if getattr(w, "resets_at", None) is not None]
        return "capped", evidence | {"basis": "window-exhausted", "window": exhausted[0].name}, (min(hints) if hints else None)
    weekly = [w for w in windows if str(w.name).strip().lower().replace(" ", "-") in
              {"week", "weekly", "weekly-quota", "weekly_remaining", "7d", "seven-day"}]
    pool = weekly or list(windows)
    low = min(remaining(w) for w in pool)
    evidence = evidence | {"remaining_percent": f"{low:g}"}
    if low < floor_percent:
        return "near-cap", evidence, None
    return "usable", evidence, None


_BALANCE_BAND_VERDICTS: Mapping[str, Verdict] = {"ok": "usable", "low": "near-cap", "exhausted": "capped"}


def _ceiling(harness: str) -> timedelta:
    return timedelta(seconds=REPROBE_CEILING_SECONDS.get(harness, DEFAULT_CEILING_SECONDS))


def reprobe_deadline(prev: UsabilityProbe, hint: datetime | None, *, harness: str) -> datetime:
    """When to look again. A hint can only pull this EARLIER, never push it out."""
    if prev.reprobe_at is not None:
        base = prev.reprobe_at
    elif prev.observed_at is not None:
        base = prev.observed_at + _ceiling(harness)
    else:
        base = datetime.min.replace(tzinfo=timezone.utc)
    return min(base, _aware(hint)) if hint is not None else base


def _default_observer_snapshot(binding: Any, state_root: Path | None) -> Any:
    from tools import persona_bringup

    adapter = persona_bringup.build_adapter(binding, harness_home=binding.account_home)
    return adapter.quota_observer(_rc()._state_root(state_root) / binding.persona).read()


def _default_claude_pane_cap(binding: Any, now: datetime) -> bool | None:
    rc = _rc()
    try:
        from tools.harness_adapter.claude import classify_claude_mid_session_cap

        target = binding.controller.tmux
        if not (rc._session_exists(target.session) and rc.pane_exists(target)):
            return None
        pid = rc._pane_process_pid(target)
        argv = rc._read_pane_cmdline(pid) if pid is not None else None
        if not argv or not rc._pane_argv_matches_binding(argv, binding):
            return None
        text = rc._capture_visible_runtime_pane(target)
        return True if text and classify_claude_mid_session_cap(text, now=now) is not None else None
    except Exception:  # noqa: BLE001 -- free tier; any failure just means "no signal"
        return None


def _default_claude_pong(binding: Any, now: datetime, state_root: Path | None) -> tuple[Any, str]:
    from tools import persona_bringup

    snapshot, outcome = persona_bringup.refresh_primary_quota_detailed(
        binding, state_dir=_rc()._state_root(state_root), now=now
    )
    return snapshot, (outcome.kind if outcome is not None else "")


def _evaluate(binding: Any, *, state_root: Path | None, now: datetime, allow_spend: bool,
              deps: ProbeDeps) -> tuple[Verdict, str, dict[str, str], datetime | None]:
    floor = float(binding.controller.policy.weekly_remaining_percent)
    harness = binding.launch.harness
    try:
        if harness == "codex":
            read = deps.codex_snapshot or (lambda b: _default_observer_snapshot(b, state_root))
            verdict, evidence, hint = classify_snapshot(read(binding), floor_percent=floor)
            return verdict, "codex-app-server", evidence, hint
        if harness != "claude":
            # T-5011 PR-B (plan §3.2 gap 2): no non-claude harness may fall into
            # the claude tiers below (the pong probe spends). hermes reads the
            # provider balance API through its adapter's quota_observer.
            read = deps.observer_snapshot or (lambda b: _default_observer_snapshot(b, state_root))
            verdict, evidence, hint = classify_snapshot(read(binding), floor_percent=floor)
            return verdict, f"{harness}-provider-api", evidence, hint
        # claude: cheapest tier first
        pane = deps.claude_pane_cap or (lambda b: _default_claude_pane_cap(b, now))
        if pane(binding):
            return "capped", "claude-pane", {"basis": "pane-cap-modal"}, None
        line = deps.claude_statusline or (lambda b: _default_observer_snapshot(b, state_root))
        snapshot = line(binding)
        if snapshot is not None:
            observed = getattr(snapshot, "observed_at", None)
            if observed is None or _aware(now) - _aware(observed) <= STATUSLINE_MAX_AGE:
                verdict, evidence, hint = classify_snapshot(snapshot, floor_percent=floor)
                if verdict != "unknown":
                    return verdict, "claude-statusline", evidence, hint
        if allow_spend:
            pong = deps.claude_pong or (lambda b, n: _default_claude_pong(b, n, state_root))
            snapshot, kind = pong(binding, now)
            if kind == "probe-capped":
                return "capped", "claude-pong", {"basis": "pong-probe-cap"}, None
            if snapshot is not None:
                verdict, evidence, hint = classify_snapshot(snapshot, floor_percent=floor)
                return verdict, "claude-pong", evidence, hint
            return "unknown", "claude-pong", {"reason": f"probe-outcome={kind or 'none'}"}, None
        return "unknown", "claude", {"reason": "no-free-signal"}, None
    except Exception as exc:  # noqa: BLE001 -- a probe fault is "unknown", never a verdict
        return "unknown", f"{harness}-probe-error", {"error": f"{type(exc).__name__}: {exc}"[:300]}, None


def probe_usability(
    binding: Any,
    *,
    state_root: Path | None,
    now: datetime,
    allow_spend: bool,
    deps: ProbeDeps | None = None,
    hint: datetime | None = None,
    force: bool = False,
) -> UsabilityProbe:
    """Live verdict for ``binding``. See the module docstring for the TTL rules."""
    now = _aware(now)
    persona = binding.persona
    ref = runtime_ref(binding)
    harness = binding.launch.harness
    prev = load_usability(persona, binding, state_root=state_root)
    if not force and prev is not None:
        if now < reprobe_deadline(prev, hint, harness=harness):
            if prev.verdict in ("capped", "unknown"):
                return prev
            if prev.observed_at is not None and now - prev.observed_at < PROBE_FRESH_TTL:
                return prev
            return UsabilityProbe(ref, "unknown", "not-due", prev.observed_at, prev.reprobe_at,
                                  prev.hint_until, {"reason": "not-due-no-live-result"})
    verdict, source, evidence, window_hint = _evaluate(
        binding, state_root=state_root, now=now, allow_spend=allow_spend, deps=deps or ProbeDeps()
    )
    hints = [h for h in (window_hint, _aware(hint) if hint is not None else None) if h is not None]
    hint_until = min(hints) if hints else None
    if verdict == "capped":
        reprobe_at = now + _ceiling(harness)
        if hint_until is not None:
            reprobe_at = min(reprobe_at, max(hint_until, now + timedelta(minutes=1)))
    elif verdict == "unknown":
        reprobe_at = now + UNKNOWN_BACKOFF
    else:
        reprobe_at = now + PROBE_FRESH_TTL
    probe = UsabilityProbe(ref, verdict, source, now, reprobe_at, hint_until, evidence)
    try:
        write_usability(persona, binding, probe, state_root=state_root)
    except OSError:
        pass
    return probe


def rank_and_select(
    candidates: Sequence[Any],
    probes: Mapping[str, UsabilityProbe],
    *,
    holds: Mapping[str, Any],
    primary_confirmed_capped: bool,
    blocked: Collection[str] = (),
) -> SelectionDecision:
    """usable > near-cap > unknown (unknown only if the primary is confirmed
    capped); capped, held and runtime_blocked (``blocked``, T-5028 m2) are never
    selected. Manifest order breaks ties."""
    per: dict[str, str] = {}
    buckets: dict[str, list[Any]] = {"usable": [], "near-cap": [], "unknown": []}
    for binding in candidates:
        ref = runtime_ref(binding)
        if ref in holds:
            per[ref] = "held"
            continue
        if ref in blocked:
            per[ref] = "runtime-blocked"
            continue
        probe = probes.get(ref)
        verdict = probe.verdict if probe is not None else "unknown"
        per[ref] = verdict
        if verdict in buckets:
            buckets[verdict].append(binding)
    order = ["usable", "near-cap"] + (["unknown"] if primary_confirmed_capped else [])
    for name in order:
        if buckets[name]:
            return SelectionDecision(buckets[name][0], "selected", per)
    return SelectionDecision(None, "no-viable-target", per)


def apply_probe_side_effects(persona: str, binding: Any, probe: UsabilityProbe, *, state_root: Path | None,
                        now: datetime) -> None:
    rc = _rc()
    attempt = rc.load_last_attempt(persona, _slug(binding), state_root=state_root) or {}
    cooldown = rc._parse_iso_or_none(attempt.get("cooldown_until"))
    cap_episode_active = (
        attempt.get("outcome") == "failed"
        and attempt.get("failure_class") == "subscription_cap"
        and cooldown is not None
        and cooldown > now
    )
    if probe.verdict == "capped" and not cap_episode_active:
        until = probe.hint_until or (now + timedelta(seconds=CAP_DEFAULT_COOLDOWN_SECONDS))
        rc.record_last_attempt(
            persona, binding, outcome="failed", failure_class="subscription_cap",
            trigger="usability-probe", state_root=state_root, now=now, cooldown_until_override=until,
        )
    elif probe.verdict in ("usable", "near-cap") and cap_episode_active:
        rc.record_last_attempt(
            persona, binding, outcome="success", trigger="early-reset-probe",
            state_root=state_root, now=now,
        )
        rc._append_controller_evidence(
            persona, "early_reset_cooldown_cleared",
            {"runtime": runtime_ref(binding), "superseded_cooldown_until": rc._iso(cooldown),
             "verdict": probe.verdict, "probe_observed_at": _iso(probe.observed_at)},
            state_root=state_root,
        )


def select_usable_fallback(
    persona: str,
    candidates: Sequence[Any],
    *,
    state_root: Path | None,
    now: datetime,
    primary_confirmed_capped: bool,
    allow_spend: bool,
    holds: Mapping[str, Any] | None = None,
    deps: ProbeDeps | None = None,
    force: bool = False,
) -> tuple[SelectionDecision, dict[str, UsabilityProbe]]:
    """Probe every non-held candidate, record side effects, rank, select."""
    rc = _rc()
    now = _aware(now)
    effective_holds = holds if holds is not None else rc.load_holds(persona, state_root=state_root)
    probes: dict[str, UsabilityProbe] = {}
    blocked: set[str] = set()
    for binding in candidates:
        ref = runtime_ref(binding)
        if ref in effective_holds:
            continue
        attempt = rc.load_last_attempt(persona, _slug(binding), state_root=state_root) or {}
        if rc._runtime_blocked_record(attempt):
            blocked.add(ref)  # T-5028 m2: never probed, never selected
            continue
        cooldown = rc._parse_iso_or_none(attempt.get("cooldown_until"))
        hint = cooldown if (attempt.get("failure_class") == "subscription_cap" and cooldown and cooldown > now) else None
        probe = probe_usability(binding, state_root=state_root, now=now, allow_spend=allow_spend,
                                deps=deps, hint=hint, force=force)
        probes[ref] = probe
        try:
            apply_probe_side_effects(persona, binding, probe, state_root=state_root, now=now)
        except Exception:  # noqa: BLE001 -- bookkeeping must never block a verdict
            pass
    return rank_and_select(candidates, probes, holds=effective_holds, blocked=blocked,
                           primary_confirmed_capped=primary_confirmed_capped), probes


def report_no_viable_target(
    persona: str,
    event: str,
    decision: SelectionDecision,
    *,
    stamp_name: str,
    state_root: Path | None,
    now: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Loud evidence row + one cooldown-gated alert. Never raises."""
    rc = _rc()
    result: dict[str, Any] = {"alert_routed": False}
    try:
        rc._append_controller_evidence(
            persona, event,
            {"decision": decision.decision, "per_candidate": dict(decision.per_candidate), **dict(extra or {})},
            state_root=state_root,
        )
        verdicts = ", ".join(f"{k}={v}" for k, v in decision.per_candidate.items()) or "none"
        msg = (f"runtime-controller {persona}: {event} -- no fallback is usable right now "
               f"({verdicts}). The seat stays on its current runtime; no swap was made.")
        result = rc._route_cooldown_gated_alert(
            persona, msg, f"controller-usability ({persona})",
            stamp_name=stamp_name, state_root=state_root, now=now,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return result
