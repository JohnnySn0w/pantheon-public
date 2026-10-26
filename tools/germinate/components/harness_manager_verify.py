"""harness-manager verify: the ONE invariant list, evaluated (ORG-080 A-6; T-5090, GF1).

Pure code over data. ``evaluate`` takes the parsed invariant list
(``harness-manager.invariants.yaml``, CURRENT and Recovery-approved, D15) and one seat's
observation, and returns one result per invariant. ``rollup`` maps results onto
the emitter state vocabulary (ORG-080 §2 ``emitter.reports.states``).

UNWIRED by ruling (T-5090 S1): the engine's ``verify`` is still the receipted
drift re-check (executor bubble 9); calling this from ``germinate verify`` is an
engine edit, filed as a seam ticket by the orchestrator. Nothing here probes a
host: in GF1 observations come only from the simulated handler or from tests.

Observation shape (one seat)::

    {"context": {"adapter": "claude", "role": "<role-id>", "systemd_runtime": true},
     "facts": {"<invariant id>": {...class-specific fields...}},
     "carve_outs": {"<invariant id>": {"verdict": "...", "verified": bool}},
     "suppressed": false}

Statuses: ``pass`` | ``fail`` | ``vacuous`` (not applicable to this seat) |
``alarm`` (could not ask: missing or malformed fact, UNMEASURABLE probe, an
unverified carve-out). Alarm is never a pass (ORG-099 §8).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import yaml

from tools.germinate.planner_sandbox.wal.artifacts import canonical_json_bytes

LIST_ID = "org.pantheon.persona-stack/v1"
LIST_PATH = Path(__file__).resolve().with_name("harness-manager.invariants.yaml")
CLASSES = frozenset(
    {
        "presence-mode",
        "schema",
        "set-compare",
        "receipt-bound",
        "live-compare",
        "trace",
        "recorded-probe",
        "hook-allowlist-parity",
        "owner-manager-match",
    }
)
SEVERITIES = ("degraded", "unhealthy")
STATES = ("ready", "degraded", "unhealthy", "not_configured", "suppressed")
#: parity_render.classify_outcome's verdict vocabulary (tools/parity_render.py).
PROBE_PASS, PROBE_FAIL = "PASS", "FAIL"


class InvariantListError(ValueError):
    """The invariant list document is malformed."""


def load_list(path: Path = LIST_PATH) -> dict[str, Any]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    check_list(doc)
    return doc


def list_digest(path: Path = LIST_PATH) -> str:
    """``sha256:`` of the canonical JSON of the parsed list (comments excluded)."""

    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return "sha256:" + hashlib.sha256(canonical_json_bytes(doc)).hexdigest()


def check_list(doc: Any) -> None:
    if not isinstance(doc, dict) or set(doc) != {"invariant_list"}:
        raise InvariantListError("top level must be exactly one `invariant_list:` mapping")
    body = doc["invariant_list"]
    if not isinstance(body, dict) or body.get("id") != LIST_ID:
        raise InvariantListError(f"invariant_list.id must be {LIST_ID}")
    entries = body.get("invariants")
    if not isinstance(entries, list) or not entries:
        raise InvariantListError("invariant_list.invariants must be a non-empty list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise InvariantListError("every invariant needs a string id")
        if entry["id"] in seen:
            raise InvariantListError(f"duplicate invariant id {entry['id']!r}")
        seen.add(entry["id"])
        if entry.get("class") not in CLASSES:
            raise InvariantListError(f"{entry['id']}: class {entry.get('class')!r} not in {sorted(CLASSES)}")
        if entry.get("on_fail") not in SEVERITIES:
            raise InvariantListError(f"{entry['id']}: on_fail must be one of {SEVERITIES}")
        if entry.get("machine_check") not in ("defined", "ambiguous"):
            raise InvariantListError(f"{entry['id']}: machine_check must be defined or ambiguous")
        applies = entry.get("applies_to")
        if applies != "all" and not (isinstance(applies, dict) and applies and set(applies) <= {"adapters", "roles", "systemd_runtime"}):
            raise InvariantListError(f"{entry['id']}: applies_to must be 'all' or {{adapters?, roles?, systemd_runtime?}}")


# ---------------------------------------------------------------------------
# Per-class rules: fact -> (status, detail). A KeyError/TypeError is "could not ask".
# ---------------------------------------------------------------------------


def _presence_mode(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    if fact["present"] is not True:
        return "fail", "absent"
    if fact["owner"] != fact["expected_owner"]:
        return "fail", f"owner {fact['owner']} != {fact['expected_owner']}"
    if fact["mode"] != fact["expected_mode"]:
        return "fail", f"mode {fact['mode']} != {fact['expected_mode']}"
    if entry.get("no_symlink") and fact["symlink"] is not False:
        return "fail", "is (or may be) a symlink"
    return "pass", "present, owner and mode match"


def _schema(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    document, required = fact["document"], fact["required_keys"]
    if not isinstance(required, list):
        raise TypeError("required_keys must be a list")
    if not isinstance(document, dict):
        return "fail", "not a mapping"
    missing = sorted(k for k in required if k not in document)
    return ("fail", f"missing keys {missing}") if missing else ("pass", "mapping with the required keys")


def _set_compare(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    actual, expected = fact["actual"], fact["expected"]
    if not isinstance(actual, list) or not isinstance(expected, list):
        raise TypeError("actual/expected must be lists")
    extra, missing = sorted(set(actual) - set(expected)), sorted(set(expected) - set(actual))
    if extra or missing:
        return "fail", f"missing {missing}, unexpected {extra}"
    return "pass", "set matches the declared set"


def _receipt_bound(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    evidence, bound = fact["evidence_sha256"], fact["receipt_sha256"]
    if not evidence or not bound:
        return "fail", "evidence or receipt binding absent"
    return ("pass", "evidence hash bound in the receipt") if evidence == bound else ("fail", "evidence hash != receipt binding")


def _live_compare(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    actual, declared = fact["actual_grants"], fact["declared"]
    if not isinstance(actual, list) or not isinstance(declared, list):
        raise TypeError("actual_grants/declared must be lists")
    if fact["current_session"] is not True:
        return "fail", "audit evidence is not from the current session (prior-session PASS is insufficient)"
    excess = sorted(set(actual) - set(declared))
    return ("fail", f"grants exceed the declared envelope: {excess}") if excess else ("pass", "grants within the declared envelope")


def _trace(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    chain = fact["chain"]
    if not isinstance(chain, list):
        raise TypeError("chain must be a list")
    if not chain or fact["resolves"] is not True:
        return "fail", "binding chain does not resolve to its pinned target"
    return "pass", " -> ".join(str(hop) for hop in chain)


def _recorded_probe(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    verdict = fact["verdict"]
    if verdict == PROBE_PASS:
        return "pass", "probe PASS"
    if verdict == PROBE_FAIL:
        return "fail", "probe FAIL"
    return "alarm", f"probe verdict {verdict!r}: could not ask"


def _hook_allowlist_parity(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    hook_matches, allowlist_matches = fact["hook_symlink_matches"], fact["allowlist_matches"]
    if not isinstance(hook_matches, bool) or not isinstance(allowlist_matches, bool):
        raise TypeError("hook_symlink_matches/allowlist_matches must be booleans")
    failures = []
    if not hook_matches:
        failures.append("hook symlink parity failed")
    if not allowlist_matches:
        failures.append("allowlist parity failed")
    return ("fail", "; ".join(failures)) if failures else ("pass", "hook symlink and allowlist match")


def _owner_manager_match(entry: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[str, str]:
    configured, observed = fact["configured_owner"], fact["observed_manager"]
    if not isinstance(configured, str) or not configured.strip():
        raise TypeError("configured_owner must be a non-empty string")
    if not isinstance(observed, str) or not observed.strip():
        raise TypeError("observed_manager must be a non-empty string")
    if configured != observed:
        return "fail", f"configured owner {configured!r} != observed manager {observed!r}"
    return "pass", "configured owner matches observed manager"


_RULES = {
    "presence-mode": _presence_mode,
    "schema": _schema,
    "set-compare": _set_compare,
    "receipt-bound": _receipt_bound,
    "live-compare": _live_compare,
    "trace": _trace,
    "recorded-probe": _recorded_probe,
    "hook-allowlist-parity": _hook_allowlist_parity,
    "owner-manager-match": _owner_manager_match,
}


def applies(entry: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    rule = entry.get("applies_to")
    if rule == "all":
        return True
    if "adapters" in rule and context.get("adapter") not in rule["adapters"]:
        return False
    if "roles" in rule and context.get("role") not in rule["roles"]:
        return False
    if rule.get("systemd_runtime") and context.get("systemd_runtime") is not True:
        return False
    return True


def evaluate_one(entry: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    iid = entry["id"]
    base = {"id": iid, "class": entry["class"], "on_fail": entry["on_fail"], "machine_check": entry["machine_check"]}
    context = observation.get("context")
    if not isinstance(context, dict):
        return dict(base, status="alarm", detail="no seat context: could not ask")
    if not applies(entry, context):
        return dict(base, status="vacuous", detail="not applicable to this seat")
    carve = (observation.get("carve_outs") or {}).get(iid)
    if carve is not None:
        if isinstance(carve, dict) and carve.get("verified") is True:
            return dict(base, status="pass", detail=f"declared carve-out ({carve.get('verdict')}) verified", carve_out=True)
        return dict(base, status="alarm", detail="declared carve-out is not verified")
    fact = (observation.get("facts") or {}).get(iid)
    if not isinstance(fact, dict):
        return dict(base, status="alarm", detail="no observation: could not ask")
    try:
        status, detail = _RULES[entry["class"]](entry, fact)
    except (KeyError, TypeError) as exc:
        return dict(base, status="alarm", detail=f"malformed observation ({type(exc).__name__}: {exc}): could not ask")
    return dict(base, status=status, detail=detail)


def evaluate(list_doc: Mapping[str, Any], observation: Mapping[str, Any]) -> list[dict[str, Any]]:
    check_list(list_doc)
    return [evaluate_one(entry, observation) for entry in list_doc["invariant_list"]["invariants"]]


def rollup(results: list[Mapping[str, Any]], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Emitter state (ORG-080 §2 reports.states) from the results.

    ``not_configured``: no seat context at all. ``suppressed``: the seat is muted
    (ORG-076 §7.1 mute != blind: reasons are still carried). Otherwise the worst
    failing/alarming invariant's ``on_fail`` (ORG-079 fail-behavior: drift is
    degraded; V11 security drift and a HARD-axis break are unhealthy), else ready.
    """

    reasons = {r["id"]: r["detail"] for r in results if r["status"] in ("fail", "alarm")}
    if not isinstance(observation.get("context"), dict):
        return {"state": "not_configured", "reasons": reasons, "security_drift": False}
    security = any(r["id"] == "ORG-079/V11" for r in results if r["status"] in ("fail", "alarm"))
    if observation.get("suppressed") is True:
        return {"state": "suppressed", "reasons": reasons, "security_drift": security}
    severities = {r["on_fail"] for r in results if r["status"] in ("fail", "alarm")}
    state = "unhealthy" if "unhealthy" in severities else "degraded" if severities else "ready"
    return {"state": state, "reasons": reasons, "security_drift": security}
