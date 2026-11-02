"""ORG-060 replaceable presence projection and interaction contract (T-3227, Child C).

Ship-dark contracts plus fixtures only. A presence surface (display, wearable,
desktop pet, card, badge, AR, embodied device) SHOWS a server-owned projection of
the companion's canonical state and offers bounded input. It never becomes an
identity issuer, memory store, relationship authority, credential carrier or
actuation bypass. The canonical public identity and authority rule and the
ownership table live in tickets/plans/T-2420-implementation-plan.md
("Canonical public identity and authority rule", "Ownership table"); this module
references them and does not restate them.

Single shape authority: this module plus the five schemas it generates
(``--write-schemas``). Every parser, and every typed constructor (which builds a
mapping and returns ``parse_*(mapping)``), runs the same pipeline:

1. ``b.load_canonical_json_mapping`` (byte bound, canonical JSON, duplicate keys);
2. for public/adapter input only, a recursive KEY-only lookalike scan
   (``PRESENCE_FORBIDDEN_PARTS``); free-text values are never scanned;
3. ``b._reject_hostile_control`` (keys and values);
4. ``b._validate_json_schema_subset`` against the generated schema;
5. semantic and cross-field checks, listed in ``SCHEMA_RUNTIME_GAPS``.

Digest preimages (pinned by tests):

* record digest (projection, surface state, render result, interaction request,
  interaction decision, lifecycle waiver): ``"sha256:" + sha256(
  b.canonical_json_bytes(record with its digest field set to None))``;
* ``auth_context_digest``: ``_sha_json({"schema": AUTH_CONTEXT_SCHEMA,
  subject_ref, uid, persona_ref, role_refs: list, instance_ref, trust_zone,
  registry_revision})``;
* ``adapter_receipt_digest``: ``_sha_json(canonical adapter receipt / interaction
  envelope mapping)``;
* ``presence_consent_digest``: ``_sha_json({"schema": CONSENT_SCHEMA, stage,
  purpose, subject_scope, consent_ref, consented_at, expires_at, surface_id,
  auth_context_digest})``;
* pc receipt digest (recomputed, pc has no public receipt parser):
  ``sha256(json.dumps({checkpoint_id, persona_id, view_sha256, payload_sha256,
  path, created_at}, sort_keys=True, separators=(",", ":")) + "\\n")`` (bare hex).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from tools import capability_navigator_contract as cn  # noqa: E402
from tools import org060_sensory_actuation_contract as b  # noqa: E402
from tools import persona_continuity as pc  # noqa: E402

SCHEMA_VERSION = "org060-presence/v1"
AUTH_CONTEXT_SCHEMA = "org060-presence-auth-context/v1"
CONSENT_SCHEMA = "org060-presence-consent/v1"
WIRE_LIMIT = b.WIRE_LIMIT
ENVELOPE_LIMIT = b.WIRE_LIMIT + 1024
SUMMARY_LIMIT = b.SUMMARY_LIMIT
CONTENT_LIMIT = 1024
ID_LIMIT = 128
REVISION_MAX = 2**31 - 1

CONNECTIVITY = ("live", "degraded", "disconnected", "provisional")
AVAILABILITY = b.AVAILABILITY
OVERRIDE_AVAILABILITY = ("quiet", "off", "away")
PRIVACY_STATE = ("local_only", "redacted_egress")
RECORDING_STATE = ("not_recording", "recording", "recording_paused")
ACTION_STATE = ("idle", "intent_pending", "acting")
INTERACTION_MODE = ("control", "display_only", "text", "voice")
INPUT_MODE = ("control", "text", "voice")
CONTROL_INPUT = ("request_available", "request_away", "request_off", "request_quiet", "request_stop")
REDUCING_CONTROLS = ("request_away", "request_off", "request_quiet", "request_stop")
FALLBACK_CHANNELS = ("control", "text", "voice")
SURFACE_KIND = ("ar", "badge", "card", "desktop_pet", "display", "embodied", "wearable")
ADAPTER_CLASS = ("fixture",)
LIFECYCLE_LEGS = ("install", "configure", "health", "update", "remove")
LEG_OUTCOMES: Mapping[str, tuple[str, ...]] = {
    "install": ("verified",),
    "configure": ("verified",),
    "health": ("healthy", "degraded", "unhealthy", "unknown"),
    "update": ("verified", "rolled_back", "interrupted"),
    "remove": ("rehearsed", "executed"),
}
SURFACE_PHASE = ("present", "update_interrupted", "removed")
WAIVABLE_LEGS = ("configure", "install", "remove", "update")
INTENT_KIND = ("conversation_turn", "control_proposal")
INDICATORS = ("connectivity", "availability", "privacy", "recording", "action")
PRESENCE_CONSENT_STAGE = "presence_recording_interaction"
assert PRESENCE_CONSENT_STAGE in b.CONSENT_STAGES

PRESENCE_ONLY_REFUSAL_CODES = frozenset(
    {
        "continuity-evidence-required",
        "continuity-binding-mismatch",
        "split-projection",
        "projection-replay",
        "projection-revision-mismatch",
        "stale-live-projection",
        "forged-live-state",
        "surface-mismatch",
        "surface-removed",
        "stale-surface-state",
        "adapter-receipt-mismatch",
        "adapter-not-fixture",
        "indicator-hidden",
        "indicator-contradiction",
        "interaction-mode-unavailable",
        "consent-binding-mismatch",
        "direct-actuation-forbidden",
        "lifecycle-leg-missing",
        "lifecycle-transition-invalid",
        "lifecycle-waiver-invalid",
        "health-stale",
        "health-unknown",
        "health-unhealthy",
        "readiness-required",
        "fallback-incomplete",
        "recovery-source-invalid",
        "stale-interaction",
        "future-timestamp",
        "recovery-state-drift",
        "surface-disconnected",
        "malformed-input",
    }
)
assert PRESENCE_ONLY_REFUSAL_CODES.isdisjoint(b.REFUSAL_CODES)
PRESENCE_REFUSAL_CODES = b.REFUSAL_CODES | PRESENCE_ONLY_REFUSAL_CODES

PRESENCE_FORBIDDEN_PARTS = tuple(b.FORBIDDEN_PUBLIC_PARTS) + (
    "actor",
    "auth",
    "identity",
    "memory",
    "relationship",
    "subject",
    "token",
    "secret",
    "session",
    "actuat",
    "effect",
    "checkpoint",
    "continuity",
    "organism",
)

# Schema-accepted records the runtime refuses. Every entry is a semantic or
# cross-field rule, or a keyword the shared B subset validator cannot evaluate.
SCHEMA_RUNTIME_GAPS: Mapping[str, str] = {
    "digest-binding": "every *_digest field equals its documented preimage digest",
    "calendar-validity": "Z timestamps are real calendar instants (the pattern admits month 13)",
    "time-ordering": "projection issued_at >= observed_at; waiver reviewed_at < expires_at",
    "canonical-array-order": "interaction_modes and waived_legs are sorted ascending",
    "validator-capability-min-unique-items": "VALIDATOR-CAPABILITY gap (the one non-semantic exception): minItems/uniqueItems are declared in the schema, but the shared B subset validator cannot evaluate them, so the runtime does",
    "lifecycle-cross-field": "phase removed <=> remove leg executed; phase update_interrupted <=> update leg interrupted (unless removed)",
    "revision-chain": "state_revision == 1 <=> prior_state_digest is null",
    "projection-identity": "projection_id == 'presence-projection:' + continuity.persona_id",
    "observed-revision-binding": "observed_projection_revision == projection_revision in render results and decisions",
}

PROJECTION_ID_PATTERN = "^presence-projection:" + pc.ID_RE.pattern[1:]


def _control_free_pattern() -> str:
    """Schema pattern excluding exactly b.CONTROL_CODEPOINTS (keeps schema == runtime)."""
    ranges: list[tuple[int, int]] = []
    for point in sorted(b.CONTROL_CODEPOINTS):
        if ranges and point == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], point)
        else:
            ranges.append((point, point))
    body = "".join(f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in ranges)
    return f"^[^{body}]*$"


FREE_TEXT_PATTERN = _control_free_pattern()


# --------------------------------------------------------------------------- refusals


@dataclass(frozen=True)
class PresenceRefusal(b.TypedRefusal):
    """Closed presence refusal: B's vocabulary plus PRESENCE_ONLY_REFUSAL_CODES."""

    def __post_init__(self) -> None:
        if self.reason not in PRESENCE_REFUSAL_CODES:
            raise ValueError(f"unknown presence refusal reason: {self.reason}")


def _refuse(reason: str, detail: str) -> PresenceRefusal:
    return PresenceRefusal(reason, detail)


def _lift(refusal: b.TypedRefusal) -> PresenceRefusal:
    return _refuse(refusal.reason, refusal.detail)


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class PresenceProjection:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["projection_digest"])

    @property
    def revision(self) -> int:
        return int(self.data["projection_revision"])  # type: ignore[call-overload]


@dataclass(frozen=True)
class PresenceSurfaceState:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["state_digest"])

    @property
    def surface_id(self) -> str:
        return str(self.data["surface_id"])


@dataclass(frozen=True)
class PresenceRenderResult:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["render_digest"])


@dataclass(frozen=True)
class PresenceInteractionRequest:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["request_digest"])


