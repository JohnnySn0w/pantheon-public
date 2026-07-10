#!/usr/bin/env python3
"""PreToolUse hook — structural guardrail against off-brief SUBAGENT drift (T-1345).

Trigger incident: 2026-06-27 echo-4c3-impl runaway. A drifting implementer subagent
did the straightforward-obvious thing at each step — read the Telegram bot token,
`curl`ed the Bot API directly to message the human (bypassing reply-tool discipline),
`git push`/`gh pr create`d to a shared repo, and committed a false ledger entry
impersonating the head orchestrator. Persona PROSE already forbade every one of these;
a drifting agent ignored the prose. This hook makes the prohibitions STRUCTURAL.

Threat model: DRIFT, not an adversary. A confused agent takes the obvious path; it does
not actively evade a deny that surfaces as an error. So a precise default-ALLOW guard
that blocks only the known-bad obvious commands is genuinely effective here (the
"bypassable via python/compound-cmd" caveat matters only vs. an adversary — out of scope;
that needs OS sandboxing, an escalation, not this hook).

Mechanism (verified against code.claude.com/docs/en/hooks): the PreToolUse input carries
`agent_type` ONLY when the hook fires inside a subagent (for custom subagents it is the
persona's frontmatter `name`, e.g. "implementer"). On the MAIN thread (the head
orchestrator) it is ABSENT. So:
  - agent_type absent  → main-thread/HO → ALLOW everything (this guard never restricts HO).
  - agent_type present → subagent → block the drift-dangerous obvious paths below.

FAIL-OPEN: any parse error / ambiguity → allow. A guard must never brick the pipeline.
Default-ALLOW: only the explicit patterns below block; cargo/git-commit/normal work pass.

Two tool surfaces are guarded: drift-dangerous Bash commands, and Edit/Write/
NotebookEdit of protected canonical docs (T-1381 — the Bash matcher alone let a
subagent mutate canonical docs via the Edit/Write tools without ever touching Bash).

Wire as a PreToolUse hook with matcher "Bash|Edit|Write|MultiEdit|NotebookEdit"
in settings.json (modeled on mcp-list-guard.py / T-0996 careful-deploy convention).
The matcher MUST include the file-edit tools or the canonical-doc branch never
fires; the guard itself still HO-exempts (agent_type absent) and fails open.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# --- Patterns blocked for ANY subagent (no worker has a legitimate need) ---

# Reading this account's Telegram bot token file (the raw Bot API credential).
# The legit reply path reads it inside the MCP plugin, never via the agent's Bash.
_TOKEN_READ = re.compile(r"\.claude/channels/telegram/\.env\b")

# Hitting the Telegram Bot API directly (raw sendMessage etc. = bypassing reply-tool).
_BOT_API = re.compile(r"api\.telegram\.org/bot", re.IGNORECASE)

# Writing/committing the orchestrator-owned shared records (ledger / ADRs / notes).
# Matches redirection, tee, or git add/commit touching these pantheon-config paths.
_HO_RECORDS = re.compile(
    r"pantheon-config/(ledger|adr)/|"
    r"pantheon-config/[^ ]*notes/|"
    # `ledger/<year>` as a path token — word-boundary (not leading-slash) anchored so
    # cwd-relative / `git -C <repo>` forms (e.g. `git add ledger/2026-W26/...`) are
    # caught too, not just absolute `/ledger/2026` paths. `ledger/<year>` is a
    # pantheon-config-specific shape (no false-positive on echo, which has no ledger/).
    r"\bledger/\d{4}",
)

# --- T-1600: shape-vs-substance refinement for the _HO_RECORDS match surface ---
#
# Two live misfires (2026-07-06/07) were pure text-match-on-command-STRING shape:
#   #1 a read-only `grep`/`cat` MENTIONING a ledger path (no write) was blocked.
#   #2 a clean-scope `git commit` was blocked because the commit MESSAGE (-m text)
#      mentioned a protected path while the staged file scope was verified clean.
#
# Fix: match against the command's real argument surface, not the whole raw string.
#   - Reconstruct the surface via `shlex.split` + rejoin. This is more than cosmetic:
#     POSIX word-splitting joins adjacent quoted/unquoted fragments into ONE token, so
#     rejoining CLOSES a pre-existing evasion gap the old raw-string match never
#     caught — `git add l"edger"/2026-W27/x.jsonl` (quote-splitting the guarded word,
#     the NAMED off-limits evasion class) never matched `\bledger/\d{4}` against the
#     raw string (the embedded quote breaks contiguity), but reconstructs to the plain
#     `ledger/2026-W27/x.jsonl` operand after tokenization — so it's caught now.
#   - For a recognized plain `git commit` (or `git -C <dir> commit`) shape, drop the
#     -m/--message VALUE from the surface before matching (misfire #2). The hook only
#     sees the command string, not actual staged-file state, so this is operand-scope
#     matching, not true staged-scope inspection — any commit shape we can't
#     confidently parse this way keeps the OLD whole-surface match as a conservative
#     fallback (documented per-function below).
#   - A single conservative read-only verb allowlist (misfire #1): grep/rg/cat/head/
#     tail/ls/wc/less/more, `find` without a WRITING primary (-delete/-exec/-execdir/
#     -ok/-okdir/-fprintf/-fprint/-fprint0/-fls), and `git log/show/diff/status/blame`
#     without an `--output[=X]` flag. Applies ONLY to a single, unambiguous,
#     non-redirecting invocation — anything compound (;, &&, ||, |, backticks, $(...))
#     or containing redirection (>, >>, <) falls straight through to the ordinary
#     match (fail-closed on ambiguity; also catches sed -i / tee / sponge / rm / mv /
#     cp targeting a protected path, none of which are on the read-only allowlist).
#
# Round-1 adversarial review (T-1600) found two holes in the FIRST cut of this
# allowlist, both "verb looks read-only but a flag diverts output to a file":
#   HOLE A: `find`'s -fprintf/-fprint/-fprint0/-fls write formatted output to a
#     file argument — `find /tmp -fprintf <protected-path> '%p\n'` was wrongly
#     allowed. Fixed by adding all four to `_FIND_MUTATING_FLAGS`.
#   HOLE B: `git log/show/diff --output=<file>` (or `--output <file>`) write real
#     content to an arbitrary path with no shell redirect char — `_REDIRECT_RE`
#     never sees it. Fixed: `_is_read_only_verb` now rejects log/show/diff whose
#     tokens carry `--output`/`--output=...` (see `_has_output_flag`), falling
#     through to the ordinary conservative match.
# General lesson carried forward: any read-only-LOOKING verb with an
# output-diverting flag is a hole in this class of allowlist — if a future verb
# is added, audit it for such flags first.
#
# Chain-coverage note (why stripping -m from `git commit` doesn't reopen a write
# path): getting protected content STAGED in the first place still requires either
# `git add <protected-path>` (blocked — `add` isn't a read-only verb, so the
# rejoined-token surface matches _HO_RECORDS) or an Edit/Write to the protected path
# (blocked by the separate _PROTECTED_DOC branch below). So a `git commit` whose
# message merely MENTIONS a protected path, with nothing protected actually staged,
# is the only shape this refinement newly allows — which is exactly misfire #2.
#
# Threat model unchanged from the module's original framing: DRIFT, not an adversary.
# This refinement does not attempt to defeat shell-variable-substitution-based evasion
# (e.g. `F=led...; git add "pantheon-config/$F/2026-W27/x"`) — that's beyond a
# confused-agent threat model and falls through the compound-structure check anyway.

_COMPOUND_RE = re.compile(r"[;&|`]|\$\(")
_REDIRECT_RE = re.compile(r">>|>|<")

_READ_ONLY_VERBS = frozenset({"grep", "rg", "cat", "head", "tail", "ls", "wc", "less", "more"})
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({"log", "show", "diff", "status", "blame"})
# `find` primaries that WRITE somewhere (not just read/report): -delete/-exec*/-ok*
# act directly; -fprintf/-fprint/-fprint0/-fls write formatted output to a FILE
# argument (round-1 reviewer HOLE A: these were the four missing from the original
# set, letting e.g. `find ... -fprintf <protected-path> '%p\n'` slip through as
# "read-only").
_FIND_MUTATING_FLAGS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprintf", "-fprint", "-fprint0", "-fls",
})


def _tokens_or_none(command: str) -> list[str] | None:
    """Tokenize `command` iff it is a single, unambiguous shell invocation.

    Returns None for compound shell structure (;, &&, ||, |, backticks, $(...)) or
    unbalanced/unparseable quoting — callers must then fall back to matching the
    raw command string (the pre-T-1600 conservative behavior). Fail-closed on
    ambiguity, never fail-open.
    """
    if _COMPOUND_RE.search(command):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _git_subcommand(tokens: list[str]) -> str | None:
    """Best-effort git subcommand, skipping a leading `-C <dir>` global flag."""
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    if i < len(tokens) and tokens[i] == "-C":
        i += 2
    if i < len(tokens) and not tokens[i].startswith("-"):
        return tokens[i]
    return None


def _has_output_flag(tokens: list[str]) -> bool:
    """True if any token is `--output` (value in the NEXT token) or `--output=X`
    (fused). `git log`/`show`/`diff` all accept this and WRITE the real diff/log
    content to the named file — no shell redirect char involved, so `_REDIRECT_RE`
    never sees it (round-1 reviewer HOLE B)."""
    return any(tok == "--output" or tok.startswith("--output=") for tok in tokens)


def _is_read_only_verb(tokens: list[str]) -> bool:
    """True for a single simple invocation of a read-only verb (see allowlist above).

    Callers only invoke this after `_tokens_or_none` succeeded (unambiguous, non-
    compound) AND the raw command has no redirection characters — both checked by
    `_ho_records_match` before calling this.
    """
    if not tokens:
        return False
    verb = tokens[0]
    if verb == "git":
        sub = _git_subcommand(tokens)
        if sub not in _GIT_READ_ONLY_SUBCOMMANDS:
            return False
        # HOLE B: log/show/diff with --output[=X] write to a file argument —
        # not read-only. Fall through to the conservative match instead.
        return not _has_output_flag(tokens)
    if verb == "find":
        return not any(tok in _FIND_MUTATING_FLAGS for tok in tokens)
    return verb in _READ_ONLY_VERBS


def _is_short_cluster_ending_in_m(tok: str) -> bool:
    """True for a combined short-option cluster ending in the message flag `m`
    with no fused value — e.g. `-am` in `git commit -am "msg"` — so the NEXT
    token is the message value (round-1 reviewer nice-to-have: bare `-m` was
    handled but `-am` still over-blocked on message text). Deliberately narrow:
    single-dash, letters-only cluster of length > 1 ending in `m`; excludes the
    bare `-m` (length 1 after the dash), which is matched separately.
    """
    if len(tok) < 3 or tok[0] != "-" or tok[1] == "-":
        return False
    body = tok[1:]
    return body.isalpha() and body[-1] == "m"


def _drop_commit_message(tokens: list[str]) -> list[str]:
    """For a recognized plain `git commit` (or `git -C <dir> commit`) token list,
    return it with the -m/--message/-<cluster>m VALUE removed — message text is
    not a file operand and must not feed protected-path matching (T-1600 misfire
    #2). Any other shape (not a recognized commit invocation) is returned
    UNCHANGED, so the caller's match against the rejoined surface stays the
    conservative fallback.
    """
    if not tokens or tokens[0] != "git":
        return tokens
    i = 1
    if i < len(tokens) and tokens[i] == "-C":
        i += 2
    if i >= len(tokens) or tokens[i] != "commit":
        return tokens
    kept = tokens[: i + 1]
    j = i + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok in ("-m", "--message") or _is_short_cluster_ending_in_m(tok):
            j += 2
            continue
        if tok.startswith("--message="):
            j += 1
            continue
        kept.append(tok)
        j += 1
    return kept


def _ho_records_match(command: str) -> bool:
    """True if `command`'s genuine argument surface touches an HO-owned protected
    path. Excludes: single read-only-verb invocations (misfire #1) and a `git
    commit`'s -m/--message text (misfire #2). Falls back to matching the whole raw
    command whenever the shape can't be confidently classified — redirection,
    compound shell structure, or unparseable quoting all fail closed to the
    pre-T-1600 behavior (still block on any mention).
    """
    if _REDIRECT_RE.search(command):
        return bool(_HO_RECORDS.search(command))
    tokens = _tokens_or_none(command)
    if not tokens:
        return bool(_HO_RECORDS.search(command))
    if _is_read_only_verb(tokens):
        return False
    tokens = _drop_commit_message(tokens)
    return bool(_HO_RECORDS.search(" ".join(tokens)))

# --- Protected canonical docs: blocked for ANY subagent via the Edit/Write tools ---
# ORG-A2 / T-1381 (external audit 2026-06-28): the Bash patterns above guard the
# git/redirect path to HO-owned records, but a subagent using the Edit/Write/
# NotebookEdit *tools* never touched Bash and so bypassed the guard entirely — it
# could mutate any canonical doc (rulebook, ADR, ledger, notes, bugs, incidents,
# lessons, SOPs, READMEs/spine indexes, BACKLOG, goals) directly. This pattern
# makes the D2 canonical-doc protection structural across the Edit/Write surface.
#
# Anchored on the pantheon-config repo DIR (incl. /tmp worktrees named
# `pantheon-config-<ticket>/`) so it (a) catches worker worktrees and (b) never
# false-positives on a SIBLING repo's docs — an echo implementer editing
# echo-prismatic/README.md or echo's bugs/ must stay ALLOWED. Code paths
# (tools/, crates/, src/, tests/) are NOT protected — writing code is the
# implementer's job. Worker evidence dirs (screenshots/, knowledge/) stay open.
_PROTECTED_DOC = re.compile(
    r"pantheon-config(-[^/]*)?/"  # repo dir; (-[^/]*)? also matches /tmp worktrees
    r"(?:"
    r"(?:rulebook|adr|ledger|notes|bugs|incidents|lessons|sop)/"  # protected dirs
    r"|BACKLOG\.md"
    r"|goals\.md"
    r"|(?:[^ ]*/)?README\.md"  # any README (autogen spine index) at any depth
    r")"
)

# --- Push/PR/merge: blocked for ANY subagent (HO-exclusive) ---
# Push/PR/merge to shared repos is reserved to the HEAD ORCHESTRATOR (agent_type
# absent → exempted by the early return in decide()). EVERY subagent — implementer,
# reviewer, project-orchestrator, researcher, AND every *named instance* of them —
# commits on its branch only and reports to HO, who pushes after review.
#
# Why blanket-block instead of an {implementer, reviewer} allowlist (the original
# T-1345 shape): the 2026-06-27 T-1336 incident showed a NAMED implementer instance
# (`name: "t1336-impl2"`) carried agent_type = the instance name, NOT the persona name
# "implementer" — so the exact-match allowlist missed it and the worker self-merged an
# unreviewed, incomplete change to main. agent_type's value is spawn-shape-dependent and
# not reliable to allowlist; its mere PRESENCE (= "is a subagent") is. So gate on presence:
# any subagent is denied push. Fail-closed for the highest-consequence action. (The
# orchestrator-pushes-after-APPROVE convenience is retired in favour of HO-exclusive push.)
_PUSH = re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+(create|merge)\b")


def _reason(what: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED (worker-guard, T-1345): a subagent attempted {what}. This is "
                "reserved to the head orchestrator / project-orchestrator tier and is "
                "structurally denied to workers after the 2026-06-27 runaway. If you "
                "believe you need this, STOP and report the need to your dispatcher in "
                "your final message — do not route around this guard."
            ),
        }
    }


def decide(data: dict) -> dict | None:
    """Return a deny-payload to BLOCK, or None to ALLOW.

    Allows everything on the main thread (agent_type absent). For subagents, blocks
    the explicit drift-dangerous patterns only; everything else passes. Covers two
    tool surfaces: drift-dangerous Bash commands, and Edit/Write/NotebookEdit of
    protected canonical docs (the audit-found gap, T-1381).
    """
    agent_type = data.get("agent_type")
    if not agent_type:
        return None  # main thread / HO — never restricted by this guard

    tool_name = data.get("tool_name")

    if tool_name == "Bash":
        command = (data.get("tool_input") or {}).get("command") or ""
        if not command:
            return None
        if _TOKEN_READ.search(command):
            return _reason("to read the Telegram bot token file directly")
        if _BOT_API.search(command):
            return _reason("to contact the Telegram Bot API directly (use the reply tool via your dispatcher)")
        if _ho_records_match(command):
            return _reason("to write/commit orchestrator-owned records (ledger / ADR / notes)")
        if _PUSH.search(command):
            return _reason("to git push / open / merge a PR (push/PR/merge is HO-exclusive; every subagent — including project-orchestrators and named instances — commits on its branch only and reports to HO, who pushes after review)")
        return None

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and _PROTECTED_DOC.search(path):
            return _reason(
                "to Edit/Write a protected canonical doc (rulebook / ADR / ledger / "
                "notes / bugs / incidents / lessons / sop / README / BACKLOG / goals) — "
                "these are orchestrator-owned; propose the change to your dispatcher instead"
            )
        return None

    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # fail-open
    try:
        payload = decide(data)
    except Exception:
        sys.exit(0)  # fail-open
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
