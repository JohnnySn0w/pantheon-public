#!/usr/bin/env python3
"""tools/seat_context_bundle.py — seed a SELECTED set of HO memory pins into a seat's
own memory store, provenance-preserving and idempotent (T-4065, ORG-075 BT1a / E2 (a)).

This is the seat context-bundle tool: it lets a seat (mason / artisan) hold the HO
context an HO-level task needs (ORG-075 D2), so mason/artisan become real HO-handoff
targets. Two-sided transport, NO cross-account writes, NO sudo (ORG-075 D15):

  export  (runs AS ORCHESTRATOR)      reads a source memory dir + a SELECTION (a named
                               slice from a slice file, or explicit --pin names —
                               never copy-everything, T-2425 precision) and writes a
                               bundle dir: manifest.json + a verbatim copy of each
                               selected pin. Pure read+copy; provenance is stamped
                               at import.

  import  (runs AS THE SEAT)   reads the bundle and seeds each pin into the seat's
                               OWN memory dir (--into), stamping provenance
                               frontmatter (seeded_by / seeded_from / seeded_at /
                               seed_sha256) and a self-describing edit-detection hash
                               (seed_applied_sha256). Per-pin idempotence + safety
                               state machine (never overwrites a locally-edited or a
                               seat-authored pin). Refreshes a tool-managed
                               delimited section in MEMORY.md.

Never sudos, never writes another account's home. `import` is defensive
create-if-absent for the seat's own memory dir (mode 0700, seat-user owner); it does
NOT claim provisioning ownership (persona_installer has no mkdir seam; the dir is
harness-created).

  # as orchestrator
  python3 tools/seat_context_bundle.py export --slice engineering_orchestrator \\
      --slice-file rulebook/seat-context/slices.yaml \\
      --source ~/.claude/projects/-home-claude/memory \\
      --out /var/lib/pantheon/context-bundles/mason
  # as the seat
  python3 tools/seat_context_bundle.py import \\
      --bundle /var/lib/pantheon/context-bundles/mason \\
      --into ~/.claude/projects/-home-suborch1-pantheon-config/memory

Chain of justification (T-4065, ORG-075 governing ADR): serves the ARC-INTENT
(seats hold selected HO context → real HO-handoff targets); T-2425 (precision:
named-slice/explicit-pin selection, no copy-all); ORG-075 D15 (two-sided, no sudo,
no cross-account write); P1 (correctness: the never-overwrite conflict/collision
invariants + fail-safe defensive create); P4 (provenance fidelity: preserve source
`verified` verbatim, record seed provenance, never fabricate a fresh verified date).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml  # idiomatic in this tree (56+ tools); not a new dependency.

# Reuse the ORG-035 baseline frontmatter parser so this tool never drifts from
# memory_lint / memory_index on what a pin's frontmatter means (same bootstrap shim
# they use: work both as `python3 tools/seat_context_bundle.py` and as
# `tools.seat_context_bundle`).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import memory_lint as ml  # noqa: E402

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Provenance fields this tool STAMPS on a seeded pin (top-level frontmatter keys).
SEEDED_BY = "seeded_by"
SEEDED_FROM = "seeded_from"
SEEDED_AT = "seeded_at"
SEED_SHA256 = "seed_sha256"          # hash of the exported SOURCE content
SEED_APPLIED_SHA256 = "seed_applied_sha256"  # hash of the seat copy's post-seed content
# Presence of these two marks a pin as "one WE seeded" (vs seat-authored).
SEEDED_MARKERS = (SEED_SHA256, SEED_APPLIED_SHA256)

# MEMORY.md tool-managed delimited section (constraint 6): a SEPARATE region, NOT
# memory_index.py's AUTOGEN backstop (that region is semantically "unfiled pins";
# putting seeded pins there would mis-flag them — the backstop's job is drift-catch).
INDEX_BEGIN = "<!-- BEGIN seeded-context -->"
INDEX_END = "<!-- END seeded-context -->"
INDEX_NOTE = "<!-- managed by tools/seat_context_bundle.py (T-4065) — do not hand-edit inside this block -->"


# ---------------------------------------------------------------------------
# Hashing (content-addressing for source + edit detection).
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_applied_line(text: str) -> str:
    """Drop the tool's own `seed_applied_sha256:` line FROM THE FRONTMATTER ONLY.

    The edit-detection hash must be computed over content that EXCLUDES the hash
    field itself (it cannot commit to its own value). But it must exclude ONLY the
    tool-written frontmatter line — NEVER a body line that merely happens to start
    with the same token. If the strip reached the body, a seat edit that appended a
    `seed_applied_sha256:`-prefixed body line would be cancelled out symmetrically
    (stamped and recomputed identically) → the edit would go undetected → a source
    change would silently OVERWRITE the seat's local edit, breaking the exact
    never-overwrite invariant this tool exists to guarantee (P1). So we split the
    frontmatter off, drop the field only there, and reconstruct — the body is never
    subject to the strip.
    """
    block, body = ml.split_frontmatter(text)
    if block is None:
        return text
    kept = [ln for ln in block.splitlines()
            if not ln.lstrip().startswith(f"{SEED_APPLIED_SHA256}:")]
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def applied_hash(text: str) -> str:
    """The 'unedited-since-seed' checksum of a seat pin's on-disk text."""
    return sha256_text(_strip_applied_line(text))


