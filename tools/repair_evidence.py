"""Evidence redaction + repair route-guard for ORG-076 runtime repair tickets (T-4707 / BT1).

Pure functions, no git, no state. RFC-004 sections implemented here:

  * §4 evidence bullet / §6.15-17: emit-time redaction. The detector-declared
    redactor runs over the evidence BEFORE it is written into the ticket. Failure
    is fail-closed: the redactor raising (or returning a non-string) yields
    ``deferred=True`` and NO snapshot at all (never a partial or unredacted one).
    The post-redaction UTF-8 size is capped at 32 KiB; over the cap also yields
    ``deferred=True`` and no snapshot (§6.16: the key is ABSENT, not empty).
  * §5 / §6.6 / §6.18: the route-guard. Prefer a live duty holder on a harness
    other than the faulted one; else any live; else page. ``evidence_deferred``
    tickets must route same-account: cross-account candidates are refused and the
    ticket is re-routed same-account or paged. Liveness and page are INJECTED
    callables (grants are BT4; this module never looks up the live fleet).

ROUTE TABLE (T-5998; the three BT2/BT3b specs: faulted harness claude, account claude-fallback,
``candidates=(RECOVERY_CANDIDATE,)`` = watchman@codex, ``any_live_fallback=(RECOVERY_CANDIDATE,)``).
Liveness is BINARY (``org076_ports.make_liveness``): a deep-sleep / parked / modal watchman counts as
NOT live, so its episodes go to Recovery (any_live) or page. ``requested`` is never passed by ``_route``.
BEFORE T-5998 = the same table with no fallback column. Recovery = RECOVERY_CANDIDATE, live or not.

  evidence     watchman   subject     BEFORE                  AFTER
  not deferred live       other       other_harness watchman  other_harness watchman
  not deferred live       recovery        other_harness watchman  other_harness watchman
  not deferred live       watchman    page                    any_live recovery (Recovery live) / page
  not deferred NOT live   other       page                    any_live recovery (Recovery live) / page
  not deferred NOT live   recovery        page                    page (Recovery is the subject; the route record and
                                                                  page body now carry excluded_subject=['recovery'],
                                                                  kind/target unchanged)
  not deferred NOT live   watchman    page                    any_live recovery (Recovery live) / page
  deferred, origin claude-fallback (every existing spec), watchman live or not, any subject:
                                      page                    page (fallback skipped: B(ii) declined)
  deferred, origin codex-fallback (no existing spec; generic): same_account watchman if live, else page;
                                      unchanged (the fallback is never consulted for deferred evidence).
Registry specs (``REPAIR_CANDIDATES``) get ``any_live_fallback=()``: unchanged. The registry detectors'
sample-FRESHNESS domains (``org076_sample_freshness.spec_for(..., detector=)``, candidates=(watchman,))
DO get the Recovery any_live fallback; the registry DropDetector specs themselves do not. Known limit: the route
is cached in the episode (``delivery.route``, including a ``page`` route), so a transient not-live
reading at first routing pins that episode to its page route.

Redaction patterns are copied (not imported -- chronicle drags in a lot) from:
  * ``tools/chronicle.py`` ``_CRED_PATTERNS`` (Authorization header, ``sk-`` keys)
  * ``tools/git_reconciler.py`` ``_redact`` (auth header with token scheme, URL
    userinfo)
plus generic token shapes and cross-account ``/home/<account>/...`` paths.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable, Iterable, Sequence

EVIDENCE_CAP_BYTES = 32 * 1024  # RFC-004 §3(a)/§4: 32 KiB post-redaction UTF-8

Redactor = Callable[[str], str]

# (pattern, replacement). Order matters: header/URL shapes first, then bare tokens.
_PATTERNS: list[tuple["re.Pattern[str]", str]] = [
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
     "<redacted-private-key>"),
    # chronicle._CRED_PATTERNS + git_reconciler._redact: Authorization header
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)(\S+)"), r"\1<redacted-cred>"),
    # git_reconciler._redact: credentials in URL userinfo
    (re.compile(r"(https?://)[^/@\s]+@"), r"\1<redacted>@"),
    # chronicle._CRED_PATTERNS: Anthropic/OpenAI style keys
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}"), "<redacted-cred>"),
    # generic token shapes
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "<redacted-cred>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "<redacted-cred>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-cred>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted-cred>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted-cred>"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "<redacted-cred>"),  # telegram bot token
    # key=value / key: value secrets
    (re.compile(r"(?i)\b((?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*)(\S+)"),
     r"\1<redacted-cred>"),
]

_HOME_PATH_RE = re.compile(r"/home/([A-Za-z0-9._-]+)(/[^\s'\"`)\]}>,;]*)?")

# Characters that str.splitlines() treats as line breaks (and other control bytes) would
# corrupt the single-line-per-row front-matter block scalar; normalise them to "\n"/" ".
_LINEBREAKS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85  ]")
_CTRL = re.compile(r"[\x00-\x08\x0e-\x1b\x1f\x7f]")


def make_default_redactor(own_account: str | None = None) -> Redactor:
    """Default redactor: credential/token shapes + cross-account ``/home/<other>/...`` paths.

    ``own_account`` paths are kept (non-sensitive, same-account); every other
    ``/home/<name>`` path is replaced by ``/home/<redacted-account>``.
    """

    def _redact(text: str) -> str:
        for pat, repl in _PATTERNS:
            text = pat.sub(repl, text)

        def _path(m: "re.Match[str]") -> str:
            if own_account is not None and m.group(1) == own_account:
                return m.group(0)
            return "/home/<redacted-account>"

        return _HOME_PATH_RE.sub(_path, text)

    return _redact


default_redactor: Redactor = make_default_redactor(None)


def normalise_text(text: str) -> str:
    """Make evidence safe for a YAML block scalar: no exotic line breaks / control chars,
    exactly one trailing newline (non-empty input)."""
    text = _LINEBREAKS.sub("\n", text)
    text = _CTRL.sub(" ", text)
    text = text.rstrip("\n")
    return text + "\n" if text else ""


@dataclasses.dataclass(frozen=True)
class EvidenceResult:
    snapshot: str | None  # None => key must be ABSENT from the ticket
    deferred: bool
    reason: str = ""


def redact_evidence(raw: "str | bytes", redactor: Redactor = default_redactor, *,
                    cap_bytes: int = EVIDENCE_CAP_BYTES) -> EvidenceResult:
    """Redact + cap. Fail-closed: any redactor failure => deferred, no snapshot."""
    try:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        out = redactor(text)
        if not isinstance(out, str):
            return EvidenceResult(None, True, f"redactor returned {type(out).__name__}, not str")
        out = normalise_text(out)
        # Belt: the declared redactor ran; the default credential patterns run again so a weak
        # custom redactor can't leak the shapes we know about. (Idempotent on clean text.)
        for pat, repl in _PATTERNS:
            out = pat.sub(repl, out)
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design (§6.17)
        return EvidenceResult(None, True, f"redactor failed: {type(exc).__name__}")
    if len(out.encode("utf-8")) > cap_bytes:
        return EvidenceResult(None, True, f"over {cap_bytes}-byte cap after redaction")
    return EvidenceResult(out, False, "")


# --------------------------------------------------------------------------- #
# Route guard (RFC-004 §5, §6.6, §6.18)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Candidate:
    """A seat holding the repair duty (grants are BT4; this is just routing data)."""
    name: str       # persona / seat name -> ticket ``target``
    harness: str    # "claude" | "codex" | ...
    account: str    # OS account the seat runs under


# ORG-048 Amendment A1 (2026-10-12): the recovery duty and its cross-account grant
# moved from Consultant to Watchman (persona watchman, codex harness, account
# codex-fallback). Every ORG-076 detector routes repair episodes here (T-5058).
RECOVERY_CANDIDATE = Candidate("watchman", "codex", "codex-fallback")

# RFC-004 section 5 (fault-domain matrix, codex row) / ORG-076 section 4: codex harness down -> the
# claude repair tier (Recovery). T-5999 (BT5.0): the generic-detector repair set. Order matters: watchman
# stays FIRST so a claude-faulted episode still routes other_harness -> watchman@codex, and a
# codex-faulted one routes other_harness -> recovery@claude.
# SCOPE (T-5999 Option A, refined by Recovery's T-5998 B(i)/B(ii) ruling): only the runner's drop-detector
# REGISTRY specs use this set as ``candidates``. The BT2/BT3b specs (quota_probe_detector,
# org076_sample_freshness, org076_meta) keep ``candidates=(RECOVERY_CANDIDATE,)`` and add Recovery ONLY as
# ``any_live_fallback`` (any_live case, never same_account / deferred evidence).
RECOVERY_CANDIDATE = Candidate("recovery", "claude", "claude-fallback")
REPAIR_CANDIDATES = (RECOVERY_CANDIDATE, RECOVERY_CANDIDATE)


@dataclasses.dataclass(frozen=True)
class Route:
    kind: str                       # other_harness | any_live | same_account | page
    target: Candidate | None
    refused_cross_account: tuple[str, ...] = ()
    excluded_subject: tuple[str, ...] = ()   # candidates dropped for being the episode's own subject


_SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # == quota_probe_detector.PERSONA_RE


def subject_of(fault_id: object) -> "str | None":
    """The persona an episode is ABOUT, parsed from a ``<kind>:<persona>`` fault_id (the last
    ``:``-separated part; detector-scoped ids like ``<kind>:<detector>:<persona>`` also resolve to the
    persona). Fault ids not in that shape (no ``:``, empty kind, a tail that is not a persona name, a
    non-string) have NO subject: they get no exclusion and never raise. Example: the store-liveness
    meta-detector's ``cycle-rate-low``."""
    if not isinstance(fault_id, str):
        return None
    kind, sep, who = fault_id.rpartition(":")
    return who if sep and kind and _SUBJECT_RE.match(who) else None


