#!/usr/bin/env python3
"""Recall-feedback hook — narrow, high-precision PreToolUse footgun guard (T-1090).

The Dei recall-hook design (T-0672 / notes/dei-foundational-wip/recall-hook.md)
DECLINED the broad "every user-turn fuzzy keyword recall" framing as
bloat-positive and lowest-leverage, and locked in ONE narrow slice worth
building: a PreToolUse hook on mutating tools that surfaces ONLY
`feedback`(+`project`)-type memories matched on EXACT, low-false-positive
tokens. The target is the *invisible-unknown* — a footgun memory about a file
or tool the agent is about to touch but has no reason to grep for.

Locked design (from recall-hook.md §"Locked design"):
  * Trigger    : PreToolUse on Edit / Write / MultiEdit / NotebookEdit / Bash.
  * Detection  : EXACT tokens only — file paths in the tool args, `T-NNNN`,
                 `ORG-NNN`, and distinctive script/file basenames. NO fuzzy
                 domain-term matching (that is the bloat source the design cuts).
  * Corpus     : `feedback` (+ `project`) memory types only — the footgun class.
                 `reference`/`user` recall is what the agent greps deliberately.
  * Precision  : surface iff the memory's description/body contains the EXACT
                 token; cap N per call (default 2); per-session dedup (never
                 re-surface the same memory id within a session).
  * Injection  : `hookSpecificOutput.additionalContext` (PreToolUse, exit 0),
                 a model-only "recalled feedback (verify it still applies)" nudge.

Design invariants:
  * FAIL-OPEN. Any error, missing field, or unparseable input -> emit nothing,
    exit 0, let the tool run. This hook must NEVER block or break a tool call.
  * Stateless except a short-TTL per-session dedup dir.

Wiring (the interactive step, NOT done by building this file): add to
~/.claude/settings.json as a PreToolUse hook with matcher
"Edit|Write|MultiEdit|NotebookEdit|Bash", AND add this basename to the
HOOK_ALLOWLIST in tools/context_fill_monitor.py (per the wire-hook discipline,
else the T-0536 checker false-flags it as an injected hook).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Tools whose invocation is an "about-to-act" moment worth a footgun check.
MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}

# Memory types that hold invisible-unknown footguns. `reference`/`user` are
# excluded by design — those are what an agent greps for deliberately.
RECALL_TYPES = {"feedback", "project"}

# Max memories surfaced per single tool call (precision cap).
MAX_SURFACED = 2

# Per-session dedup state: surfaced memory names, keyed by session id.
SEEN_DIR = Path.home() / ".claude" / "hooks" / ".recall_seen"
SEEN_TTL_SECONDS = 24 * 3600

# High-signal exact-token patterns. Each is inherently distinctive (near-zero
# false-positive substring match against a memory body).
_RE_TICKET = re.compile(r"\bT-\d{4,}\b")  # T-3215
_RE_ORG = re.compile(r"\bORG-\d{3}\b")
# A path segment or filename with a known code/config extension.
_CODE_EXT = (
    r"py|md|sh|json|svelte|ts|js|jsx|tsx|yaml|yml|toml|cfg|ini|service|timer|"
    r"socket|env|lock|txt|sql"
)
_RE_PATHISH = re.compile(r"[\w./~-]*[\w-]+\.(?:" + _CODE_EXT + r")\b")
# A multi-segment path (contains a slash), e.g. tools/ticket.py, ~/.claude/x.
_RE_SLASHPATH = re.compile(r"[\w.~-]+(?:/[\w.~-]+)+")
# Shell command-segment separators — a command line may chain several commands.
_RE_CMD_SPLIT = re.compile(r"\|\||&&|[|;\n]")
# A balanced single/double-quoted span — stripped before signature extraction so
# quoted DATA args (commit messages, here-strings) never contribute command-word
# signatures (T-1133: a commit message containing ';' + prose split into a prose
# "segment" whose leading words formed the generic signature "this is the").
_RE_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# Common English stopwords — a multi-word signature composed ENTIRELY of these is
# generic prose, not a command (T-1133 backstop behind the quote-strip).
_STOPWORDS = frozenset(
    "a an and are as at be by do for from has have in is it its of on or that the "
    "their then this to up with you your".split()
)
# A "command word" — a bare alnum/dot/hyphen token that is NOT a flag, path,
# env-assignment, or redirect. The leading run of these per segment is the
# command + its subcommands (e.g. `git stash push`, `tailscale serve status`).
_RE_CMD_WORD = re.compile(r"^[a-zA-Z][\w.-]*$")
# Generic wrappers to skip when finding a segment's real leading command.
_CMD_WRAPPERS = {"sudo", "env", "time", "nohup", "exec", "command", "xargs", "then", "do"}
# Shell control-flow OPENERS (T-1138): a segment whose leading command-word is one
# of these is loop/conditional PLUMBING, not a command — its leading run mines the
# keyword + the loop variable (often a 1-char, non-stopword token like `m`/`t`),
# producing generic signatures ("for m", "while read") that substring-match prose
# in memory descriptions. The actual commands live in the `do`/`then` segments
# (split off on `;`), which mine normally, so we just drop the opener-led segment.
_CONTROLFLOW_OPENERS = {"for", "while", "until", "if", "elif", "case", "select", "function"}
# Read-only commands: a footgun guard fires at the about-to-MUTATE moment, so a
# pure read (cat/ls/grep/…) should never trigger recall. If EVERY chained
# segment leads with one of these, skip the call entirely.
_READONLY_CMDS = {
    "cat", "ls", "ll", "grep", "rg", "egrep", "fgrep", "head", "tail", "find",
    "echo", "wc", "sort", "uniq", "diff", "stat", "which", "file", "less",
    "more", "tree", "pwd", "cd", "test", "true", "false", "date", "whoami",
    "printenv", "column", "cut", "tr", "jq", "basename", "dirname", "realpath",
    "readlink", "du", "df", "ps", "top", "id", "groups", "hostname",
}


def _memory_dir() -> Path:
    """Resolve the memory corpus dir (overridable for tests)."""
    env = os.environ.get("RECALL_MEMORY_DIR")
    if env:
        return Path(env)
    # Recovery's canonical memory dir. Falls back to a glob if the slug differs.
    default = Path.home() / ".claude" / "projects" / "-home-claude-fallback" / "memory"
    if default.is_dir():
        return default
    matches = sorted(Path.home().glob(".claude/projects/*/memory"))
    return matches[0] if matches else default


def extract_tokens(tool_name: str, tool_input: dict) -> set[str]:
    """Extract the set of EXACT high-signal tokens from a tool call's args.

    Edit/Write/MultiEdit/NotebookEdit -> the file_path (the file about to be
    touched). Bash -> the command string (paths + script names + ids in it).
    Returns lowercased tokens plus their basenames; ids stay as-is.
    """
    surface_parts: list[str] = []
    if tool_name == "Bash":
        surface_parts.append(str(tool_input.get("command", "")))
    else:
        # Edit / Write / MultiEdit / NotebookEdit all carry a file path.
        for key in ("file_path", "notebook_path", "path"):
            v = tool_input.get(key)
            if v:
                surface_parts.append(str(v))
    surface = "\n".join(surface_parts)
    if not surface.strip():
        return set()

    tokens: set[str] = set()
    # Ticket / ORG ids — keep canonical case (T-NNNN / ORG-NNN).
    for m in _RE_TICKET.findall(surface):
        tokens.add(m)
    for m in _RE_ORG.findall(surface):
        tokens.add(m)
    # Path-ish tokens: full path + basename, lowercased.
    for rx in (_RE_PATHISH, _RE_SLASHPATH):
        for raw in rx.findall(surface):
            raw = raw.strip("/.~")
            if not raw:
                continue
            tokens.add(raw.lower())
            base = raw.rsplit("/", 1)[-1]
            if base and base != raw:
                tokens.add(base.lower())
    # Multi-word command signatures (Bash only): the leading command +
    # subcommand n-grams (e.g. `git stash`, `tailscale serve status`). These
    # are distinctive as PHRASES — low false-positive — and catch the large
    # footgun class keyed on a command rather than a file (the locked design's
    # "function/command names" key, kept precise via the ≥2-word requirement).
    if tool_name == "Bash":
        if _is_all_readonly(surface):
            return set()
        tokens.update(_command_signatures(surface))
    return {t for t in tokens if _is_distinctive(t)}


def _is_all_readonly(command: str) -> bool:
    """True iff every chained segment leads with a read-only command.

    A pure read (`cat x`, `ls y && grep z`) is not an about-to-mutate moment, so
    the footgun guard stays silent. A single mutating segment in the chain
    (`cat x && systemctl restart y`) disables the skip.
    """
    segments = [s.strip() for s in _RE_CMD_SPLIT.split(command) if s.strip()]
    if not segments:
        return False
    for seg in segments:
        lead = None
        for tok in seg.split():
            if tok.startswith("-") or "=" in tok or tok in _CMD_WRAPPERS:
                continue
            lead = tok.lower()
            break
        if lead is None or lead not in _READONLY_CMDS:
            return False
    return True


def _all_stopwords(sig: str) -> bool:
    """True if every word in a signature is a common English stopword (generic
    prose like 'this is the', not a real command). T-1133 backstop."""
    return all(w in _STOPWORDS for w in sig.split())


# ──────────────────────────────────────────────────────────────────────────────
# SUBSTRATE WATCH (T-1138): the multi-word command-signature matcher has now had
# TWO false-positive-class fixes in one day — T-1133 (quoted-prose: quote-strip +
# stopword guard) and T-1138 (control-flow openers: drop opener-led segments).
# Both were clean STRUCTURAL eliminations (a whole class each), so this is healthy
# hardening of a young matcher, NOT yet stacking. But the root shape — extracting
# arbitrary multi-word phrases from a command line and SUBSTRING-matching them
# against free PROSE in memory descriptions — has an inherent noise floor.
# If a THIRD distinct FP class surfaces here, PAUSE the reflexive patch and apply
# lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md: ask whether
# the matcher SUBSTRATE is the problem (e.g. match command phrases only against a
# distinctive/structured signature surface, not free description prose) before
# shipping amendment #3. Two clean eliminations = healthy; a third = the smell.
# ──────────────────────────────────────────────────────────────────────────────


def _command_signatures(command: str) -> set[str]:
    """Extract leading 2-word and 3-word command signatures from a shell line.

    Strips quoted spans first (quoted prose is a DATA arg, not the argv head),
    then splits on `&&`/`||`/`|`/`;`/newline; for each segment takes the leading
    run of bare command words (skipping wrappers like `sudo`, flags, paths, and
    env-assignments) and emits the 2-gram and 3-gram prefixes. Multi-word only —
    a bare `git`/`ls` is too generic to match precisely. Signatures composed
    entirely of stopwords are dropped (T-1133: quote-strip is the structural fix,
    the stopword guard is defense-in-depth for prose that escapes it, e.g. an
    unbalanced quote). A segment whose leading command-word is a control-flow
    opener (`for`/`while`/`if`/...) is dropped entirely (T-1138): it's loop/
    conditional plumbing, not a command, and its leading run would mine the
    keyword + the loop variable (`for m`, `while read`) as a generic signature."""
    sigs: set[str] = set()
    for segment in _RE_CMD_SPLIT.split(_RE_QUOTED.sub(" ", command)):
        words: list[str] = []
        for tok in segment.strip().split():
            if tok.startswith("-"):
                continue  # a flag (e.g. --user, -C) — skip, don't terminate
            if "=" in tok:
                continue  # env assignment like FOO=bar
            if tok in _CMD_WRAPPERS:
                continue
            if not _RE_CMD_WORD.match(tok):
                # A path, quote, redirect, or operator ends the leading run.
                if words:
                    break
                continue
            if not words and tok.lower() in _CONTROLFLOW_OPENERS:
                break  # T-1138: control-flow head — not a command; drop this segment
            words.append(tok.lower())
            if len(words) >= 3:
                break
        if len(words) >= 2:
            sig2 = " ".join(words[:2])
            if not _all_stopwords(sig2):
                sigs.add(sig2)
        if len(words) >= 3:
            sig3 = " ".join(words[:3])
            if not _all_stopwords(sig3):
                sigs.add(sig3)
    return sigs


def _is_distinctive(token: str) -> bool:
    """A token earns a substring match only if it's inherently low-FP.

    Ticket/ORG ids always qualify. Otherwise require length >= 5 AND a path
    separator or an extension dot — so `x.py` (too generic) and bare words are
    rejected, but `ticket.py`, `tools/x.py`, `settings.json` qualify.
    """
    if _RE_TICKET.fullmatch(token.upper()) or _RE_ORG.fullmatch(token.upper()):
        return True
    if " " in token:
        return True  # multi-word command signature — distinctive as a phrase
    if len(token) < 5:
        return False
    return ("/" in token) or ("." in token)


def _parse_memory(path: Path) -> tuple[str, str] | None:
    """Return (mem_type, haystack) for a memory file, or None if unreadable.

    `mem_type` is taken from the frontmatter `type:` line (handles both the
    nested `metadata:\\n  type:` and flat `type:` forms), falling back to the
    filename prefix. `haystack` is the lowercased DESCRIPTION (the memory's
    one-line subject) plus the name slug — NOT the full body. Matching on the
    subject, not incidental body mentions, is the precision floor: a footgun
    memory names its trigger (file/command/id) in its description; a body-only
    mention is usually an incidental reference, not what the memory is about.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    mem_type = ""
    description = ""
    for line in text.splitlines()[:12]:
        s = line.strip()
        if s.startswith("type:") and not s.startswith("node_type:"):
            mem_type = s.split(":", 1)[1].strip()
        elif s.startswith("description:"):
            description = s.split(":", 1)[1].strip()
    if not mem_type:
        mem_type = path.name.split("_", 1)[0]
    # Name slug joins matching too — its underscores normalize to spaces so a
    # `git stash`-style command phrase can hit `feedback_git_stash_...`.
    slug = path.stem.replace("_", " ")
    return mem_type, (description + "\n" + slug).lower()


