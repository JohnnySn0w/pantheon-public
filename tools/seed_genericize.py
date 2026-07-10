#!/usr/bin/env python3
"""seed_genericize.py — T-1327 (recurring weekly).

Genericize the PUBLIC pantheon-public seed ADRs (option B — "redacted worked
example": keep each shipped framework ADR's full shape + reasoning, scrub the
internals in place) so the mechanism transfers without leaking our deployment's
operator name, ticket-IDs, message-IDs, dates, commit hashes, host/account/
service names, or instance-project names.

Two parts:
  1. an ordered substitution pipeline (most-specific-first) that rewrites the
     known leak forms into generic role/structure-preserving tokens, plus a
     punctuation-cleanup pass that repairs the dangling `(via )` / `per 's`
     fragments the removals leave behind; and
  2. a STRICT fail-on-leak gate — after scrubbing, the output is scanned for any
     surviving leak marker and the run ABORTS (non-zero exit) if one remains.
     This is the public-repo safety net: a missed substitution can never ship,
     it can only fail the run loudly so a rule gets added.

The gate is intentionally a little over-eager (e.g. it flags any bare 7-hex
token). A false abort is SAFE — it fails closed and asks for a look — whereas a
false pass would publish a leak. Tune SUBSTITUTIONS to satisfy the gate, never
the reverse.

Usage:
  seed_genericize.py --check FILE...        # scrub in memory, run gate, print residual leaks; exit 2 if any
  seed_genericize.py --print FILE           # scrub + gate, print the scrubbed file to stdout (review before push)
  seed_genericize.py --in-place FILE...     # scrub + gate, write back ONLY if gate passes (never writes a leak)

Role/structure tokens PRESERVED on purpose: Orchestrator, Specialist, Recovery,
operator, principle refs (P1/D1/D7/…), and ORG-NNN cross-references (those are
the framework ADRs that stay in the seed). "Pantheon" and "Telegram" are kept —
the framework's public name and a generic product, not leaks.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WORKED_EXAMPLE_BANNER = (
    "> **Worked example from a real deployment.** This ADR keeps the full shape "
    "and reasoning of a decision as it was actually ratified and operated; the "
    "deployment-specific identifiers have been genericized. Replace the role "
    "tokens (Orchestrator / Specialist / Recovery / operator) and substrate "
    "details with your own instance's.\n"
)

# Ordered, most-specific-first. Each entry: (pattern, replacement). re.MULTILINE
# is applied globally; patterns that must NOT span lines anchor accordingly.
SUBSTITUTIONS: list[tuple[str, str]] = [
    # --- genericization-artifact doubling (from the ORG-030 persona pass) ---
    (r"Orchestrator \(Orchestrator\)", "Orchestrator"),
    (r"Orchestrator \(HO\)", "Orchestrator"),
    (r"\bOrchestrator \(the Orchestrator\)", "Orchestrator"),
    # --- orchestrator abbreviation ---
    (r"\bHO-locked\b", "Orchestrator-locked"),
    (r"\bHO\b", "the Orchestrator"),

    # --- operator identity (possessive first) ---
    (r"Michael's", "the operator's"),
    (r"\bMichael\b", "the operator"),
    # lowercase form: vocab tokens (`human:michael`, `target: michael`), frontmatter
    # (`ratified_by: michael`), filenames (`ho-absent-michael-...`). Keep token-clean → "operator".
    (r"\bmichael\b", "operator"),

    # --- commit hashes (backtick-wrapped 7-40 hex) -> generic, then de-dup "commit" ---
    # require >=1 a-f letter so pure-digit backtick numbers (bot/chat IDs) fall through
    # to the numeric-id rule (-> <id>) instead of being mislabeled "a commit".
    (r"commit `(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}`", "a commit"),
    (r"`(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}`", "a commit"),
    # bare (non-backtick) commit hashes: "commit 9f2dc63", "repo@107301a", "committed at X".
    # Require >=1 a-f letter so pure-digit 7+ runs (counts/years) are NOT matched.
    (r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", "`<commit>`"),

    # --- ledger timestamps (ISO datetime + seq); leave any preceding "ledger entry" noun standing ---
    (r" `?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z-\d{3}`?", ""),

    # --- host / accounts / service units / sudoers files ---
    (r"\bswarm VPS\b", "the host"),
    (r"\bthe swarm\b", "the host"),
    (r"\bswarm\b", "the host"),
    (r"claude-telegram-fallback\.service", "the Recovery agent's service unit"),
    (r"pantheon-bridge\.service", "the Telegram bridge service"),
    (r"`fallback-rescues-prime` \+ `50-claude-fallback-research`", "the configured sudoers entries"),
    (r"`fallback-rescues-prime`, `50-claude-fallback-research`", "the configured sudoers entries"),
    (r"`fallback-rescues-prime`", "a configured sudoers entry"),
    (r"`50-claude-fallback-research`", "a configured sudoers entry"),
    # account-home paths (infra) -> ~ ; path form FIRST so the account-name subs below
    # don't mangle them into "/home/the Recovery account/...".
    (r"/home/claude-fallback", "~"),
    (r"/home/claude-research", "~"),
    (r"/home/claude-telegram[\w-]*", "~"),
    (r"/home/claude\b", "~"),
    (r"\bclaude-fallback\b", "the Recovery account"),
    (r"\bclaude-research\b", "the Specialist account"),
    (r"claude-telegram-research\.service", "the Specialist agent's service unit"),
    (r"claude-telegram[\w-]*\.service", "the agent's service unit"),
    # Telegram bot names (c_swarm_bot etc. — embed the host name) and bot/chat/user IDs
    # (9-12 digit numbers — partial credentials). Bot-name FIRST so its embedded "swarm"
    # is consumed as a unit before the swarm subs below.
    (r"\bc_\w+_bot\b", "<bot>"),
    (r"\b\d{9,12}\b", "<id>"),

    # --- instance-project NAMES are intentionally KEPT (operator decision) ---
    # kino / codex / echo(-prismatic) are project codenames, not sensitive: codex is
    # even a public tool, and keeping the real names makes the worked examples more
    # concrete (the point of the redacted-worked-example form). Only identity / tracking
    # IDs / dates / internal-infra names are scrubbed — NOT project names. (The service
    # *unit* names above ARE scrubbed: they're infra, not project codenames.)
    # --- product-direction scrubbing (operator decision: scrub product/business
    # *direction* even though codenames are KEPT). Keeps the transferable reasoning,
    # removes the monetization/unreleased-concept signal. ---
    # echo: keep the name, drop the product concept ("what echo is")
    (r'"echo = a voice interface to plug anything into"', '"echo = <its product north-star>"'),
    # revenue/monetization framing -> neutral domain label (orchestration reasoning is
    # domain-agnostic; "revenue" is the only business-direction signal in ORG-032/014)
    (r"Revenue-project", "New-domain project"),
    (r"revenue-project", "new-domain project"),
    (r"\btreasurer-class\b", "domain-specialist-class"),
    (r"\brevenue\b", "new-domain"),

    # --- ticket-ids: load-bearing inline forms first ---
    (r"\bT-\d{3,4}'s\b", "the relevant ticket's"),
    (r"\bT-\d{3,4} \+ T-\d{3,4}\b", "the ratifying tickets"),
    # ref immediately followed by a descriptive parenthetical: KEEP the description,
    # drop the ref + its parens ("T-0309 (this ADR's authoring ticket)" -> the prose).
    (r"\b(?:T-\d{3,4}|msg[ _-]\d+) \(([^)]*)\)", r"\1"),

    # --- message-ids (bare, after the descriptive-paren unwrap) ---
    (r"\bTelegram msg[ _-]\d+\b", "Telegram"),
    (r"_?msg[ _-]?\d+", ""),  # boundary-agnostic: catches msg_123, msg 123, and embedded _msg1271

    # --- ticket-ids (bare, last) ---
    (r"\bT-\d{3,4}\b", ""),

    # --- frontmatter + status dates ---
    (r"(?m)^date:\s*\d{4}-\d{2}-\d{2}\s*$\n", ""),
    (r"(?m)^(status:\s*Ratified)\s+\d{4}-\d{2}-\d{2}\s*$", r"\1"),
    (r"\bRatified \d{4}-\d{2}-\d{2}\b", "Ratified"),
    (r"\bDrafted \d{4}-\d{2}-\d{2}\b", "Drafted"),
    (r"\bAmended \d{4}-\d{2}-\d{2}\b", "Amended"),
    (r"\bshipped \d{4}-\d{2}-\d{2}\b", "shipped"),
    (r"\bvalidated live \d{4}-\d{2}-\d{2}\b", "validated live"),
    (r"\bdeferred \d{4}-\d{2}-\d{2}\b", "deferred"),
    (r"\bfinalized by Orchestrator \d{4}-\d{2}-\d{2}\b", "finalized by Orchestrator"),
    # date leading a bold header ("**2026-05-05 status update:**" -> "**status update:**")
    (r"\*\*\d{4}-\d{2}-\d{2} ", "**"),
    # bare dates, parenthetical or inline (after the labelled forms above)
    (r"\(\s*\d{4}-\d{2}-\d{2}\s*\)", ""),
    # full ISO datetimes (optional seconds/millis, optional Z) — embedded, no trailing \b
    (r"\b\d{4}-\d{2}-\d{2}T[0-9:.]+Z?", "the incident"),
    (r"\b\d{4}-\d{2}-\d{2}\b", ""),
    # internal incident/batch codenames (b33, b36, …) — internal tracking IDs; scrub like
    # ticket/msg IDs. Coarse fallback only: the Sonnet producer genericizes these contextually
    # ("b33" -> "the ticket-state-sweep incident"); this keeps the gate satisfiable for the
    # legacy regex producer. Lowercase-b + digits, so it never hits the `<date-Bnn>` placeholder.
    (r"\bb\d{2,3}\b", "an internal incident"),
]

# Cleanup pass — repair fragments left by the removals above.
CLEANUPS: list[tuple[str, str]] = [
    (r"\[\[\s*\]\]", ""),                    # empty wikilink [[ ]] (from emptied [[T-NNNN]]/[[msg]])
    (r"\bthe Orchestrator/Orchestrator\b", "the Orchestrator"),  # HO/Orchestrator doubling
    (r"\bOrchestrator/Orchestrator\b", "Orchestrator"),
    (r" +:\*\*", ":**"),                     # "decommission :**" -> "decommission:**"
    (r",?\s*via\s*\)", ")"),                 # "..., via )" / "...via )" -> ")"  (drop dangling via)
    (r"\(\s*via\s*\)", ""),                  # (via )
    (r"\(Telegram[ +,]*\)", ""),             # (Telegram +, )  <- msg-ids + via removed
    (r"\(\s*per\s*,?\s*\)", ""),             # (per , )
    (r"\(\s*\+\s*\)", ""),                   # ( + )
    (r"\((?:\s*,)+\s*\)", ""),               # (, ) / (, , ) -> empty (comma-only parens)
    (r"\(\s*,\s*", "("),                     # ( , foo  -> (foo
    (r",\s*\)", ")"),                        # foo, )   -> foo)
    (r",\s*,", ","),                         # double comma
    (r"\s+,", ","),                          # space-comma
    (r"\(\s+", "("),                         # ( foo -> (foo
    (r"\s+\)", ")"),                         # foo ) -> foo)
    (r"\(\s*\)", ""),                        # ( )  (final empty-paren sweep, after comma collapses)
    (r"\bper\s+/\s*", "per "),               # "per  / X" (citation T-NNNN / Y) -> "per X"
    (r"\s+per\s*:", ":"),                    # "...per :"  -> "...:"  (per T-NNNN: removed)
    (r"\s+per\s+(?=[—–-])", " "),            # "...per  —" (per msg-NNNN —) -> "... —"
    (r"\s+via\s+(?=[—–-])", " "),            # "...via  —" (via T-NNNN —) -> "... —"
    (r"\s+;", ";"),                          # space-semicolon (left by removed citation)
    (r" +\.", "."),                          # space-period
    (r" {2,}", " "),                         # collapse runs of spaces (not newlines)
    (r"(?m)^\*\*[A-Z][A-Za-z /]*:\*\*\s*$\n", ""),  # drop metadata stubs emptied by scrubbing (e.g. **Date authored:**)
    (r"[ \t]+$", ""),                        # trailing whitespace (MULTILINE)
    (r"\n{3,}", "\n\n"),                     # collapse 3+ blank lines
]

# Fail-on-leak gate. Any match in the SCRUBBED text aborts the run.
# (label, pattern) — keep aligned with the spec; over-eager is safe (fails closed).
LEAK_PATTERNS: list[tuple[str, str]] = [
    ("operator-name", r"(?i)\bmichael\b"),
    ("ticket-id", r"\bT-\d{3,4}\b"),
    ("message-id", r"(?i)msg[ _-]?\d+"),
    ("iso-date", r"\d{4}-\d{2}-\d{2}"),
    ("commit-hash", r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),
    ("home-path", r"/home/claude"),
    ("host-swarm", r"(?i)swarm"),
    ("bot-name", r"\bc_\w+_bot\b"),
    ("numeric-id", r"\b\d{9,12}\b"),
    ("account-name", r"\bclaude-(?:fallback|research|telegram)\b"),
    ("orchestrator-abbrev", r"\bHO\b"),
    # internal incident/batch codenames (b33, b36, …) — caught after the Sonnet+reviewer pass
    # nearly shipped them inconsistently (b35 scrubbed, b36 kept) to the public seed (T-1327).
    ("incident-codename", r"\bb\d{2,3}\b"),
    # NOTE: project names (kino/codex/echo) are deliberately NOT a leak class — kept per
    # operator decision (codenames, not sensitive). Account/service names stay scrubbed.
    ("sudoers-file", r"\b(?:fallback-rescues-prime|50-claude-fallback-research)\b"),
    # product-direction (operator: scrub even though names are kept) — enforce the scrub
    ("product-direction", r"\b(?:revenue|voice interface)\b"),
]


def scrub(text: str) -> str:
    for pat, repl in SUBSTITUTIONS:
        text = re.sub(pat, repl, text, flags=re.MULTILINE)
    for pat, repl in CLEANUPS:
        text = re.sub(pat, repl, text, flags=re.MULTILINE)
    return text


def find_leaks(text: str) -> list[tuple[str, str, int]]:
    """Return (label, matched_text, lineno) for every surviving leak marker."""
    leaks: list[tuple[str, str, int]] = []
    for label, pat in LEAK_PATTERNS:
        for m in re.finditer(pat, text):
            lineno = text.count("\n", 0, m.start()) + 1
            leaks.append((label, m.group(0), lineno))
    return leaks


def add_banner(text: str) -> str:
    """Insert the worked-example banner after the YAML frontmatter (or at top)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            head = text[: end + len("\n---\n")]
            rest = text[end + len("\n---\n") :]
            # place banner after the first H1 if present, else right after frontmatter
            h1 = re.search(r"(?m)^# .*$", rest)
            if h1:
                idx = h1.end()
                return head + rest[:idx] + "\n\n" + WORKED_EXAMPLE_BANNER + rest[idx:]
            return head + "\n" + WORKED_EXAMPLE_BANNER + rest
    return WORKED_EXAMPLE_BANNER + "\n" + text


