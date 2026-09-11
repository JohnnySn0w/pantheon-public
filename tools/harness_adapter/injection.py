"""Concordance injection contract — the typed shape every harness encoder implements.

ORG-073 §4 names the harness-neutral retrieval contract:
``HookInjectionProvider.retrieve(request: InjectionRequest) -> InjectionResult``.
This module IS that contract — types only. Retrieval, corpus adapters, HTTP
transport, and native hook encoders live in separate modules (T-2816 build
steps); nothing here imports or performs any of them.

Model-silence is structural, not conventional (ORG-073 §3: a no-op MUST NOT
inject an "explicit nothing relevant" message):

1. Exactly one result class — :class:`Injection` — has a text-bearing
   attribute. :class:`InjectionNoOp` and :class:`InjectionBackendFailure`
   carry typed reason/evidence fields only; there is no markdown/content/text
   attribute to render.
2. The result sum type is sealed: :class:`InjectionResult` cannot be
   instantiated directly, and defining a subclass outside this module raises
   ``TypeError`` at class-definition time. A fourth, chattier kind cannot be
   bolted on.
3. An :class:`Injection` cannot be blank, source-less, or stale — construction
   rejects blank markdown, empty sources, and any source whose supersession
   state is ``"superseded"`` (ORG-073 §3 stale-decision protection).
4. Providers are reached only through :meth:`HookInjectionProvider.invoke`,
   which is ``@final`` and override-refused at class-definition time. It
   validates the request, never lets a provider ``Exception`` escape, and
   converts a non-:class:`InjectionResult` return into a typed
   ``contract_violation`` failure.
5. There is ONE sanctioned seam to model-visible text —
   :func:`model_visible_text` — which returns the markdown of an
   :class:`Injection` and ``None`` for everything else. Silence is ``None``,
   never ``""``.

Honest limit: Python cannot stop an encoder from printing ``f"{result!r}"``
to stdout. That last inch — never rendering a silent result into the
harness-native response — is the obligation of each native encoder ticket,
verified by that encoder's own tests.
"""

from __future__ import annotations

import traceback as _traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping, final, get_args

from tools.harness_adapter.types import HarnessName

InjectionKind = Literal["inject", "no_op", "backend_failure"]
CorpusClass = Literal["adr", "ledger", "note"]
SupersessionState = Literal["current", "superseded", "unknown"]
NoOpReason = Literal[
    "missing_required_field",
    "empty_corpus",
    "no_candidates",
    "all_superseded",
    "all_seen_this_session",
    "provenance_unestablished",
]
FailureKind = Literal[
    "provider_exception",
    "contract_violation",
    "invalid_request",
    "backend_unavailable",
    "backend_error",
]

CORPUS_CLASSES: frozenset[str] = frozenset(get_args(CorpusClass))
SUPERSESSION_STATES: frozenset[str] = frozenset(get_args(SupersessionState))
NO_OP_REASONS: frozenset[str] = frozenset(get_args(NoOpReason))
FAILURE_KINDS: frozenset[str] = frozenset(get_args(FailureKind))

# The only three subclasses of InjectionResult that may exist (see
# InjectionResult.__init_subclass__). Names checked together with the defining
# module so an identically-named class elsewhere is still refused.
_SANCTIONED_RESULTS: frozenset[str] = frozenset(
    {"Injection", "InjectionNoOp", "InjectionBackendFailure"}
)