def match_memories(tokens: set[str], memory_dir: Path) -> list[tuple[str, str, str]]:
    """Find feedback/project memories whose text contains an exact token.

    Returns a ranked list of (memory_name, token, description) — most specific
    token first (ids > slash-paths > basenames), then longer tokens. At most
    one entry per memory (its best-matching token).
    """
    if not tokens or not memory_dir.is_dir():
        return []
    matches: list[tuple[str, str, str]] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        parsed = _parse_memory(path)
        if parsed is None:
            continue
        mem_type, haystack = parsed
        if mem_type not in RECALL_TYPES:
            continue
        hit = _best_token(tokens, haystack)
        if hit is not None:
            matches.append((path.stem, hit, _description_of(path)))
    matches.sort(key=lambda m: _token_rank(m[1]), reverse=True)
    return matches


def _best_token(tokens: set[str], haystack: str) -> str | None:
    """Return the most-specific token present in haystack, or None."""
    present = [t for t in tokens if t.lower() in haystack]
    if not present:
        return None
    return max(present, key=lambda t: (_token_rank(t), len(t)))


def _token_rank(token: str) -> tuple[int, int]:
    """Specificity rank: ids highest, then slash-paths, then the rest."""
    if _RE_TICKET.fullmatch(token.upper()) or _RE_ORG.fullmatch(token.upper()):
        return (3, len(token))
    # A slash-path or a 3-word command phrase is more specific than a bare
    # basename or 2-word phrase.
    word_count = token.count(" ") + 1
    if "/" in token or word_count >= 3:
        return (2, len(token))
    return (1, len(token))