@dataclass(frozen=True)
class PresenceInteractionDecision:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["decision_digest"])

    @property
    def intent(self) -> Mapping[str, object]:
        return self.data["intent"]  # type: ignore[return-value]


@dataclass(frozen=True)
class PresenceCompanionState:
    """Server-owned companion state a projection is built from (never public)."""

    availability: str
    privacy: str
    recording: str
    action: str
    fallback_channels: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True)
class PresenceReadiness:
    """Internal readiness evidence bound to one exact surface state digest."""

    surface_id: str
    surface_state_digest: str
    state_revision: int
    evaluated_at: str
    waived_legs: tuple[str, ...]
    health_observed_at: str


@dataclass(frozen=True)
class PresenceLifecycleEvent:
    leg: str
    evidence_ref: str
    evidence_digest: str
    observed_at: str
    outcome: str
    to_revision: str | None


class BridgePresenceAdapter(Protocol):
    """Fixture-only seam for the future Bridge owner (no live Bridge dependency)."""

    fixture_only: Literal[True]

    def render(
        self,
        projection_json: Mapping[str, object],
        *,
        surface_id: str,
        indicators: Mapping[str, str],
    ) -> Mapping[str, object]: ...

    def read_interaction(self, *, surface_id: str) -> Mapping[str, object]: ...


