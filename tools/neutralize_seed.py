#!/usr/bin/env python3
"""Build the pantheon-public seed from a pantheon-config source tree.

Two responsibilities, run as one pipeline:

1. SELECTION (`select_paths`) — the KEEP/CLEAR/JUDGMENT/MANIFEST-CLASS
   allowlist from ORG-029 (`adr/ORG-029-pantheon-distribution-model.md`) plus
   the operator-judgment calls recorded in T-1676's report. Anything not
   explicitly allowlisted is excluded — this is a default-deny selector, not
   a default-allow one, so a new top-level dir added upstream does NOT leak
   into the seed until someone deliberately adds it here.

2. NEUTRALIZATION (`substitute_content` / `rename_basename`) — deterministic,
   idempotent, word/identifier-boundary-safe persona->role token convergence
   (3 vocabularies: persona names, audit role-ids, instance role-keys) plus a
   privacy scrub. Rebuilt for T-1676 from a recovered draft of the original
   (never-committed) T-0914 tool; extended to close two residual gaps found
   by diffing the shipped seed (79fa734) against ground truth: `claude-prime`
   and `Janus` (both Thoth aliases, per CLAUDE.md) were never mapped.

Boundary rule: (?<![A-Za-z])TOKEN(?![A-Za-z]) — matches prose words AND
snake_case/kebab-case components (underscore/hyphen/dot/digit are boundaries)
while skipping letter-embedded substrings (forward/toward/award/wardrobe/
steward/claude-primeval are safe).

Run as a script: `python3 tools/neutralize_seed.py <source_root> <dest_root>
[--carry-forward <existing_seed_root>]`. `dest_root` should be an empty
working tree (e.g. a fresh clone of the seed repo with only `.git/` present);
this script does not touch git state — commit the result yourself.
"""
import argparse
import shutil
import re
import sys
from pathlib import Path


def L(tok):  # letter-bounded token pattern
    return r'(?<![A-Za-z])' + tok + r'(?![A-Za-z])'


def LN(tok):
    """Like L() but the LEFT boundary also accepts the literal 2-char escape
    sequences \\n / \\t / \\r (backslash + letter), which is what a .py
    source file's raw bytes hold where its own string literals embed an
    escaped newline/tab — read_text() sees those as two ordinary characters,
    not an actual newline byte, so L()'s plain letter-boundary check treats
    the escape's trailing letter as part of a compound word. Scoped to the
    round-2 Michael/Mahan rules only (see SUBS) — not a general L() change."""
    return r'(?:(?<![A-Za-z])|(?<=\\n)|(?<=\\t)|(?<=\\r))' + tok + r'(?![A-Za-z])'


