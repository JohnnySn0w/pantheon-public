"""watch component planner (ORG-080 §2; T-5964).

An ADAPTER over the legacy watch/research pipeline, which it does not change
(ORG-080 A-5: wrap, don't rewrite). The pipeline is three user-unit pairs on the
``claude-research`` (specialist) account -- ``pantheon-watch``, ``pantheon-knowledge-sweep``
and ``pantheon-watch-digest`` (the stems ``tools/watch_pipeline_decommission.py``
tears down) -- installed by hand from the ``tools/systemd/`` templates.
``pantheon-policy-outflow`` is NOT part of it (it runs on ``claude``; T-5964 D1).

No other seat can read that account's units, state or journal, so every leg is
OBSERVE-AND-ATTEST over repo-observable evidence only (``watch_evidence.collect``):

* the six unit templates' sha256 (the canonical content the install copies);
* the newest committed day index under ``knowledge/watch/reports/`` (written by
  ``watch_runner.append_day_index_entry``) and the author of the commit that
  landed it -- proof the pipeline ran, and when;
* ``knowledge/watch/topics.md`` as ``watch_runner.parse_topics`` reads it;
* the HERMES-guard byte-sequence scan of ``knowledge/watch/reports/**`` (W6).

Every finding carries a severity: FAILED refuses (and verify reports unhealthy);
DEGRADED attests (and verify reports degraded). All inputs parse strictly.

The attestation proves the repo templates plus a recent landed run by the
pipeline account. It does NOT prove the installed copies on claude-research match
the templates (that is the T-2444 cross-account drift sweep's job).

Legs (each refuses, naming the hand-run path, unless its evidence is healthy; a
rerun over identical evidence plans nothing -- verify, not mutate):

* install   -- one ``watch.adopt-attest`` op on ``watch:legacy-install``;
* configure -- one ``watch.adopt-attest`` op on ``watch:topics``;
* update    -- one ``watch.adopt-attest`` op on ``watch:update`` for a code-only
  update; a change to any unit template refuses (the hand re-install on
  claude-research is not typed: T-5972);
* remove    -- one ``watch.attest-retire`` op retiring every attestation, only
  after an EXECUTED decommission report is observed; the engine never runs the
  decommission tool (its own account check; no sudo).

The rule functions below are pure and stdlib-only; the handler
(``watch_handlers.py``) re-checks with the same functions at preflight and
verify, so evidence that drifts between plan and apply halts before any mutation.

Returns typed operation data only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

HANDLER = "watch-legacy"
KIND = "watch.adopt-attest"
RETIRE_KIND = "watch.attest-retire"
INSTALL_TARGET = "watch:legacy-install"
TOPICS_TARGET = "watch:topics"
UPDATE_TARGET = "watch:update"
ATTEST_TARGETS = (INSTALL_TARGET, TOPICS_TARGET, UPDATE_TARGET)
RETIRE_TARGET = "watch:attestations"
GATE = "adopt-legacy-install"
RETIRE_GATE = "watch-retire"
ABSENT = "absent"
OWNER = "watch"

#: the account the pipeline lives on (== watch_pipeline_decommission.PIPELINE_ACCOUNT)
PIPELINE_ACCOUNT = "claude-research"
#: the pipeline's unit stems (== watch_pipeline_decommission.WATCH_UNIT_STEMS; a test pins it)
UNIT_STEMS = ("pantheon-watch", "pantheon-knowledge-sweep", "pantheon-watch-digest")
UNITS = tuple(f"{stem}{suffix}" for stem in UNIT_STEMS for suffix in (".timer", ".service"))
#: W2 freshness tiers, from the observed landing cadence of knowledge/watch/reports/ (T-5964 r2,
#: specialist + Recovery): since about 2026-10-19 the daily topic flipper-one-launch-watch lands a day
#: index every day, so more than 2 days without one is a missed cycle (DEGRADED: attests, verify
#: reports degraded); before that the weekly topics landed with 6-7 day gaps, so more than 8 days
#: is a dead pipeline (FAILED: install refuses, verify reports unhealthy). Age is measured from
#: 00:00Z of the landed day (a day index carries a date, not a time), so it errs toward stale.
FRESH_DEGRADED_DAYS = 2
FRESH_FAILED_DAYS = 8
UPDATE_REV_KEYS = ("source_rev_from", "source_rev_to")

FAILED, DEGRADED = "failed", "degraded"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OBSERVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

HAND_INSTALL = (
    "the live install is hand-run on claude-research from tools/systemd/ (tools/systemd/README.md, "
    "'Watch loop (run as claude-research)'); the engine never installs it or reads that account"
)
HAND_TEARDOWN = (
    "the host teardown is tools/watch_pipeline_decommission.py --execute, run as claude-research (T-2887); "
    "the engine never runs it"
)


class PlanRefused(RuntimeError):
    """The legacy watch evidence does not support this leg."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def parse_day(value):
    """A date from exactly ``YYYY-MM-DD`` (a str; no prefix slicing, no basic form), else None."""

    if not (isinstance(value, str) and DAY_RE.match(value)):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_observed_at(value):
    """A UTC datetime from exactly ``YYYY-MM-DDTHH:MM:SSZ``, else None."""

    if not (isinstance(value, str) and OBSERVED_AT_RE.match(value)):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- rules (shared with the handler) ---------------------------------------------


