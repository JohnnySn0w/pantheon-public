#!/usr/bin/env python3
"""seed_rebrand.py — official Pantheon seed rebrand tool (T-2072).

Rename the `pantheon-*` / `pantheon_*` identifier prefix to a self-hoster's own
brand across a forked seed instance — units, skills, persona/service names, state
paths, and code — while PROTECTING the keep-list (upstream repo/provenance names
that must stay pointing at upstream). Cross-platform (Python; the seed runs on
Linux), dry-run by DEFAULT, and it uses `git mv` for file renames so history
follows the file.

Motivation (T-2072, surfaced by the 2026-07-27 wish-factory seed review): the first
external self-hoster (noodle) had to hand-roll a rename script AND figure out the
keep-list by trial — and still shipped a "Rename leftovers" cleanup pass. This tool
ships that work so every seeder gets a supported, tested path.

THE KEEP-LIST (why these are protected by default — see also
sop/standard/pantheon-bringup.md §Rebranding):

  - `pantheon-pwa`, `pantheon-public`  — SEPARATE upstream repos. A fork references
    them as provenance / "what I forked from"; renaming them would dangle those
    references. ALWAYS keep.
  - `pantheon-bridge`                   — the RETIRED upstream telegram-bridge,
    referenced only in historical comments/docs/tests. Renaming is cosmetic and
    risks touching test fixtures; keep by default.
  - `pantheon-config`                   — NUANCED. Upstream this is THE hub repo.
    A conservative fork keeps it as an upstream-provenance ref (default: PROTECTED,
    matching the "keep pantheon-config/pwa for history" guidance). A fork that
    MERGES seed+hub into one repo (as noodle did) instead renames it to the fork's
    own hub name — pass `--rename-hub <name>` for that case.

Everything else `pantheon-<x>` / `pantheon_<x>` renames to `<brand>-<x>` /
`<brand>_<x>`.

Usage (run from the fork's repo ROOT):
    python3 tools/seed_rebrand.py --brand wish                 # dry run (default)
    python3 tools/seed_rebrand.py --brand wish --apply         # actually rename
    python3 tools/seed_rebrand.py --brand wish --rename-hub wish-factory --apply
    python3 tools/seed_rebrand.py --brand wish --keep pantheon-config,pantheon-pwa,pantheon-public,pantheon-bridge --apply

Safety: dry-run by default (prints a plan; changes NOTHING). `--apply` mutates
files; run it on a clean git tree so `git diff` / `git status` is your review +
undo. The tool never touches its own log and never walks `.git/`.

What the tool DELIBERATELY leaves alone (per the wish-factory review §1.1/§2/§5):
  - Its OWN source (`seed_rebrand.py` + test) — a rename tool contains the tokens
    it replaces; rewriting itself would break idempotence for the pull-reconcile
    workflow that re-runs it after every upstream pull. See SELF_EXCLUDE.
  - The DESIGN-HISTORY corpus (`knowledge/ lessons/ bugs/` + the whole
    `architecture/` subtree — `architecture/adr/ architecture/rfc/
    architecture/engineering-practices/ architecture/atlas/` — plus the bare
    `adr/` name for pre-T-3472 forks that still hold ADRs at the root) —
    skipped by default; rewriting the origin's reasoning-prose turns it into
    the fork's, and rewriting `[[ORG-NNN]]` ADR references breaks the vault
    link graph. Pass `--include-corpus` to override. See CORPUS_DIRS.
  - The bare `pantheon` STATE/LOG dir (`~/.local/state/pantheon`, `.../log/pantheon`)
    — the rules only match `pantheon-` / `pantheon_` (trailing sep), never a bare
    `pantheon/` or `"pantheon"`, so the state/log root is untouched ON PURPOSE.
    Renaming it is the split-brain trap from §1.1 (docs rewritten, the 22 piecewise
    `Path(...)/"pantheon"` constants not); parameterization (T-2071) is the right
    fix, not find-and-replace. `--verify` warns if a partial rename is detected.
  - The `PANTHEON_*` env namespace (uppercase) — case-sensitive rules skip it; it's
    invisible to the operator, so leaving it fixed is safe. `--verify` still checks
    for dynamically-constructed env names, which a blanket rename would break.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Default keep-list (see module docstring). pantheon-config is protected by
# default (conservative); --rename-hub adds a hub rename on top.
DEFAULT_KEEP = ["pantheon-config", "pantheon-pwa", "pantheon-public", "pantheon-bridge"]

# Text file extensions the content pass rewrites. Binary/asset types are skipped.
TEXT_SUFFIXES = {
    ".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml", ".service", ".timer",
    ".path", ".txt", ".cfg", ".ini", ".conf", ".ts", ".js", ".svelte", ".rs",
    ".ps1", ".env", ".example", ".snippet", ".jsonl",
}
# Dirs never walked.
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".svelte-kit", "dist", "build"}

# CORPUS dirs — the origin instance's DESIGN-HISTORY prose. Skipped by default
# (--include-corpus to override) per the wish-factory review §2: rewriting the
# `pantheon-` tokens inside these turns the origin's reasoning into the fork's,
# and rewriting ADR-filename references breaks the `[[ORG-NNN]]` link graph +
# vault_link_check. A fork should KEEP this corpus as-is (it's the most valuable
# thing in the seed) or rewrite files from scratch — not blanket-rename it.
CORPUS_DIRS = {"knowledge", "lessons", "adr", "bugs", "architecture"}

# SELF-EXCLUSION (review §5): a rename tool contains the very tokens it replaces
# (its rules + keep-list + docstring). Without this it rewrites its own rules on
# the first run and stops being idempotent — fatal for the reconcile workflow,
# which re-runs it after every upstream pull.
SELF_EXCLUDE = {"seed_rebrand.py", "test_seed_rebrand.py"}

_SENTINEL = "\x00KEEP{}\x00"  # NUL-wrapped placeholder — cannot occur in source text


# A keep-token protects an identifier only at a WORD BOUNDARY — so `pantheon-config`
# (the hub) is shielded but `pantheon-config-sync` (a service that MUST rename) is
# NOT. Identifier continuation chars are [A-Za-z0-9_-]; a trailing `.`/`/`/space/@
# etc. is a boundary (so `pantheon-config.md` is still protected).
_IDENT_CONT = r"[A-Za-z0-9_-]"


def _protect(text: str, keep: list[str]) -> tuple[str, dict]:
    """Replace each keep-list token (at a word boundary) with a NUL sentinel so the
    rename pass can't touch it; return the shielded text + the restore map."""
    restore = {}
    for i, tok in enumerate(keep):
        ph = _SENTINEL.format(i)
        pat = re.compile(re.escape(tok) + r"(?!" + _IDENT_CONT + r")")
        if pat.search(text):
            text = pat.sub(ph, text)
            restore[ph] = tok
    return text, restore


