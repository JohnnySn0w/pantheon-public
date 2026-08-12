#!/usr/bin/env python3
"""Mirror publish-state manager (T-1994).

The drift-sweep (`tools/mirror_drift_sweep.py`) cannot read the outward-facing
mirrors — recovery's account has no read path to 3 of the 4 (gh unauthenticated,
HTTPS no creds, SSH deploy keys source-scoped), AND the genericized mirrors have
DISJOINT history (published via `git bundle` as orphan heads, no shared ancestry
with the source), so `git rev-list mirror..source` is meaningless anyway. Drift
is therefore measured against a RECORDED ANCHOR — the SOURCE head from which each
mirror was last published — never by reading the mirror.

This module is the single reader/writer of that anchor state
(`mirror-published-state.yaml`). Two producers write to it:

* the BUILD leg (recovery), at bundle build+deliver time, records a *staged* bundle
  via `record-staged` — carrying the source head the bundle was built from. This
  is what powers the built-but-unpushed alert.
* the PUBLISH leg (orchestrator for all four pairs — ORG-039 / T-1673 push-is-HO), at
  publish time, records the anchor via `record-published`. The anchor is a SOURCE
  sha the publisher does NOT independently derive, so the value MUST travel in the
  bundle handoff (the mailbox drop) and be passed to `record-published
  --source-head <sha>`; if it is absent the sweep reports a coverage-gap rather
  than crashing or false-filing (per the finalized T-1994 contract with orchestrator).

Schema (`mirror-published-state.yaml`):

    version: 1
    pairs:
      <slug>:
        source_head_at_last_publish: <sha>|null   # the drift anchor
        published_at: <iso8601>|null               # when record-published ran
        published_mirror_head: <sha>|null          # informational (audit only)
        published_by: <persona>|null
        staged_bundle:                             # null when nothing staged
          source_head_at_build: <sha>
          built_at: <iso8601>
          bundle_path: <str>|null                  # informational

Slugs are STABLE, role-based (T-0915 rename tolerance): claude-harness-mirror,
codex-harness-mirror, pwa-release-mirror, pantheon-public-mirror. They are the join
key between this state and the static `mirror-drift-pairs.yaml`.

CLI:
    python3 tools/mirror_state.py record-published <slug> --source-head <sha> \
        [--mirror-head <sha>] [--by orchestrator]
    python3 tools/mirror_state.py record-staged <slug> --source-head <sha> \
        [--bundle-path <path>]
    python3 tools/mirror_state.py clear-staged <slug>
    python3 tools/mirror_state.py get <slug>        # print one pair's state (json)
    python3 tools/mirror_state.py show               # print whole state (yaml)

`record-published` clears a staged bundle whose `source_head_at_build` matches the
just-published `--source-head` (the staged bundle became the published anchor).

No git, no network, no ticket-filing — this is pure local state. The sweep and
the systemd units are separate. Tests: `python3 -m unittest tools.test_mirror_state`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = REPO_ROOT / "mirror-published-state.yaml"

STATE_VERSION = 1

# Canonical stable slugs (T-0915 rename tolerance). Kept here as the authoritative
# set so a typo'd slug on the CLI is rejected rather than silently creating a
# ghost pair the sweep never reads.
KNOWN_SLUGS = (
    "claude-harness-mirror",
    "codex-harness-mirror",
    "pwa-release-mirror",
    "pantheon-public-mirror",
)


class MirrorStateError(Exception):
    """Raised on any state-file or argument error (surfaced with exit 2)."""


# ---------------------------------------------------------------------------
# Time helper (module-level so tests patch one symbol).
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# Load / save.
# ---------------------------------------------------------------------------


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "pairs": {}}


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load and lightly validate the state file.

    A missing file yields an empty state (the sweep then reports coverage-gaps
    for every pair — the honest pre-seed state, never a crash). A present-but-
    malformed file is a hard error (a corrupted anchor store must not be treated
    as "no drift").
    """
    if not path.exists():
        return _empty_state()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MirrorStateError(f"state file {path} is not valid YAML: {exc}") from exc
    if raw is None:
        return _empty_state()
    if not isinstance(raw, dict):
        raise MirrorStateError(f"state file {path} must be a mapping, got {type(raw).__name__}")
    pairs = raw.get("pairs")
    if pairs is None:
        raw["pairs"] = {}
    elif not isinstance(pairs, dict):
        raise MirrorStateError(f"state file {path}: 'pairs' must be a mapping")
    raw.setdefault("version", STATE_VERSION)
    return raw


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    """Write the state file atomically (temp + os.replace) so a crash mid-write
    never leaves a truncated anchor store.
    """
    state.setdefault("version", STATE_VERSION)
    state.setdefault("pairs", {})
    text = yaml.safe_dump(state, sort_keys=True, default_flow_style=False, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def get_pair(state: dict[str, Any], slug: str) -> dict[str, Any]:
    """Return the mutable per-pair sub-dict, creating an empty skeleton if absent.

    An unknown pair sub-dict has every field defaulted to None (missing anchor =>
    coverage-gap, never a crash).
    """
    pairs = state.setdefault("pairs", {})
    entry = pairs.get(slug)
    if entry is None:
        entry = {
            "source_head_at_last_publish": None,
            "published_at": None,
            "published_mirror_head": None,
            "published_by": None,
            "staged_bundle": None,
        }
        pairs[slug] = entry
    return entry


# ---------------------------------------------------------------------------
# Mutations.
# ---------------------------------------------------------------------------


def _validate_slug(slug: str) -> None:
    if slug not in KNOWN_SLUGS:
        raise MirrorStateError(
            f"unknown slug {slug!r}; known slugs: {', '.join(KNOWN_SLUGS)}"
        )


def _validate_sha(sha: str) -> str:
    s = sha.strip().lower()
    if not (7 <= len(s) <= 40) or any(c not in "0123456789abcdef" for c in s):
        raise MirrorStateError(f"not a valid git sha: {sha!r}")
    return s


def record_published(
    state: dict[str, Any],
    slug: str,
    source_head: str,
    *,
    mirror_head: str | None = None,
    by: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Record that `slug` was published from SOURCE head `source_head`.

    Sets the drift anchor. Clears a staged bundle whose `source_head_at_build`
    matches `source_head` (the staged build became this publish). A staged bundle
    for a DIFFERENT source head is left intact — it represents a newer build not
    yet published (and would itself be a built-but-unpushed signal).
    """
    _validate_slug(slug)
    source_head = _validate_sha(source_head)
    if mirror_head is not None:
        mirror_head = _validate_sha(mirror_head)
    entry = get_pair(state, slug)
    entry["source_head_at_last_publish"] = source_head
    entry["published_at"] = now_iso or _now_iso()
    entry["published_mirror_head"] = mirror_head
    entry["published_by"] = by
    staged = entry.get("staged_bundle")
    if isinstance(staged, dict) and staged.get("source_head_at_build") == source_head:
        entry["staged_bundle"] = None
    return entry


def record_staged(
    state: dict[str, Any],
    slug: str,
    source_head: str,
    *,
    bundle_path: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Record that a refresh bundle for `slug` was BUILT from SOURCE head
    `source_head` and handed off for publish (staged-but-unpublished).

    Powers the built-but-unpushed alert: the sweep flags a pair whose
    `staged_bundle.source_head_at_build` differs from
    `source_head_at_last_publish` (a build was delivered but not yet published).
    """
    _validate_slug(slug)
    source_head = _validate_sha(source_head)
    entry = get_pair(state, slug)
    entry["staged_bundle"] = {
        "source_head_at_build": source_head,
        "built_at": now_iso or _now_iso(),
        "bundle_path": bundle_path,
    }
    return entry


def clear_staged(state: dict[str, Any], slug: str) -> dict[str, Any]:
    """Clear any staged bundle for `slug` (e.g. an abandoned/superseded build)."""
    _validate_slug(slug)
    entry = get_pair(state, slug)
    entry["staged_bundle"] = None
    return entry


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mirror publish-state manager (T-1994).")
    p.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="path to mirror-published-state.yaml (default: repo root)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("record-published", help="record a publish (sets drift anchor)")
    sp.add_argument("slug")
    sp.add_argument("--source-head", required=True, help="SOURCE sha the mirror was built from")
    sp.add_argument("--mirror-head", default=None, help="published mirror sha (informational)")
    sp.add_argument("--by", default=None, help="publishing persona (e.g. orchestrator)")

    ss = sub.add_parser("record-staged", help="record a built-and-delivered bundle")
    ss.add_argument("slug")
    ss.add_argument("--source-head", required=True, help="SOURCE sha the bundle was built from")
    ss.add_argument("--bundle-path", default=None, help="path to the staged bundle (informational)")

    sc = sub.add_parser("clear-staged", help="clear a staged bundle (abandoned/superseded)")
    sc.add_argument("slug")

    sg = sub.add_parser("get", help="print one pair's state as JSON")
    sg.add_argument("slug")

    sub.add_parser("show", help="print the whole state as YAML")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = Path(args.state_path)
    try:
        state = load_state(path)
        if args.cmd == "record-published":
            entry = record_published(
                state, args.slug, args.source_head,
                mirror_head=args.mirror_head, by=args.by,
            )
            save_state(state, path)
            print(json.dumps({args.slug: entry}, indent=2, sort_keys=True))
        elif args.cmd == "record-staged":
            entry = record_staged(
                state, args.slug, args.source_head, bundle_path=args.bundle_path,
            )
            save_state(state, path)
            print(json.dumps({args.slug: entry}, indent=2, sort_keys=True))
        elif args.cmd == "clear-staged":
            entry = clear_staged(state, args.slug)
            save_state(state, path)
            print(json.dumps({args.slug: entry}, indent=2, sort_keys=True))
        elif args.cmd == "get":
            _validate_slug(args.slug)
            entry = state.get("pairs", {}).get(args.slug)
            print(json.dumps({args.slug: entry}, indent=2, sort_keys=True))
        elif args.cmd == "show":
            print(yaml.safe_dump(state, sort_keys=True, default_flow_style=False))
    except MirrorStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