# ---------------------------------------------------------------------------
# Slice file (schema is ours; content is orchestrator's — do NOT ship slices.yaml).
# Schema: slices: {<name>: {pins: [<filename>...], description?: str}}
# ---------------------------------------------------------------------------


class SliceError(ValueError):
    """A slice file that violates the schema."""


def parse_slice_file(text: str) -> dict:
    """Parse + validate a slice file. Returns {name: {"pins": [...], "description": ...}}.

    Rejects: unknown top-level keys, a non-mapping `slices`, a slice that is not a
    mapping, a missing/non-list `pins`, non-string pin entries, and unknown per-slice
    keys. Strict-by-construction so a malformed slice fails loud, never silently
    seeds the wrong set (P1 correctness; T-2425 precision).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SliceError(f"invalid YAML: {exc}") from exc
    if data is None:
        raise SliceError("empty slice file")
    if not isinstance(data, dict):
        raise SliceError("top level must be a mapping")
    unknown_top = set(data) - {"slices"}
    if unknown_top:
        raise SliceError(f"unknown top-level key(s): {', '.join(sorted(unknown_top))}")
    if "slices" not in data:
        raise SliceError("missing top-level `slices`")
    slices = data["slices"]
    if not isinstance(slices, dict):
        raise SliceError("`slices` must be a mapping of name -> {pins, description?}")
    out: dict = {}
    for name, spec in slices.items():
        if not isinstance(name, str):
            raise SliceError(f"slice name must be a string: {name!r}")
        if not isinstance(spec, dict):
            raise SliceError(f"slice {name!r} must be a mapping with a `pins` list")
        unknown = set(spec) - {"pins", "description"}
        if unknown:
            raise SliceError(
                f"slice {name!r} has unknown key(s): {', '.join(sorted(unknown))}")
        pins = spec.get("pins")
        if not isinstance(pins, list):
            raise SliceError(f"slice {name!r}: `pins` must be a list")
        for p in pins:
            if not isinstance(p, str):
                raise SliceError(f"slice {name!r}: pin entries must be strings: {p!r}")
        desc = spec.get("description")
        if desc is not None and not isinstance(desc, str):
            raise SliceError(f"slice {name!r}: `description` must be a string")
        out[name] = {"pins": list(pins), "description": desc}
    return out


def load_slice_file(path: Path) -> dict:
    return parse_slice_file(path.read_text(encoding="utf-8"))


def resolve_selection(slice_file: Path | None, slice_name: str | None,
                      pins: list[str] | None) -> list[str]:
    """Union of a named slice's pins and any explicit --pin names (order-stable, deduped).

    Raises if the selection is empty (T-2425: no copy-everything mode) or if a named
    slice is requested with no slice file (or an absent slice name).
    """
    selected: list[str] = []
    if slice_name:
        if slice_file is None:
            raise SliceError("--slice requires --slice-file")
        parsed = load_slice_file(slice_file)
        if slice_name not in parsed:
            raise SliceError(
                f"slice {slice_name!r} not in {slice_file} "
                f"(have: {', '.join(sorted(parsed)) or '<none>'})")
        selected.extend(parsed[slice_name]["pins"])
    if pins:
        selected.extend(pins)
    if not selected:
        raise SliceError(
            "empty selection — pass --slice <name> and/or --pin <file> "
            "(no copy-everything mode, T-2425)")
    # De-dupe, preserving first-seen order.
    seen: set[str] = set()
    deduped: list[str] = []
    for f in selected:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


# ---------------------------------------------------------------------------
# export — runs AS ORCHESTRATOR. Pure read + copy; provenance stamped at import.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_bare_basename(filename: str) -> str:
    """Reject any pin filename that is not a plain basename (traversal / absolute).

    `into / filename` and `out / filename` must never resolve outside their target
    dir. A crafted or corrupt manifest/selection entry like `../ESCAPED.md` or an
    absolute path would otherwise escape (`Path("/x") ` wins the join; `..` climbs
    out). Bundles live in the group-shared setgid /var/lib drop dir, so the
    "writes ONLY under --into/--out" invariant is enforced in code, not assumed
    (P1 fail-safe containment; ORG-075 D15 no cross-account write). Fail loud.
    """
    if (not filename or os.path.isabs(filename)
            or Path(filename).name != filename):
        raise ValueError(
            f"unsafe pin filename (must be a bare basename, no path / no absolute): "
            f"{filename!r}")
    return filename


def run_export(source: Path, out: Path, selection: list[str], slice_name: str | None,
               exported_by: str) -> dict:
    """Write bundle-dir/{manifest.json, <pin copies>}. Returns the manifest dict.

    Raises FileNotFoundError listing any selected pin missing from `source`.
    """
    if not source.is_dir():
        raise NotADirectoryError(f"source memory dir not found: {source}")
    for filename in selection:
        _require_bare_basename(filename)  # containment: no escape from --source/--out
    missing = [f for f in selection if not (source / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"selected pin(s) not in {source}: {', '.join(missing)}")

    out.mkdir(parents=True, exist_ok=True)
    pins_manifest = []
    for filename in selection:
        src_text = (source / filename).read_text(encoding="utf-8")
        _block, _body = ml.split_frontmatter(src_text)
        if not _body.strip():
            # An empty-body pin would seed a pin that FAILS memory_lint (empty body
            # = ERROR), violating the lint-clean goal. Refuse at the source (P1).
            raise ValueError(
                f"refusing to export empty-body source pin (would seed a "
                f"memory_lint ERROR): {filename}")
        (out / filename).write_text(src_text, encoding="utf-8")  # verbatim copy
        pins_manifest.append({
            "filename": filename,
            "source_sha256": sha256_text(src_text),
        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "exported_by": exported_by,
        "exported_at": _now_iso(),
        "slice": slice_name,
        "pins": pins_manifest,
    }
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# import — runs AS THE SEAT. Provenance stamping + per-pin state machine.
# ---------------------------------------------------------------------------


def _split_doc(text: str) -> tuple[str | None, str]:
    """(frontmatter_block, body) — reuse memory_lint's splitter (None if no block)."""
    return ml.split_frontmatter(text)


