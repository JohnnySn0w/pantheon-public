#!/usr/bin/env python3
"""Find (and with --execute remove) test-fixture residue in the REAL Chronicle and persona state.

T-6118 PR-0. Until the isolation fixes landed, test suites leaked fixture sessions into
``~/.local/state/pantheon/chronicle`` (Recovery: 477 of 513 records since 2026-10-27, bursts of ~47
per suite run) and fixture persona dirs into ``~/.local/state/pantheon/personas``. This tool
lists that residue, and removes it on an explicit ``--execute``.

Targets
  (a) Chronicle session records, in a fixture persona's chronicle dir, whose ``*.session.json``
      has ``handoff_file`` under ``/tmp/`` (a real session's handoff lives in the account home;
      a fixture's lives in a TemporaryDirectory). The whole archive unit goes: every
      ``<ts>-<uuid>.*`` file in the day dir, the matching ``index.md`` line(s) and the
      matching ``.claims/*.json`` claim(s).
  (a') Chronicle dirs that exist only as fixtures (``swap-acceptance-*``, ``bringup-acceptance-*``)
      and are not the running seat's own: removed whole.
  (a'') Any Chronicle persona dir other than the running account's seat: removed whole. Production
      session-end writers run as the target seat account and write that account's bound persona;
      cross-account drivers sudo into the target account before invoking them. If the running seat
      cannot be resolved this rule is refused rather than guessed.
  (b) ``personas/<fixture>`` dirs that do not belong to the running seat. REFUSED when the
      running seat cannot be resolved (no guessing which persona is real).

"Fixture persona": the names the test fixtures drive the controller with: ``recovery`` (the most
common), ``swap-acceptance-persona`` and ``bringup-acceptance-*``. A fixture name never
protects itself: ``recovery`` is a fixture on artisan and the REAL persona on Recovery's account, where
(b) skips it and (a) still removes only /tmp-handoff records.

Safety: operates only on the running account's roots (passwd home, never $HOME and never
another account's home; the CLI takes no path arguments). Symlinks are never followed and a
unit containing one is refused. Every path is re-checked to lie inside its root immediately
before deletion. Dry-run is the default. ``--execute`` first writes the full manifest to
``<state dir>/residue-cleanup/<UTC ts>.jsonl`` (a sibling of chronicle/, NOT inside it, so no
Chronicle reader can mistake it for an agent dir), then appends one ``deleted``/``failed``
line per target, plus the removed ``index.md`` lines verbatim so a deletion is auditable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _real_state_tripwire as tripwire  # noqa: E402

FIXTURE_PERSONA_EXACT = frozenset({"recovery", "swap-acceptance-persona"})
FIXTURE_PERSONA_PREFIXES = ("bringup-acceptance-",)
#: Names that can only ever be fixtures (never a real persona), removable as whole dirs.
FIXTURE_ONLY_EXACT = frozenset({"swap-acceptance-persona"})
FIXTURE_ONLY_PREFIXES = FIXTURE_PERSONA_PREFIXES

_SESSION_JSON = re.compile(r"^(?P<stem>.+)\.session\.json(?:\.\d+)?$")
_STEM = re.compile(r"^(?P<ts>.+?Z)-(?P<uuid>.+)$")


def is_fixture_persona(name: str) -> bool:
    return name in FIXTURE_PERSONA_EXACT or name.startswith(FIXTURE_PERSONA_PREFIXES)


def is_fixture_only(name: str) -> bool:
    return name in FIXTURE_ONLY_EXACT or name.startswith(FIXTURE_ONLY_PREFIXES)


class Refused(Exception):
    pass


@dataclass
class Target:
    kind: str  # "chronicle-record" | "chronicle-fixture-dir" | "chronicle-foreign-persona-dir" | "persona-dir"
    reason: str
    root: Path
    paths: list[Path]  # files (record) or the single dir
    size: int = 0
    index_file: Path | None = None
    index_uuid: str | None = None
    claims: list[Path] = field(default_factory=list)

    def manifest(self) -> dict:
        return {
            "kind": self.kind, "reason": self.reason, "size": self.size,
            "paths": [str(p) for p in self.paths], "claims": [str(c) for c in self.claims],
            "index": str(self.index_file) if self.index_file else None, "index_uuid": self.index_uuid,
        }


def _check_inside(root: Path, path: Path) -> None:
    root_real = Path(os.path.realpath(root))
    parent_real = Path(os.path.realpath(path.parent))
    if parent_real != root_real and root_real not in parent_real.parents:
        raise Refused(f"{path} escapes {root}")


def _lstat_no_symlink(path: Path) -> os.stat_result:
    st = os.lstat(path)
    if os.path.islink(path):
        raise Refused(f"{path} is a symlink")
    return st


def _tree_size(path: Path) -> int:
    """Total size of a dir tree; raises Refused on any symlink inside it."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in list(dirnames) + filenames:
            child = Path(dirpath) / name
            if child.is_symlink():
                raise Refused(f"{child} is a symlink")
        for name in filenames:
            total += os.lstat(Path(dirpath) / name).st_size
    return total