# Ordered; each entry (pattern, replacement). Order: personas, then role-ids,
# then instance-keys, then the two T-1676 gap-closes, then privacy scrub.
# Source tokens are disjoint so order is safe.
_RAW = [
    # --- orchestrator (Thoth) ---
    (L('THOTH'), 'ORCHESTRATOR'),
    (L('Thoth'), 'Orchestrator'),
    (L('thoth'), 'orchestrator'),
    (L('head_orchestrator'), 'orchestrator'),
    (L('head-orchestrator'), 'orchestrator'),
    (r'(?<![A-Za-z])Head Orchestrator(?![A-Za-z])', 'Orchestrator'),
    # --- specialist (Argus) ---
    (L('ARGUS'), 'SPECIALIST'),
    (L('Argus'), 'Specialist'),
    (L('argus'), 'specialist'),
    (L('science_team'), 'specialist'),
    (L('science-team'), 'specialist'),
    (r'(?<![A-Za-z])Science Team(?![A-Za-z])', 'Specialist'),
    # --- recovery (Ward) ---
    (L('WARD'), 'RECOVERY'),
    (L('Ward'), 'Recovery'),
    (L('ward'), 'recovery'),
    (L('recovery-fallback'), 'recovery'),
    (L('recovery_fallback'), 'recovery'),
    (r'(?<![A-Za-z])Recovery-Fallback(?![A-Za-z])', 'Recovery'),
    # --- T-1676 gap closes: sibling Thoth aliases missed by the original tool ---
    (L('CLAUDE-PRIME'), 'ORCHESTRATOR'),
    (L('Claude-Prime'), 'Orchestrator'),
    (L('claude-prime'), 'orchestrator'),
    (L('JANUS'), 'ORCHESTRATOR'),
    (L('Janus'), 'Orchestrator'),
    (L('janus'), 'orchestrator'),
    # --- privacy scrub ---
    (r'JohnnySn0w', 'example-org'),
    (L('money-engine'), 'revenue-project'),
    (L('money_engine'), 'revenue_project'),
    (r'mahan\.io', 'example.com'),
    (r'\$20/mo for Plus', 'a paid subscription tier'),
    (r'already pays \$20/mo', 'already pays for a subscription tier'),
    # --- T-1676 round 2 (Michael reversed the Q2 keep-precedent ruling,
    # TG 5147): the operator's bare first name must not appear in the seed.
    #
    # ATTRIBUTION: the base mapping (Michael's -> the operator's / Michael ->
    # the operator / michael -> operator) is adopted from tools/
    # seed_genericize.py's "operator identity" substitution class (T-1327,
    # a separate pre-existing generator for per-file worked-example
    # genericization) — that is the "newer iteration" precedent Michael
    # remembered scrubbing his name. Not a clean `import` (seed_genericize's
    # SUBSTITUTIONS list mixes the operator-identity class inline with 8
    # other classes — ticket-ids/dates/commit-hashes/msg-ids/host/account —
    # that this round's scope explicitly excludes; see the ruling's point 2:
    # applying those wholesale would strip structural provenance from the
    # seed), so the 3-rule mapping is reproduced here directly with this
    # attribution rather than sliced out of that list.
    #
    # This tool needs strictly MORE than that 3-rule base because it runs
    # UNATTENDED across the full ~515-file selection with no per-file human
    # review step (unlike seed_genericize.py's reviewed single-ADR workflow
    # gated by a hard fail-on-leak check) — the extensions below (ALL-CAPS
    # constants, hyphen-adjacent compound identifiers, sentence-start
    # capitalization, Python-escape-sequence-adjacent fixture strings) close
    # gaps a full-selection simulation surfaced that a curated per-file
    # workflow would never hit. Each is called out at its own rule below.
    #
    # seed_genericize.py's CLEANUPS (punctuation-repair) pass was evaluated
    # and NOT adopted: empirically running it against this tool's substituted
    # output (a full-selection simulation) produced 1432 "hits" that are
    # overwhelmingly false positives on PRE-EXISTING, unrelated content —
    # e.g. `(?m)^\*\*[A-Z][A-Za-z /]*:\*\*\s*$\n` would delete real section
    # headers like "**How to apply:**", ` {2,}` would collapse intentional
    # multi-space table/code formatting, `\(Telegram[ +,]*\)` would strip
    # innocuous "(Telegram)" mentions that were never adjacent to a removed
    # ticket-id/msg-id. CLEANUPS is calibrated to repair dangling fragments
    # left by seed_genericize's REMOVAL-style classes (ticket-id/date/msg-id
    # stripped to nothing); this tool's Michael/Mahan rules always REPLACE
    # with substantive text, never strip to nothing, so there is structurally
    # no dangling-fragment class for CLEANUPS to repair here. Zero genuine
    # Michael-substitution-caused fragments were found.
    #
    # Order matters: most-specific phrases first, then the sentence/list-start
    # capitalized form (fixed-width lookbehind alternation — Python `re`
    # allows differing fixed widths per branch), then the bare fallback, then
    # a bare-Mahan catch-all for anything the phrase rules didn't reach.
    # Targeted literal-identifier fix, most specific first: "Michael" embedded
    # in a camelCase Python identifier with no separator (verified real leak:
    # tools/test_activity_monitor.py's `TestMichaelLastInboundAge` class). The
    # general letter-boundary rule correctly refuses compound-word matches
    # (must also leave "MichaelJordan" alone) — a whole-identifier literal is
    # the safe way to close this one instance without loosening that safety.
    (r'TestMichaelLastInboundAge', 'TestOperatorLastInboundAge'),
    # A GFM markdown-anchor slug (auto-generated from a heading by GitHub;
    # this tool doesn't implement that slugifier) — verified real leak in
    # knowledge/infra-abstractions/scheduled.md: `#...-othersward-ward-
    # selfmichael`. Punctuation removal during slugification concatenates
    # words with no separator ("self→Michael" -> "selfmichael"), which is the
    # same compound-word situation as the camelCase class name above, just
    # for "ward" as well as "michael" — a pre-existing gap this round's
    # broader grep surfaced, fixed here as one literal-to-literal rule since
    # it's a single occurrence in a single file.
    # NOTE: by this point in the pipeline the earlier boundary-safe `ward`
    # rule has already converted the standalone second "ward" to "recovery"
    # (it's boundary-safe: preceded by "-"); only the compound "othersward"
    # survives untouched (not boundary-safe) — hence the mixed intermediate
    # form matched below, not the fully-original string.
    (r'othersward-recovery-selfmichael', 'othersrecovery-recovery-selfthe-operator'),
    # "Michael" immediately adjacent to a hyphen is a COMPOUND TITLE/
    # IDENTIFIER, not prose (verified real breakage, 4 instances found in the
    # prose-integrity spot-read): "HO-absent-Michael-keep-working" (title),
    # "(Michael-absent-with-pullable-work)", "no Michael-shelving is
    # pending", "...; Michael-ratified." The two-word "the operator" breaks
    # these; bare "operator" (matching the FILE_TOK convention already used
    # for the filename form of the same compound) is correct here. Must run
    # BEFORE the general Michael->"the operator" rule (more specific first).
    (r'(?<![A-Za-z])Michael(?=-)', 'operator'),
    (r'(?<=-)Michael(?![A-Za-z])', 'operator'),
    (r'(?<![A-Za-z])Studio Mahan(?: LLC)?(?![A-Za-z])', "the operator's studio"),
    # ALL-CAPS form — verified real leak: MICHAEL_CHAT_ID, MICHAEL_MOOT_RECENCY_DAYS,
    # P1B_MICHAEL_ABSENT_THRESHOLD_SECONDS. The other 3 tokens all have
    # THOTH/Thoth/thoth-style full case coverage; this closes the same gap for Michael.
    (LN('MICHAEL'), 'OPERATOR'),
    (r'(?:(?<=^)|(?<=\n)|(?<=[.!?] )|(?<=- )|(?<=\* ))Michael(?![A-Za-z])', 'The operator'),
    # LN(): like L() but also treats the literal 2-char escape sequences
    # \n/\t/\r (backslash + letter, as they appear unrendered in a .py
    # source file's raw bytes) as boundaries. Verified real leak:
    # tools/test_ticket_lint.py fixtures like `"...\\nMichael will..."` —
    # read_text() sees the literal characters backslash+n, not a newline
    # byte, so the plain letter-boundary check (correctly) treats 'n' as
    # part of a compound word and refuses to match. Scoped to Michael/Mahan
    # only, not the shared L() used by other tokens, to avoid widening the
    # blast radius of an already-reviewed set of substitutions.
    (LN('Michael'), 'the operator'),
    (LN('Mahan'), 'the operator'),
    # Bare lowercase "michael" is a STRUCTURED-DATA VALUE throughout
    # tools/ticket.py & co (`target: michael`, `requester: michael`,
    # `target == "michael"`) — the ticket system's role vocabulary treats it
    # as a value the same way "thoth"/"argus"/"ward" are values elsewhere.
    # Bare "operator" (no article), matching tools/seed_genericize.py's own
    # established precedent for the identical case. Simulation-verified: 67
    # selected files carry this before this rule closes them.
    (LN('michael'), 'operator'),
    # --- REBOUND-2 (T-1676 review): a/an agreement ---
    # Substituting a consonant-initial token (Michael, thoth) with a
    # vowel-initial one (operator, orchestrator) leaves a preceding "a"
    # article wrong: "a Michael-ratified decision" -> "a operator-ratified
    # decision" (should be "an"). Also folds in the pre-existing round-1 case
    # (thoth -> orchestrator, flagged by the reviewer as already-shipped) —
    # same defect class, one fix. MUST be the LAST rule: it corrects the
    # OUTPUT of the token substitutions above, not the source text, so it
    # only makes sense to run after all of them. A callback (not a plain
    # string) preserves whichever case-form the token substitution actually
    # produced (operator/Operator/orchestrator/Orchestrator) — "a Thoth
    # would" mid-sentence still capitalizes to "a Orchestrator" (Thoth is
    # always a proper-noun replacement, capitalized regardless of sentence
    # position), so both the lowercase and Title-case token forms need this,
    # not just the lowercase prose form "operator" gets.
    (re.compile(r'\b([Aa]) (operator|orchestrator|Operator|Orchestrator)\b'),
     lambda m: ('An' if m.group(1) == 'A' else 'an') + ' ' + m.group(2)),
    # Verified real instance (rulebook/goals.md): "a Michael roadmap call"
    # (Michael as a bare adjectival modifier, no hyphen) falls through to the
    # general Michael->"the operator" rule, which inserts ITS OWN article,
    # producing "a the operator roadmap call" — a double article, worse than
    # the simple mismatch above. Collapse straight to "an operator"/"an
    # orchestrator". Must run AFTER the simple a/an rule above (order doesn't
    # matter between the two — disjoint patterns — but keeping it last is
    # correct since it's also correcting substitution output).
    (re.compile(r'\b([Aa]) the (operator|orchestrator)\b'),
     lambda m: ('An' if m.group(1) == 'A' else 'an') + ' ' + m.group(2)),
]
SUBS = [(re.compile(p) if isinstance(p, str) else p, r) for p, r in _RAW]