def stamp_seeded(source_text: str, *, exported_by: str, seeded_from: str,
                 seeded_at: str, seed_sha256: str) -> str:
    """Return the seeded pin text: source content + PREPENDED seed provenance.

    Seed fields are prepended as TOP-LEVEL frontmatter keys (above any existing
    content, including a trailing nested `metadata:` block, so they can't be
    misparsed as nested). Source frontmatter + body are preserved verbatim,
    including the source `verified` (P4: never restamp a verified the seat didn't
    earn). `seed_applied_sha256` is computed over the post-seed text with its own
    line excluded, then inserted — so a later re-read can recompute + compare.
    """
    block, body = _split_doc(source_text)
    seed_lines = [
        f"{SEEDED_BY}: {exported_by}",
        f"{SEEDED_FROM}: {seeded_from}",
        f"{SEEDED_AT}: {seeded_at}",
        f"{SEED_SHA256}: {seed_sha256}",
    ]
    if block is None:
        # No source frontmatter: seed fields become the whole frontmatter block.
        new_block = "\n".join(seed_lines)
        body_out = source_text  # treat entire source as body
    else:
        new_block = "\n".join(seed_lines) + "\n" + block
        body_out = body
    text_without_applied = f"---\n{new_block}\n---\n{body_out}"
    applied = sha256_text(text_without_applied)
    # Insert the applied-hash line as the first frontmatter line.
    final_block = f"{SEED_APPLIED_SHA256}: {applied}\n" + new_block
    return f"---\n{final_block}\n---\n{body_out}"


def _read_seeded_fields(text: str) -> dict:
    """Frontmatter fields of an on-disk pin (flat), via the shared parser."""
    block, _body = _split_doc(text)
    if block is None:
        return {}
    return ml.parse_frontmatter(block)


def is_seeded(fields: dict) -> bool:
    """True iff a pin carries our seed markers (i.e. WE wrote it, not the seat)."""
    return all(fields.get(m) for m in SEEDED_MARKERS)