def _agent_dirs(chronicle: Path) -> Iterable[Path]:
    try:
        entries = sorted(chronicle.iterdir())
    except OSError:
        return []
    return [e for e in entries if e.is_dir() and not e.is_symlink() and not e.name.startswith(".")]


def _record_units(agent_dir: Path) -> list[Target]:
    out: list[Target] = []
    claims_dir = agent_dir / ".claims"
    claim_docs: list[tuple[Path, dict]] = []
    if claims_dir.is_dir() and not claims_dir.is_symlink():
        for c in sorted(claims_dir.glob("*.json")):
            if c.is_symlink():
                continue
            try:
                doc = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                claim_docs.append((c, doc))
    for day in sorted(agent_dir.iterdir()):
        if not day.is_dir() or day.is_symlink() or day.name.startswith("."):
            continue
        for meta in sorted(day.iterdir()):
            m = _SESSION_JSON.match(meta.name)
            if not m or meta.is_symlink():
                continue
            if not tripwire._is_tmp_handoff_session(meta):
                continue
            stem = m.group("stem")
            sm = _STEM.match(stem)
            uuid = sm.group("uuid") if sm else None
            unit = sorted(p for p in day.iterdir() if p.name.startswith(stem + ".") and (p.is_file() or p.is_symlink()))
            size = sum(os.lstat(p).st_size for p in unit)  # a symlink stays IN the unit: deletion refuses it
            claims = [c for c, d in claim_docs if uuid and str(d.get("stem_uuid")) == uuid]
            size += sum(os.lstat(c).st_size for c in claims)
            out.append(Target(
                kind="chronicle-record",
                reason=f"session.json handoff_file under /tmp/ in fixture persona dir '{agent_dir.name}'",
                root=agent_dir.parent, paths=unit, size=size,
                index_file=(agent_dir / "index.md") if uuid else None, index_uuid=uuid, claims=claims,
            ))
    return out


def scan(chronicle: Path, personas: Path, *, own: str | None) -> list[Target]:
    """Candidate residue under the two roots (read-only)."""
    out: list[Target] = []
    for agent_dir in _agent_dirs(chronicle):
        name = agent_dir.name
        if own is not None and name != own:
            out.append(Target(
                "chronicle-foreign-persona-dir",
                f"chronicle persona '{name}' is not the running seat ('{own}')",
                chronicle,
                [agent_dir],
                _tree_size_or_zero(agent_dir),
            ))
        elif is_fixture_only(name) and name != own:
            out.append(Target("chronicle-fixture-dir", f"fixture-only chronicle dir '{name}'",
                              chronicle, [agent_dir], _tree_size_or_zero(agent_dir)))
        elif is_fixture_persona(name):
            out.extend(_record_units(agent_dir))
    if own is None:
        return out
    try:
        pdirs = sorted(personas.iterdir())
    except OSError:
        pdirs = []
    for p in pdirs:
        if p.is_dir() and not p.is_symlink() and is_fixture_persona(p.name) and p.name != own:
            out.append(Target("persona-dir", f"fixture persona '{p.name}' is not the running seat ('{own}')",
                              personas, [p], _tree_size_or_zero(p)))
    return out