# Basename token map for file/dir renames (same convergence applied to names).
# REBOUND 1 (T-1676 review): the persona tokens were the only basename map —
# the privacy-scrub tokens never touched filenames, so a project name could
# leak in a FILENAME even when correctly scrubbed from content. Verified real
# instance: adr/ORG-032-money-engine-orchestration-shape.md.
FILE_TOK = [(re.compile(L(a)), b) for a, b in [
    ('thoth', 'orchestrator'), ('argus', 'specialist'), ('ward', 'recovery'),
    ('money-engine', 'revenue-project'), ('money_engine', 'revenue_project'),
    ('JohnnySn0w', 'example-org'),
    # T-1676 round 2: live trigger is sop/standard/ho-absent-michael-keep-working.md.
    ('michael', 'operator'), ('Michael', 'Operator'),
    ('mahan', 'operator'), ('Mahan', 'Operator'),
]] + [(re.compile(r'mahan\.io'), 'example.com')]  # no current filename trigger; folded in for completeness

TEXT_EXT = {'.py', '.md', '.yaml', '.yml', '.sh', '.json', '.txt', '.toml',
            '.cfg', '.ini', '.service', '.timer', ''}


def substitute_content(text: str) -> tuple[str, int]:
    """Apply the full SUBS pipeline to `text`. Returns (new_text, hit_count)."""
    hits = 0
    for rx, rep in SUBS:
        text, n = rx.subn(rep, text)
        hits += n
    return text, hits