def install_findings(evidence):
    """``[(invariant id, severity, message)]`` for the install evidence (W1, W2, W3, W6).

    FAILED findings refuse the install leg; DEGRADED ones attest and verify reports them."""

    if not isinstance(evidence, dict):
        return [("*", FAILED, "no watch evidence is observed")]
    out = []
    templates = evidence.get("templates")
    if not isinstance(templates, dict):
        out.append(("W1-templates-present", FAILED, "the unit template digests are not a mapping"))
        templates = {}
    for unit in UNITS:
        digest = templates.get(unit)
        if digest is None:
            out.append(("W1-templates-present", FAILED, f"unit template tools/systemd/{unit} is absent"))
        elif not (isinstance(digest, str) and DIGEST_RE.match(digest)):
            out.append(("W1-templates-present", FAILED, f"unit template tools/systemd/{unit} has a malformed digest {digest!r}"))
    scan = evidence.get("byte_scan")
    if not (isinstance(scan, dict) and isinstance(scan.get("ok"), bool)):
        out.append(("W6-reports-byte-sequence-clean", FAILED, "no byte-sequence scan of knowledge/watch/reports/ is observed"))
    elif not scan["ok"]:
        out.append(("W6-reports-byte-sequence-clean", FAILED, f"the forbidden byte sequence (HERMES guard) is present at {scan.get('detail')}"))
    landed = evidence.get("landed")
    if not isinstance(landed, dict):
        out.append(("W2-landed-run-fresh", FAILED, "no landed watch run is observed (no day index under knowledge/watch/reports/)"))
        out.append(("W3-landed-by-pipeline-account", FAILED, "no landed watch run is observed"))
        return out
    when = landed.get("date")
    if landed.get("unreadable"):
        out.append(("W2-landed-run-fresh", FAILED, f"the newest day index is unreadable: {landed['unreadable']}"))
    if landed.get("author") != PIPELINE_ACCOUNT:
        out.append(("W3-landed-by-pipeline-account", FAILED, f"the newest day index {when} was landed by {landed.get('author')!r}, not {PIPELINE_ACCOUNT}"))
    rows = landed.get("rows")
    if not isinstance(rows, list):
        out.append(("W3-landed-by-pipeline-account", FAILED, f"the newest day index {when} rows are not a list"))
    elif not any(isinstance(r, dict) and r.get("status") == "ok" for r in rows):
        out.append(("W3-landed-by-pipeline-account", FAILED, f"the newest day index {when} has no ok row"))
    landed_day, observed = parse_day(when), parse_observed_at(evidence.get("observed_at"))
    if landed_day is None:
        out.append(("W2-landed-run-fresh", FAILED, f"the landed day {when!r} is not a YYYY-MM-DD date"))
    if observed is None:
        out.append(("W2-landed-run-fresh", FAILED, f"observed_at {evidence.get('observed_at')!r} is not a YYYY-MM-DDTHH:MM:SSZ timestamp"))
    if landed_day is not None and observed is not None:
        age = observed - datetime(landed_day.year, landed_day.month, landed_day.day, tzinfo=timezone.utc)
        if age < timedelta(0):
            out.append(("W2-landed-run-fresh", FAILED, f"the newest day index {when} is dated after the observation"))
        elif age > timedelta(days=FRESH_FAILED_DAYS):
            out.append(("W2-landed-run-fresh", FAILED, f"stale: the newest landed run {when} is {age} old (failed past {FRESH_FAILED_DAYS} days)"))
        elif age > timedelta(days=FRESH_DEGRADED_DAYS):
            out.append(("W2-landed-run-fresh", DEGRADED, f"degraded: the newest landed run {when} is {age} old (degraded past {FRESH_DEGRADED_DAYS} days)"))
    return out


