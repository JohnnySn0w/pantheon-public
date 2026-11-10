"""Ticket-frontmatter field registry, read at run time from ORG-021 (T-4751 S1).

ORG-021 §"Frontmatter field registry (machine-read)" carries one fenced YAML
block listing every ticket frontmatter field, its kind, its allowed values
(enums only), and whether it is `active` or `legacy`. This module loads that
block and checks frontmatter WRITES against it, so the ADR (not a copy in
code) is the authority on what a ticket may carry — "documentation IS code"
(the operator TG 11484/11485).

Contract:

* `validate_write(old_fields, new_fields)` checks ONLY what the write ADDS or
  CHANGES relative to `old_fields` (None = a brand-new ticket). Values already
  on disk are never re-validated; removing a key is always allowed. Findings:
    - `unknown-field`   a field the registry does not list;
    - `bad-value`       an enum/int/bool value outside the entry's `values`
                        (list-kind fields carry no value check — labels are
                        open; a bool written `false` against `values: [true]`
                        is a removal, not a value);
    - `legacy-write`    a NEW write of a `status: legacy` field.
* `enforce_write(...)` applies the mode from env
  `PANTHEON_FRONTMATTER_REGISTRY_MODE` (warn | refuse | off; default refuse):
  warn prints one stderr line per finding and proceeds; refuse raises
  `RegistryRefusal` (callers invoke it BEFORE any file write / git step); off
  skips. S2 (T-4762) flipped the default from warn to refuse.
  System-written operational fields (SYSTEM_WARN_FIELDS: the auto_delivered
  stamp, delivered_to, auto_declined_by) stay WARN-only even in refuse mode: a registry
  mis-edit must never stop fleet-wide work intake (P7). The drift test and CI
  catch such a mis-edit instead (T-4762).
* Fail-OPEN: a missing ADR, a missing section/block, or unparseable YAML emits
  ONE loud stderr WARN per process and validation is skipped — even in refuse
  mode. A broken registry edit must never freeze ticketing (ORG-021 §registry).
* The parsed registry is cached per process (per path).

There is deliberately NO old-fields store here (T-4760 removed the per-id
snapshot store S1 kept): every writer passes the fields IT parsed as
`old_fields`, so a same-id ticket read from another path, a file rewritten by
another writer after this one's read, or a refused/never-written build can
never move what "old" means for an unrelated write.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "architecture" / "adr"
ADR_GLOB = "ORG-021-*.md"
SECTION_HEADING = "## Frontmatter field registry (machine-read)"

MODE_ENV = "PANTHEON_FRONTMATTER_REGISTRY_MODE"
# Test seam / operator override: point the loader at a different ADR file.
PATH_ENV = "PANTHEON_FRONTMATTER_REGISTRY_PATH"
MODES = ("warn", "refuse", "off")
DEFAULT_MODE = "refuse"
# T-4762: written by the inbox consumer / claim path, not by a human. A refusal
# here would stop work intake fleet-wide, so findings on these are WARN-only in
# every mode. test_frontmatter_registry pins them to the real ADR registry.
# `state` is deliberately NOT in this set: it is core, and a registry edit that
# drops it is loud (CI + the drift test catch it), so it stays refuse-checked.
SYSTEM_WARN_FIELDS = frozenset({
    "auto_delivered", "auto_delivered_at", "delivered_to",
    "auto_declined_by",  # inbox_consumer._clear_auto_delivered decline record (T-5049)
})

PREFIX = "[frontmatter-registry]"
AUTHORING_HINT = (
    "add or amend the entry in ORG-021 §Frontmatter field registry "
    "(architecture/adr/ORG-021-ticket-state-taxonomy-compress-direction.md) "
    "in the same PR as the code that writes it"
)


class RegistryRefusal(Exception):
    """Raised by enforce_write in refuse mode, before any side effect."""


@dataclass(frozen=True)
class FieldEntry:
    field: str
    kind: str
    values: frozenset | None  # normalized string forms; None = no value check
    required: Any
    authority: str
    status: str  # active | legacy


@dataclass(frozen=True)
class Registry:
    source: Path
    version: Any
    fields: dict  # field name -> FieldEntry

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def get(self, name: str) -> FieldEntry | None:
        return self.fields.get(name)


@dataclass(frozen=True)
class Finding:
    kind: str  # unknown-field | bad-value | legacy-write
    field: str
    value: Any = None
    allowed: tuple = field(default_factory=tuple)

    def message(self) -> str:
        if self.kind == "unknown-field":
            return f"field `{self.field}` is not in the ORG-021 registry"
        if self.kind == "bad-value":
            return (f"`{self.field}: {self.value}` is not an allowed value "
                    f"(registry allows: {', '.join(self.allowed)})")
        if self.kind == "legacy-write":
            return (f"`{self.field}` is a legacy field (round-trips on read, "
                    "must not be newly written)")
        return f"{self.kind}: {self.field}"


class RegistryUnavailable(Exception):
    """Internal: the registry could not be located or parsed."""


# ---------------------------------------------------------------------------
# Loading (cached per process, fail-open)
# ---------------------------------------------------------------------------

_CACHE: dict = {}  # resolved path str -> Registry | RegistryUnavailable
# ASCII-only: `\d` / str.isdigit() also match non-ASCII digits (T-4760).
_ASCII_INT_RE = re.compile(r"-?[0-9]+")
_WARNED_UNAVAILABLE: set = set()


def norm_value(val: Any) -> Any:
    """Normalize a frontmatter value to its on-disk string form.

    ticket.py keeps scalars as strings (`auto_delivered: true` parses to
    "true"); the YAML registry yields bool/int. Compare both as strings.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (list, tuple)):
        return tuple(norm_value(v) for v in val)
    return str(val).strip()