def _description_of(path: Path) -> str:
    """Pull the frontmatter `description:` line for the nudge text."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:12]:
            s = line.strip()
            if s.startswith("description:"):
                return s.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _load_seen(session_id: str) -> tuple[Path | None, set[str]]:
    """Load the per-session set of already-surfaced memory names."""
    if not session_id:
        return None, set()
    try:
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
        _prune_seen()
    except OSError:
        return None, set()
    f = SEEN_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', session_id)}.json"
    seen: set[str] = set()
    if f.is_file():
        try:
            seen = set(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()
    return f, seen


def _prune_seen() -> None:
    now = time.time()
    for old in SEEN_DIR.glob("*.json"):
        try:
            if now - old.stat().st_mtime > SEEN_TTL_SECONDS:
                old.unlink()
        except OSError:
            pass


def _save_seen(f: Path | None, seen: set[str]) -> None:
    if f is None:
        return
    try:
        f.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError:
        pass


def build_context(surfaced: list[tuple[str, str, str]]) -> str:
    """Render the model-only additionalContext nudge."""
    lines = [
        "🧠 Recalled feedback (verify it still applies — these reflect "
        "when-written state; confirm the named file/tool/id still exists):"
    ]
    for name, token, desc in surfaced:
        suffix = f" — {desc}" if desc else ""
        lines.append(f"• [[{name}]] (matched `{token}`){suffix}")
    return "\n".join(lines)


def _emit(context: str) -> None:
    """Emit a non-blocking PreToolUse context injection and exit 0."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


def _allow() -> None:
    """No output -> defer to normal flow (tool runs)."""
    sys.exit(0)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        _allow()
        return

    tool_name = data.get("tool_name", "")
    if tool_name not in MUTATING_TOOLS:
        _allow()
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        _allow()
        return

    try:
        tokens = extract_tokens(tool_name, tool_input)
        if not tokens:
            _allow()
            return
        candidates = match_memories(tokens, _memory_dir())
        if not candidates:
            _allow()
            return

        session_id = str(data.get("session_id", ""))
        seen_file, seen = _load_seen(session_id)
        fresh = [m for m in candidates if m[0] not in seen][:MAX_SURFACED]
        if not fresh:
            _allow()
            return

        for name, _tok, _desc in fresh:
            seen.add(name)
        _save_seen(seen_file, seen)
        _emit(build_context(fresh))
    except Exception:
        # FAIL-OPEN — never let recall break a tool call.
        _allow()


if __name__ == "__main__":
    main()