def _restore(text: str, restore: dict) -> str:
    for ph, tok in restore.items():
        text = text.replace(ph, tok)
    return text


def build_rules(brand: str, rename_hub: str | None) -> list[tuple[str, str]]:
    """Ordered (most-specific-first) content substitution rules. The hub rename,
    when requested, must precede the generic `pantheon-`/`pantheon_` rules."""
    rules: list[tuple[str, str]] = []
    if rename_hub:
        rules.append(("pantheon-config", rename_hub))
    rules.append(("pantheon-", f"{brand}-"))
    rules.append(("pantheon_", f"{brand}_"))
    return rules


def rebrand_text(text: str, rules: list[tuple[str, str]], keep: list[str]) -> str:
    """Apply the rename rules to `text` with the keep-list shielded."""
    shielded, restore = _protect(text, keep)
    for frm, to in rules:
        shielded = shielded.replace(frm, to)
    return _restore(shielded, restore)


def rename_basename(name: str, brand: str, rename_hub: str | None, keep: list[str]) -> str:
    """New basename for a file, or the same name if it's protected / unaffected."""
    for tok in keep:
        # protect only if the basename's identifier-stem IS the keep token — i.e.
        # `pantheon-config.md`/`pantheon-config`, NOT `pantheon-config-sync.service`
        # (a superstring identifier, which must still rename).
        if name == tok or re.match(re.escape(tok) + r"(?![A-Za-z0-9_-])", name):
            return name  # a file whose name IS a protected token
    new = name
    if rename_hub:
        new = re.sub(r"^pantheon-config", rename_hub, new)
    new = re.sub(r"^pantheon-", f"{brand}-", new)
    new = re.sub(r"^pantheon_", f"{brand}_", new)
    return new


