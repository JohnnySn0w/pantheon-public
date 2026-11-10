"""ORG-050 gate 3 data contract: the metadata-only credential describe report (T-4835).

Pure: no I/O, no installer imports. A producer (T-6129, owner side) writes the
report; the installer only parses and classifies it. The report never carries a
secret value: it says whether a declared credential ref is present, owned and
moded as declared, of what kind, and when it expires.

Statuses (per declared ref):
  resolved    entry present, available, owner_matches, mode_ok, kind matches, not expired
  unresolved  entry present but available/owner_matches/mode_ok false, or errors non-empty
  mismatch    entry kind differs from the manifest kind
  expired     expires_at is before the apply-time clock (not the report's checked_at)
  stale       the report is older than CREDENTIAL_REPORT_MAX_AGE_SECONDS (or from the future)
  undescribed no report configured, or a valid report with no entry for the ref
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

CREDENTIAL_REPORT_SCHEMA = "credential-describe-report/v1"

# A describe report is a point-in-time check. One hour spans a human approval cycle
# (producer, dry-run, review, --execute) and bounds how long a chmod/rotation drift
# can go unseen during a session.
CREDENTIAL_REPORT_MAX_AGE_SECONDS = 3600
# A checked_at further in the future than this is untrustworthy clock evidence: stale.
CREDENTIAL_REPORT_FUTURE_SKEW_SECONDS = 300

STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVED = "unresolved"
STATUS_MISMATCH = "mismatch"
STATUS_EXPIRED = "expired"
STATUS_STALE = "stale"
STATUS_UNDESCRIBED = "undescribed"
REFUSING_STATUSES = frozenset(
    {STATUS_UNRESOLVED, STATUS_MISMATCH, STATUS_EXPIRED, STATUS_STALE}
)

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# Same spirit as the broker's diagnostic validation: short, plain, value-free text.
_DIAGNOSTIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,79}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_KIND_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_REPORT_KEYS = frozenset({"schema", "checked_at", "entries"})
_ENTRY_REQUIRED = frozenset(
    {"credential_id", "available", "owner_matches", "mode_ok", "kind", "errors"}
)
_ENTRY_KEYS = _ENTRY_REQUIRED | {"expires_at"}
_MAX_ENTRIES = 1024
_MAX_ERRORS = 16


class ReportInvalid(ValueError):
    """The report is malformed. Messages name a field path only, never a value."""


@dataclass(frozen=True)
class CredentialDescribeEntry:
    credential_id: str
    available: bool
    owner_matches: bool
    mode_ok: bool
    kind: str
    expires_at: str | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class CredentialDescribeReport:
    checked_at: str
    entries: tuple[CredentialDescribeEntry, ...]
    sha256: str

    def entry(self, credential_id: str) -> CredentialDescribeEntry | None:
        for item in self.entries:
            if item.credential_id == credential_id:
                return item
        return None


def _timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ReportInvalid(f"{where}: not a UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ReportInvalid(f"{where}: not a UTC timestamp") from None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def load_report(raw: bytes) -> CredentialDescribeReport:
    """Strictly parse a report. Unknown or malformed anything raises ReportInvalid."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ReportInvalid("report: not bytes")
    try:
        document = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReportInvalid("report: not valid JSON") from None
    if not isinstance(document, dict):
        raise ReportInvalid("report: not an object")
    if set(document) != _REPORT_KEYS:
        raise ReportInvalid("report: unexpected or missing keys")
    if document["schema"] != CREDENTIAL_REPORT_SCHEMA:
        raise ReportInvalid("report.schema: unsupported")
    _timestamp(document["checked_at"], "report.checked_at")
    entries_raw = document["entries"]
    if not isinstance(entries_raw, list) or len(entries_raw) > _MAX_ENTRIES:
        raise ReportInvalid("report.entries: not a bounded list")
    entries: list[CredentialDescribeEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(entries_raw):
        where = f"report.entries[{index}]"
        if not isinstance(item, dict):
            raise ReportInvalid(f"{where}: not an object")
        keys = set(item)
        if not _ENTRY_REQUIRED <= keys <= _ENTRY_KEYS:
            raise ReportInvalid(f"{where}: unexpected or missing keys")
        credential_id = item["credential_id"]
        if not isinstance(credential_id, str) or not _ID_RE.fullmatch(credential_id) or ".." in credential_id:
            raise ReportInvalid(f"{where}.credential_id: invalid")
        if credential_id in seen:
            raise ReportInvalid(f"{where}.credential_id: duplicate")
        seen.add(credential_id)
        for flag in ("available", "owner_matches", "mode_ok"):
            if not isinstance(item[flag], bool):
                raise ReportInvalid(f"{where}.{flag}: not a bool")
        kind = item["kind"]
        if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
            raise ReportInvalid(f"{where}.kind: invalid")
        expires_at = item.get("expires_at")
        if expires_at is not None:
            _timestamp(expires_at, f"{where}.expires_at")
        errors = item["errors"]
        if not isinstance(errors, list) or len(errors) > _MAX_ERRORS:
            raise ReportInvalid(f"{where}.errors: not a bounded list")
        for error in errors:
            if not isinstance(error, str) or not _DIAGNOSTIC_RE.fullmatch(error):
                raise ReportInvalid(f"{where}.errors: unsafe diagnostic text")
        entries.append(
            CredentialDescribeEntry(
                credential_id=credential_id,
                available=item["available"],
                owner_matches=item["owner_matches"],
                mode_ok=item["mode_ok"],
                kind=kind,
                expires_at=expires_at,
                errors=tuple(errors),
            )
        )
    return CredentialDescribeReport(
        checked_at=document["checked_at"],
        entries=tuple(entries),
        sha256=hashlib.sha256(_canonical_bytes(document)).hexdigest(),
    )


def classify_ref(
    report: CredentialDescribeReport | None,
    credential_id: str,
    manifest_kind: str,
    now: datetime,
) -> str:
    if report is None:
        return STATUS_UNDESCRIBED
    age = (now - _timestamp(report.checked_at, "report.checked_at")).total_seconds()
    if age > CREDENTIAL_REPORT_MAX_AGE_SECONDS or age < -CREDENTIAL_REPORT_FUTURE_SKEW_SECONDS:
        return STATUS_STALE
    entry = report.entry(credential_id)
    if entry is None:
        return STATUS_UNDESCRIBED
    if not (entry.available and entry.owner_matches and entry.mode_ok) or entry.errors:
        return STATUS_UNRESOLVED
    if entry.kind != manifest_kind:
        return STATUS_MISMATCH
    # judged against the apply-time clock: a credential that expired after the check
    # but before apply must not pass on the report's say-so
    if entry.expires_at is not None and _timestamp(entry.expires_at, "expires_at") < now:
        return STATUS_EXPIRED
    return STATUS_RESOLVED


def classify_refs(
    report: CredentialDescribeReport | None,
    refs: Sequence[tuple[str, str]],
    now: datetime,
) -> tuple[Mapping[str, Any], ...]:
    """Resolution entries ``{ref_id, status, checked_at, report_sha256}`` for
    ``refs`` (a sequence of ``(credential_id, manifest_kind)``)."""
    return tuple(
        {
            "ref_id": credential_id,
            "status": classify_ref(report, credential_id, kind, now),
            "checked_at": report.checked_at if report is not None else None,
            "report_sha256": report.sha256 if report is not None else None,
        }
        for credential_id, kind in refs
    )