class FixtureBridgePresenceAdapter:
    """Deterministic, non-production adapter. No I/O, network, credential or service."""

    fixture_only: Literal[True] = True

    def __init__(
        self,
        *,
        clock: str,
        receipt_overrides: Mapping[str, object] | None = None,
        displayed_overrides: Mapping[str, object] | None = None,
        drop_displayed: Sequence[str] = (),
        interactions: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.clock = clock
        self.receipt_overrides = dict(receipt_overrides or {})
        self.displayed_overrides = dict(displayed_overrides or {})
        self.drop_displayed = tuple(drop_displayed)
        self.interactions = [copy.deepcopy(dict(item)) for item in interactions]
        self.rendered: list[Mapping[str, object]] = []

    def render(
        self,
        projection_json: Mapping[str, object],
        *,
        surface_id: str,
        indicators: Mapping[str, str],
    ) -> Mapping[str, object]:
        self.rendered.append(copy.deepcopy(dict(projection_json)))
        displayed: dict[str, object] = dict(indicators)
        displayed.update(self.displayed_overrides)
        for key in self.drop_displayed:
            displayed.pop(key, None)
        receipt: dict[str, object] = {
            "surface_id": surface_id,
            "projection_id": projection_json.get("projection_id"),
            "projection_revision": projection_json.get("projection_revision"),
            "projection_digest": projection_json.get("projection_digest"),
            "displayed": displayed,
            "rendered_at": self.clock,
        }
        receipt.update(self.receipt_overrides)
        return receipt

    def read_interaction(self, *, surface_id: str) -> Mapping[str, object]:
        if not self.interactions:
            return {}
        return self.interactions.pop(0)


# --------------------------------------------------------------------------- helpers


def _sha_json(value: Mapping[str, object] | Sequence[object]) -> str:
    return "sha256:" + hashlib.sha256(b.canonical_json_bytes(value)).hexdigest()


def _digest_record(data: Mapping[str, object], digest_field: str) -> str:
    preimage = dict(data)
    preimage[digest_field] = None
    return _sha_json(preimage)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_z(value: object) -> datetime | None:
    if not isinstance(value, str) or b.Z_RE.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_fresh(value: object, now: datetime, freshness: b.FreshnessWindow) -> bool:
    seen = _parse_z(value)
    if seen is None:
        return False
    return 0 <= (now - seen).total_seconds() <= freshness.max_age_seconds


def _check_now(now: object) -> PresenceRefusal | None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        return _refuse("wrong-type", "now must be an aware datetime")
    return None


def _check_freshness(freshness: object, field: str) -> PresenceRefusal | None:
    if not isinstance(freshness, b.FreshnessWindow):
        return _refuse("wrong-type", f"{field} must be FreshnessWindow")
    age = freshness.max_age_seconds
    if not isinstance(age, int) or isinstance(age, bool) or age < 0:
        return _refuse("wrong-type", f"{field}.max_age_seconds")
    return None


def _reject_key_lookalikes(value: object, *, path: str = "$") -> PresenceRefusal | None:
    """Recursive scan of mapping KEYS only; string values are free text."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            lower = str(key).lower()
            if any(part in lower for part in PRESENCE_FORBIDDEN_PARTS):
                return _refuse("identity-authority-lookalike", f"{path}.{key}")
            refused = _reject_key_lookalikes(child, path=f"{path}.{key}")
            if refused is not None:
                return refused
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for idx, child in enumerate(value):
            refused = _reject_key_lookalikes(child, path=f"{path}[{idx}]")
            if refused is not None:
                return refused
    return None


def _load(raw: object, *, limit: int, public: bool) -> dict[str, object] | PresenceRefusal:
    """Untrusted-input boundary: never raises (lone surrogates, raising mappings)."""
    try:
        if not isinstance(raw, (bytes, str, Mapping)):
            return _refuse("wrong-type", "expected bytes, str or mapping")
        if isinstance(raw, str):
            try:
                raw.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return _refuse("malformed-input", "str input is not encodable UTF-8 (lone surrogate)")
        loaded = b.load_canonical_json_mapping(raw, limit=limit)
        if isinstance(loaded, b.TypedRefusal):
            return _lift(loaded)
        if public:
            lookalike = _reject_key_lookalikes(loaded)
            if lookalike is not None:
                return lookalike
        hostile = b._reject_hostile_control(loaded)
        if isinstance(hostile, b.TypedRefusal):
            return _lift(hostile)
        return copy.deepcopy(dict(loaded))
    except Exception as exc:  # untrusted boundary: any hostile object becomes a refusal
        return _refuse("malformed-input", exc.__class__.__name__)


@functools.lru_cache(maxsize=None)
def _schema(name: str) -> Mapping[str, object]:
    return json.loads(_GENERATORS[name]().decode("utf-8"))


def _validate(value: Mapping[str, object], name: str) -> PresenceRefusal | None:
    result = b._validate_json_schema_subset(value, _schema(name))
    if isinstance(result, b.TypedRefusal):
        return _lift(result)
    return None


def _check_digest(data: Mapping[str, object], field: str) -> PresenceRefusal | None:
    if data.get(field) != _digest_record(data, field):
        return _refuse("tampered-source-binding", f"{field} mismatch")
    return None


def _check_times(fields: Sequence[tuple[str, object]]) -> PresenceRefusal | None:
    for name, value in fields:
        if value is not None and _parse_z(value) is None:
            return _refuse("invalid-pattern", f"{name} is not a calendar instant")
    return None


def _sorted_unique(values: object) -> bool:
    if not isinstance(values, list) or not values:
        return False
    return values == sorted(set(values))


# --------------------------------------------------------------------------- parsers


def parse_presence_projection(raw: bytes | str | Mapping[str, object]) -> PresenceProjection | PresenceRefusal:
    data = _load(raw, limit=WIRE_LIMIT, public=False)
    if isinstance(data, PresenceRefusal):
        return data
    state = data.get("state")
    if isinstance(state, Mapping) and "fallback_channels" in state:
        if state["fallback_channels"] != list(FALLBACK_CHANNELS):
            return _refuse("fallback-incomplete", "projection must declare text/voice/control fallback")
    refused = _validate(data, "projection")
    if refused is not None:
        return refused
    continuity = data["continuity"]
    assert isinstance(continuity, Mapping)
    if data["projection_id"] != f"presence-projection:{continuity['persona_id']}":
        return _refuse("continuity-binding-mismatch", "projection_id does not name the continuity persona")
    refused = _check_times((("observed_at", data["observed_at"]), ("issued_at", data["issued_at"])))
    if refused is not None:
        return refused
    if str(data["issued_at"]) < str(data["observed_at"]):
        return _refuse("invalid-pattern", "issued_at precedes observed_at")
    refused = _check_digest(data, "projection_digest")
    if refused is not None:
        return refused
    return PresenceProjection(data)


def parse_presence_surface_state(raw: bytes | str | Mapping[str, object]) -> PresenceSurfaceState | PresenceRefusal:
    data = _load(raw, limit=WIRE_LIMIT, public=False)
    if isinstance(data, PresenceRefusal):
        return data
    refused = _validate(data, "surface-state")
    if refused is not None:
        return refused
    if not _sorted_unique(data["interaction_modes"]):
        return _refuse("invalid-enum", "interaction_modes must be sorted, unique and non-empty")
    lifecycle = data["lifecycle"]
    assert isinstance(lifecycle, Mapping)
    times: list[tuple[str, object]] = [("connectivity_observed_at", data["connectivity_observed_at"])]
    for leg in LIFECYCLE_LEGS:
        evidence = lifecycle[leg]
        if isinstance(evidence, Mapping):
            times.append((f"lifecycle.{leg}.observed_at", evidence["observed_at"]))
    waiver = lifecycle["waiver"]
    if isinstance(waiver, Mapping):
        times.extend((("waiver.reviewed_at", waiver["reviewed_at"]), ("waiver.expires_at", waiver["expires_at"])))
    refused = _check_times(times)
    if refused is not None:
        return refused
    remove = lifecycle["remove"]
    update = lifecycle["update"]
    removed_executed = isinstance(remove, Mapping) and remove["outcome"] == "executed"
    update_interrupted = isinstance(update, Mapping) and update["outcome"] == "interrupted"
    if (data["phase"] == "removed") != removed_executed:
        return _refuse("lifecycle-transition-invalid", "phase removed requires an executed remove leg")
    if (data["phase"] == "update_interrupted") != (update_interrupted and data["phase"] != "removed"):
        return _refuse("lifecycle-transition-invalid", "phase update_interrupted requires an interrupted update leg")
    if (data["state_revision"] == 1) != (data["prior_state_digest"] is None):
        return _refuse("tampered-source-binding", "revision chain broken")
    if isinstance(waiver, Mapping):
        if not _sorted_unique(waiver["waived_legs"]):
            return _refuse("lifecycle-waiver-invalid", "waived_legs must be sorted, unique and non-empty")
        if str(waiver["reviewed_at"]) >= str(waiver["expires_at"]):
            return _refuse("lifecycle-waiver-invalid", "waiver expires before review")
        if waiver["waiver_digest"] != _digest_record(waiver, "waiver_digest"):
            return _refuse("lifecycle-waiver-invalid", "waiver digest mismatch")
    refused = _check_digest(data, "state_digest")
    if refused is not None:
        return refused
    return PresenceSurfaceState(data)


def parse_presence_render_result(raw: bytes | str | Mapping[str, object]) -> PresenceRenderResult | PresenceRefusal:
    data = _load(raw, limit=WIRE_LIMIT, public=False)
    if isinstance(data, PresenceRefusal):
        return data
    refused = _validate(data, "render-result")
    if refused is not None:
        return refused
    if data["observed_projection_revision"] != data["projection_revision"]:
        return _refuse("projection-revision-mismatch", "observed revision differs from bound revision")
    refused = _check_times((("rendered_at", data["rendered_at"]),))
    if refused is not None:
        return refused
    refused = _check_digest(data, "render_digest")
    if refused is not None:
        return refused
    return PresenceRenderResult(data)


def parse_presence_interaction_request(
    raw: bytes | str | Mapping[str, object],
) -> PresenceInteractionRequest | PresenceRefusal:
    data = _load(raw, limit=WIRE_LIMIT, public=True)
    if isinstance(data, PresenceRefusal):
        return data
    bounded = data.get("input")
    if isinstance(bounded, Mapping) and bounded.get("mode") == "control":
        if bounded.get("content") not in CONTROL_INPUT:
            return _refuse("direct-actuation-forbidden", "control input is a closed presence control, never an effect")
    refused = _validate(data, "interaction-request")
    if refused is not None:
        return refused
    refused = _check_digest(data, "request_digest")
    if refused is not None:
        return refused
    return PresenceInteractionRequest(data)


def parse_presence_interaction_decision(
    raw: bytes | str | Mapping[str, object],
) -> PresenceInteractionDecision | PresenceRefusal:
    data = _load(raw, limit=WIRE_LIMIT, public=False)
    if isinstance(data, PresenceRefusal):
        return data
    refused = _validate(data, "interaction-decision")
    if refused is not None:
        return refused
    if data["observed_projection_revision"] != data["projection_revision"]:
        return _refuse("projection-revision-mismatch", "observed revision differs from bound revision")
    refused = _check_times((("decided_at", data["decided_at"]),))
    if refused is not None:
        return refused
    refused = _check_digest(data, "decision_digest")
    if refused is not None:
        return refused
    return PresenceInteractionDecision(data)


def _record_json(record: object, cls: type) -> dict[str, object] | PresenceRefusal:
    if type(record) is not cls:
        return _refuse("wrong-type", f"{cls.__name__} required")
    try:
        return copy.deepcopy(dict(record.data))  # type: ignore[attr-defined]
    except Exception as exc:
        return _refuse("malformed-input", exc.__class__.__name__)


def presence_projection_to_json(record: PresenceProjection) -> dict[str, object] | PresenceRefusal:
    return _record_json(record, PresenceProjection)


def presence_surface_state_to_json(record: PresenceSurfaceState) -> dict[str, object] | PresenceRefusal:
    return _record_json(record, PresenceSurfaceState)


def presence_render_result_to_json(record: PresenceRenderResult) -> dict[str, object] | PresenceRefusal:
    return _record_json(record, PresenceRenderResult)


def presence_interaction_request_to_json(record: PresenceInteractionRequest) -> dict[str, object] | PresenceRefusal:
    return _record_json(record, PresenceInteractionRequest)


def presence_interaction_decision_to_json(record: PresenceInteractionDecision) -> dict[str, object] | PresenceRefusal:
    return _record_json(record, PresenceInteractionDecision)


def _reparse_projection(value: object) -> PresenceProjection | PresenceRefusal:
    if type(value) is not PresenceProjection:
        return _refuse("wrong-type", "PresenceProjection required")
    return parse_presence_projection(value.data)


def _reparse_surface(value: object) -> PresenceSurfaceState | PresenceRefusal:
    if type(value) is not PresenceSurfaceState:
        return _refuse("wrong-type", "server-owned PresenceSurfaceState required")
    return parse_presence_surface_state(value.data)


# --------------------------------------------------------------------------- server-owned bindings


def _check_context(context: object) -> PresenceRefusal | None:
    if type(context) is not cn.AuthenticatedContext:
        return _refuse("authenticated-context-required", "real server-owned AuthenticatedContext required")
    for name in ("subject_ref", "persona_ref", "instance_ref", "trust_zone", "registry_revision"):
        if not isinstance(getattr(context, name), str) or not getattr(context, name):
            return _refuse("authenticated-context-required", name)
    if not isinstance(context.uid, int) or isinstance(context.uid, bool):
        return _refuse("authenticated-context-required", "uid")
    if not isinstance(context.role_refs, tuple) or not all(isinstance(item, str) for item in context.role_refs):
        return _refuse("authenticated-context-required", "role_refs")
    return None


def presence_auth_context_digest(context: cn.AuthenticatedContext) -> str | PresenceRefusal:
    refused = _check_context(context)
    if refused is not None:
        return refused
    try:
        return _auth_digest(context)
    except Exception as exc:  # hostile caller values never escape
        return _refuse("authenticated-context-required", exc.__class__.__name__)


def _auth_digest(context: cn.AuthenticatedContext) -> str:
    return _sha_json(
        {
            "schema": AUTH_CONTEXT_SCHEMA,
            "subject_ref": context.subject_ref,
            "uid": context.uid,
            "persona_ref": context.persona_ref,
            "role_refs": list(context.role_refs),
            "instance_ref": context.instance_ref,
            "trust_zone": context.trust_zone,
            "registry_revision": context.registry_revision,
        }
    )


def presence_consent_digest(
    *,
    purpose: str,
    subject_scope: str,
    consent_ref: str,
    consented_at: str,
    expires_at: str,
    surface_id: str,
    auth_context_digest: str,
) -> str | PresenceRefusal:
    try:
        return _consent_digest(
            purpose=purpose,
            subject_scope=subject_scope,
            consent_ref=consent_ref,
            consented_at=consented_at,
            expires_at=expires_at,
            surface_id=surface_id,
            auth_context_digest=auth_context_digest,
        )
    except Exception as exc:  # hostile caller values never escape
        return _refuse("consent-binding-mismatch", exc.__class__.__name__)


def _consent_digest(
    *,
    purpose: str,
    subject_scope: str,
    consent_ref: str,
    consented_at: str,
    expires_at: str,
    surface_id: str,
    auth_context_digest: str,
) -> str:
    return _sha_json(
        {
            "schema": CONSENT_SCHEMA,
            "stage": PRESENCE_CONSENT_STAGE,
            "purpose": purpose,
            "subject_scope": subject_scope,
            "consent_ref": consent_ref,
            "consented_at": consented_at,
            "expires_at": expires_at,
            "surface_id": surface_id,
            "auth_context_digest": auth_context_digest,
        }
    )


def checkpoint_receipt_digest(receipt: pc.CheckpointReceipt) -> str | PresenceRefusal:
    if type(receipt) is not pc.CheckpointReceipt:
        return _refuse("continuity-evidence-required", "real pc.CheckpointReceipt required")
    try:
        return _receipt_digest(receipt)
    except Exception as exc:
        return _refuse("continuity-evidence-required", exc.__class__.__name__)


def _receipt_digest(receipt: pc.CheckpointReceipt) -> str:
    preimage = {
        "checkpoint_id": receipt.checkpoint_id,
        "persona_id": receipt.persona_id,
        "view_sha256": receipt.view_sha256,
        "payload_sha256": receipt.payload_sha256,
        "path": receipt.path,
        "created_at": receipt.created_at,
    }
    return hashlib.sha256((json.dumps(preimage, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()


def _bind_continuity(
    view_path: object, receipt: object, checkpoint_root: object
) -> dict[str, object] | PresenceRefusal:
    if not isinstance(view_path, Path) or not isinstance(checkpoint_root, Path):
        return _refuse("continuity-evidence-required", "view_path and checkpoint_root must be Paths")
    if type(receipt) is not pc.CheckpointReceipt:
        return _refuse("continuity-evidence-required", "real pc.CheckpointReceipt required")
    for name in ("checkpoint_id", "persona_id", "view_sha256", "payload_sha256", "path", "created_at", "receipt_sha256"):
        if not isinstance(getattr(receipt, name), str):
            return _refuse("continuity-evidence-required", f"receipt.{name}")
    try:
        view = pc.load_resolved_view(view_path)
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=checkpoint_root)
    except pc.ContinuityError as exc:
        return _refuse("continuity-binding-mismatch", exc.code)
    except Exception as exc:  # pc loaders over caller-supplied paths
        return _refuse("continuity-binding-mismatch", exc.__class__.__name__)
    if not (view.persona_id == envelope.persona_id == receipt.persona_id):
        return _refuse("continuity-binding-mismatch", "persona_id differs across view/checkpoint/receipt")
    if view.organism_id != envelope.organism_id:
        return _refuse("continuity-binding-mismatch", "organism_id differs across view/checkpoint")
    if not (envelope.view_sha256 == view.view_sha256 == receipt.view_sha256):
        return _refuse("continuity-binding-mismatch", "view_sha256 differs across view/checkpoint/receipt")
    if receipt.checkpoint_id != envelope.checkpoint_id or receipt.payload_sha256 != envelope.payload_sha256:
        return _refuse("continuity-binding-mismatch", "receipt does not bind the exact checkpoint payload")
    if receipt.created_at != envelope.boundary.created_at:
        return _refuse("continuity-binding-mismatch", "receipt created_at differs from checkpoint boundary")
    if checkpoint_receipt_digest(receipt) != receipt.receipt_sha256:
        return _refuse("continuity-binding-mismatch", "receipt digest mismatch")
    return {
        "persona_id": envelope.persona_id,
        "organism_id": envelope.organism_id,
        "view_sha256": envelope.view_sha256,
        "checkpoint_id": envelope.checkpoint_id,
        "checkpoint_payload_sha256": envelope.payload_sha256,
        "checkpoint_receipt_sha256": receipt.receipt_sha256,
        "prior_checkpoint_sha256": envelope.prior_checkpoint_sha256,
    }


def _bind_navigator(navigator: object, context: object) -> dict[str, object] | PresenceRefusal:
    if type(navigator) is not cn.NavigatorProjection:
        return _refuse("projection-binding-mismatch", "real cn.NavigatorProjection required")
    try:
        reparsed = cn.parse_projection(cn.projection_to_json(navigator))
    except Exception as exc:  # cn strict parser over a caller-supplied object
        return _refuse("projection-binding-mismatch", exc.__class__.__name__)
    if reparsed != navigator:
        return _refuse("projection-binding-mismatch", "navigator projection does not round-trip")
    auth_digest = presence_auth_context_digest(context)  # type: ignore[arg-type]
    if isinstance(auth_digest, PresenceRefusal):
        return auth_digest
    return {
        "assembly_id": navigator.assembly_id,
        "projection_sha256": navigator.projection_sha256,
        "catalogue_sha256": navigator.catalogue_sha256,
        "lock_sha256": navigator.lock_sha256,
        "auth_context_digest": auth_digest,
    }


def _companion_state(companion: object, now: datetime) -> dict[str, object] | PresenceRefusal:
    if type(companion) is not PresenceCompanionState:
        return _refuse("wrong-type", "server-owned PresenceCompanionState required")
    if type(companion.fallback_channels) is not tuple or companion.fallback_channels != FALLBACK_CHANNELS:
        return _refuse("fallback-incomplete", "server state must declare text/voice/control fallback")
    for value, allowed, name in (
        (companion.availability, AVAILABILITY, "availability"),
        (companion.privacy, PRIVACY_STATE, "privacy"),
        (companion.recording, RECORDING_STATE, "recording"),
        (companion.action, ACTION_STATE, "action"),
    ):
        if value not in allowed:
            return _refuse("invalid-enum", name)
    observed = _parse_z(companion.observed_at)
    if observed is None:
        return _refuse("invalid-pattern", "companion.observed_at")
    if observed > now:
        return _refuse("future-timestamp", "companion.observed_at")
    return {
        "availability": companion.availability,
        "privacy": companion.privacy,
        "recording": companion.recording,
        "action": companion.action,
        "fallback_channels": list(FALLBACK_CHANNELS),
    }


# --------------------------------------------------------------------------- API 1: build


def build_presence_projection(
    *,
    view_path: Path,
    checkpoint_receipt: pc.CheckpointReceipt,
    checkpoint_root: Path,
    navigator: cn.NavigatorProjection,
    context: cn.AuthenticatedContext,
    companion: PresenceCompanionState,
    prior: PresenceProjection | None,
    now: datetime,
) -> PresenceProjection | PresenceRefusal:
    refused = _check_now(now)
    if refused is not None:
        return refused
    continuity = _bind_continuity(view_path, checkpoint_receipt, checkpoint_root)
    if isinstance(continuity, PresenceRefusal):
        return continuity
    navigator_binding = _bind_navigator(navigator, context)
    if isinstance(navigator_binding, PresenceRefusal):
        return navigator_binding
    state = _companion_state(companion, now)
    if isinstance(state, PresenceRefusal):
        return state
    revision = 1
    if prior is not None:
        prior_projection = _reparse_projection(prior)
        if isinstance(prior_projection, PresenceRefusal):
            return prior_projection
        prior_continuity = prior_projection.data["continuity"]
        assert isinstance(prior_continuity, Mapping)
        if (
            prior_continuity["persona_id"] != continuity["persona_id"]
            or prior_continuity["organism_id"] != continuity["organism_id"]
        ):
            return _refuse("continuity-binding-mismatch", "prior projection names another persona/organism")
        if (
            prior_continuity["checkpoint_payload_sha256"] != continuity["checkpoint_payload_sha256"]
            and continuity["prior_checkpoint_sha256"] != prior_continuity["checkpoint_payload_sha256"]
        ):
            return _refuse("split-projection", "checkpoint does not continue the prior projection's checkpoint")
        revision = prior_projection.revision + 1
        if revision > REVISION_MAX:
            return _refuse("byte-bound-exceeded", "projection_revision")
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "projection_id": f"presence-projection:{continuity['persona_id']}",
        "projection_revision": revision,
        "continuity": continuity,
        "navigator": navigator_binding,
        "state": state,
        "observed_at": companion.observed_at,
        "issued_at": _z(now),
        "projection_digest": None,
    }
    data["projection_digest"] = _digest_record(data, "projection_digest")
    return parse_presence_projection(data)


# --------------------------------------------------------------------------- surface state (server-owned)


def make_presence_surface_state(
    *,
    surface_id: str,
    surface_kind: str,
    connectivity: str,
    connectivity_observed_at: str,
    interaction_modes: Sequence[str],
    adapter_id: str,
    adapter_revision: str,
    exact_revision: str,
    rollback_ref: str,
    now: datetime,
) -> PresenceSurfaceState | PresenceRefusal:
    refused = _check_now(now)
    if refused is not None:
        return refused
    observed = _parse_z(connectivity_observed_at)
    if observed is not None and observed > now:
        return _refuse("future-timestamp", "connectivity_observed_at")
    if isinstance(interaction_modes, (str, bytes)) or not isinstance(interaction_modes, Sequence):
        return _refuse("wrong-type", "interaction_modes")
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "surface_id": surface_id,
        "surface_kind": surface_kind,
        "state_revision": 1,
        "phase": "present",
        "connectivity": connectivity,
        "connectivity_observed_at": connectivity_observed_at,
        "interaction_modes": list(interaction_modes),
        "adapter": {"adapter_id": adapter_id, "adapter_class": "fixture", "adapter_revision": adapter_revision},
        "lifecycle": {
            "install": None,
            "configure": None,
            "health": None,
            "update": None,
            "remove": None,
            "exact_revision": exact_revision,
            "rollback_ref": rollback_ref,
            "provisioning_anchor": None,
            "waiver": None,
        },
        "prior_state_digest": None,
        "state_digest": None,
    }
    try:
        # Registration anchor: digest of the registration record (anchor and digest unset).
        data["lifecycle"]["provisioning_anchor"] = _sha_json(data)  # type: ignore[index]
        data["state_digest"] = _digest_record(data, "state_digest")
    except Exception:  # hostile caller values never escape
        return _refuse("wrong-type", "surface state fields must be JSON values")
    return parse_presence_surface_state(data)


def _advance_surface(
    surface: object, expected_state_digest: object, now: object
) -> tuple[PresenceSurfaceState, dict[str, object]] | PresenceRefusal:
    refused = _check_now(now)
    if refused is not None:
        return refused
    current = _reparse_surface(surface)
    if isinstance(current, PresenceRefusal):
        return current
    if expected_state_digest != current.digest:
        return _refuse("stale-surface-state", "expected_state_digest is not the current surface state")
    return current, copy.deepcopy(dict(current.data))


def _commit_surface(current: PresenceSurfaceState, data: dict[str, object]) -> PresenceSurfaceState | PresenceRefusal:
    revision = int(current.data["state_revision"]) + 1  # type: ignore[call-overload]
    if revision > REVISION_MAX:
        return _refuse("byte-bound-exceeded", "state_revision")
    data["state_revision"] = revision
    data["prior_state_digest"] = current.digest
    data["state_digest"] = None
    try:
        data["state_digest"] = _digest_record(data, "state_digest")
    except Exception:  # hostile caller values never escape
        return _refuse("wrong-type", "surface state fields must be JSON values")
    return parse_presence_surface_state(data)


def observe_presence_connectivity(
    surface: PresenceSurfaceState,
    *,
    connectivity: str,
    observed_at: str,
    expected_state_digest: str,
    now: datetime,
) -> PresenceSurfaceState | PresenceRefusal:
    advanced = _advance_surface(surface, expected_state_digest, now)
    if isinstance(advanced, PresenceRefusal):
        return advanced
    current, data = advanced
    if data["phase"] == "removed":
        return _refuse("surface-removed", current.surface_id)
    seen = _parse_z(observed_at)
    if seen is None:
        return _refuse("invalid-pattern", "observed_at")
    if seen > now:
        return _refuse("future-timestamp", "observed_at")
    data["connectivity"] = connectivity
    data["connectivity_observed_at"] = observed_at
    return _commit_surface(current, data)


# --------------------------------------------------------------------------- API 5: lifecycle


def record_presence_lifecycle(
    surface: PresenceSurfaceState,
    event: PresenceLifecycleEvent,
    *,
    expected_state_digest: str,
    now: datetime,
) -> PresenceSurfaceState | PresenceRefusal:
    """Apply one ORG-068 lifecycle leg. Transition table (anything else refuses):

    install(verified)      present with no install leg -> present, sets install (first install)
    install(verified)      removed -> present, every other leg and the waiver cleared (reprovision)
    configure(verified)    present -> present, sets configure
    health(*)              present -> present, sets health
    update(verified)       present|update_interrupted -> present, exact_revision := to_revision, health cleared
    update(rolled_back)    present|update_interrupted -> present, revision kept, health cleared
    update(interrupted)    present -> update_interrupted, health cleared
    remove(rehearsed)      present -> present, sets remove (removal path proven)
    remove(executed)       present|update_interrupted -> removed
    """
    advanced = _advance_surface(surface, expected_state_digest, now)
    if isinstance(advanced, PresenceRefusal):
        return advanced
    current, data = advanced
    if type(event) is not PresenceLifecycleEvent:
        return _refuse("wrong-type", "PresenceLifecycleEvent required")
    if event.leg not in LIFECYCLE_LEGS:
        return _refuse("invalid-enum", "leg")
    if event.outcome not in LEG_OUTCOMES[event.leg]:
        return _refuse("invalid-enum", f"{event.leg}.outcome")
    if not isinstance(event.evidence_ref, str) or b.ID_RE.fullmatch(event.evidence_ref) is None:
        return _refuse("invalid-pattern", "evidence_ref")
    if not isinstance(event.evidence_digest, str) or b.SHA_RE.fullmatch(event.evidence_digest) is None:
        return _refuse("invalid-pattern", "evidence_digest")
    seen = _parse_z(event.observed_at)
    if seen is None:
        return _refuse("invalid-pattern", "observed_at")
    if seen > now:
        return _refuse("future-timestamp", "observed_at")
    needs_revision = event.leg == "update" and event.outcome == "verified"
    if needs_revision:
        if not isinstance(event.to_revision, str) or b.ID_RE.fullmatch(event.to_revision) is None:
            return _refuse("lifecycle-transition-invalid", "update(verified) requires to_revision")
    elif event.to_revision is not None:
        return _refuse("lifecycle-transition-invalid", "to_revision only applies to update(verified)")
    evidence = {
        "evidence_ref": event.evidence_ref,
        "evidence_digest": event.evidence_digest,
        "observed_at": event.observed_at,
        "outcome": event.outcome,
    }
    phase = data["phase"]
    lifecycle = data["lifecycle"]
    assert isinstance(lifecycle, dict)
    leg = event.leg
    if phase == "removed":
        if leg != "install":
            return _refuse("lifecycle-transition-invalid", f"{leg} on a removed surface")
        for name in LIFECYCLE_LEGS:
            lifecycle[name] = None
        lifecycle["install"] = evidence
        lifecycle["waiver"] = None
        lifecycle["provisioning_anchor"] = current.digest
        data["phase"] = "present"
    elif leg == "install":
        if phase != "present" or lifecycle["install"] is not None:
            return _refuse("lifecycle-transition-invalid", "install requires a new or removed surface")
        lifecycle["install"] = evidence
        lifecycle["provisioning_anchor"] = current.digest
    elif leg in ("configure", "health"):
        if phase != "present":
            return _refuse("lifecycle-transition-invalid", f"{leg} during {phase}")
        lifecycle[leg] = evidence
    elif leg == "update":
        if event.outcome == "interrupted":
            if phase != "present":
                return _refuse("lifecycle-transition-invalid", "interrupted update during interrupted update")
            data["phase"] = "update_interrupted"
        else:
            data["phase"] = "present"
            if event.outcome == "verified":
                lifecycle["exact_revision"] = event.to_revision
        lifecycle["update"] = evidence
        lifecycle["health"] = None
    else:
        if event.outcome == "rehearsed":
            if phase != "present":
                return _refuse("lifecycle-transition-invalid", f"remove rehearsal during {phase}")
        else:
            data["phase"] = "removed"
        lifecycle["remove"] = evidence
    return _commit_surface(current, data)


def waive_presence_lifecycle_legs(
    surface: PresenceSurfaceState,
    *,
    waiver_id: str,
    waived_legs: Sequence[str],
    reason: str,
    review_ref: str,
    reviewed_at: str,
    expires_at: str,
    expected_state_digest: str,
    now: datetime,
) -> PresenceSurfaceState | PresenceRefusal:
    """Attach a typed waiver. It only satisfies readiness once its digest is in the
    server's reviewed set and it names exactly the missing legs (never health)."""
    advanced = _advance_surface(surface, expected_state_digest, now)
    if isinstance(advanced, PresenceRefusal):
        return advanced
    current, data = advanced
    if data["phase"] == "removed":
        return _refuse("surface-removed", current.surface_id)
    if isinstance(waived_legs, (str, bytes)) or not isinstance(waived_legs, Sequence):
        return _refuse("lifecycle-waiver-invalid", "waived_legs must be a sequence")
    legs = list(waived_legs)
    if not all(isinstance(leg, str) for leg in legs):
        return _refuse("lifecycle-waiver-invalid", "waived_legs items must be strings")
    if not legs or len(set(legs)) != len(legs) or not set(legs) <= set(WAIVABLE_LEGS):
        return _refuse("lifecycle-waiver-invalid", "waived_legs must be unique, non-empty and exclude health")
    reviewed = _parse_z(reviewed_at)
    if reviewed is None:
        return _refuse("invalid-pattern", "reviewed_at")
    if reviewed > now:
        return _refuse("future-timestamp", "reviewed_at")
    lifecycle = data["lifecycle"]
    assert isinstance(lifecycle, dict)
    adapter_binding = data["adapter"]
    assert isinstance(adapter_binding, Mapping)
    waiver: dict[str, object] = {
        "waiver_id": waiver_id,
        "surface_id": current.surface_id,
        "adapter_id": adapter_binding["adapter_id"],
        "provisioning_anchor": lifecycle["provisioning_anchor"],
        "waived_legs": sorted(legs),
        "reason": reason,
        "review_ref": review_ref,
        "reviewed_at": reviewed_at,
        "expires_at": expires_at,
        "waiver_digest": None,
    }
    try:
        waiver["waiver_digest"] = _digest_record(waiver, "waiver_digest")
    except Exception:  # hostile caller values never escape
        return _refuse("wrong-type", "waiver fields must be JSON values")
    lifecycle["waiver"] = waiver
    return _commit_surface(current, data)


# --------------------------------------------------------------------------- API 4: readiness


def evaluate_presence_readiness(
    surface: PresenceSurfaceState,
    *,
    reviewed_waiver_digests: frozenset[str],
    now: datetime,
    health_freshness: b.FreshnessWindow,
) -> PresenceReadiness | PresenceRefusal:
    refused = _check_now(now) or _check_freshness(health_freshness, "health_freshness")
    if refused is not None:
        return refused
    if not isinstance(reviewed_waiver_digests, (frozenset, set)) or not all(
        isinstance(item, str) for item in reviewed_waiver_digests
    ):
        return _refuse("wrong-type", "reviewed_waiver_digests must be a set of digests")
    current = _reparse_surface(surface)
    if isinstance(current, PresenceRefusal):
        return current
    data = current.data
    if data["phase"] == "removed":
        return _refuse("surface-removed", current.surface_id)
    if data["phase"] != "present":
        return _refuse("lifecycle-transition-invalid", f"surface phase {data['phase']}")
    lifecycle = data["lifecycle"]
    assert isinstance(lifecycle, Mapping)
    health = lifecycle["health"]
    if not isinstance(health, Mapping) or health["outcome"] == "unknown":
        return _refuse("health-unknown", "no current health evidence")
    if health["outcome"] != "healthy":
        return _refuse("health-unhealthy", str(health["outcome"]))
    if not _is_fresh(health["observed_at"], now, health_freshness):
        return _refuse("health-stale", "health evidence outside freshness window")
    missing = sorted(leg for leg in LIFECYCLE_LEGS if lifecycle[leg] is None)
    waived: tuple[str, ...] = ()
    if missing:
        waiver = lifecycle["waiver"]
        if not isinstance(waiver, Mapping):
            return _refuse("lifecycle-leg-missing", ",".join(missing))
        adapter_binding = data["adapter"]
        assert isinstance(adapter_binding, Mapping)
        if (
            waiver["surface_id"] != current.surface_id
            or waiver["adapter_id"] != adapter_binding["adapter_id"]
            or waiver["provisioning_anchor"] != lifecycle["provisioning_anchor"]
        ):
            return _refuse("lifecycle-waiver-invalid", "waiver is bound to another surface, adapter or provisioning")
        if list(waiver["waived_legs"]) != missing:  # type: ignore[call-overload]
            return _refuse("lifecycle-waiver-invalid", "waiver does not name exactly the missing legs")
        if waiver["waiver_digest"] not in reviewed_waiver_digests:
            return _refuse("lifecycle-waiver-invalid", "waiver digest is not in the reviewed set")
        reviewed_at = _parse_z(waiver["reviewed_at"])
        expires_at = _parse_z(waiver["expires_at"])
        if reviewed_at is None or expires_at is None or not (reviewed_at <= now < expires_at):
            return _refuse("lifecycle-waiver-invalid", "waiver outside its review window")
        waived = tuple(missing)
    return PresenceReadiness(
        surface_id=current.surface_id,
        surface_state_digest=current.digest,
        state_revision=int(data["state_revision"]),  # type: ignore[call-overload]
        evaluated_at=_z(now),
        waived_legs=waived,
        health_observed_at=str(health["observed_at"]),
    )


def _check_readiness(
    readiness: object, surface: PresenceSurfaceState, now: datetime, freshness: b.FreshnessWindow
) -> PresenceRefusal | None:
    if type(readiness) is not PresenceReadiness:
        return _refuse("readiness-required", "PresenceReadiness required")
    if (
        readiness.surface_id != surface.surface_id
        or readiness.surface_state_digest != surface.digest
        or readiness.state_revision != surface.data["state_revision"]
    ):
        return _refuse("readiness-required", "readiness is for another surface state")
    if not _is_fresh(readiness.evaluated_at, now, freshness):
        return _refuse("readiness-required", "readiness outside freshness window")
    return None


def _compare_projection_binding(
    revision: object, digest: object, projection_id: object, current: PresenceProjection
) -> PresenceRefusal | None:
    if not isinstance(revision, int) or isinstance(revision, bool):
        return _refuse("wrong-type", "projection_revision")
    if revision < current.revision:
        return _refuse("projection-replay", "older projection revision")
    if revision > current.revision:
        return _refuse("projection-revision-mismatch", "revision ahead of the server-owned projection")
    if digest != current.digest or projection_id != current.data["projection_id"]:
        return _refuse("split-projection", "second digest in play for one revision")
    return None


# --------------------------------------------------------------------------- API 2: render


def _render_receipt_schema() -> Mapping[str, object]:
    return {
        "$defs": _defs(),
        **_obj(
            ("surface_id", "projection_id", "projection_revision", "projection_digest", "displayed", "rendered_at"),
            {
                "surface_id": _ref("id"),
                "projection_id": _ref("projection_id"),
                "projection_revision": _ref("revision"),
                "projection_digest": _ref("sha"),
                "displayed": _ref("indicators"),
                "rendered_at": _ref("ztime"),
            },
        ),
    }


def _parse_render_receipt(raw: object) -> dict[str, object] | PresenceRefusal:
    receipt = _load(raw, limit=WIRE_LIMIT, public=True)
    if isinstance(receipt, PresenceRefusal):
        return receipt
    displayed = receipt.get("displayed")
    if not isinstance(displayed, Mapping):
        return _refuse("indicator-hidden", "adapter displayed no indicators")
    for key in INDICATORS:
        if displayed.get(key) is None:
            return _refuse("indicator-hidden", key)
    result = b._validate_json_schema_subset(receipt, _render_receipt_schema())
    if isinstance(result, b.TypedRefusal):
        return _lift(result)
    return receipt


def _derive_indicators(projection: PresenceProjection, surface: PresenceSurfaceState) -> dict[str, str]:
    state = projection.data["state"]
    assert isinstance(state, Mapping)
    return {
        "connectivity": str(surface.data["connectivity"]),
        "availability": str(state["availability"]),
        "privacy": str(state["privacy"]),
        "recording": str(state["recording"]),
        "action": str(state["action"]),
    }


def render_presence(
    current: PresenceProjection,
    *,
    surface: PresenceSurfaceState,
    readiness: PresenceReadiness,
    adapter: BridgePresenceAdapter,
    now: datetime,
    freshness: b.FreshnessWindow,
) -> PresenceRenderResult | PresenceRefusal:
    refused = _check_now(now) or _check_freshness(freshness, "freshness")
    if refused is not None:
        return refused
    projection = _reparse_projection(current)
    if isinstance(projection, PresenceRefusal):
        return projection
    surf = _reparse_surface(surface)
    if isinstance(surf, PresenceRefusal):
        return surf
    if surf.data["phase"] == "removed":
        return _refuse("surface-removed", surf.surface_id)
    refused = _check_readiness(readiness, surf, now, freshness)
    if refused is not None:
        return refused
    try:
        fixture_only = getattr(adapter, "fixture_only", None) is True
    except Exception as exc:  # untrusted adapter boundary
        return _refuse("adapter-not-fixture", exc.__class__.__name__)
    if not fixture_only:
        return _refuse("adapter-not-fixture", "Child C admits fixture adapters only")
    indicators = _derive_indicators(projection, surf)
    if indicators["connectivity"] == "live" and (
        not _is_fresh(surf.data["connectivity_observed_at"], now, freshness)
        or not _is_fresh(projection.data["observed_at"], now, freshness)
    ):
        return _refuse("stale-live-projection", "stale state cannot render as live")
    try:
        raw_receipt = adapter.render(
            presence_projection_to_json(projection), surface_id=surf.surface_id, indicators=dict(indicators)
        )
        receipt = _parse_render_receipt(raw_receipt)
    except Exception as exc:  # untrusted adapter boundary: the call AND every touch of its result
        return _refuse("adapter-receipt-mismatch", exc.__class__.__name__)
    if isinstance(receipt, PresenceRefusal):
        return receipt
    if receipt["surface_id"] != surf.surface_id:
        return _refuse("surface-mismatch", "receipt names another surface")
    refused = _compare_projection_binding(
        receipt["projection_revision"], receipt["projection_digest"], receipt["projection_id"], projection
    )
    if refused is not None:
        return refused
    rendered_at = _parse_z(receipt["rendered_at"])
    if rendered_at is None:
        return _refuse("invalid-pattern", "rendered_at")
    if rendered_at > now:
        return _refuse("future-timestamp", "rendered_at")
    if not _is_fresh(receipt["rendered_at"], now, freshness):
        return _refuse("adapter-receipt-mismatch", "render receipt outside freshness window")
    displayed = receipt["displayed"]
    assert isinstance(displayed, Mapping)
    for key in INDICATORS:
        if key == "connectivity" and displayed[key] == "live" and indicators[key] != "live":
            return _refuse("forged-live-state", "surface displayed live while server state is not live")
        if displayed[key] != indicators[key]:
            return _refuse("indicator-contradiction", key)
    adapter_receipt_digest = _sha_json(receipt)
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "render_id": f"presence-render:{adapter_receipt_digest[7:39]}",
        "surface_id": surf.surface_id,
        "surface_state_digest": surf.digest,
        "projection_id": projection.data["projection_id"],
        "projection_revision": projection.revision,
        "projection_digest": projection.digest,
        "observed_projection_revision": receipt["projection_revision"],
        "indicators": indicators,
        "fallback_channels": list(FALLBACK_CHANNELS),
        "adapter_receipt_digest": adapter_receipt_digest,
        "rendered_at": receipt["rendered_at"],
        "render_digest": None,
    }
    data["render_digest"] = _digest_record(data, "render_digest")
    return parse_presence_render_result(data)