def _is_git_tree(root: Path) -> bool:
    return (root / ".git").exists()


def _skip_dirs(include_corpus: bool) -> set[str]:
    """Dir names pruned from the walk. The design-history corpus is skipped unless
    the caller explicitly opts in with include_corpus (review §2)."""
    return SKIP_DIRS if include_corpus else (SKIP_DIRS | CORPUS_DIRS)


def _iter_text_files(root: Path, include_corpus: bool):
    """Yield (Path, relpath) for every REBRANDABLE text file — the same surface the
    content pass rewrites: skip binary types, SKIP_DIRS, the corpus (unless
    include_corpus), and the tool's own source (self-exclusion, §5)."""
    skip = _skip_dirs(include_corpus)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn in SELF_EXCLUDE:
                continue
            p = Path(dirpath) / fn
            if p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            yield p, str(p.relative_to(root))


def run(root: Path, brand: str, *, rename_hub=None, keep=None, apply=False,
        include_corpus=False, runner=subprocess.run) -> dict:
    """Walk `root`, rewrite content + rename files. Returns a plan/report dict.
    apply=False (default) changes nothing."""
    keep = keep or list(DEFAULT_KEEP)
    rules = build_rules(brand, rename_hub)
    is_git = _is_git_tree(root)
    skip = _skip_dirs(include_corpus)
    content_changed: list[str] = []
    renamed: list[tuple[str, str]] = []

    # Pass 1 — content (skip binary types, skip dirs + corpus, self-exclude the tool).
    for p, rel in _iter_text_files(root, include_corpus):
        try:
            original = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = rebrand_text(original, rules, keep)
        if new != original:
            content_changed.append(rel)
            if apply:
                p.write_text(new, encoding="utf-8")

    # Pass 2 — filenames (bottom-up so parent dir renames don't invalidate paths).
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if any(part in skip for part in Path(dirpath).relative_to(root).parts):
            continue
        for fn in filenames:
            if fn in SELF_EXCLUDE:
                continue
            new_fn = rename_basename(fn, brand, rename_hub, keep)
            if new_fn == fn:
                continue
            src = Path(dirpath) / fn
            dst = Path(dirpath) / new_fn
            renamed.append((str(src.relative_to(root)), str(dst.relative_to(root))))
            if apply:
                if is_git:
                    runner(["git", "-C", str(root), "mv", str(src), str(dst)], check=True)
                else:
                    src.rename(dst)

    report = {
        "brand": brand, "rename_hub": rename_hub, "keep": keep, "applied": apply,
        "git": is_git, "include_corpus": include_corpus,
        "content_changed": content_changed,
        "renamed": renamed,
    }
    report["verify"] = verify_tree(root, brand, rules, keep, include_corpus=include_corpus)
    return report


