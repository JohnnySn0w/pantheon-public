#!/usr/bin/env python3
"""ORG-102 weekly digest builder (step 5).

Builds the operator-facing Markdown digest from the ORG-102 JSONL feeds and,
as part of the same weekly run, ensures that week's H2 blind-sample outbox
exists. The first live send remains an Orchestrator hand relay; this tool only writes
the on-VPS artifacts.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import quant_delivery_collect as qd  # noqa: E402
from tools import quant_h2_sampler as h2  # noqa: E402
from tools import quant_outcomes_collect as qo  # noqa: E402

DEFAULT_OUT = qd.DEFAULT_OUT
DEFAULT_LIMIT = h2.DEFAULT_LIMIT
OBSERVATIONAL_NOTE = (
    "H1, H3 and H4 are observational correlations, not causation; H2 is the direct "
    "operator-agreement check."
)
H2_OUTAGE_PHRASE = "paraphrase renderer unavailable"


def h2_outage_phrase(reason: str | None) -> str:
    """Fixed operator-facing wording for an H2 renderer outage.

    Raw codex stderr (banner, prompt echo) never reaches the digest or the
    pointer; the full reason stays in the machine-readable fields only.
    """
    if reason and "usage limit" in reason.lower():
        return f"{H2_OUTAGE_PHRASE} (codex usage limit)"
    return H2_OUTAGE_PHRASE


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return h2.load_jsonl(path)


def latest_run_id(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        run_id = row.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return "empty"


def pct_interval(interval: dict[str, Any] | None) -> str:
    if not interval:
        return "interval unavailable"
    return (
        f"{interval.get('mid', 0) * 100:.1f}% "
        f"[{interval.get('low', 0) * 100:.1f}%, {interval.get('high', 0) * 100:.1f}%]"
    )


def verdict_word(value: str | None) -> str:
    return str(value or "insufficient-n")


def pd_guidance_sort_key(value: str) -> tuple[str, int]:
    return (value[0], int(value[1:]))


def summarize_h1(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_group = {str(row.get("group")): row for row in rows if row.get("schema") == "pantheon.quant.call_h1/v1"}
    overall = by_group.get("ALL")
    cited = by_group.get("cited_pd")
    uncited = by_group.get("uncited_pd")
    if not overall:
        return {
            "verdict": "insufficient-n",
            "reason": "no H1 rows",
            "n": 0,
            "rows": {},
            "citation_comparison": "insufficient-n",
            "pd_adverse_above_overall": [],
        }
    verdict = verdict_word(overall.get("verdict"))
    reasons = list(overall.get("verdict_reasons") or [])
    cited_calls = int((cited or {}).get("calls") or 0)
    uncited_calls = int((uncited or {}).get("calls") or 0)
    overall_calls = int(overall.get("calls") or 0)
    if uncited_calls == 0 and cited_calls == overall_calls and overall_calls > 0:
        citation_comparison = "untestable by design"
        reasons.append("cited-vs-uncited comparison is untestable by design")
    elif verdict != "fail" and (
        verdict_word((cited or {}).get("verdict")) == "insufficient-n"
        or verdict_word((uncited or {}).get("verdict")) == "insufficient-n"
    ):
        citation_comparison = "insufficient-n"
        verdict = "insufficient-n"
        reasons.append("cited/uncited comparison has insufficient n")
    else:
        citation_comparison = "tested"
    if citation_comparison != "untestable by design" and verdict != "fail" and (
        verdict_word((cited or {}).get("verdict")) == "fail"
        or verdict_word((uncited or {}).get("verdict")) == "fail"
    ):
        verdict = "fail"
        reasons.append("cited/uncited comparison failed")
    overall_mid = float((overall.get("adverse_rate_wilson90") or {}).get("mid") or 0.0)
    pd_adverse = []
    known_guidance = set(qo.guidance_ids())
    for gid in sorted(known_guidance, key=pd_guidance_sort_key):
        row = by_group.get(gid)
        if not row or int(row.get("calls") or 0) < 30:
            continue
        interval = row.get("adverse_rate_wilson90") or {}
        if float(interval.get("low") or 0.0) > overall_mid:
            pd_adverse.append(row)
    return {
        "verdict": verdict,
        "reason": "; ".join(dict.fromkeys(reasons)) or "falsifier not triggered",
        "n": int(overall.get("calls") or 0),
        "adverse_interval": overall.get("adverse_rate_wilson90"),
        "rows": {"overall": overall, "cited": cited, "uncited": uncited},
        "citation_comparison": citation_comparison,
        "pd_adverse_above_overall": pd_adverse,
    }


def summarize_h2(rows: list[dict[str, Any]], week_start: str, offered: int) -> dict[str, Any]:
    matches = [row for row in rows if row.get("week_start") == week_start]
    row = matches[-1] if matches else None
    if not row:
        return {"verdict": "insufficient-n", "reason": "awaiting sample", "offered": offered, "row": None}
    if int(row.get("answered") or 0) == 0:
        return {"verdict": "insufficient-n", "reason": "awaiting the operator grade", "offered": offered, "row": row}
    return {
        "verdict": verdict_word(row.get("verdict")),
        "reason": "graded",
        "offered": int(row.get("offered") or offered),
        "row": row,
    }


def _week_rows(rows: list[dict[str, Any]], week_start: str) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        wk = row.get("week_start")
        if isinstance(wk, str) and wk <= week_start and row.get("repo") == "ALL":
            out.append(row)
    return sorted(out, key=lambda row: str(row.get("week_start")))[-8:]


def summarize_h3(rows: list[dict[str, Any]], week_start: str) -> dict[str, Any]:
    usable = [
        row for row in _week_rows(rows, week_start)
        if isinstance(row.get("operator_touches_per_arc"), (int, float))
        and isinstance(row.get("failure_rate_wilson90"), dict)
    ]
    if len(usable) < 8:
        return {"verdict": "insufficient-n", "reason": "requires 8 usable weekly delivery rows", "weeks": len(usable)}
    first = usable[:4]
    last = usable[-4:]
    touch_first = statistics.mean(float(row["operator_touches_per_arc"]) for row in first)
    touch_last = statistics.mean(float(row["operator_touches_per_arc"]) for row in last)
    fail_first = statistics.mean(float(row["failure_rate_wilson90"]["mid"]) for row in first)
    fail_last = statistics.mean(float(row["failure_rate_wilson90"]["mid"]) for row in last)
    if touch_last >= touch_first:
        verdict = "fail"
        reason = "the operator touches per arc are flat or rising"
    elif fail_last > fail_first:
        verdict = "fail"
        reason = "falling touches coincide with rising change-failure rate"
    else:
        verdict = "pass"
        reason = "touches per arc fell without a rising change-failure rate"
    return {
        "verdict": verdict,
        "reason": reason,
        "weeks": len(usable),
        "touches_first4": round(touch_first, 3),
        "touches_last4": round(touch_last, 3),
        "failure_first4": round(fail_first, 4),
        "failure_last4": round(fail_last, 4),
    }


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _breakdown_sum(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {"revert_prs": 0, "hotfix_prs": 0, "rollback_prs": 0}
    for row in rows:
        breakdown = row.get("change_failure_breakdown") or {}
        for key in out:
            out[key] += int(breakdown.get(key) or 0)
    return out


def _failed_arc_spot_checks(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    failed = [row for row in rows if int(row.get("change_failures") or 0) > 0]
    failed.sort(key=lambda row: (-int(row.get("change_failures") or 0), str(row.get("arc_id") or "")))
    return [
        {
            "arc_id": row.get("arc_id"),
            "repo": row.get("repo"),
            "merged_prs": int(row.get("merged_prs") or row.get("pr_count") or 0),
            "change_failures": int(row.get("change_failures") or 0),
            "breakdown": row.get("change_failure_breakdown") or {},
            "failed_pr_refs": row.get("failed_pr_refs") or [],
        }
        for row in failed[:limit]
    ]


def summarize_h4(arcs: list[dict[str, Any]]) -> dict[str, Any]:
    single = [row for row in arcs if int(row.get("merged_prs") or row.get("pr_count") or 0) == 1]
    multi = [row for row in arcs if int(row.get("merged_prs") or row.get("pr_count") or 0) >= 3]
    single_prs = sum(int(row.get("merged_prs") or row.get("pr_count") or 0) for row in single)
    multi_prs = sum(int(row.get("merged_prs") or row.get("pr_count") or 0) for row in multi)
    single_fail = sum(int(row.get("change_failures") or 0) for row in single)
    multi_fail = sum(int(row.get("change_failures") or 0) for row in multi)
    single_interval = qd.wilson_interval(single_fail, single_prs)
    multi_interval = qd.wilson_interval(multi_fail, multi_prs)
    single_lead = median([
        float(row["lead_time_arc_hours"]) for row in single
        if isinstance(row.get("lead_time_arc_hours"), (int, float))
    ])
    multi_lead_per_pr = median([
        float(row["lead_time_arc_hours"]) / max(1, int(row.get("merged_prs") or row.get("pr_count") or 1))
        for row in multi
        if isinstance(row.get("lead_time_arc_hours"), (int, float))
    ])
    base = {
        "single_arcs": len(single),
        "multi_arcs": len(multi),
        "single_prs": single_prs,
        "multi_prs": multi_prs,
        "single_failures": single_fail,
        "multi_failures": multi_fail,
        "single_breakdown": _breakdown_sum(single),
        "multi_breakdown": _breakdown_sum(multi),
        "failed_arc_spot_checks": _failed_arc_spot_checks(single + multi),
        "single_failure_interval": single_interval,
        "multi_failure_interval": multi_interval,
        "single_lead_hours_median": single_lead,
        "multi_lead_hours_per_pr_median": multi_lead_per_pr,
    }
    if single_prs < 5 or multi_prs < 5 or not single_interval or not multi_interval:
        return {
            **base,
            "verdict": "insufficient-n",
            "reason": "requires at least 5 single-PR and 5 multi-PR merged PRs",
            "failure_rate_verdict": "insufficient-n",
            "failure_rate_reason": "requires at least 5 single-PR and 5 multi-PR merged PRs",
            "lead_time_verdict": "insufficient-n",
            "lead_time_reason": "requires at least 5 single-PR and 5 multi-PR merged PRs",
        }
    if multi_interval["low"] > single_interval["high"]:
        failure_rate_verdict = "fail"
        failure_rate_reason = "multi-PR failure interval is clearly higher"
    else:
        failure_rate_verdict = "no-clear-difference"
        failure_rate_reason = "failure-rate intervals overlap"
    if single_lead is None or multi_lead_per_pr is None:
        lead_time_verdict = "insufficient-n"
        lead_time_reason = "lead-time scaling source is insufficient"
    elif multi_lead_per_pr > single_lead:
        lead_time_verdict = "fail"
        lead_time_reason = "multi-PR work is slower per PR"
    else:
        lead_time_verdict = "pass"
        lead_time_reason = "lead time scales per PR"
    if failure_rate_verdict == "fail" or lead_time_verdict == "fail":
        verdict = "fail"
    elif lead_time_verdict == "insufficient-n":
        verdict = "insufficient-n"
    else:
        verdict = "pass"
    if verdict == "fail":
        failed = []
        if failure_rate_verdict == "fail":
            failed.append(f"failure rate failed ({failure_rate_reason})")
        if lead_time_verdict == "fail":
            failed.append(f"lead-time scaling failed ({lead_time_reason})")
        if failure_rate_verdict == "no-clear-difference":
            failed.append("failure rate had no clear difference")
        reason = "; ".join(failed)
    else:
        reason = f"{failure_rate_reason}; {lead_time_reason}"
    return {
        **base,
        "verdict": verdict,
        "reason": reason,
        "failure_rate_verdict": failure_rate_verdict,
        "failure_rate_reason": failure_rate_reason,
        "lead_time_verdict": lead_time_verdict,
        "lead_time_reason": lead_time_reason,
    }


def dead_guidance(h1_rows: list[dict[str, Any]]) -> list[str]:
    return sorted(str(row.get("group")) for row in h1_rows if row.get("verdict") == "dead-guidance")


def top_uncited_calls(outcomes: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    rows = []
    known_guidance = set(qo.guidance_ids())
    for row in outcomes:
        via_ids = [str(v) for v in row.get("via_ids") or []]
        if any(v in known_guidance for v in via_ids):
            continue
        if row.get("outcome") not in {"held", "reversed", "overridden"}:
            continue
        rows.append(row)
    rows.sort(
        key=lambda row: (
            0 if row.get("outcome") in {"reversed", "overridden"} else 1,
            str(row.get("call_occurred_at") or ""),
            str(row.get("call_id") or ""),
        )
    )
    return rows[:limit]


def pd_cite_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {gid: 0 for gid in qo.guidance_ids()}
    for row in outcomes:
        if not row.get("eligible_for_h1"):
            continue
        for via_id in row.get("via_ids") or []:
            via_id = str(via_id)
            if via_id in counts:
                counts[via_id] += 1
    return counts


def via_class_breakdown(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in outcomes:
        if not row.get("eligible_for_h1"):
            continue
        classes = row.get("via_classes") or {}
        if not classes:
            key = "none"
        else:
            key = "+".join(f"{name}:{classes[name]}" for name in sorted(classes))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def ensure_h2_outbox(args: argparse.Namespace, renderer: Any | None = None) -> dict[str, Any]:
    samples = load_jsonl(args.out / "call_h2_samples.jsonl")
    existing = [row for row in samples if row.get("week_start") == args.week_start]
    if existing:
        sample_id = str(existing[0]["sample_id"])
        outbox = args.out / "h2" / "outbox" / f"{sample_id}.md"
        return {"sample_id": sample_id, "outbox": str(outbox), "created": False, "offered": len(existing)}

    outcomes = load_jsonl(args.outcomes)
    seed = args.seed or f"digest:{args.week_start}:{latest_run_id(outcomes)}"
    sample_args = argparse.Namespace(
        out=args.out,
        outcomes=args.outcomes,
        week_start=args.week_start,
        seed=seed,
        sample_id=args.sample_id,
        limit=args.limit,
        days=args.days,
        renderer_timeout=args.renderer_timeout,
        renderer_max_attempts=args.renderer_max_attempts,
    )
    try:
        run = h2.sample(sample_args, renderer=renderer)
    except h2.RendererUnavailable as exc:
        # Degrade, do not fail (T-4780): the digest still ships without this
        # week's H2 sample. h2.sample raises before writing any sample row or
        # outbox, so this week gets no call_h2_weekly row and cannot count as
        # a graded or low week for the H2 falsifier. A later manual
        # `quant_h2_sampler.py sample` for the week is picked up by the
        # `existing` path above on the next digest build.
        reason = str(exc)
        print(
            f"WARNING: quant_weekly_digest: H2 sample for week {args.week_start} not drawn "
            f"({h2_outage_phrase(reason)}); digest built without it, sample to be drawn late; "
            f"detail: {' '.join(reason.split())}",
            file=sys.stderr,
        )
        return {
            "sample_id": None,
            "outbox": None,
            "created": False,
            "offered": 0,
            "renderer_unavailable": reason,
        }
    return {
        "sample_id": run["sample_id"],
        "outbox": run["outbox"],
        "created": True,
        "offered": int(run["records"]["call_h2_samples"]),
        "eligible": run.get("eligible"),
        "skipped": run.get("skipped", {}),
    }


def render_markdown(
    *,
    week_start: str,
    generated_at: str,
    h1_summary: dict[str, Any],
    h2_summary: dict[str, Any],
    h3_summary: dict[str, Any],
    h4_summary: dict[str, Any],
    dead: list[str],
    uncited: list[dict[str, Any]],
    pd_counts: dict[str, int],
    via_breakdown: dict[str, int],
    h2_info: dict[str, Any],
) -> str:
    h1_rows = h1_summary["rows"]
    h2_row = h2_summary.get("row") or {}
    lines = [
        f"# ORG-102 weekly digest - {week_start}",
        "",
        f"Generated: {generated_at}",
        "",
        OBSERVATIONAL_NOTE,
        "",
        "## Verdicts",
        "",
        f"- H1 calls hold: {h1_summary['verdict']} (n={h1_summary['n']}, adverse {pct_interval(h1_summary.get('adverse_interval'))}). {h1_summary['reason']}.",
        f"- H2 the operator agreement: {h2_summary['verdict']} (offered={h2_summary.get('offered', 0)}, answered={h2_row.get('answered', 0)}, agreement {pct_interval(h2_row.get('agreement_rate_wilson90'))}). {h2_summary['reason']}.",
        f"- H3 autonomy without quality loss: {h3_summary['verdict']} ({h3_summary['reason']}).",
        f"- H4 bigger projects: {h4_summary['verdict']} ({h4_summary['reason']}).",
        "",
        "## H1 Calls Hold",
        "",
    ]
    for label, row in (("overall", h1_rows.get("overall")), ("cited P/D", h1_rows.get("cited")), ("uncited P/D", h1_rows.get("uncited"))):
        if row:
            lines.append(
                f"- {label}: {row.get('verdict')} n={row.get('calls')} adverse={pct_interval(row.get('adverse_rate_wilson90'))}"
            )
    lines.append(f"- cited-vs-uncited comparison: {h1_summary.get('citation_comparison')}")
    lines.extend(["", "## H1 P/D Adverse Rates Above Overall", ""])
    pd_adverse = h1_summary.get("pd_adverse_above_overall") or []
    if pd_adverse:
        for row in pd_adverse:
            lines.append(
                f"- {row.get('group')}: n={row.get('calls')} adverse={pct_interval(row.get('adverse_rate_wilson90'))}"
            )
    else:
        lines.append("- none detected at n>=30")
    lines.extend(["", "## H2 the operator Agreement", ""])
    if h2_info.get("renderer_unavailable"):
        lines.append(
            f"- H2 sample not drawn this run: {h2_outage_phrase(h2_info['renderer_unavailable'])}."
        )
        lines.append("- The sample will be drawn late, once the renderer is back; no outbox was written.")
        lines.append(
            "- Until that late sample is drawn and graded, this week does not count toward the H2 "
            "falsifier; once graded, it counts like any other week."
        )
    else:
        lines.append(f"- sample_id: {h2_info.get('sample_id')}")
        lines.append(f"- outbox: {h2_info.get('outbox')}")
    lines.append(f"- verdict: {h2_summary['verdict']} ({h2_summary['reason']})")
    lines.extend(["", "## H3 Autonomy And Quality", ""])
    if h3_summary.get("weeks"):
        lines.append(f"- usable weeks: {h3_summary['weeks']}")
    if "touches_first4" in h3_summary:
        lines.append(f"- touches per arc: first4={h3_summary['touches_first4']} last4={h3_summary['touches_last4']}")
        lines.append(f"- failure midpoint: first4={h3_summary['failure_first4']} last4={h3_summary['failure_last4']}")
    lines.extend(["", "## H4 Bigger Projects", ""])
    lines.append(
        f"- single-PR work: arcs={h4_summary.get('single_arcs', 0)}, merged_prs={h4_summary.get('single_prs', 0)}, failures={h4_summary.get('single_failures', 0)}, rate={pct_interval(h4_summary.get('single_failure_interval'))}"
    )
    lines.append(
        f"- multi-PR work: arcs={h4_summary.get('multi_arcs', 0)}, merged_prs={h4_summary.get('multi_prs', 0)}, failures={h4_summary.get('multi_failures', 0)}, rate={pct_interval(h4_summary.get('multi_failure_interval'))}"
    )
    lines.append("- change failures count only revert PRs, hotfix PRs, and rollback PRs; REBOUND/review churn is excluded.")
    lines.append(f"- single breakdown: {h4_summary.get('single_breakdown')}")
    lines.append(f"- multi breakdown: {h4_summary.get('multi_breakdown')}")
    lines.append(
        f"- lead time: single median={h4_summary.get('single_lead_hours_median')}h; multi median per PR={h4_summary.get('multi_lead_hours_per_pr_median')}h"
    )
    lines.append(
        f"- failure-rate comparison: {h4_summary.get('failure_rate_verdict')} ({h4_summary.get('failure_rate_reason')})"
    )
    lines.append(
        f"- lead-time scaling: {h4_summary.get('lead_time_verdict')} ({h4_summary.get('lead_time_reason')})"
    )
    lines.extend(["", "## H4 Failed Arc Spot Check", ""])
    spot_checks = h4_summary.get("failed_arc_spot_checks") or []
    if spot_checks:
        lines.append(f"- listed {len(spot_checks)} failed arcs (limit 5)")
        for row in spot_checks[:5]:
            lines.append(
                f"- {row.get('repo')} {row.get('arc_id')}: merged_prs={row.get('merged_prs')}, "
                f"failures={row.get('change_failures')}, breakdown={row.get('breakdown')}, "
                f"failed_pr_refs={row.get('failed_pr_refs')}"
            )
    else:
        lines.append("- no failed arcs detected")
    lines.extend(["", "## Dead Guidance", ""])
    if dead:
        lines.extend(f"- {group}" for group in dead[:25])
    else:
        lines.append("- none detected")
    lines.extend(["", "## P/D Cite Counts", ""])
    for gid, count in sorted(pd_counts.items(), key=lambda item: (item[0][0], int(item[0][1:]))):
        lines.append(f"- {gid}: {count}")
    lines.extend(["", "## Via Class Breakdown", ""])
    for label, count in list(via_breakdown.items())[:20]:
        lines.append(f"- {label}: {count}")
    lines.extend(["", "## Top Uncited Calls", ""])
    if uncited:
        for row in uncited:
            call = " ".join(str(row.get("call") or "").split())
            if len(call) > 180:
                call = call[:177].rstrip() + "..."
            lines.append(f"- {row.get('outcome')} {row.get('call_ref') or row.get('call_id')}: {call}")
    else:
        lines.append("- none detected")
    lines.extend(["", "## Delivery Trends", ""])
    lines.append(f"- {h3_summary['reason']}")
    lines.append(f"- {h4_summary['reason']}")
    return "\n".join(lines) + "\n"


def render_pointer(
    *,
    h1_summary: dict[str, Any],
    h2_summary: dict[str, Any],
    h3_summary: dict[str, Any],
    h4_summary: dict[str, Any],
    notable: str,
    digest_path: Path,
    h2_outbox: str | None,
    h2_unavailable: str | None = None,
) -> str:
    h2_row = h2_summary.get("row") or {}
    if h2_unavailable and not h2_row:
        agree_line = f"You agree: no sample this week, {h2_outage_phrase(h2_unavailable)}; will be drawn late"
    elif int(h2_row.get("answered") or 0) == 0:
        agree_line = "You agree: awaiting your grade"
    else:
        agree_line = f"You agree: {h2_summary['verdict']} ({pct_interval(h2_row.get('agreement_rate_wilson90'))})"
    h1_interval = h1_summary.get("adverse_interval") or {}
    calls_rate = h1_interval.get("mid")
    if isinstance(calls_rate, (int, float)):
        calls_verdict = f"{calls_rate * 100:.0f}% reversed ({h1_summary['verdict']})"
    elif h1_summary["verdict"] == "insufficient-n":
        calls_verdict = "not enough comparison data"
    else:
        calls_verdict = h1_summary["verdict"]
    autonomy_verdict = "not enough weeks yet" if h3_summary["verdict"] == "insufficient-n" else h3_summary["verdict"]
    if h4_summary["verdict"] == "insufficient-n":
        projects_verdict = "not enough projects yet"
    elif (
        h4_summary.get("failure_rate_verdict") == "no-clear-difference"
        and h4_summary.get("lead_time_verdict") == "fail"
    ):
        projects_verdict = "same failure rate, but slower per PR"
    else:
        projects_verdict = h4_summary["verdict"]
    lines = [
        f"Calls holding: {calls_verdict}",
        agree_line,
        f"Autonomy/quality: {autonomy_verdict}",
        f"Bigger projects: {projects_verdict}",
    ]
    if notable:
        lines.append(f"Notable: {notable}")
    calls_ref = h2_outbox if h2_outbox else "not drawn yet (renderer unavailable)"
    lines.append(f"Digest: {digest_path}; weekly calls: {calls_ref}")
    return "\n".join(lines) + "\n"


def write_version(out_dir: Path, run: dict[str, Any]) -> None:
    h2.write_json(
        out_dir / "schema" / "version.json",
        {
            "schema": "pantheon.quant.schema_manifest/v1",
            "generated_at": run["generated_at"],
            "run_id": run["run_id"],
            "schema_file": "schemas/quant-delivery.schema.json",
            "feeds": {
                "digest_weekly": "pantheon.quant.digest_weekly/v1",
                "runs": "pantheon.quant.run/v1",
            },
        },
    )


def build_digest(args: argparse.Namespace, renderer: Any | None = None) -> dict[str, Any]:
    generated_at = qd.iso_z(qd.now_utc())
    run_id = f"quant-weekly-digest-{qd.now_utc().strftime('%Y%m%dT%H%M%SZ')}"
    h2_info = ensure_h2_outbox(args, renderer=renderer)
    h2.recompute_weekly(args.out, run_id, generated_at)

    h1_rows = load_jsonl(args.out / "call_h1.jsonl")
    h2_rows = load_jsonl(args.out / "call_h2_weekly.jsonl")
    outcomes = load_jsonl(args.out / "call_outcomes.jsonl")
    delivery_prs = load_jsonl(args.out / "delivery_prs.jsonl")
    if delivery_prs:
        delivery_weekly = qd.weekly_records(
            delivery_prs,
            run_id,
            generated_at,
            {"status": "insufficient-source", "used_percentage": None, "readings": 0},
        )
        arcs = qd.arc_records(delivery_prs, run_id, generated_at)
    else:
        delivery_weekly = load_jsonl(args.out / "delivery_weekly.jsonl")
        arcs = load_jsonl(args.out / "delivery_arcs.jsonl")

    h1_summary = summarize_h1(h1_rows)
    h2_summary = summarize_h2(h2_rows, args.week_start, int(h2_info.get("offered") or 0))
    h2_unavailable = h2_info.get("renderer_unavailable")
    if h2_unavailable and h2_summary.get("row") is None:
        # Verdict stays as summarize_h2 computed it from the existing weekly
        # rows; only the reason is made honest about the outage.
        h2_summary["reason"] = f"sample not drawn, {h2_outage_phrase(h2_unavailable)}; will be drawn late"
    h3_summary = summarize_h3(delivery_weekly, args.week_start)
    h4_summary = summarize_h4(arcs)
    dead = dead_guidance(h1_rows)
    uncited = top_uncited_calls(outcomes)
    pd_counts = pd_cite_counts(outcomes)
    via_breakdown = via_class_breakdown(outcomes)
    if dead:
        noun = "id" if len(dead) == 1 else "ids"
        notable = f"{len(dead)} dead guidance {noun}"
    elif h4_summary["verdict"] == "fail":
        notable = f"bigger projects: {h4_summary['reason']}"
    elif uncited:
        notable = f"{len(uncited)} uncited calls to review"
    else:
        notable = ""

    digest_path = args.out / "digest" / "weekly" / f"quant-{args.week_start}.md"
    pointer_path = args.out / "digest" / "outbox" / f"quant-{args.week_start}-telegram.txt"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        render_markdown(
            week_start=args.week_start,
            generated_at=generated_at,
            h1_summary=h1_summary,
            h2_summary=h2_summary,
            h3_summary=h3_summary,
            h4_summary=h4_summary,
            dead=dead,
            uncited=uncited,
            pd_counts=pd_counts,
            via_breakdown=via_breakdown,
            h2_info=h2_info,
        ),
        encoding="utf-8",
    )
    pointer_path.write_text(
        render_pointer(
            h1_summary=h1_summary,
            h2_summary=h2_summary,
            h3_summary=h3_summary,
            h4_summary=h4_summary,
            notable=notable,
            digest_path=digest_path,
            h2_outbox=h2_info.get("outbox"),
            h2_unavailable=h2_unavailable,
        ),
        encoding="utf-8",
    )
    record = {
        "schema": "pantheon.quant.digest_weekly/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "week_start": args.week_start,
        "digest_file": str(digest_path),
        "telegram_pointer_file": str(pointer_path),
        "h2_sample_id": h2_info.get("sample_id"),
        "h2_outbox": h2_info.get("outbox"),
        "h2_sample_created": bool(h2_info.get("created")),
        "h2_renderer_unavailable": h2_unavailable,
        "verdicts": {
            "h1": h1_summary["verdict"],
            "h2": h2_summary["verdict"],
            "h3": h3_summary["verdict"],
            "h4": h4_summary["verdict"],
        },
        "dead_guidance_count": len(dead),
        "top_uncited_count": len(uncited),
    }
    h2.append_jsonl(args.out / "digest_weekly.jsonl", [record])
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "collector": "weekly-digest",
        "days": args.days,
        "repos": [],
        "source_status_by_repo": {},
        "records": {"digest_weekly": 1},
        "week_start": args.week_start,
        "digest_file": str(digest_path),
        "telegram_pointer_file": str(pointer_path),
        "h2_outbox": h2_info.get("outbox"),
        "h2_info": h2_info,
        "degraded": ["h2_renderer_unavailable"] if h2_unavailable else [],
        "verdicts": record["verdicts"],
    }
    h2.append_jsonl(args.out / "runs" / f"{run_id}.jsonl", [run])
    write_version(args.out, run)
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", nargs="?", default="build", choices=["build"])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--week-start", default=None)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--outcomes", type=Path, default=None)
    parser.add_argument("--seed")
    parser.add_argument("--sample-id")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--renderer-timeout", type=int, default=120)
    parser.add_argument("--renderer-max-attempts", type=int, default=h2.DEFAULT_RENDERER_MAX_ATTEMPTS)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.week_start = h2.parse_week(args.week_start)
    args.outcomes = args.outcomes or (args.out / "call_outcomes.jsonl")
    return args


def main(argv: list[str] | None = None) -> int:
    args = normalize_args(build_parser().parse_args(argv))
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2
    result = build_digest(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