# --------------------------------------------------------------------------- API 3: interaction


def make_presence_interaction_request(
    *,
    request_id: str,
    surface_id: str,
    purpose: str,
    mode: str,
    content: str,
    observed_projection_revision: int,
    observed_projection_digest: str,
    consent_ref: str,
) -> PresenceInteractionRequest | PresenceRefusal:
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "surface_id": surface_id,
        "purpose": purpose,
        "input": {"mode": mode, "content": content},
        "observed_projection_revision": observed_projection_revision,
        "observed_projection_digest": observed_projection_digest,
        "consent_ref": consent_ref,
        "request_digest": None,
    }
    try:
        data["request_digest"] = _digest_record(data, "request_digest")
    except Exception:  # hostile caller values never escape
        return _refuse("wrong-type", "request fields must be JSON values")
    return parse_presence_interaction_request(data)


def _interaction_envelope_schema() -> Mapping[str, object]:
    return {
        "$defs": _defs(),
        **_obj(
            ("surface_id", "read_at", "request"),
            {"surface_id": _ref("id"), "read_at": _ref("ztime"), "request": {"type": "object"}},
        ),
    }


def _grant_fields_ok(grant: b.ConsentGrant) -> bool:
    return all(
        isinstance(getattr(grant, name), str)
        for name in ("stage", "purpose", "subject_scope", "consent_ref", "consent_digest", "consented_at", "expires_at")
    ) and isinstance(grant.revoked, bool)