def resolve_route(faulted_harness: str, candidates: Sequence[Candidate], *,
                  liveness: Callable[[Candidate], bool],
                  page: Callable[[str, str], None],
                  evidence_deferred: bool = False,
                  origin_account: str | None = None,
                  requested: Candidate | None = None,
                  subject: str = "ORG-076 repair route: no eligible repair target",
                  episode_subject: str | None = None,
                  any_live_fallback: Sequence[Candidate] = ()) -> Route:
    """Pick the repair target. Never silently drops: no eligible target => ``page``.

    ``episode_subject`` (T-5999, Recovery condition): the persona the episode is ABOUT (``subject_of`` its
    fault_id). That candidate is removed from the pool BEFORE every route kind (requested,
    other_harness, same_account, any_live): a seat never repairs itself. If nothing remains, the episode
    PAGES. ``None`` => no exclusion (the pre-T-5999 behaviour). ``subject`` is the page's subject line.

    ``any_live_fallback`` (T-5998, Recovery's B(i)): candidates consulted LAST, only when the normal pool
    produced no live target AND the evidence is NOT deferred; the first live one that is not the
    episode subject wins as ``any_live``. It never joins ``candidates``, so ``requested`` /
    ``other_harness`` / ``same_account`` are untouched and a deferred ticket never reaches it (B(ii) stays
    declined: no episode about Recovery's account routes to Recovery). See the route table in the module docstring."""
    refused: list[str] = []
    pool: list[Candidate] = list(candidates)
    if requested is not None and requested not in pool:
        pool.insert(0, requested)
    excluded = tuple(c.name for c in pool if episode_subject is not None and c.name == episode_subject)
    if excluded:
        pool = [c for c in pool if c.name != episode_subject]
        if requested is not None and requested.name == episode_subject:
            requested = None
    if evidence_deferred:
        keep: list[Candidate] = []
        for c in pool:
            if origin_account is not None and c.account == origin_account:
                keep.append(c)
            else:
                refused.append(c.name)
        pool = keep
    live = [c for c in pool if liveness(c)]
    if requested is not None and requested in live:
        return Route("same_account" if evidence_deferred else "requested", requested, tuple(refused), excluded)
    other = [c for c in live if c.harness != faulted_harness]
    if other:
        return Route("same_account" if evidence_deferred else "other_harness", other[0], tuple(refused), excluded)
    if live:
        return Route("same_account" if evidence_deferred else "any_live", live[0], tuple(refused), excluded)
    if not evidence_deferred:  # T-5998 B(i): never for deferred evidence (B(ii) declined)
        for c in any_live_fallback:
            if c.name == episode_subject and episode_subject is not None:
                excluded = excluded if c.name in excluded else (*excluded, c.name)
                continue
            if liveness(c):
                return Route("any_live", c, tuple(refused), excluded)
    body = (f"faulted_harness={faulted_harness} deferred={evidence_deferred} "
            f"refused_cross_account={sorted(refused)} candidates={[c.name for c in candidates]}")
    if excluded:
        body += f" excluded_subject={list(excluded)}"
    page(subject, body)
    return Route("page", None, tuple(refused), excluded)