def rename_basename(name: str) -> str:
    for rx, rep in FILE_TOK:
        name = rx.sub(rep, name)
    return name


# ---------------------------------------------------------------------------
# Selection (ORG-029 KEEP / CLEAR / JUDGMENT / MANIFEST-CLASS)
# ---------------------------------------------------------------------------

WHOLESALE_KEEP_DIRS = ('rulebook', 'sop', 'adr', 'tools', 'tests', 'skills', '.githooks')
JUDGMENT_INCLUDE_DIRS = ('bugs', 'lessons')
KNOWLEDGE_INCLUDE_FILES = ('knowledge/README.md', 'knowledge/pantheon-glossary.md')
KNOWLEDGE_INCLUDE_DIRS = ('knowledge/infra-abstractions',)
MANIFEST_CLASS_FILES = ('repos.yaml', 'instance.yaml')
MANIFEST_CLASS_DIRS = ('pinned-rules',)
# Hand-authored onboarding/hygiene content with no source-tree analog — copied
# from the existing seed as-is, never regenerated from pantheon-config.
CARRY_FORWARD_FILES = ('README.md', '.gitignore')

# T-1676 process lesson: the generator ships with the arc (tools/ is
# wholesale-KEEP), so on every future refresh this script becomes part of its
# OWN input. Its source holds persona tokens as regex PATTERN LITERALS, not
# prose — substitution can't tell the difference and would corrupt its own
# SUBS/FILE_TOK tables (verified failure mode: a self-build silently turned
# `(L('thoth'), 'orchestrator')` into `(L('orchestrator'), 'orchestrator')`,
# a no-op map). These two files are selected normally but copied verbatim,
# never passed through substitute_content/apply_manifest_banner.
SELF_EXEMPT_FROM_SUBSTITUTION = frozenset({
    'tools/neutralize_seed.py',
    'tools/test_neutralize_seed.py',
    # T-1676 round 2: tools/seed_genericize.py (T-1327) is a SEPARATE,
    # pre-existing generator with its own Michael/michael pattern table for
    # the identical reason this tool has one — it must ship unmodified, not
    # partially mangled by this tool's own substitution pass sweeping over
    # it via KEEP-wholesale. Same class as the two entries above.
    'tools/seed_genericize.py',
    'tools/test_seed_genericize.py',
})