def _evaluate_presence_consent(
    *,
    request: Mapping[str, object],
    grants: object,
    surface_id: str,
    auth_context_digest: str,
    now: datetime,
) -> b.ConsentGrant | PresenceRefusal:
    if isinstance(grants, (str, bytes)) or not isinstance(grants, Sequence):
        return _refuse("wrong-type", "consent_evidence")
    if not all(type(grant) is b.ConsentGrant and _grant_fields_ok(grant) for grant in grants):
        return _refuse("wrong-type", "consent_evidence must be org060.ConsentGrant")
    consent_ref = request["consent_ref"]
    candidates = [g for g in grants if g.stage == PRESENCE_CONSENT_STAGE and g.consent_ref == consent_ref]
    if not candidates:
        return _refuse("missing-consent", PRESENCE_CONSENT_STAGE)
    for grant in candidates:
        if grant.purpose != request["purpose"]:
            return _refuse("purpose-drift", grant.consent_ref)
        if grant.revoked:
            return _refuse("revoked-consent", grant.consent_ref)
        expires = _parse_z(grant.expires_at)
        consented = _parse_z(grant.consented_at)
        if expires is None or consented is None:
            return _refuse("invalid-pattern", "consent time")
        if expires <= now:
            return _refuse("expired-consent", grant.consent_ref)
        if consented > now:
            return _refuse("future-timestamp", "consented_at")
        if grant.subject_scope not in b.SUBJECT_SCOPE:
            return _refuse("invalid-enum", "subject_scope")
        if grant.subject_scope in ("bystander", "unknown"):
            return _refuse("missing-consent", "bystander/unknown subjects refuse")
        try:
            expected = _consent_digest(
                purpose=grant.purpose,
                subject_scope=grant.subject_scope,
                consent_ref=grant.consent_ref,
                consented_at=grant.consented_at,
                expires_at=grant.expires_at,
                surface_id=surface_id,
                auth_context_digest=auth_context_digest,
            )
        except Exception as exc:  # hostile caller values never escape
            return _refuse("consent-binding-mismatch", exc.__class__.__name__)
        if grant.consent_digest != expected:
            return _refuse("consent-binding-mismatch", "consent is bound to another surface or context")
    if len({(g.subject_scope, g.consent_digest) for g in candidates}) > 1:
        return _refuse("contradictory-consent", "multiple scopes or digests for one consent_ref")
    return candidates[0]