def install_problems(evidence):
    """Why the observed evidence does not show a healthy legacy install ([] = attestable).
    Only FAILED findings count; a DEGRADED install still attests."""

    return [message for _iid, severity, message in install_findings(evidence) if severity == FAILED]


def install_desired(evidence):
    """The attested install evidence (call only when install_problems is empty)."""

    templates = evidence["templates"]
    return {"pipeline_account": PIPELINE_ACCOUNT, **{f"unit.{unit}": templates[unit] for unit in UNITS}}


def topics_problems(evidence):
    topics = evidence.get("topics") if isinstance(evidence, dict) else None
    if not isinstance(topics, dict):
        return ["no parsed knowledge/watch/topics.md is observed"]
    problems = []
    warnings, active, parsed = topics.get("warnings"), topics.get("active"), topics.get("parsed")
    if not isinstance(warnings, list):
        problems.append(f"topics warnings is not a list ({type(warnings).__name__})")
    elif warnings:
        problems.append(f"watch_runner.parse_topics warns: {sorted(str(w) for w in warnings)}")
    if not isinstance(active, list):
        problems.append(f"topics active is not a list ({type(active).__name__})")
    elif not active:
        problems.append("knowledge/watch/topics.md declares no active topic")
    if not isinstance(parsed, list):
        problems.append(f"topics parsed is not a list ({type(parsed).__name__})")
    if not (isinstance(topics.get("sha256"), str) and DIGEST_RE.match(topics["sha256"])):
        problems.append(f"the topics.md digest {topics.get('sha256')!r} is malformed")
    return problems


def topics_desired(evidence):
    topics = evidence["topics"]
    return {
        "topics_sha256": str(topics.get("sha256")),
        "parsed_sha256": sha(topics.get("parsed")),
        "active_count": str(len(topics.get("active") or [])),
    }


def landed_failures(evidence):
    """Topic rows in the newest day index whose status is not ok (W5, degraded)."""

    landed = evidence.get("landed") if isinstance(evidence, dict) else None
    rows = landed.get("rows") if isinstance(landed, dict) and isinstance(landed.get("rows"), list) else []
    return sorted(str(r.get("slug")) for r in rows if isinstance(r, dict) and r.get("status") != "ok")


def decommission_problems(report):
    """Why the observed decommission report does not show an executed teardown ([] = it does)."""

    if not isinstance(report, dict):
        return ["no decommission report is observed (the pipeline was not torn down)"]
    changed, warnings = report.get("changed"), report.get("warnings")
    malformed = []
    if not isinstance(report.get("dry_run"), bool):
        malformed.append(f"dry_run is {report.get('dry_run')!r}, not a boolean")
    for key, value in (("changed", changed), ("warnings", warnings)):
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            malformed.append(f"{key} is not a list of strings")
    if malformed:
        return [f"the decommission report is malformed: {'; '.join(malformed)}"]
    problems = []
    if report["dry_run"]:
        problems.append("the decommission report is a dry run")
    if warnings:
        problems.append(f"the decommission report carries warnings: {warnings}")
    for stem in UNIT_STEMS:
        if f"disabled --now {stem}.timer" not in changed:
            problems.append(f"the decommission report does not disable {stem}.timer")
    return problems


def changed_keys(attested, current):
    return sorted(k for k in set(attested) | set(current) if attested.get(k) != current.get(k))


# --- planning ----------------------------------------------------------------------


def _observed(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict):
        raise PlanRefused(f"request carries no {HANDLER} observation")
    return observed


def _refuse(what, problems, path=HAND_INSTALL):
    raise PlanRefused(f"watch {what} is not attestable: {'; '.join(problems)}; {path}")


def _attest_op(op_id, target, desired, prior, provenance):
    return {
        "operation_id": op_id,
        "layer": 0,
        "kind": KIND,
        "target": target,
        "precondition": {"attestation_sha256": ABSENT if prior is None else sha(prior)},
        "desired": desired,
        "inverse": {"attestation": ABSENT} if prior is None else {"attestation_sha256": sha(prior)},
        "privilege": "seat",
        "owner": OWNER,
        "provenance": dict(
            {
                "planner": "tools.germinate.components.watch (observe-and-attest)",
                "live_install": "hand-run on claude-research from tools/systemd/; never run by the engine",
            },
            **provenance,
        ),
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
        "gate": GATE,
    }


def _healthy_install(observed, what):
    evidence = observed.get("evidence")
    problems = install_problems(evidence)
    if problems:
        _refuse(what, problems)
    return evidence