MANIFEST_BANNERS = {
    'repos.yaml': (
        "# " + "=" * 76 + "\n"
        "# ⚠️ REFERENCE-INSTANCE EXAMPLE — REPLACE FOR YOUR INSTANCE.\n"
        "# The repo registry + grants below are the reference instance's. Replace with\n"
        "# your own repos. Structure/schema is framework; the entries are instance data.\n"
        "# " + "=" * 76 + "\n"
    ),
    'instance.yaml': (
        "# " + "=" * 76 + "\n"
        "# ⚠️ REFERENCE-INSTANCE EXAMPLE — REPLACE FOR YOUR INSTANCE.\n"
        "# The instance:/roles: blocks below bind the REFERENCE operator's personas,\n"
        "# accounts, and token-file paths. They are kept as a worked example of the\n"
        "# schema, NOT config to run as-is. A new operator replaces them with their own\n"
        "# bindings (same role keys: orchestrator / specialist / recovery).\n"
        "# token_ref values are PATHS to credential files, not secrets. Persona→role\n"
        "# neutralization of the wider scaffolding is ORG-030 Phase-3 (in progress).\n"
        "# " + "=" * 76 + "\n"
    ),
}


def apply_manifest_banner(rel_posix: str, text: str) -> str:
    """Prepend the fixed MANIFEST-CLASS banner for repos.yaml/instance.yaml.

    Idempotent: a no-op if the banner marker is already present.
    """
    banner = MANIFEST_BANNERS.get(rel_posix)
    if banner is None:
        return text
    if 'REFERENCE-INSTANCE EXAMPLE' in text:
        return text
    return banner + text