def _tree_size_or_zero(path: Path) -> int:
    try:
        return _tree_size(path)
    except (Refused, OSError):
        return 0


def _delete_target(t: Target, gone_claims: list[str] | None = None) -> list[str]:
    """Delete one target; returns the removed index lines. Raises Refused before touching
    anything the target must not touch. A claim shared with an earlier target may already be
    gone (ENOENT): it is skipped and appended to ``gone_claims``; other claim errors still raise."""
    removed_index: list[str] = []
    live_claims: list[Path] = []
    for p in t.paths:
        _check_inside(t.root, p)
        _lstat_no_symlink(p)
        if t.kind != "chronicle-record":
            _tree_size(p)  # refuses symlinks inside
    for c in t.claims:
        _check_inside(t.root, c)
        try:
            _lstat_no_symlink(c)
        except FileNotFoundError:
            if gone_claims is not None:
                gone_claims.append(str(c))
            continue
        live_claims.append(c)
    if t.kind == "chronicle-record":
        for p in t.paths:
            p.unlink()
        for c in live_claims:
            c.unlink(missing_ok=True)
        if t.index_file is not None and t.index_uuid and t.index_file.is_file() and not t.index_file.is_symlink():
            kept, gone = [], []
            for line in t.index_file.read_text(encoding="utf-8").splitlines(keepends=True):
                parts = [x.strip() for x in line[2:].split("|", 3)] if line.startswith("- ") else []
                (gone if len(parts) >= 3 and parts[2] == t.index_uuid else kept).append(line)
            if gone:
                tmp = t.index_file.with_name(t.index_file.name + ".residue-tmp")
                tmp.write_text("".join(kept), encoding="utf-8")
                os.replace(tmp, t.index_file)
                removed_index = [g.rstrip("\n") for g in gone]
    else:
        shutil.rmtree(t.paths[0])
    return removed_index


def run(chronicle: Path, personas: Path, state_dir: Path, *, own: str | None, execute: bool,
        now: datetime | None = None, out=sys.stdout) -> int:
    targets = scan(chronicle, personas, own=own)
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"[{mode}] chronicle={chronicle} personas={personas} running-seat-persona={own or 'UNRESOLVED'}", file=out)
    if own is None:
        print("running seat persona unresolved: persona-dir (b) targets REFUSED", file=out)
    total = 0
    for t in targets:
        total += t.size
        print(f"{'delete' if execute else 'would-delete'}\t{t.kind}\t{t.size}B\t{t.reason}", file=out)
        for p in t.paths:
            print(f"\t{p}", file=out)
        for c in t.claims:
            print(f"\t{c}", file=out)
        if t.index_uuid:
            print(f"\tindex line(s) for {t.index_uuid} in {t.index_file}", file=out)
    print(f"{len(targets)} target(s), {total} bytes", file=out)
    if not execute or not targets:
        return 0
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    log_dir = state_dir / "residue-cleanup"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{stamp}.jsonl"
    failures = 0
    with log.open("a", encoding="utf-8") as fh:
        for t in targets:
            fh.write(json.dumps({"event": "planned", **t.manifest()}, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        for t in targets:
            try:
                gone_claims: list[str] = []
                removed = _delete_target(t, gone_claims)
                event = {"event": "deleted", "paths": [str(p) for p in t.paths], "removed_index_lines": removed}
                if gone_claims:
                    event["claims_already_gone"] = gone_claims
                fh.write(json.dumps(event, sort_keys=True) + "\n")
            except (Refused, OSError) as exc:
                failures += 1
                fh.write(json.dumps({"event": "failed", "paths": [str(p) for p in t.paths],
                                     "error": str(exc)}, sort_keys=True) + "\n")
                print(f"FAILED {t.paths[0]}: {exc}", file=out)
            fh.flush()
    print(f"manifest/log: {log}", file=out)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="delete (default: dry-run, list only)")
    args = ap.parse_args(argv)
    roots = tripwire.real_roots()
    chronicle, personas = roots["chronicle"], roots["personas"]
    return run(chronicle, personas, chronicle.parent, own=tripwire.own_persona(), execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