def plan_install(request):
    observed = _observed(request)
    evidence = _healthy_install(observed, "install")
    desired = install_desired(evidence)
    prior = (observed.get("attestations") or {}).get(INSTALL_TARGET)
    if prior == desired:
        return []  # already attested over this exact evidence: verify, not mutate
    return [
        _attest_op(
            "adopt-attest-watch-install",
            INSTALL_TARGET,
            desired,
            prior,
            {"evidence": "tools/systemd unit templates + the newest committed day index (watch_runner.append_day_index_entry) + its landing author"},
        )
    ]


def _require_install_attested(observed, what):
    if (observed.get("attestations") or {}).get(INSTALL_TARGET) is None:
        raise PlanRefused(f"watch {what} needs an attested install first (run the install leg)")


def plan_configure(request):
    observed = _observed(request)
    _require_install_attested(observed, "configure")
    evidence = _healthy_install(observed, "configure")
    problems = topics_problems(evidence)
    if problems:
        _refuse("configure", problems, "fix knowledge/watch/topics.md (watch_runner.parse_topics is the reader)")
    desired = topics_desired(evidence)
    prior = (observed.get("attestations") or {}).get(TOPICS_TARGET)
    if prior == desired:
        return []
    return [
        _attest_op(
            "adopt-attest-watch-topics",
            TOPICS_TARGET,
            desired,
            prior,
            {"evidence": "knowledge/watch/topics.md through tools.watch_runner.parse_topics"},
        )
    ]


def plan_update(request):
    """Re-attest the install over a source-revision change that touches no unit template.

    Watch's code moves with the account's pantheon-config clone (its units run
    %h/pantheon-config/tools/...), so a code-only update needs nothing on the host;
    this leg verifies and receipts it. A unit-template change needs the hand
    re-install on claude-research, which is not typed here (T-5972): refuse."""

    from_rev, to_rev = request.get("source_rev_from"), request.get("source_rev_to")
    if not from_rev or not to_rev:
        raise PlanRefused("the watch update leg needs --from-rev and --to-rev")
    observed = _observed(request)
    _require_install_attested(observed, "update")
    evidence = _healthy_install(observed, "update")
    head = evidence.get("repo_head")
    if head != to_rev:
        raise PlanRefused(f"watch update is planned on the tree it lands: --to-rev {to_rev} is not the observed HEAD {head}")
    attested = observed["attestations"][INSTALL_TARGET]
    current = install_desired(evidence)
    moved = changed_keys(attested, current)
    if moved:
        raise PlanRefused(
            f"watch update {from_rev}..{to_rev} changes unit template(s) {moved}: the installed copies on "
            "claude-research need the hand re-install (tools/systemd/README.md, 'Watch loop (run as "
            "claude-research)'), which is not typed here (T-5972); re-install, then re-run the install leg"
        )
    desired = dict(current, source_rev_from=str(from_rev), source_rev_to=str(to_rev))
    prior = (observed.get("attestations") or {}).get(UPDATE_TARGET)
    if prior == desired:
        return []
    return [
        _attest_op(
            "adopt-attest-watch-update",
            UPDATE_TARGET,
            desired,
            prior,
            {"evidence": "code-only update: unit templates unchanged since the install attestation; the code moves with the pantheon-config clone"},
        )
    ]


def plan_remove(request):
    observed = _observed(request)
    report = observed.get("decommission")
    problems = decommission_problems(report)
    if problems:
        _refuse("remove", problems, HAND_TEARDOWN)
    attestations = observed.get("attestations") or {}
    if not attestations:
        return []  # nothing engine-owned to retire
    prior = sha(attestations)
    return [
        {
            "operation_id": "attest-retire-watch",
            "layer": 0,
            "kind": RETIRE_KIND,
            "target": RETIRE_TARGET,
            "precondition": {"attestations_sha256": prior},
            "desired": {"attestations": ABSENT, "decommission_report_sha256": sha(report)},
            "inverse": {"attestations_sha256": prior},
            "privilege": "seat",
            "owner": OWNER,
            "provenance": {
                "planner": "tools.germinate.components.watch.plan_remove (observe-and-attest-retire)",
                "evidence": "the executed tools.watch_pipeline_decommission.decommission report",
                "live_teardown": "tools/watch_pipeline_decommission.py --execute as claude-research (T-2887); never run by the engine",
            },
            "crash_contract": "write-ahead-inverse-durable",
            "side_effect_free": False,
            "gate": RETIRE_GATE,
        }
    ]