def _presence_replay_key(consent_ref: str) -> str:
    """Presence replay key. '|' is outside b.ID_RE, so no B consent_ref can equal it."""
    return f"{PRESENCE_CONSENT_STAGE}|{consent_ref}"


def accept_presence_interaction(
    interaction: bytes | str | Mapping[str, object],
    *,
    current: PresenceProjection,
    surface: PresenceSurfaceState,
    readiness: PresenceReadiness,
    context: cn.AuthenticatedContext,
    consent_evidence: Sequence[b.ConsentGrant],
    replay_cache: b.ConsentReplayCache,
    now: datetime,
    freshness: b.FreshnessWindow,
) -> PresenceInteractionDecision | PresenceRefusal:
    refused = _check_now(now) or _check_freshness(freshness, "freshness")
    if refused is not None:
        return refused
    if type(replay_cache) is not b.ConsentReplayCache:
        return _refuse("wrong-type", "org060.ConsentReplayCache required")
    envelope = _load(interaction, limit=ENVELOPE_LIMIT, public=True)
    if isinstance(envelope, PresenceRefusal):
        return envelope
    result = b._validate_json_schema_subset(envelope, _interaction_envelope_schema())
    if isinstance(result, b.TypedRefusal):
        return _lift(result)
    request = parse_presence_interaction_request(envelope["request"])  # type: ignore[arg-type]
    if isinstance(request, PresenceRefusal):
        return request
    refused = _check_context(context)
    if refused is not None:
        return refused
    projection = _reparse_projection(current)
    if isinstance(projection, PresenceRefusal):
        return projection
    surf = _reparse_surface(surface)
    if isinstance(surf, PresenceRefusal):
        return surf
    if surf.data["phase"] == "removed":
        return _refuse("surface-removed", surf.surface_id)
    refused = _check_readiness(readiness, surf, now, freshness)
    if refused is not None:
        return refused
    if surf.data["connectivity"] == "disconnected":
        return _refuse("surface-disconnected", "a disconnected surface cannot submit interaction")
    req = request.data
    if envelope["surface_id"] != surf.surface_id or req["surface_id"] != surf.surface_id:
        return _refuse("surface-mismatch", "interaction is not from this surface")
    read_at = _parse_z(envelope["read_at"])
    if read_at is None:
        return _refuse("invalid-pattern", "read_at")
    if read_at > now:
        return _refuse("future-timestamp", "read_at")
    if not _is_fresh(envelope["read_at"], now, freshness):
        return _refuse("stale-interaction", "interaction read outside freshness window")
    refused = _compare_projection_binding(
        req["observed_projection_revision"], req["observed_projection_digest"], projection.data["projection_id"], projection
    )
    if refused is not None:
        return refused
    bounded = req["input"]
    assert isinstance(bounded, Mapping)
    mode = bounded["mode"]
    if mode not in surf.data["interaction_modes"]:  # type: ignore[operator]
        return _refuse("interaction-mode-unavailable", str(mode))
    state = projection.data["state"]
    assert isinstance(state, Mapping)
    if state["availability"] in OVERRIDE_AVAILABILITY and not (
        mode == "control" and bounded["content"] in REDUCING_CONTROLS
    ):
        return _refuse("availability-override", str(state["availability"]))
    auth_digest = presence_auth_context_digest(context)
    if isinstance(auth_digest, PresenceRefusal):
        return auth_digest
    grant = _evaluate_presence_consent(
        request=req, grants=consent_evidence, surface_id=surf.surface_id, auth_context_digest=auth_digest, now=now
    )
    if isinstance(grant, PresenceRefusal):
        return grant
    # Mark LAST: single-use per grant like org060 (b.ConsentReplayCache.mark), namespaced to presence scope.
    if not replay_cache.mark(_presence_replay_key(grant.consent_ref)):
        return _refuse("replayed-consent", grant.consent_ref)
    adapter_receipt_digest = _sha_json(envelope)
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": f"presence-decision:{request.digest[7:39]}",
        "request_digest": request.digest,
        "surface_id": surf.surface_id,
        "surface_state_digest": surf.digest,
        "projection_id": projection.data["projection_id"],
        "projection_revision": projection.revision,
        "projection_digest": projection.digest,
        "observed_projection_revision": req["observed_projection_revision"],
        "adapter_receipt_digest": adapter_receipt_digest,
        "auth_context_digest": auth_digest,
        "intent": {
            "kind": "control_proposal" if mode == "control" else "conversation_turn",
            "mode": mode,
            "content": bounded["content"],
            "purpose": req["purpose"],
        },
        "consent_ref": grant.consent_ref,
        "consent_digest": grant.consent_digest,
        "decided_at": _z(now),
        "decision_digest": None,
    }
    data["decision_digest"] = _digest_record(data, "decision_digest")
    return parse_presence_interaction_decision(data)