def process(path: Path, banner: bool = True) -> tuple[str, list[tuple[str, str, int]]]:
    text = path.read_text(encoding="utf-8")
    out = scrub(text)
    if banner:
        out = add_banner(out)
    leaks = find_leaks(out)
    return out, leaks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="scrub + gate, report residual leaks only; exit 2 if any")
    g.add_argument("--print", dest="do_print", action="store_true", help="scrub + gate, print scrubbed file to stdout")
    g.add_argument("--in-place", action="store_true", help="scrub + gate, write back ONLY if gate passes")
    ap.add_argument("--no-banner", action="store_true", help="skip the worked-example banner")
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args(argv)

    any_leak = False
    for path in args.files:
        if not path.exists():
            print(f"ERROR: {path} does not exist", file=sys.stderr)
            return 3
        out, leaks = process(path, banner=not args.no_banner)
        if leaks:
            any_leak = True
            print(f"LEAK GATE FAILED for {path} — {len(leaks)} surviving marker(s):", file=sys.stderr)
            for label, txt, lineno in leaks:
                print(f"  line {lineno}: [{label}] {txt!r}", file=sys.stderr)
            continue  # never write/emit a leaking file
        if args.do_print:
            sys.stdout.write(out)
        elif args.in_place:
            path.write_text(out, encoding="utf-8")
            print(f"scrubbed (gate clean): {path}", file=sys.stderr)
        elif args.check:
            print(f"OK (gate clean): {path}", file=sys.stderr)

    return 2 if any_leak else 0


if __name__ == "__main__":
    raise SystemExit(main())