# Per-pin state-machine outcomes.
SEEDED = "seeded"        # target absent → written
UPDATED = "updated"      # source changed + target unedited → rewritten
UNCHANGED = "unchanged"  # source same + target unedited → no-op
CONFLICT = "conflict"    # target edited since seed → never overwrite
COLLISION = "collision"  # seat-authored same-name pin → never overwrite

# Outcomes that mean the seat store now HAS this pin as a seeded pin (for indexing).
_INDEXABLE = {SEEDED, UPDATED, UNCHANGED, CONFLICT}


class PinResult:
    __slots__ = ("filename", "status", "detail")

    def __init__(self, filename: str, status: str, detail: str = ""):
        self.filename = filename
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PinResult({self.filename!r}, {self.status!r}, {self.detail!r})"


def decide_pin(target_path: Path, bundle_source_sha256: str) -> tuple[str, str]:
    """The per-pin state machine (pure decision — no writes). Returns (status, detail).

    - target absent                         → SEEDED
    - target present, NOT seeded by us       → COLLISION   (seat-authored; never touch)
    - target present, seeded, edited since seed → CONFLICT (never overwrite)
    - target present, seeded, unedited:
        - recorded seed_sha256 == bundle    → UNCHANGED    (no-op)
        - else (source changed)             → UPDATED
    """
    if not target_path.exists():
        return SEEDED, "target absent"
    text = target_path.read_text(encoding="utf-8")
    fields = _read_seeded_fields(text)
    if not is_seeded(fields):
        return COLLISION, "seat-authored pin with same filename (no seed provenance)"
    recorded_applied = fields.get(SEED_APPLIED_SHA256, "")
    if applied_hash(text) != recorded_applied:
        return CONFLICT, "seat copy edited since seed (hash mismatch)"
    if fields.get(SEED_SHA256, "") == bundle_source_sha256:
        return UNCHANGED, "source unchanged, seat copy unedited"
    return UPDATED, "source changed, seat copy unedited"


def _ensure_into_dir(into: Path) -> None:
    """Defensive create-if-absent of the seat's OWN memory dir (mode 0700).

    Does NOT claim provisioning ownership — if a future owner (persona_installer)
    provisions it, this simply no-ops. Never writes cross-account (the caller runs
    as the seat user; this only touches the seat's own home). Justifies-via P1
    (fail-safe) + ORG-075 D15.
    """
    if not into.exists():
        into.mkdir(parents=True, mode=0o700)


def run_import(bundle: Path, into: Path) -> list[PinResult]:
    """Seed the bundle's pins into `into` per the state machine, then refresh index.

    Writes ONLY into `into` (the seat's own dir). Never overwrites a CONFLICT or
    COLLISION pin. Returns per-pin results (in manifest order).
    """
    manifest_path = bundle / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} in bundle: {bundle}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported manifest schema_version {manifest.get('schema_version')!r} "
            f"(this tool speaks {SCHEMA_VERSION})")
    exported_by = manifest.get("exported_by", "unknown")

    # Containment: validate EVERY filename before touching the store, so a crafted /
    # corrupt manifest is rejected loud with nothing written (P1; ORG-075 D15).
    entries = manifest.get("pins", [])
    for entry in entries:
        _require_bare_basename(entry["filename"])

    _ensure_into_dir(into)
    results: list[PinResult] = []
    for entry in entries:
        filename = entry["filename"]
        source_sha256 = entry["source_sha256"]
        target = into / filename
        status, detail = decide_pin(target, source_sha256)
        if status in (SEEDED, UPDATED):
            bundle_pin = bundle / filename
            if not bundle_pin.is_file():
                results.append(PinResult(
                    filename, CONFLICT,
                    "manifest lists pin but bundle copy is missing"))
                continue
            src_text = bundle_pin.read_text(encoding="utf-8")
            seeded_text = stamp_seeded(
                src_text, exported_by=exported_by, seeded_from=filename,
                seeded_at=_now_iso(), seed_sha256=source_sha256)
            target.write_text(seeded_text, encoding="utf-8")
        results.append(PinResult(filename, status, detail))

    refresh_index(into)
    return results


# ---------------------------------------------------------------------------
# MEMORY.md tool-managed delimited section (constraint 6).
# ---------------------------------------------------------------------------