# --------------------------------------------------------------------------- API 6: recovery


def recover_presence_projection(
    durable_projection: bytes,
    *,
    view_path: Path,
    checkpoint_receipt: pc.CheckpointReceipt,
    checkpoint_root: Path,
    navigator: cn.NavigatorProjection,
    context: cn.AuthenticatedContext,
    companion: PresenceCompanionState,
    now: datetime,
) -> PresenceProjection | PresenceRefusal:
    refused = _check_now(now)
    if refused is not None:
        return refused
    if type(durable_projection) is not bytes:
        return _refuse("recovery-source-invalid", "recovery reads durable bytes only, never session-local objects")
    projection = parse_presence_projection(durable_projection)
    if isinstance(projection, PresenceRefusal):
        return projection
    state = _companion_state(companion, now)
    if isinstance(state, PresenceRefusal):
        return state
    if state != projection.data["state"]:
        # No silent rebuild: the caller rebuilds via build_presence_projection(prior=durable).
        return _refuse("recovery-state-drift", "server companion state differs from the durable projection")
    continuity = _bind_continuity(view_path, checkpoint_receipt, checkpoint_root)
    if isinstance(continuity, PresenceRefusal):
        return continuity
    if continuity != projection.data["continuity"]:
        return _refuse("continuity-binding-mismatch", "durable projection does not bind the durable checkpoint")
    navigator_binding = _bind_navigator(navigator, context)
    if isinstance(navigator_binding, PresenceRefusal):
        return navigator_binding
    if navigator_binding != projection.data["navigator"]:
        return _refuse("projection-binding-mismatch", "durable projection does not bind the current navigator/context")
    return projection


# --------------------------------------------------------------------------- schemas