# ── Post-run verification (review §6) ────────────────────────────────────────
# State/log path roots to check for a split-brain rename (review §1.1). A clean
# run leaves the bare `pantheon` state/log dir untouched (both shapes stay on
# `pantheon`, consistent); this catches a HALF-done rename where some references
# moved to <brand> and some are still `pantheon` — the silent split-brain trap.
_PATH_ROOTS = ("state", "log")
# Dynamically-constructed env-var names (review §1.4): if any of these appear, a
# blanket `PANTHEON_*` rename would be silently wrong. The tool never renames that
# namespace, but the check warns a fork that DOES that it can't be done blindly.
_DYN_ENV_RE = re.compile(r'environ(?:\.get)?\(\s*f"|"PANTHEON_"\s*\+|f"PANTHEON_')


def _shape_counts(text: str, root_word: str, ident: str) -> int:
    """Count occurrences of a state/log path root pointing at `ident`, in BOTH the
    slash shape (`state/<ident>`) and the piecewise shape (`"state" / "<ident>"`).
    A trailing identifier char (so `state/pantheon-config`) is NOT a match — we
    only mean the bare dir root."""
    slash = re.compile(re.escape(root_word) + r"/" + re.escape(ident) + r"(?![\w-])")
    piece = re.compile(r'"' + re.escape(root_word) + r'"\s*/\s*"' + re.escape(ident) + r'"')
    return len(slash.findall(text)) + len(piece.findall(text))