def _require_non_blank_str(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string, got {value!r}")


@dataclass(frozen=True)
class InjectionRequest:
    """One prompt-time retrieval request (ORG-073 §4 minimum carry-set).

    Construction never validates — a native encoder must be able to build a
    request from whatever a hook payload contained, then let ``invoke()``
    convert missing required fields into a typed no-op rather than an
    ambiguous empty string.
    """

    prompt_text: str
    session_id: str
    harness: HarnessName | None = None
    hook_event: str | None = None
    cwd: Path | None = None
    repo_identity: str | None = None
    role_domain: str | None = None
    native: Mapping[str, Any] = field(default_factory=dict)

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ("prompt_text", "session_id")

    def missing_required(self) -> tuple[str, ...]:
        """Names of required fields that are absent (None / non-str / blank)."""
        missing = []
        for name in self.REQUIRED_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
        return tuple(missing)


@dataclass(frozen=True)
class SourceRecord:
    """Provenance for one injected excerpt (ORG-058 envelope carrier)."""

    source_id: str
    corpus_class: CorpusClass
    path: str
    rank: int
    supersession: SupersessionState = "unknown"
    git_sha: str | None = None
    locator: str | None = None
    content_sha256: str | None = None
    excerpt: str | None = None
    title: str | None = None
    observed_at: str | None = None
    score: float | None = None  # informational, NEVER a gate — ORG-073 §1

    def __post_init__(self) -> None:
        _require_non_blank_str(self.source_id, "SourceRecord.source_id")
        if self.corpus_class not in CORPUS_CLASSES:
            raise ValueError(
                f"SourceRecord.corpus_class must be one of {sorted(CORPUS_CLASSES)}, "
                f"got {self.corpus_class!r}"
            )
        _require_non_blank_str(self.path, "SourceRecord.path")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError(f"SourceRecord.rank must be an int >= 1, got {self.rank!r}")
        if self.supersession not in SUPERSESSION_STATES:
            raise ValueError(
                f"SourceRecord.supersession must be one of {sorted(SUPERSESSION_STATES)}, "
                f"got {self.supersession!r}"
            )


@dataclass(frozen=True)
class InjectionResult:
    """Sealed sum-type root. Exactly three subclasses exist, all in this module."""

    kind: ClassVar[InjectionKind]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__name__ not in _SANCTIONED_RESULTS or cls.__module__ != __name__:
            raise TypeError(
                "InjectionResult is a sealed sum type: Injection | InjectionNoOp | "
                f"InjectionBackendFailure. {cls.__module__}.{cls.__qualname__} is not "
                "a sanctioned member (ORG-073 §4 — a fourth result kind cannot be "
                "added outside tools/harness_adapter/injection.py)."
            )

    def __post_init__(self) -> None:
        # Only reachable via direct InjectionResult() — every sanctioned
        # subclass defines its own __post_init__.
        if type(self) is InjectionResult:
            raise TypeError(
                "InjectionResult is abstract; construct Injection, InjectionNoOp, "
                "or InjectionBackendFailure"
            )


@dataclass(frozen=True)
class Injection(InjectionResult):
    """The ONE result kind that carries model-visible text."""

    kind: ClassVar[InjectionKind] = "inject"
    markdown: str
    sources: tuple[SourceRecord, ...]

    def __post_init__(self) -> None:
        _require_non_blank_str(self.markdown, "Injection.markdown")
        sources = self.sources
        if isinstance(sources, (str, bytes)):
            raise ValueError("Injection.sources must be a sequence of SourceRecord")
        if not isinstance(sources, tuple):
            try:
                sources = tuple(sources)
            except TypeError:
                raise ValueError(
                    f"Injection.sources must be a sequence of SourceRecord, got {sources!r}"
                ) from None
            object.__setattr__(self, "sources", sources)
        if not sources:
            raise ValueError("Injection.sources must be non-empty (provenance-carrying)")
        for src in sources:
            if not isinstance(src, SourceRecord):
                raise ValueError(f"Injection.sources entries must be SourceRecord, got {src!r}")
            if src.supersession == "superseded":
                raise ValueError(
                    f"Injection must not carry a superseded source ({src.source_id}): "
                    "a superseded decision never injects as current (ORG-073 §3)"
                )


@dataclass(frozen=True)
class InjectionNoOp(InjectionResult):
    """Model-silent no-op: a typed reason, never a 'nothing found' message."""

    kind: ClassVar[InjectionKind] = "no_op"
    reason: NoOpReason
    detail: str = ""
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.reason not in NO_OP_REASONS:
            raise ValueError(
                f"InjectionNoOp.reason must be one of {sorted(NO_OP_REASONS)}, "
                f"got {self.reason!r}"
            )


@dataclass(frozen=True)
class InjectionBackendFailure(InjectionResult):
    """Model-silent backend/contract failure with audit evidence."""

    kind: ClassVar[InjectionKind] = "backend_failure"
    failure: FailureKind
    detail: str
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.failure not in FAILURE_KINDS:
            raise ValueError(
                f"InjectionBackendFailure.failure must be one of {sorted(FAILURE_KINDS)}, "
                f"got {self.failure!r}"
            )


def model_visible_text(result: InjectionResult) -> str | None:
    """THE single sanctioned seam to model-visible text.

    An :class:`Injection` renders its markdown; everything else — no-op,
    backend failure, or a non-result passed by a defective encoder — renders
    ``None``. Silence is ``None``, never ``""``.
    """
    if isinstance(result, Injection):
        return result.markdown
    return None


class HookInjectionProvider(ABC):
    """Harness-neutral retrieval provider (ORG-073 §4).

    Concrete providers implement :meth:`retrieve` only. Callers (native
    encoders) go through :meth:`invoke` only — the guard that keeps the
    contract's failure modes typed and model-silent.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "invoke" in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} must not override HookInjectionProvider.invoke; "
                "it is the final contract seam — implement retrieve() instead"
            )

    @abstractmethod
    def retrieve(self, request: InjectionRequest) -> InjectionResult:
        """Run retrieval for one prompt and return a typed result."""

    @final
    def invoke(self, request: InjectionRequest) -> InjectionResult:
        """Validate, delegate to :meth:`retrieve`, and contain every failure.

        Never raises ``Exception``; ``BaseException`` process-control signals
        (``KeyboardInterrupt``, ``SystemExit``) propagate to the hook harness.
        """
        if not isinstance(request, InjectionRequest):
            return InjectionBackendFailure(
                failure="invalid_request",
                detail=(
                    "invoke() requires an InjectionRequest, "
                    f"got {type(request).__name__}"
                ),
            )
        missing = request.missing_required()
        if missing:
            return InjectionNoOp(reason="missing_required_field", detail=",".join(missing))
        try:
            result = self.retrieve(request)
        except Exception as exc:  # noqa: BLE001 — the containment IS the contract
            exc_type = type(exc).__name__
            try:
                message = str(exc)
            except Exception:
                message = "<unprintable exception>"
            try:
                tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
            except Exception:
                tb = "<traceback unavailable>"
            return InjectionBackendFailure(
                failure="provider_exception",
                detail=f"{exc_type}: {message}",
                evidence={
                    "exception_type": exc_type,
                    "message": message,
                    "traceback": tb,
                    "provider": type(self).__name__,
                },
            )
        if not isinstance(result, InjectionResult):
            return InjectionBackendFailure(
                failure="contract_violation",
                detail=(
                    f"retrieve() returned {type(result).__name__}; "
                    "providers must return an InjectionResult"
                ),
                evidence={"provider": type(self).__name__},
            )
        return result