def _obj(required: Sequence[str], properties: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _ref(name: str) -> dict[str, object]:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(name: str) -> dict[str, object]:
    return {"oneOf": [_ref(name), {"type": "null"}]}


def _enum(values: Sequence[str]) -> dict[str, object]:
    return {"type": "string", "enum": list(values)}


def _defs() -> dict[str, object]:
    defs: dict[str, object] = {
        "schema_version": _enum((SCHEMA_VERSION,)),
        "sha": {"type": "string", "maxLength": 71, "pattern": b.SHA_RE.pattern},
        "hex64": {"type": "string", "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
        "ztime": {"type": "string", "maxLength": 20, "pattern": b.Z_RE.pattern},
        "id": {"type": "string", "maxLength": ID_LIMIT, "pattern": b.ID_RE.pattern},
        "pc_id": {"type": "string", "maxLength": ID_LIMIT, "pattern": pc.ID_RE.pattern},
        "stable_id": {"type": "string", "maxLength": ID_LIMIT, "pattern": cn.STABLE_ID_PATTERN},
        "projection_id": {"type": "string", "maxLength": ID_LIMIT + 20, "pattern": PROJECTION_ID_PATTERN},
        "summary": {"type": "string", "maxLength": SUMMARY_LIMIT, "pattern": FREE_TEXT_PATTERN},
        "content": {"type": "string", "maxLength": CONTENT_LIMIT, "pattern": FREE_TEXT_PATTERN},
        "revision": {"type": "integer", "minimum": 1, "maximum": REVISION_MAX},
        "fallback_channels": {
            "type": "array",
            "maxItems": len(FALLBACK_CHANNELS),
            "items": _enum(FALLBACK_CHANNELS),
            "enum": [list(FALLBACK_CHANNELS)],
        },
        "indicators": _obj(
            INDICATORS,
            {
                "connectivity": _enum(CONNECTIVITY),
                "availability": _enum(AVAILABILITY),
                "privacy": _enum(PRIVACY_STATE),
                "recording": _enum(RECORDING_STATE),
                "action": _enum(ACTION_STATE),
            },
        ),
        "waiver": _obj(
            ("waiver_id", "surface_id", "adapter_id", "provisioning_anchor", "waived_legs", "reason", "review_ref", "reviewed_at", "expires_at", "waiver_digest"),
            {
                "waiver_id": _ref("id"),
                "surface_id": _ref("id"),
                "adapter_id": _ref("id"),
                "provisioning_anchor": _ref("sha"),
                "waived_legs": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "maxItems": len(WAIVABLE_LEGS),
                    "items": _enum(WAIVABLE_LEGS),
                },
                "reason": _ref("summary"),
                "review_ref": _ref("id"),
                "reviewed_at": _ref("ztime"),
                "expires_at": _ref("ztime"),
                "waiver_digest": _ref("sha"),
            },
        ),
    }
    for leg in LIFECYCLE_LEGS:
        defs[f"{leg}_evidence"] = _obj(
            ("evidence_ref", "evidence_digest", "observed_at", "outcome"),
            {
                "evidence_ref": _ref("id"),
                "evidence_digest": _ref("sha"),
                "observed_at": _ref("ztime"),
                "outcome": _enum(LEG_OUTCOMES[leg]),
            },
        )
    return defs


def _schema_document(schema_id: str, required: Sequence[str], properties: Mapping[str, object]) -> bytes:
    body = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://pantheon.local/schemas/{schema_id}",
        "$defs": _defs(),
        **_obj(required, properties),
    }
    return b.canonical_json_bytes(body)


def generate_presence_projection_schema() -> bytes:
    props = {
        "schema_version": _ref("schema_version"),
        "projection_id": _ref("projection_id"),
        "projection_revision": _ref("revision"),
        "continuity": _obj(
            (
                "persona_id",
                "organism_id",
                "view_sha256",
                "checkpoint_id",
                "checkpoint_payload_sha256",
                "checkpoint_receipt_sha256",
                "prior_checkpoint_sha256",
            ),
            {
                "persona_id": _ref("pc_id"),
                "organism_id": _ref("pc_id"),
                "view_sha256": _ref("hex64"),
                "checkpoint_id": _ref("pc_id"),
                "checkpoint_payload_sha256": _ref("hex64"),
                "checkpoint_receipt_sha256": _ref("hex64"),
                "prior_checkpoint_sha256": _nullable("hex64"),
            },
        ),
        "navigator": _obj(
            ("assembly_id", "projection_sha256", "catalogue_sha256", "lock_sha256", "auth_context_digest"),
            {
                "assembly_id": _ref("stable_id"),
                "projection_sha256": _ref("sha"),
                "catalogue_sha256": _ref("sha"),
                "lock_sha256": _ref("sha"),
                "auth_context_digest": _ref("sha"),
            },
        ),
        "state": _obj(
            ("availability", "privacy", "recording", "action", "fallback_channels"),
            {
                "availability": _enum(AVAILABILITY),
                "privacy": _enum(PRIVACY_STATE),
                "recording": _enum(RECORDING_STATE),
                "action": _enum(ACTION_STATE),
                "fallback_channels": _ref("fallback_channels"),
            },
        ),
        "observed_at": _ref("ztime"),
        "issued_at": _ref("ztime"),
        "projection_digest": _ref("sha"),
    }
    return _schema_document("org060-presence-projection.schema.json", tuple(props), props)


def generate_presence_surface_state_schema() -> bytes:
    lifecycle_props: dict[str, object] = {leg: _nullable(f"{leg}_evidence") for leg in LIFECYCLE_LEGS}
    lifecycle_props.update(
        {"exact_revision": _ref("id"), "rollback_ref": _ref("id"), "provisioning_anchor": _ref("sha"), "waiver": _nullable("waiver")}
    )
    props = {
        "schema_version": _ref("schema_version"),
        "surface_id": _ref("id"),
        "surface_kind": _enum(SURFACE_KIND),
        "state_revision": _ref("revision"),
        "phase": _enum(SURFACE_PHASE),
        "connectivity": _enum(CONNECTIVITY),
        "connectivity_observed_at": _ref("ztime"),
        "interaction_modes": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "maxItems": len(INTERACTION_MODE),
            "items": _enum(INTERACTION_MODE),
        },
        "adapter": _obj(
            ("adapter_id", "adapter_class", "adapter_revision"),
            {"adapter_id": _ref("id"), "adapter_class": _enum(ADAPTER_CLASS), "adapter_revision": _ref("id")},
        ),
        "lifecycle": _obj(tuple(lifecycle_props), lifecycle_props),
        "prior_state_digest": _nullable("sha"),
        "state_digest": _ref("sha"),
    }
    return _schema_document("org060-presence-surface-state.schema.json", tuple(props), props)


def generate_presence_render_result_schema() -> bytes:
    props = {
        "schema_version": _ref("schema_version"),
        "render_id": _ref("id"),
        "surface_id": _ref("id"),
        "surface_state_digest": _ref("sha"),
        "projection_id": _ref("projection_id"),
        "projection_revision": _ref("revision"),
        "projection_digest": _ref("sha"),
        "observed_projection_revision": _ref("revision"),
        "indicators": _ref("indicators"),
        "fallback_channels": _ref("fallback_channels"),
        "adapter_receipt_digest": _ref("sha"),
        "rendered_at": _ref("ztime"),
        "render_digest": _ref("sha"),
    }
    return _schema_document("org060-presence-render-result.schema.json", tuple(props), props)


def _input_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _obj(("mode", "content"), {"mode": _enum(("text", "voice")), "content": _ref("content")}),
            _obj(("mode", "content"), {"mode": _enum(("control",)), "content": _enum(CONTROL_INPUT)}),
        ]
    }


def generate_presence_interaction_request_schema() -> bytes:
    props = {
        "schema_version": _ref("schema_version"),
        "request_id": _ref("id"),
        "surface_id": _ref("id"),
        "purpose": _ref("summary"),
        "input": _input_schema(),
        "observed_projection_revision": _ref("revision"),
        "observed_projection_digest": _ref("sha"),
        "consent_ref": _ref("id"),
        "request_digest": _ref("sha"),
    }
    return _schema_document("org060-presence-interaction-request.schema.json", tuple(props), props)


def generate_presence_interaction_decision_schema() -> bytes:
    intent = {
        "oneOf": [
            _obj(
                ("kind", "mode", "content", "purpose"),
                {
                    "kind": _enum(("conversation_turn",)),
                    "mode": _enum(("text", "voice")),
                    "content": _ref("content"),
                    "purpose": _ref("summary"),
                },
            ),
            _obj(
                ("kind", "mode", "content", "purpose"),
                {
                    "kind": _enum(("control_proposal",)),
                    "mode": _enum(("control",)),
                    "content": _enum(CONTROL_INPUT),
                    "purpose": _ref("summary"),
                },
            ),
        ]
    }
    props = {
        "schema_version": _ref("schema_version"),
        "decision_id": _ref("id"),
        "request_digest": _ref("sha"),
        "surface_id": _ref("id"),
        "surface_state_digest": _ref("sha"),
        "projection_id": _ref("projection_id"),
        "projection_revision": _ref("revision"),
        "projection_digest": _ref("sha"),
        "observed_projection_revision": _ref("revision"),
        "adapter_receipt_digest": _ref("sha"),
        "auth_context_digest": _ref("sha"),
        "intent": intent,
        "consent_ref": _ref("id"),
        "consent_digest": _ref("sha"),
        "decided_at": _ref("ztime"),
        "decision_digest": _ref("sha"),
    }
    return _schema_document("org060-presence-interaction-decision.schema.json", tuple(props), props)


_GENERATORS = {
    "projection": generate_presence_projection_schema,
    "surface-state": generate_presence_surface_state_schema,
    "render-result": generate_presence_render_result_schema,
    "interaction-request": generate_presence_interaction_request_schema,
    "interaction-decision": generate_presence_interaction_decision_schema,
}


def generate_schemas() -> Mapping[str, bytes]:
    return {f"org060-presence-{name}.schema.json": generator() for name, generator in _GENERATORS.items()}


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-schemas"]:
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        for filename, content in generate_schemas().items():
            (schema_root / filename).write_bytes(content)
    else:
        raise SystemExit("usage: org060_presence_contract.py --write-schemas")