def _scan_seeded_pins(into: Path) -> list[dict]:
    """[{filename, name, seeded_from, seeded_by}] for every seeded pin in `into`."""
    out: list[dict] = []
    for path in sorted(into.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        fields = _read_seeded_fields(path.read_text(encoding="utf-8"))
        if not is_seeded(fields):
            continue
        out.append({
            "filename": path.name,
            "name": fields.get("name") or path.stem,
            "seeded_from": fields.get(SEEDED_FROM, path.name),
            "seeded_by": fields.get(SEEDED_BY, "unknown"),
        })
    return out


def render_index_block(seeded: list[dict]) -> str:
    """The delimited section text (markers inclusive), one pointer line per pin."""
    lines = [INDEX_BEGIN, INDEX_NOTE]
    if not seeded:
        lines.append("<!-- no seeded pins -->")
    else:
        for pin in seeded:
            lines.append(
                f"- [{pin['name']}]({pin['filename']}) "
                f"— seeded from `{pin['seeded_from']}` by {pin['seeded_by']}")
    lines.append(INDEX_END)
    return "\n".join(lines) + "\n"


def splice_index_block(current: str, block: str) -> str:
    """Replace the delimited section in `current` with `block` (idempotent).

    If the markers are absent, append the block at the end. Everything OUTSIDE the
    markers — the seat's hand-written MEMORY.md — is preserved byte-for-byte.
    """
    start = current.find(INDEX_BEGIN)
    if start == -1:
        sep = "" if current == "" else ("\n" if current.endswith("\n") else "\n\n")
        return current + sep + block
    end = current.find(INDEX_END, start)
    if end == -1:
        # Corrupt (start without end): replace from start to EOF with the fresh block.
        return current[:start] + block
    end += len(INDEX_END)
    # Swallow a single trailing newline right after the end marker so re-runs are stable.
    tail = current[end:]
    if tail.startswith("\n"):
        tail = tail[1:]
    return current[:start] + block.rstrip("\n") + "\n" + tail


def refresh_index(into: Path) -> None:
    """Rewrite the seeded-context section in <into>/MEMORY.md idempotently."""
    seeded = _scan_seeded_pins(into)
    index = into / "MEMORY.md"
    current = index.read_text(encoding="utf-8") if index.exists() else ""
    block = render_index_block(seeded)
    new_text = splice_index_block(current, block)
    if new_text != current:
        index.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    slice_file = Path(args.slice_file) if args.slice_file else None
    try:
        selection = resolve_selection(slice_file, args.slice, args.pin)
    except SliceError as exc:
        print(f"seat_context_bundle export: {exc}", file=sys.stderr)
        return 2
    exported_by = args.as_ or getpass.getuser()
    try:
        manifest = run_export(
            Path(args.source), Path(args.out), selection, args.slice, exported_by)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"seat_context_bundle export: {exc}", file=sys.stderr)
        return 2
    print(f"exported {len(manifest['pins'])} pin(s) as {exported_by} "
          f"(slice={args.slice!r}) → {args.out}", file=sys.stderr)
    for p in manifest["pins"]:
        print(f"  {p['filename']}  {p['source_sha256'][:12]}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        results = run_import(Path(args.bundle), Path(args.into))
    except (FileNotFoundError, ValueError) as exc:
        print(f"seat_context_bundle import: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        print(f"  {r.status.upper():9} {r.filename}"
              + (f"  ({r.detail})" if r.detail else ""))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    n_flag = counts.get(CONFLICT, 0) + counts.get(COLLISION, 0)
    print(f"seat_context_bundle import: {len(results)} pin(s) [{summary}] → {args.into}",
          file=sys.stderr)
    if n_flag:
        print(f"seat_context_bundle import: {n_flag} pin(s) NOT seeded "
              f"(conflict/collision — never overwritten; review above)", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="(as orchestrator) read source pins → write a bundle dir")
    ex.add_argument("--source", required=True, help="source memory dir (orchestrator's)")
    ex.add_argument("--out", required=True, help="bundle dir to write")
    ex.add_argument("--slice", default=None, help="named slice from --slice-file")
    ex.add_argument("--slice-file", default=None, help="YAML slice file (schema-validated)")
    ex.add_argument("--pin", action="append", default=None,
                    help="explicit pin filename (repeatable); unions with --slice")
    ex.add_argument("--as", dest="as_", default=None,
                    help="exporter identity for the manifest (default: current user)")
    ex.set_defaults(func=_cmd_export)

    im = sub.add_parser("import", help="(as the seat) seed a bundle into --into")
    im.add_argument("--bundle", required=True, help="bundle dir written by export")
    im.add_argument("--into", required=True, help="the seat's OWN memory dir")
    im.set_defaults(func=_cmd_import)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