def select_paths(source_root: Path) -> list[Path]:
    """Return the allowlisted set of relative paths (posix Path) to ship.

    Default-deny: a top-level dir/file not named below never enters the seed,
    even if it exists in `source_root`.
    """
    selected: list[Path] = []

    def add_tree(rel_dir: str):
        d = source_root / rel_dir
        if not d.is_dir():
            return
        for p in sorted(d.rglob('*')):
            if not p.is_file():
                continue
            if '__pycache__' in p.parts:
                continue
            if '.pytest_cache' in p.parts:
                continue
            if p.suffix in ('.pyc', '.pyo'):
                continue
            selected.append(p.relative_to(source_root))

    for d in WHOLESALE_KEEP_DIRS:
        add_tree(d)
    for d in JUDGMENT_INCLUDE_DIRS:
        add_tree(d)
    for rel in KNOWLEDGE_INCLUDE_FILES:
        p = source_root / rel
        if p.is_file():
            selected.append(p.relative_to(source_root))
    for d in KNOWLEDGE_INCLUDE_DIRS:
        add_tree(d)
    for rel in MANIFEST_CLASS_FILES:
        p = source_root / rel
        if p.is_file():
            selected.append(p.relative_to(source_root))
    for d in MANIFEST_CLASS_DIRS:
        add_tree(d)

    return selected


def is_text(p: Path) -> bool:
    if p.suffix not in TEXT_EXT:
        return False
    try:
        p.read_text(encoding='utf-8')
        return True
    except (UnicodeDecodeError, OSError):
        return False


def dest_rel_path(rel: Path) -> Path:
    """Map a source-relative path to its destination-relative path (renamed)."""
    parts = [rename_basename(part) for part in rel.parts]
    return Path(*parts)


def build_seed(source_root: Path, dest_root: Path, carry_forward_root: Path | None = None) -> dict:
    """Build the neutralized seed tree at `dest_root` from `source_root`.

    Deterministic and re-runnable: `dest_root` is not cleared here (caller's
    responsibility — typically an otherwise-empty fresh clone), but every
    write is a fresh `write_text`/`write_bytes`, so re-running with the same
    inputs reproduces byte-identical output.
    """
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    files_written = 0
    sub_hits = 0
    renamed = 0

    for rel in select_paths(source_root):
        src = source_root / rel
        rel_posix = rel.as_posix()
        out_rel = dest_rel_path(rel)
        if out_rel != rel:
            renamed += 1
        dst = dest_root / out_rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        if rel_posix in SELF_EXEMPT_FROM_SUBSTITUTION:
            shutil.copy2(src, dst)
        elif is_text(src):
            text = src.read_text(encoding='utf-8')
            new_text, hits = substitute_content(text)
            new_text = apply_manifest_banner(rel_posix, new_text)
            sub_hits += hits
            dst.write_text(new_text, encoding='utf-8')
            shutil.copystat(src, dst)  # write_text drops mode bits (e.g. .githooks/* +x)
        else:
            shutil.copy2(src, dst)
        files_written += 1

    if carry_forward_root is not None:
        carry_forward_root = Path(carry_forward_root)
        for rel_name in CARRY_FORWARD_FILES:
            src = carry_forward_root / rel_name
            if src.is_file():
                dst = dest_root / rel_name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                files_written += 1

    return {
        'files_written': files_written,
        'sub_hits': sub_hits,
        'renamed': renamed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source_root', type=Path, help='pantheon-config working tree to build the seed from')
    ap.add_argument('dest_root', type=Path, help='destination working tree (e.g. a fresh seed clone)')
    ap.add_argument('--carry-forward', type=Path, default=None,
                     help='existing seed tree to copy README.md/.gitignore from unchanged')
    args = ap.parse_args(argv)

    if not args.source_root.is_dir():
        print(f"source_root not found: {args.source_root}", file=sys.stderr)
        return 1
    args.dest_root.mkdir(parents=True, exist_ok=True)

    stats = build_seed(args.source_root, args.dest_root, carry_forward_root=args.carry_forward)
    print(f"seed build: {stats['files_written']} files written, "
          f"{stats['sub_hits']} substitutions, {stats['renamed']} renamed")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