def default_registry_path() -> Path | None:
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
    matches = sorted(ADR_DIR.glob(ADR_GLOB))
    return matches[0] if matches else None


def _extract_yaml_block(text: str) -> str:
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == SECTION_HEADING)
    except StopIteration:
        raise RegistryUnavailable(f"section {SECTION_HEADING!r} not found") from None
    in_block = False
    block: list[str] = []
    for ln in lines[start + 1:]:
        if not in_block:
            if re.match(r"^## ", ln):
                break  # next section reached with no yaml block
            if re.match(r"^```ya?ml\s*$", ln.strip()):
                in_block = True
            continue
        if ln.strip() == "```":
            return "\n".join(block) + "\n"
        block.append(ln)
    if in_block:
        raise RegistryUnavailable("yaml block in registry section is not closed")
    raise RegistryUnavailable("no fenced ```yaml block in registry section")


def _parse_registry(path: Path) -> Registry:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryUnavailable(f"cannot read {path}: {exc}") from None
    block = _extract_yaml_block(text)
    try:
        import yaml  # pyyaml: existing dependency
        data = yaml.safe_load(block)
    except Exception as exc:  # noqa: BLE001 — any parse failure fails open
        raise RegistryUnavailable(f"yaml parse error: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
        raise RegistryUnavailable("yaml block has no `fields:` list")
    entries: dict = {}
    for raw in data["fields"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("field"), str):
            raise RegistryUnavailable(f"malformed registry entry: {raw!r}")
        name = raw["field"]
        if name in entries:
            raise RegistryUnavailable(f"duplicate registry entry: {name}")
        status = raw.get("status", "active")
        if status not in ("active", "legacy"):
            raise RegistryUnavailable(f"entry {name}: unknown status {status!r}")
        kind = raw.get("kind", "scalar")
        if not isinstance(kind, str) or not kind:
            raise RegistryUnavailable(f"entry {name}: `kind` is not a non-empty string")
        values = raw.get("values")
        if values is not None:
            if not isinstance(values, list) or not values:
                raise RegistryUnavailable(f"entry {name}: `values` is not a non-empty list")
            # T-4751 r1 NIT-1: a null / nested / float value would crash the
            # comparison + sort later; reject it here so it fails OPEN instead.
            bad = [v for v in values
                   if v is None or not isinstance(v, (str, int, bool)) or v == ""]
            if bad:
                raise RegistryUnavailable(
                    f"entry {name}: `values` holds null or non-scalar item(s) {bad!r}")
            # T-4760 (r2 NIT-5): an int-kind value must be an int or an ASCII
            # digit string. str.isdigit() accepts "²" / "٣", which int() then
            # rejects or mis-reads; refuse them here so the registry fails OPEN.
            if kind == "int":
                bad_int = [v for v in values
                           if isinstance(v, bool)
                           or not (isinstance(v, int) or _ASCII_INT_RE.fullmatch(str(v)))]
                if bad_int:
                    raise RegistryUnavailable(
                        f"entry {name}: int `values` holds non-integer item(s) {bad_int!r}")
            values = frozenset(norm_value(v) for v in values)
        entries[name] = FieldEntry(
            field=name,
            kind=kind,
            values=values,
            required=raw.get("required"),
            authority=str(raw.get("authority", "")),
            status=status,
        )
    return Registry(source=path, version=data.get("registry_version"), fields=entries)


def _warn_once(key: str, line: str) -> None:
    if key in _WARNED_UNAVAILABLE:
        return
    _WARNED_UNAVAILABLE.add(key)
    print(f"{PREFIX} WARN {line}", file=sys.stderr)


def _warn_unavailable(key: str, reason: str) -> None:
    reason = " ".join(str(reason).split())  # one line, even for multi-line yaml errors
    _warn_once(key, f"registry unavailable: {reason} — validation skipped")


def load_registry(path: Path | None = None) -> Registry | None:
    """Return the parsed registry, or None (after one loud WARN) if unavailable."""
    if path is None:
        path = default_registry_path()
    if path is None:
        _warn_unavailable("<none>", f"no {ADR_GLOB} under {ADR_DIR}")
        return None
    key = str(Path(path).resolve()) if Path(path).exists() else str(path)
    cached = _CACHE.get(key)
    if cached is None:
        try:
            cached = _parse_registry(Path(path))
        except RegistryUnavailable as exc:
            cached = exc
        _CACHE[key] = cached
    if isinstance(cached, RegistryUnavailable):
        _warn_unavailable(key, str(cached))
        return None
    return cached


def clear_cache() -> None:
    """Test helper: drop the per-process registry cache and WARN-once memory."""
    _CACHE.clear()
    _WARNED_UNAVAILABLE.clear()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _changed(old_fields: dict | None, key: str, new_val: Any) -> bool:
    if old_fields is None or key not in old_fields:
        return True
    return norm_value(old_fields[key]) != norm_value(new_val)


def check_fields(registry: Registry, old_fields: dict | None,
                 new_fields: dict) -> list[Finding]:
    findings: list[Finding] = []
    for key, val in new_fields.items():
        if not _changed(old_fields, key, val):
            continue
        entry = registry.get(key)
        if entry is None:
            findings.append(Finding("unknown-field", key, norm_value(val)))
            continue
        if entry.status == "legacy":
            findings.append(Finding("legacy-write", key, norm_value(val)))
            continue
        if entry.values is None or entry.kind == "list" or val is None:
            continue
        if isinstance(val, (list, tuple)):
            continue
        nv = norm_value(val)
        if entry.kind == "bool" and nv == "false" and "false" not in entry.values:
            continue  # bool false against values:[true] == removal semantics
        if nv not in entry.values:
            findings.append(Finding("bad-value", key, nv,
                                    tuple(sorted(entry.values, key=_sort_key))))
    return findings


def _sort_key(v: str):
    return (0, int(v), "") if _ASCII_INT_RE.fullmatch(v) else (1, 0, v)


def validate_write(old_fields: dict | None, new_fields: dict) -> list[Finding]:
    """Findings for what this write adds/changes. [] if registry unavailable."""
    registry = load_registry()
    if registry is None:
        return []
    return check_fields(registry, old_fields, new_fields)


def get_mode() -> str:
    raw = (os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    if raw in MODES:
        return raw
    _warn_once(f"mode:{raw}", f"{MODE_ENV}={raw!r} is not one of {MODES}; "
               f"using {DEFAULT_MODE}")
    return DEFAULT_MODE


def enforce_write(old_fields: dict | None, new_fields: dict, *,
                  ticket_id: str | None = None) -> list[Finding]:
    """Validate a frontmatter write and apply the configured mode.

    MUST be called before the write and before any git step. warn: prints one
    `[frontmatter-registry] WARN` line per finding, returns them. refuse:
    raises RegistryRefusal (SYSTEM_WARN_FIELDS findings only warn). off: returns [] without loading the registry.
    """
    mode = get_mode()
    if mode == "off":
        return []
    findings = validate_write(old_fields, new_fields)
    if not findings:
        return []
    who = ticket_id or str(new_fields.get("id") or "<ticket>")
    if mode == "refuse":
        blocking = [f for f in findings if f.field not in SYSTEM_WARN_FIELDS]
        for f in findings:
            if f.field in SYSTEM_WARN_FIELDS:
                print(f"{PREFIX} WARN {who}: {f.message()} (system field, never "
                      f"refused; {AUTHORING_HINT})", file=sys.stderr)
        if not blocking:
            return findings
        lines = "; ".join(f.message() for f in blocking)
        raise RegistryRefusal(
            f"{PREFIX} REFUSE {who}: {lines}. Nothing was written. To register "
            f"a field/value, {AUTHORING_HINT}."
        )
    for f in findings:
        print(f"{PREFIX} WARN {who}: {f.message()} ({AUTHORING_HINT})",
              file=sys.stderr)
    return findings