def verify_tree(root: Path, brand: str, rules, keep, *, include_corpus=False) -> dict:
    """Post-run checks (review §6). Returns {warnings: [...], notes: [...], ...}.
    Read-only: scans the rebrandable surface and reports what to eyeball."""
    warnings: list[str] = []
    notes: list[str] = []

    # (1) rule-prefix overlap — one rule's `from` is a strict prefix of another's
    # (review §1.2). Ordering (most-specific-first) resolves it; report so the
    # operator can confirm the intent.
    froms = [frm for frm, _ in rules]
    for i, a in enumerate(froms):
        for j, b in enumerate(froms):
            if i != j and b.startswith(a) and b != a:
                notes.append(f"rule '{a}' is a prefix of '{b}' — resolved by "
                             f"most-specific-first ordering (rule {j+1} runs before rule {i+1}).")

    # Scan the rebrandable surface once for the remaining checks.
    pan_by_root = {r: 0 for r in _PATH_ROOTS}
    brand_by_root = {r: 0 for r in _PATH_ROOTS}
    protected_hits = {tok: 0 for tok in keep}
    # Count a keep-token only at a WORD BOUNDARY — the same shape _protect shields —
    # so `pantheon-config` isn't inflated by every `pantheon-config-sync` substring.
    keep_pats = {tok: re.compile(re.escape(tok) + r"(?!" + _IDENT_CONT + r")") for tok in keep}
    dyn_env_files: list[str] = []
    env_ref_count = 0
    for p, rel in _iter_text_files(root, include_corpus):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for r in _PATH_ROOTS:
            pan_by_root[r] += _shape_counts(text, r, "pantheon")
            brand_by_root[r] += _shape_counts(text, r, brand)
        for tok, pat in keep_pats.items():
            protected_hits[tok] += len(pat.findall(text))
        env_ref_count += text.count("PANTHEON_")
        if _DYN_ENV_RE.search(text):
            dyn_env_files.append(rel)

    # (2) split-brain state/log dir (review §1.1) — some refs on <brand>, some
    # still `pantheon`, for the SAME root.
    for r in _PATH_ROOTS:
        if pan_by_root[r] and brand_by_root[r]:
            warnings.append(
                f"split-brain on the {r} dir: {brand_by_root[r]} reference(s) point at "
                f"'{r}/{brand}' but {pan_by_root[r]} still point at '{r}/pantheon'. The "
                f"state/log root should be ONE value everywhere (see review §1.1 — "
                f"parameterize it, don't half-rename). Reconcile before shipping.")

    # (3) dynamically-constructed env names (review §1.4)
    if dyn_env_files:
        warnings.append(
            "dynamically-constructed PANTHEON_* env var name(s) found in "
            f"{len(dyn_env_files)} file(s) (e.g. {dyn_env_files[0]}) — a blanket env "
            "namespace rename would be silently wrong. This tool leaves PANTHEON_* "
            "alone; flagged only if you plan to rename it manually.")

    return {
        "warnings": warnings,
        "notes": notes,
        "protected_hits": protected_hits,
        "env_ref_count": env_ref_count,
        "path_roots": {r: {"pantheon": pan_by_root[r], "brand": brand_by_root[r]}
                       for r in _PATH_ROOTS},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebrand a forked Pantheon seed (pantheon-* -> <brand>).")
    ap.add_argument("--brand", required=True, help="new identifier prefix, e.g. 'wish' (pantheon-x -> wish-x)")
    ap.add_argument("--root", default=".", help="fork repo root (default: cwd — run from the repo root)")
    ap.add_argument("--rename-hub", default=None,
                    help="ALSO rename pantheon-config -> this (merged seed+hub forks); default keeps it as provenance")
    ap.add_argument("--keep", default=",".join(DEFAULT_KEEP),
                    help="comma-separated keep-list (protected from rename). Default: " + ",".join(DEFAULT_KEEP))
    ap.add_argument("--apply", action="store_true", help="actually rename (default: dry run — prints the plan)")
    ap.add_argument("--include-corpus", action="store_true",
                    help="also rewrite the design-history corpus (knowledge/ lessons/ adr/ bugs/). "
                         "Default: SKIP it (protects the origin's reasoning-prose + [[ORG-NNN]] links)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "instance.yaml").exists() and not (root / "instance.yaml.example").exists():
        print(f"WARNING: {root} has no instance.yaml[.example] — are you at the fork's repo root?",
              file=sys.stderr)
    keep = [k.strip() for k in args.keep.split(",") if k.strip()]

    report = run(root, args.brand, rename_hub=args.rename_hub, keep=keep,
                 apply=args.apply, include_corpus=args.include_corpus)
    mode = "APPLIED" if args.apply else "DRY RUN (nothing changed — pass --apply to commit)"
    print(f"seed_rebrand [{mode}]  brand=pantheon-* -> {args.brand}-*"
          + (f"  hub: pantheon-config -> {args.rename_hub}" if args.rename_hub else ""))
    print(f"  keep-list (protected): {', '.join(keep)}")
    print(f"  corpus (knowledge/lessons/adr/bugs): "
          + ("INCLUDED (--include-corpus)" if args.include_corpus else "SKIPPED (default; --include-corpus to rewrite)"))
    print(f"  content rewrites: {len(report['content_changed'])} file(s)")
    print(f"  file renames:     {len(report['renamed'])}"
          + ("" if report["git"] else "  (NOT a git tree — plain rename, no history)"))
    for s, d in report["renamed"][:40]:
        print(f"    {s}  ->  {d}")
    if len(report["renamed"]) > 40:
        print(f"    … and {len(report['renamed']) - 40} more")

    # Post-run verification (review §6).
    v = report["verify"]
    print("\n  ── verification ──")
    hits = ", ".join(f"{tok}×{n}" for tok, n in v["protected_hits"].items() if n) or "none present"
    print(f"  protected tokens surviving (kept on purpose): {hits}")
    print(f"  PANTHEON_* env refs (left untouched): {v['env_ref_count']}")
    for note in v["notes"]:
        print(f"  note: {note}")
    if v["warnings"]:
        for w in v["warnings"]:
            print(f"  ⚠ WARNING: {w}")
    else:
        print("  ✓ no split-brain / dynamic-env-var warnings.")
    print("  → run the test suite after applying (env + path renames touch test "
          "assertions; green means the rename is internally consistent).")

    if not args.apply:
        print("  review the plan, then re-run with --apply (on a clean git tree so `git diff` is your undo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
