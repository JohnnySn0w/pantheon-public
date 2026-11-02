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
Two fail-CLOSED exceptions (T-4389, security-gating hooks fail closed on their own clock):
a subagent decision not finished DECIDE_DEADLINE_S after PROCESS START is DENIED by main() instead of being
left to the harness hook timeout (600s default, and a timed-out hook is non-blocking, so the
call would PROCEED); and a subagent Bash command longer than BASH_COMMAND_MAX_CHARS is denied
by decide() before any parsing.
Default-ALLOW: only the explicit patterns below block; cargo/git-commit/normal work pass.
One exception (T-4145): the gh CLI is default-DENY for subagents. Only allowlisted
(group, verb) reads pass, and anything the hook cannot read fails closed.

Four tool surfaces are guarded: drift-dangerous Bash commands; Edit/Write/
NotebookEdit of protected canonical docs (T-1381 — the Bash matcher alone let a
subagent mutate canonical docs via the Edit/Write tools without ever touching Bash);
Read of a credential file (T-4139, the Read-tool sibling of T-4135's Bash-only
credential-file coverage); and a Grep whose `path` operand names a credential file
directly (T-4139; directory-scoped Grep is a named, out-of-scope gap).

Wire as a PreToolUse hook with matcher "Bash|Edit|Write|MultiEdit|NotebookEdit"
in settings.json (modeled on mcp-list-guard.py / T-0996 careful-deploy convention).
The matcher MUST include the file-edit tools or the canonical-doc branch never
fires; the guard itself still HO-exempts (agent_type absent) and fails open.
T-4139 note: the live matcher does NOT yet include "Read|Grep", so the two new
branches below are dormant until the matcher is widened — that widening is the
head orchestrator's, deliberately out of this ticket's scope.

T-4177 (entry shim): this module is the guard BODY. The settings.json / codex command target
stays `tools/hooks/worker-guard.py`, now a thin shim that reads stdin, returns a silent allow
for any payload that cannot carry an `agent_type` key (no per-call import or compile of this
file on the main thread), and otherwise imports this module (cached .pyc) and calls
`main(raw)`. Run directly (`python3 worker_guard_impl.py`) this file is the pre-shim guard,
byte for byte in behavior; tools/test_worker_guard_shim.py uses that as the oracle.
"""

from __future__ import annotations

import bisect
import contextlib
import fnmatch
import functools
import heapq
import json
import os
import re
import shlex
import sys

# --- Patterns blocked for ANY subagent (no worker has a legitimate need) ---

# Reading this account's Telegram bot token file (the raw Bot API credential).
# The legit reply path reads it inside the MCP plugin, never via the agent's Bash.
_TOKEN_READ = re.compile(r"\.claude/channels/telegram/\.env\b")

# Hitting the Telegram Bot API directly (raw sendMessage etc. = bypassing reply-tool).
_BOT_API = re.compile(r"api\.telegram\.org/bot", re.IGNORECASE)

# T-5332: the shell names every shell-aware rule reads as a shell (`rbash` is bash in
# restricted mode; `busybox sh|ash|hush` and `static-sh` are busybox's shells). One set,
# shared by the extglob rule, the code-string walk (`_PY_CODE_SHELLS`), the T-4785 dynamic
# write gate, the stdin-fed-shell reading and the escalation walk (`_SHELLS`).
_SHELL_NAMES = frozenset({"bash", "rbash", "sh", "dash", "zsh", "ksh", "ash", "mksh", "hush", "static-sh"})

# T-5326 r6: enabling bash's extglob (`shopt -s extglob`, `bash -O extglob`, a BASHOPTS
# value bash imports at startup). Extglob forms (`@(..)`, `+(..)`, `!(..)`) are syntax errors
# without it, but with it they are globs this guard cannot model (`find R @(-delete) ...`).
# A raw-text rule on the command with `\`+newline joined (bash joins them before reading
# words, T-5332) and quotes and backslashes dropped: `extglob` after `shopt -s` (any option
# order, more names), as a shell's own `-O`/`+O` value (clustered too) or in a `BASHOPTS`
# value; or a `shopt` argument, shell `-O` value or BASHOPTS value built at run time: `$`,
# backtick, and (T-5332) a brace or glob character (`ext{g,g}lob`, `ext*`), which bash
# expands before the builtin reads it. `shopt -u extglob` and a bare `shopt extglob` query
# pass. Over-blocks a commit message or echo that names both words (use `git commit -F`).
# T-5332 narrowing (over-blocks only; every enabling form still denies): a shell's `-O` is
# read only in the shell's own option run (`bash build.sh -O $OUT` passes script args;
# `./fetch.sh -O "$D"` names no shell), and a run-time character after `shopt` does not
# count in a segment whose command is a read-only text tool (`grep -n shopt "$f"`) with no
# `(`/backtick, whose output is piped only into more such tools.
# T-5332 r2: `$'...'` is decoded first; a word built at run time (`$BASH`, `` `which bash` ``,
# `ba${x}sh`) may name a shell, and inside a shell's option run it may be empty or an option
# (`bash $(true) -O extglob`), with a `$(...)`/backtick span kept in one word; each `o`/`O`
# of a cluster takes the next word (`-Oe extglob`); a value word that looks like an option is
# read as one (`-O -O extglob`); a run-time command word with `-s` and `extglob` may be shopt
# (`x=shopt; $x -s extglob`); the `&` of `2>&1` does not split a segment.
_EXTGLOB_STRIP = re.compile(r"[\\'\"]")
# A list separator. The `&` of a redirection (`2>&1`, `<&0`, `&>f`) is not one (r2).
_EXTGLOB_SEP = re.compile(r"\|\||\|&|&&|(?<![<>])&(?!>)|[;|\n]")
_EXTGLOB_SHOPT = re.compile(r"\bshopt\b")
_EXTGLOB_SHOPT_S = re.compile(r"\s-[A-Za-z]*s")
_EXTGLOB_DYN = re.compile(r"[$`{*?\[]")
_EXTGLOB_VALUE = re.compile(r"extglob|[$`{*?\[]")
_EXTGLOB_ANSI = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_EXTGLOB_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# A redirection and its target (`<f`, `2>&1`, `&>/dev/null`): not a shell's option word.
_EXTGLOB_REDIR = re.compile(r"\d*(?:&>>?|[<>]+[&|]?)\s*[^\s()`<>;&|]*")
_EXTGLOB_DATA_HEADS = frozenset({"grep", "egrep", "fgrep", "zgrep", "rg", "cat", "head", "tail",
                                 "wc", "ls", "sort", "uniq", "cut"})


def _extglob_text(command: str) -> str:
    """`command` as the rule reads it: `\\`+newline joined, each `$'...'` decoded (r2:
    `bash $'-O' extglob`), quotes and backslashes dropped. T-5360: an `env -S` string's
    `\\_` (env's word separator: `env -S 'bash\\_-O\\_extglob'`) reads as a blank, anywhere
    (a superset: elsewhere it only splits more words)."""
    text = command.replace("\\\n", "")
    if "$'" in text:
        text = _EXTGLOB_ANSI.sub(
            lambda m: (_py_ansi_c(m.group(1)) or m.group(1)) if "\\" in m.group(1) else m.group(1), text)
    return _EXTGLOB_STRIP.sub("", text.replace("\\_", " "))


def _extglob_tokens(seg: str) -> list[str]:
    """`seg`'s words, split at blanks, parens and `<>`; a `$(...)` or backtick span stays
    inside its word (r2: `bash $(true) -O ...`, `` `which bash` -O ...``). Linear."""
    toks: list[str] = []
    cur: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "`":
            j = seg.find("`", i + 1)
            j = n - 1 if j < 0 else j
            cur.append(seg[i:j + 1])
            i = j + 1
        elif c == "$" and seg.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (seg[j] == "(") - (seg[j] == ")")
                j += 1
            cur.append(seg[i:j])
            i = j
        elif c.isspace() or c in "()<>":
            if cur:
                toks.append("".join(cur))
                cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    if cur:
        toks.append("".join(cur))
    return toks


def _extglob_shell_token(tok: str) -> bool:
    """True if `tok` (quotes dropped) may name a shell: its basename is one; it is built at
    run time (`$BASH`, `` `which bash` ``, `ba${x}sh`, r2); or a brace or glob form could
    expand to one or names one (`{bash,}`, `/bin/b?sh`)."""
    if "$" in tok or "`" in tok:
        return True
    if tok.lstrip("{!").rsplit("/", 1)[-1] in _SHELL_NAMES:
        return True
    if not any(ch in tok for ch in "{*?["):
        return False
    base = tok.rsplit("/", 1)[-1]
    if len(base) > 256 or any(name in base for name in _SHELL_NAMES):
        return True  # fail closed rather than expand a long pattern
    return any(_ho_glob_match(alt, name) for alt in _ho_brace_alts(base) for name in _SHELL_NAMES)


def _extglob_shell_o(toks: list[str]) -> bool:
    """True if a shell word among `toks` has, in its own option run, a `-O`/`+O` whose
    value is `extglob` or built at run time. Each `o`/`O` of a cluster takes the next word
    in order (r2: `-Oe extglob`, `-oO pipefail extglob`); a word built at run time may be
    empty or an option, so the run goes on past it. Each word is scanned once (linear)."""
    n = len(toks)
    j = 0
    while j < n:
        if not _extglob_shell_token(toks[j]):
            j += 1
            continue
        k = j + 1
        while k < n:
            w = toks[k]
            if "$" in w or "`" in w:
                k += 1
                continue
            if len(w) < 2 or w[0] not in "-+":
                break
            # A value word that looks like an option is read as one too, and the run goes on
            # past `--` (a superset: `bash -O -O extglob`, `bash -- -O extglob` stay denied).
            if w[:2] == "--":
                k += 2 if w in ("--rcfile", "--init-file") and k + 1 < n and toks[k + 1][:1] not in "-+" else 1
                continue
            x = w.find("O", 1)
            if x > 0 and _EXTGLOB_VALUE.search(w, x + 1):
                return True  # a value glued to the `O`
            v = k + 1
            for ch in w[1:]:
                if ch in "oO":
                    if ch == "O" and v < n and _EXTGLOB_VALUE.search(toks[v]):
                        return True
                    if v < n and toks[v][:1] not in "-+":
                        v += 1
            k = v
        j = max(j + 1, k)
    return False


def _extglob_segments(text: str) -> list[tuple[int, int, str]]:
    """(start, end, separator after) for each list segment of `text`."""
    segs = []
    pos = 0
    for m in _EXTGLOB_SEP.finditer(text):
        segs.append((pos, m.start(), m.group()))
        pos = m.end()
    segs.append((pos, len(text), ""))
    return segs


def _extglob_data_seg(text: str, segs: list, i: int) -> int:
    """T-5332 (4): segment `i` is a read-only text tool's (`grep -n shopt "$f"`): no `(` or
    backtick in it, and its output goes nowhere but into more such tools. Returns the index
    of the last segment of the pipeline so checked (each one such a tool), or -1."""
    while True:
        a, b, sep = segs[i]
        seg = text[a:b]
        if "(" in seg or "`" in seg:
            return -1
        words = seg.split()
        if not words or words[0].rsplit("/", 1)[-1] not in _EXTGLOB_DATA_HEADS:
            return -1
        if sep not in ("|", "|&"):
            return i  # the output stays on the terminal (or `||` is a list)
        i += 1
        if i >= len(segs):
            return -1


def _extglob_enable_match(command: str) -> bool:
    """T-5326 r6 / T-5332: True if `command` may enable extglob (see the comment above).
    Linear: each segment is scanned from its FIRST `shopt` word only (a later one sees a
    suffix), and each shell word's option run once."""
    text = _extglob_text(command)
    # Every rule below needs a `shopt` word, an `O` (a shell's `-O`, `BASHOPTS`) or the
    # option name (a run-time `shopt`: `${x}opt -s extglob`, r4): a cheap exit without.
    if ("extglob" not in text and not _EXTGLOB_DYN.search(text)) or (
            "shopt" not in text and "O" not in text and "extglob" not in text):
        return False
    segs = _extglob_segments(text)
    checked = -1  # the last segment of an all-text-tool pipeline already checked
    for i, (a, b, _sep) in enumerate(segs):
        seg = text[a:b]
        sm = _EXTGLOB_SHOPT.search(seg)
        if sm:
            rest = seg[sm.end():]
            if "extglob" in rest and _EXTGLOB_SHOPT_S.search(rest):
                return True
            if _EXTGLOB_DYN.search(rest) and i > checked:
                checked = _extglob_data_seg(text, segs, i)
                if checked < 0:
                    return True
        # r3: words are split only for a segment that can hold an `-O` or the option name
        dyn_shopt = "extglob" in seg and _EXTGLOB_SHOPT_S.search(seg)
        if "O" not in seg and not dyn_shopt:
            toks = []
        else:
            toks = _extglob_tokens(_EXTGLOB_REDIR.sub(" ", seg))
        if "O" in seg and _extglob_shell_o(toks):
            return True
        if dyn_shopt:
            # r2: a command word built at run time may be shopt (`x=shopt; $x -s extglob`)
            lead = next((t for t in toks if not _EXTGLOB_ASSIGN.match(t)), "")
            if "$" in lead or "`" in lead:
                return True
        k = seg.find("BASHOPTS")
        if k >= 0:
            for tok in seg[k:].split():
                if "BASHOPTS" in tok and _EXTGLOB_VALUE.search(tok, tok.find("BASHOPTS")):
                    return True
    return False

# Writing/committing the orchestrator-owned shared records (ledger / ADRs / notes).
# Matches redirection, tee, or git add/commit touching these pantheon-config paths.
#
# T-2655 (2026-08-09): the bare `ledger/<year>` alternative used to be a pure
# `\bledger/\d{4}` word-boundary match — it fired on the substring ANYWHERE in the
# command, including nested partway through an unrelated absolute path (e.g. a
# throwaway `/tmp/scratch/ledger/2026-W30/x.jsonl` used for a hook wet-run, which
# has nothing to do with pantheon-config). Anchored below to require the token be
# the START of a path — preceded by start-of-string / whitespace / a quote char,
# never by `/` — so a real cwd-relative reference (`git add ledger/2026-W26/...`,
# `git -C ~/pantheon-config add ledger/...`, both space-preceded) still matches,
# while a `.../unrelated-dir/ledger/2026-.../...` nested substring (`/`-preceded)
# no longer does. Anchoring this required ALSO adding the worktree suffix
# `(-[^/]*)?` (copied from `_PROTECTED_DOC` below) to the two explicit-prefix
# alternatives: they previously lacked it and so never matched
# `pantheon-config-<ticket>/ledger|adr|notes/...` directly — that worktree shape
# was only being caught *accidentally* via the old unanchored bare token, which
# the anchor above would otherwise have silently stopped catching.
#
# T-4545: ORG-094 moved `adr/` to `architecture/adr/` and `notes/methodology/` to
# `architecture/engineering-practices/`; the anchor kept naming the dead `adr/`, so a worker
# could write an ADR through Bash. The directory alternation stays FLAT (no nested group): the
# canary in tools/test_worker_guard.py (TestProtectedDirsCanary) reads the directory names out
# of this pattern and `_PROTECTED_DOC` / `_PROTECTED_ROOTREL` / `_HO_FB_ROOTREL_HINT`, and
# checks each holds a real file. Keep the four lists in step.
#
# T-4545 r1: the second alternative keeps a coverage the old `(ledger|adr)` alternative had BY
# ACCIDENT. In `cd /tmp/pantheon-config-T-9 && git add architecture/adr/x.md` (the normal worker
# shape: cwd resets between Bash calls) the `-[^/]*` run spans `-T-9 && git add architecture`
# and `/adr/` follows, so the old rule denied the chain. The `architecture/`-prefixed names in
# the first alternative need the dir right after that run and no longer match, so this one
# names the tail (`adr`, `engineering-practices`) and requires a `-<suffix>` worktree dir, which
# also covers the chain for engineering-practices (it was not denied under notes/methodology/).
# The wider `cd <checkout> && <write> <relative path>` gap is T-4578's, not closed here.
_HO_RECORDS = re.compile(
    r"pantheon-config(-[^/]*)?/(ledger|architecture/adr|architecture/engineering-practices)/|"
    r"pantheon-config-[^/]*/(?:adr|engineering-practices)/|"
    r"pantheon-config(-[^/]*)?/[^ ]*notes/|"
    r"(?:^|[\s'\"])ledger/\d{4}",
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
# Threat model unchanged from the module's original framing: DRIFT, not structural
# security. T-4785 fail-closes unresolved expansion/indirection/glob words when they
# occupy named write-target surfaces; broader actively evasive shell behavior remains
# the T-1347 / OS-isolation residual.

_COMPOUND_RE = re.compile(r"[;&|`]|\$\(")
_REDIRECT_RE = re.compile(r">>|>|<")

_READ_ONLY_VERBS = frozenset({"grep", "rg", "cat", "head", "tail", "ls", "wc", "less", "more"})
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({
    "log", "show", "diff", "status", "blame",
    "ls-tree", "rev-parse", "cat-file", "hash-object",
})
_GIT_OUTPUT_SUBCOMMANDS = frozenset({"log", "show", "diff"})
_GIT_GLOBAL_FLAGS = frozenset({"--no-pager"})
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset({"-C", "-c"})
_GIT_GLOBAL_OPTS_FUSED_VALUE: tuple[str, ...] = ()
# `find` primaries that WRITE somewhere (not just read/report): -delete/-exec*/-ok*
# act directly; -fprintf/-fprint/-fprint0/-fls write formatted output to a FILE
# argument (round-1 reviewer HOLE A: these were the four missing from the original
# set, letting e.g. `find ... -fprintf <protected-path> '%p\n'` slip through as
# "read-only").
_FIND_MUTATING_FLAGS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprintf", "-fprint", "-fprint0", "-fls",
})


# shlex grows each token by string concatenation on an attribute, which is
# quadratic in the token's length: one 200K-char word took ~25 s across decide()'s
# calls (T-4135 round-2 review). Past this bound a command is treated as
# ambiguous, i.e. matched on its raw text: the conservative direction, and a
# single invocation this long is rare.
_TOKENIZE_MAX_CHARS = 16_384


def _tokens_or_none(command: str) -> list[str] | None:
    """Tokenize `command` iff it is a single, unambiguous shell invocation.

    Returns None for compound shell structure (;, &&, ||, |, backticks, $(...)) or
    unbalanced/unparseable quoting — callers must then fall back to matching the
    raw command string (the pre-T-1600 conservative behavior). Fail-closed on
    ambiguity, never fail-open.
    """
    if len(command) > _TOKENIZE_MAX_CHARS or _COMPOUND_RE.search(command):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _git_subcommand_pos(tokens: list[str]) -> int | None:
    """Best-effort git subcommand index, skipping known git global options."""
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok in _GIT_GLOBAL_FLAGS or tok.startswith(_GIT_GLOBAL_OPTS_FUSED_VALUE):
            i += 1
            continue
        if not tok.startswith("-"):
            return i
        return None
    return None


def _git_subcommand(tokens: list[str]) -> str | None:
    """Best-effort git subcommand, skipping a leading `-C <dir>` global flag."""
    i = _git_subcommand_pos(tokens)
    return tokens[i] if i is not None else None


def _git_hash_object_write_mode(tokens: list[str]) -> bool:
    """True iff a git hash-object invocation includes object write mode."""
    i = _git_subcommand_pos(tokens)
    if i is None or tokens[i] != "hash-object":
        return False
    for tok in tokens[i + 1:]:
        if tok == "--":
            return False
        if tok == "-w" or (tok.startswith("-") and not tok.startswith("--") and "w" in tok[1:]):
            return True
    return False


def _git_output_targets(tokens: list[str]) -> list[str]:
    """Return git log/show/diff --output targets, including fused --output=PATH."""
    i = _git_subcommand_pos(tokens)
    if i is not None and tokens[i] not in _GIT_OUTPUT_SUBCOMMANDS:
        return []
    start = i + 1 if i is not None else 1
    targets: list[str] = []
    rest = tokens[start:]
    for j, tok in enumerate(rest):
        if tok == "--output":
            if j + 1 < len(rest):
                targets.append(rest[j + 1])
        elif tok.startswith("--output="):
            targets.append(tok.split("=", 1)[1])
    return targets


def _git_output_writes_protected(tokens: list[str], cwd: str) -> bool:
    """True when a git --output target lands on a protected doc."""
    for target in _git_output_targets(tokens):
        if target and (_PROTECTED_DOC.search(target) or _protected_doc_via_worktree(target, cwd)):
            return True
    return False


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
        if sub in _GIT_OUTPUT_SUBCOMMANDS and _has_output_flag(tokens):
            return False
        return not _git_hash_object_write_mode(tokens)
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


def _ho_records_match(command: str, cwd: str = "") -> bool:
    """True if `command`'s genuine argument surface touches an HO-owned protected
    path. Excludes: single read-only-verb invocations (misfire #1) and a `git
    commit`'s -m/--message text (misfire #2). Falls back to matching the whole raw
    command whenever the shape can't be confidently classified — redirection,
    compound shell structure, or unparseable quoting all fail closed to the
    pre-T-1600 behavior (still block on any mention).

    T-3663: also OR in the worktree-operand resolution (`_ho_records_via_worktree`)
    so a cwd-relative protected path inside a pantheon-config isolation worktree —
    which carries no "pantheon-config" segment and so misses _HO_RECORDS — is still
    caught. A no-op when `cwd` is absent (fail-open).
    """
    return _ho_records_match_base(command, cwd) or _ho_cd_dir_hit(command, cwd)


def _ho_records_match_base(command: str, cwd: str = "") -> bool:
    """`_ho_records_match` less the T-4578 cd-chain / bare-directory rule."""
    if _REDIRECT_RE.search(command):
        return _ho_records_fallback(command, cwd) or _ho_records_via_worktree(command, cwd)
    tokens = _tokens_or_none(command)
    if not tokens:
        return _ho_records_fallback(command, cwd)
    # T-4497: a newline or `$'` is read as bash does (see `_ho_git_dir_hit`).
    if ("\n" in command or "$'" in command) and _ho_records_segmented(command, cwd):
        return True
    if _git_output_writes_protected(tokens, cwd):
        return True
    if _is_read_only_verb(tokens):
        return False
    if _ho_git_dir_hit(tokens, cwd):
        return True
    tokens = _drop_commit_message(tokens)
    return bool(_HO_RECORDS.search(" ".join(tokens))) or _ho_records_via_worktree(command, cwd)

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
# implementer's job. Worker evidence dirs (screenshots/, knowledge/) stay open —
# with the single named exception immediately below.
#
# T-3089 (2026-08-23): `knowledge/` is otherwise legitimately worker-writable
# (research reports, watch-dispatch artifacts, episodic notes) so it is
# deliberately NOT blanket-covered here — see the T-3039 reviewer finding this
# ticket closes. `knowledge/watch/topics.md` is the one exception: its own
# header self-declares "Workers do not edit this file (D2)" (the operator/HO
# ratification only), but the structural guard silently permitted worker Edit/
# Write anyway until this line was added. Path-prefix-exact, not a pattern, per
# the brief's explicit preference — narrower blast radius than widening the
# knowledge/ surface generally. Audited every other `knowledge/**/*.md` for the
# same self-declared-D2 shape (T-3089 report) and found no sibling match — this
# is the only file with that property today.
#
# T-4545: `adr` and `notes/methodology` moved under `architecture/` (ORG-094), so the
# protected dirs name `architecture/adr` and `architecture/engineering-practices`, the two
# subdirs this guard covered before the move. `architecture/atlas/` and `architecture/rfc/`
# are NOT covered: workers author artisan bundles on agent/<ticket> branches (Orchestrator's T-3249
# ruling, subagent-authors/HO-adopts) and this guard never covered rfc/. Widening to the whole
# umbrella is an HO decision. Keep the flat alternation in step with the three copies below
# (`_PROTECTED_ROOTREL`, `_HO_FB_ROOTREL_HINT`, `_HO_RECORDS`): the canary checks all four.
_PROTECTED_DOC = re.compile(
    r"pantheon-config(-[^/]*)?/"  # repo dir; (-[^/]*)? also matches /tmp worktrees
    r"(?:"
    r"(?:rulebook|ledger|notes|bugs|incidents|lessons|sop|architecture/adr|architecture/engineering-practices)/"  # protected dirs
    r"|BACKLOG\.md"
    r"|goals\.md"
    r"|(?:[^ ]*/)?README\.md"  # any README (autogen spine index) at any depth
    r"|knowledge/watch/topics\.md"  # self-declared D2 (T-3089); exact-path, not blanket knowledge/
    r")"
)

# --- T-3662 (2026-09-13): worktree-shape coverage gap in _PROTECTED_DOC above ---
# _PROTECTED_DOC anchors on the literal `pantheon-config(-[^/]*)?/` path segment, so
# it catches the main checkout and `/tmp/pantheon-config-<ticket>/` dispatch worktrees
# but MISSES the Agent-tool `isolation:"worktree"` shape, whose worktree lives at
# `<account>/.claude/worktrees/agent-<id>/` — NO "pantheon-config" in the path. A
# D2-writer implementer there (confirmed live: T-3166 wrote sop/standard/project-
# lifecycle.md from .claude/worktrees/agent-a491ad4c4b5bd6309/) evades the guard.
# Also misses a relative `file_path`. agent_type DOES populate for that spawn (T-3185's
# README write was correctly blocked), so this is purely a path-anchor gap, not the
# T-1673 presence-gating class.
#
# Can't just drop the prefix: sibling repos share dir names — echo-prismatic has
# tracked adr/, notes/, sop/, BACKLOG.md (verified 2026-09-13), so a prefix-less
# `sop/` match would wrongly block an echo implementer. The disambiguator is the
# enclosing git worktree's COMMON dir: a pantheon-config worktree's `.git` pointer
# resolves into a `.../pantheon-config.../.git/...` common dir; echo's does not.
# So: resolve the target path, find its worktree root + common dir, and apply a
# ROOT-RELATIVE protected pattern ONLY when the common dir is pantheon-config's.
# Root-relative (not "anywhere") mirrors _PROTECTED_DOC's top-level semantics and
# avoids over-matching a nested `.../foo/notes/` deep in the tree.
_PROTECTED_ROOTREL = re.compile(
    r"^(?:rulebook|ledger|notes|bugs|incidents|lessons|sop|architecture/adr|architecture/engineering-practices)/"
    r"|^BACKLOG\.md$"
    r"|^goals\.md$"
    r"|(?:^|/)README\.md$"  # any README (autogen spine index) at any depth in the repo
    r"|^knowledge/watch/topics\.md$"
)


def _worktree_root_and_common(start: str) -> tuple[str | None, str | None]:
    """Walk up from `start` (a file or dir path, need not exist) to the nearest
    ancestor holding a `.git`. Return (worktree_root, common_dir_string). For a
    linked worktree `.git` is a FILE (`gitdir: <path>`); for a main checkout it is
    a DIR. (None, None) on any failure — fail-open."""
    try:
        d = start if os.path.isdir(start) else os.path.dirname(start)
        for _ in range(64):  # bounded walk
            marker = os.path.join(d, ".git")
            if os.path.isfile(marker):
                with open(marker, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                m = re.search(r"gitdir:\s*(.+)", txt)
                return d, (m.group(1).strip() if m else None)
            if os.path.isdir(marker):
                return d, marker
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    except Exception:
        return None, None
    return None, None


def _protected_doc_via_worktree(path: str, cwd: str) -> bool:
    """True if `path` (resolved against `cwd`) is a pantheon-config protected doc
    reached via a shape _PROTECTED_DOC misses — a relative path, or an
    `.claude/worktrees/agent-<id>/` absolute path with no "pantheon-config" segment.
    Confirms the repo via the enclosing worktree's git COMMON dir so a sibling
    repo's same-named dir (echo's sop/adr/notes) is NOT matched. Fail-open (False)."""
    try:
        if not path:
            return False
        if os.path.isabs(path):
            abspath = os.path.normpath(path)
        elif cwd:
            abspath = os.path.normpath(os.path.join(cwd, path))
        else:
            return False  # relative path, no cwd to anchor → can't confirm → allow
        root, common = _worktree_root_and_common(abspath)
        if not root or not common or "pantheon-config" not in common:
            return False
        rel = os.path.relpath(abspath, root)
        return bool(_PROTECTED_ROOTREL.search(rel))
    except Exception:
        return False


# --- T-3663 (2026-09-13): the SAME worktree-shape gap on the Bash surface ---
# _HO_RECORDS (the git/redirect matcher) keys on the literal `pantheon-config(-[^/]*)?/`
# prefix exactly as _PROTECTED_DOC did, so a subagent inside an Agent-tool
# `isolation:"worktree"` worktree can `git add sop/foo.md`, or redirect/tee/mv/cp
# into a protected doc via a CWD-RELATIVE operand, and evade the block — no
# "pantheon-config" segment appears in the operand. (_HO_RECORDS' bare relative
# `ledger/<year>` alternative is the only cwd-relative catch, and it covers ledger
# ONLY.) This mirrors _protected_doc_via_worktree for the Bash operand surface:
# resolve each path-like operand against cwd, confirm the enclosing worktree's git
# common dir is pantheon-config's, and apply the ROOT-RELATIVE protected pattern.
# T-5212: a `scheme://...` token is a URL (an upstream README fetched by `curl`/`wget`),
# not a cwd-relative repo path. Resolving it against cwd made `.../README.md` look like
# a protected doc. Only the URL token itself is dropped from the operand loop below;
# write targets (`-o X`, `> X`, `tee X`, `cp .. X`) are separate tokens, still judged.
_URL_TOKEN_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_pure_url_token(tok: str, cwd: str) -> bool:
    """True only if `tok` is a URL that can NOT also be a real relative path.
    `x://../sop/a.md` is a genuine path when a directory `x:` exists under cwd
    (it normalizes to `sop/a.md`), and `-o`/`-O` take that token as a WRITE target,
    so skipping every URL-shaped token opened a bypass (REBOUND r1). Skip only when
    the remainder has no `..` segment AND the `<scheme>:` pseudo-dir is absent.
    Residual: a `scheme:` dir created earlier in the same compound command is not
    visible here; compound commands already fail open in `_ho_records_via_worktree`
    (single unambiguous invocations only), so this adds no new gap."""
    if not _URL_TOKEN_RE.match(tok):
        return False
    scheme, rest = tok.split("//", 1)  # scheme includes its trailing ':'
    if ".." in rest.split("/"):
        return False
    return not os.path.lexists(os.path.join(cwd, scheme))


def _ho_records_via_worktree(command: str, cwd: str) -> bool:
    """True if any cwd-relative path operand (or redirect target) in `command`
    resolves to a pantheon-config protected doc via a shape _HO_RECORDS misses
    (an `.claude/worktrees/agent-<id>/` worktree, or a plain relative operand).
    Single unambiguous invocations only — compound/unparseable → False (fail-open;
    the raw _HO_RECORDS fallback still blocks literal-prefix mentions). Fail-open."""
    try:
        if not cwd:
            return False
        tokens = _tokens_or_none(command)
        if not tokens:
            return False
        # A redirect (`>`, `>>`) turns even a read-only verb into a writer, so the
        # read-only-verb skip applies ONLY when there is no redirection — mirroring
        # _ho_records_match, whose redirect branch bypasses the read-only allowlist.
        # (An input `<` from a protected path is a harmless over-block under the
        # drift model — safe direction, consistent with the module's stance.)
        if not _REDIRECT_RE.search(command) and _is_read_only_verb(tokens):
            return False
        for tok in _drop_commit_message(tokens):
            if not tok or tok[0] == "-":
                continue
            if tok in (">", ">>", "<", "|", "&", ";", "&&", "||"):
                continue
            if _is_pure_url_token(tok, cwd):
                continue
            if _protected_doc_via_worktree(tok, cwd):
                return True
        return False
    except Exception:
        return False


# --- T-4482: the fallback reads each segment's words as bash does ---
# `_ho_records_match` falls back to its raw-text match for every command it
# can't read as one invocation (a separator, `$(`, backticks, a redirect, or
# quoting shlex rejects). That raw match read a quote-split path literally
# (`led''ger/2026-W27/x`, which bash runs as `ledger/2026-W27/x`), so it was
# missed in any segment but a lone first one; and the T-3663 worktree check
# was skipped for the whole command. The fallback now also:
#   - matches `_HO_RECORDS` against a copy with every quote char and
#     backslash dropped (`$'`/`$"` read as plain quotes), the T-4413 gh
#     fallbacks' reading: no structure, so no nesting or `case` pattern can
#     put it out of step with bash (a file-writing quoted heredoc body is
#     left out, as `_data_heredoc_text` reads it: data);
#   - lexes the text into segments of dequoted words (`_ho_dequoted_segments`:
#     `'..'`, `".."`, `$'..'` decoded, `$".."`, backslashes, `\`+newline,
#     comments, heredoc bodies skipped, `$(..)` and backticks read as their own
#     segments, a `case` pattern's `)` read as a pattern), and matches
#     `_HO_RECORDS` against each segment's words, and runs the T-3663 worktree
#     check on each segment as the single-invocation path does (a commit
#     message is dropped; a segment without a write redirect is skipped when
#     its verb is read-only, or writes no file it names: cd, for, echo,
#     printf, sed/awk without an in-place option; `2>&1`, `>/dev/null` are
#     not write redirects);
#   - fails closed: a word it can't dequote (an unterminated quote, a `$'..'`
#     escape `_py_ansi_c` can't decode) after a `git` word in its segment
#     counts as a protected path.
# One pass over the text: linear. Only adds denials to the raw match.
_HO_FB_ROOTREL_HINT = re.compile(
    r"(?:rulebook|ledger|notes|bugs|incidents|lessons|sop|architecture/adr|architecture/engineering-practices)/"
    r"|BACKLOG\.md|goals\.md|README\.md|knowledge/watch/topics\.md"
)
_HO_FB_LEAD_WORDS = frozenset({"if", "then", "do", "else", "elif", "while", "until", "!", "{", "time"})
_HO_FB_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_HO_FB_PLAIN = re.compile(r"[^\s;&|<>()`'\"\\$#]+")
_HO_FB_DQ_PLAIN = re.compile(r"[^\"\\$`]+")
_HO_FB_HEREDOC = re.compile(r"<<(-?)[ \t]*([^\s;&|<>()]+)")
# `>&2`, `2>&1`, `>/dev/null`, `>>/dev/null`: no file is written.
_HO_FB_NULL_REDIRECT = re.compile(r">[>|]?[ \t]*(?:&(?:[0-9]+-?|-|/dev/null)|/dev/null)(?=[\s;&|<>()]|$)")
# Commands that write no file they name (without a write redirect): skipped by
# the per-segment worktree check, as `_is_read_only_verb`'s are. sed and awk
# count only without an in-place option (`-i`, `-ni`, `--in-place`, gawk's
# `-i inplace`).
_HO_FB_READ_SHAPED = frozenset({"cd", "pushd", "popd", "for", "echo", "printf"})
_HO_FB_INPLACE = re.compile(r"-[^-]*i|--in-place|inplace$")


def _ho_fb_read_shaped(words: list[str]) -> bool:
    verb = words[0]
    if verb in _HO_FB_READ_SHAPED:
        return True
    if verb in ("sed", "awk", "gawk"):
        return not any(_HO_FB_INPLACE.match(w) for w in words[1:])
    return False


class _HoDynWord(str):
    """A dequoted word holding a `$(..)` or backtick value (T-4497)."""

    __slots__ = ()


# --- T-4497: git's directory options, joined and `$'..'` spellings, reads ---
# `git -C <dir>` (and `--work-tree`, `--git-dir`) moves the directory a path
# operand resolves from, so `git -C ~/pantheon-config add ledger/x` writes a
# protected doc although no operand names pantheon-config. `_ho_git_dir_hit`
# resolves the directory as the rule resolves cwd (a literal path, `~`, relative
# to cwd) and judges each operand of a non-read-only git verb against it with
# the Edit/Write rule (`_PROTECTED_DOC`, so any ledger/ path under a
# pantheon-config dir counts, year or not) and the T-3663 worktree rule. A
# dynamic directory (`$X`, `$(..)`, backticks, a glob) is unknown: an operand
# that looks protected-relative then counts as protected.
# The single-invocation path reads a command with a newline or `$'` through
# `_ho_dequoted_segments` too (`\`+newline joins, `$'..'` decoded as bash
# does, a newline ends a command): shlex keeps `\`+newline as a newline, reads
# `$'\x6c'` literally, and runs `cat x<newline>git add ...` as one `cat`.
# When every segment only reads (`_ho_seg_reads`, no write redirect), the
# fallback's dequoted `_HO_RECORDS` matches are skipped, as the single-invocation
# path skips a read-only verb; the raw-text match stays as before. One writing
# segment brings them all back (r2: `echo <path> | xargs git add` writes).
# An operand is joined to its directory before the protected match and read
# both as a file and as a directory (r2: `add adr`, `rm -r notes/`, `-C adr`).
_HO_GIT_DIR_OPTS = ("--git-dir", "--work-tree")
_HO_GIT_DIR_HINT = re.compile(r"(?:^|[\s'\"])(?:-C|--git-dir|--work-tree)(?=[\s='\"]|$)")
_HO_DYN_PATH = re.compile(r"[$`*?\[]")
_HO_GIT_BASE_MAX = 4096


def _ho_seg_reads(words: list[str]) -> bool:
    """True for a command that writes no file it names (without a write
    redirect): a read-only verb, cd/for/echo/printf. sed and awk are left
    out: a script can write a file (`w path`, `print > path`)."""
    return _is_read_only_verb(words) or words[0] in _HO_FB_READ_SHAPED


def _ho_strip_lead(words: list[str], time_opts: bool = False) -> list[str]:
    """Leading reserved words and `NAME=v` dropped. `time_opts` (T-5326): a `time` word's
    options are dropped too (`time -p`, GNU time's `-f FMT`/`-o FILE`/`--format=..`)."""
    k = 0
    while k < len(words) and (words[k] in _HO_FB_LEAD_WORDS or _HO_FB_ASSIGN.match(words[k])):
        k += 1
        if time_opts and words[k - 1] == "time":
            k = _ho_skip_time_options(words, k)
    return words[k:]


def _ho_skip_time_options(words: list[str], k: int) -> int:
    """T-5326: the index after the options of a `time` whose options start at `k`. Bash's
    keyword takes `-p`; GNU time (`/usr/bin/time`, or what bash runs for any other option)
    takes `-f FMT`, `-o FILE` and flags. An option neither knows is skipped as a flag, so
    the command after it is still judged (an over-read only adds denials)."""
    n = len(words)
    while k < n and words[k][:1] == "-" and len(words[k]) > 1:
        w = words[k]
        k += 1
        if w == "--":
            break
        nw = words[k] if k < n else None
        _at, take_next, key = _option_parse("time", w, nw, "fo", frozenset({"format", "output"}), False)
        if take_next and key != "?":
            k += 1
    return k


def _ho_dir_join(base: str | None, value: str) -> str | None:
    """`value` (a git directory option) resolved from `base`; None if unknown."""
    if isinstance(value, _HoDynWord) or _HO_DYN_PATH.search(value):
        return None
    v = os.path.expanduser(value)
    if not os.path.isabs(v) and base:
        v = os.path.join(base, v)
    v = os.path.normpath(v) if v else v
    return v if len(v) <= _HO_GIT_BASE_MAX else None


def _ho_git_dir_hit(words: list[str], cwd: str, cache: dict | None = None) -> bool:
    """True if `words` (one git command) writes a protected doc through
    `-C`/`--work-tree`/`--git-dir` (see the T-4497 comment above)."""
    if not words or words[0].rsplit("/", 1)[-1] != "git":
        return False
    base: str | None = cwd or ""
    roots: dict[str, str | None] = {}
    seen = dynamic = False
    i, n = 1, len(words)
    while i < n and words[i][:1] == "-" and words[i] != "--":
        w = words[i]
        opt, val = None, None
        if w in ("-C",) + _HO_GIT_DIR_OPTS:
            opt, val = w, (words[i + 1] if i + 1 < n else "")
            i += 2
        elif w.startswith(("--git-dir=", "--work-tree=")):
            opt, _, val = w.partition("=")
            i += 1
        elif w == "-c":
            i += 2
            continue
        else:
            i += 1
            continue
        seen = True
        if opt == "-C":
            base = _ho_dir_join(base, val) if base is not None else None
            dynamic = dynamic or base is None
            continue
        root = _ho_dir_join(base, val) if base is not None else None
        if root is None:
            dynamic = True
        elif opt == "--git-dir" and os.path.basename(root) == ".git":
            root = os.path.dirname(root)
        roots[opt] = root
    if not seen or i >= n:
        return False
    rest = ["git"] + words[i:]
    if _is_read_only_verb(rest) or _ho_git_dir_read(rest):
        return False
    bases = [b for b in (base, *roots.values()) if b is not None]
    # r2: a write run from inside a protected dir (`git -C ~/pantheon-config/adr
    # add x.md`) counts, whatever its operands.
    if any(_ho_protected_path(b) for b in bases if b):
        return True
    for op in _drop_commit_message(rest)[2:]:
        if not op or op[0] == "-":
            continue
        if dynamic:
            if _HO_FB_ROOTREL_HINT.search(op.rstrip("/") + "/"):
                return True
            continue
        for b in bases:
            key = (op, b)
            if cache is not None and key in cache:
                hit = cache[key]
            else:
                o = os.path.expanduser(op)
                hit = _ho_protected_path(os.path.join(b, o) if b else o)
                if cache is not None:
                    cache[key] = hit
            if hit:
                return True
    return False


# T-4497 r3: git subcommands that only read the work tree or write only inside `.git`
# (fetch), so a `-C` into a protected dir, or a protected dir operand, is no write
# there. grep is a read unless `-O`/`--open-files-in-pager` (which runs a command)
# may be present: any short-option cluster holding `O`, or any `--o...` abbreviation,
# ANYWHERE in the words (r4: a `--` can be the value of `-e`/`-f`/`--regexp`, so the
# scan does not stop there; a literal `-O...` search pattern after `--` is refused too).
_HO_GIT_DIR_READS = frozenset({"grep", "ls-files", "rev-parse", "rev-list", "cat-file", "ls-tree", "fetch"})


def _ho_git_dir_read(rest: list[str]) -> bool:
    """True if `rest` (["git", subcommand, ...]) is a read for `_ho_git_dir_hit`."""
    if len(rest) < 2 or rest[1] not in _HO_GIT_DIR_READS:
        return False
    if rest[1] == "grep":
        for w in rest[2:]:
            if w.startswith("--o") or (w[:1] == "-" and w[1:2] != "-" and "O" in w):
                return False
    return True


def _ho_protected_path(path: str) -> bool:
    """T-4497 r2: True if `path` (the joined directory + operand) is a protected
    doc, read both as a file and as a directory, so `adr`, `ledger/` and a `-C`
    into `notes/` count. The hint filter runs on the joined path, not the raw
    operand; the T-3663 worktree rule runs for an absolute path only."""
    p = os.path.normpath(path)
    d = p + "/"
    if not _HO_FB_ROOTREL_HINT.search(d):
        return False
    if _PROTECTED_DOC.search(p) or _PROTECTED_DOC.search(d):
        return True
    return os.path.isabs(p) and (_protected_doc_via_worktree(p, "") or _protected_doc_via_worktree(
        os.path.join(p, "_"), ""))


def _ho_segments_hit(segs: list[tuple[list[str], bool]], cwd: str, rootrel: bool,
                     drop_msg: bool = False) -> bool:
    """The per-segment checks: a non-read segment's words (less a commit
    message if `drop_msg`) against `_HO_RECORDS`, its git directory options,
    and (if `rootrel`) the T-3663 worktree rule.
    `_protected_doc_via_worktree` runs once per distinct word.
    r2: a reading segment is skipped by the `_HO_RECORDS` match only when every
    segment reads (as `blind_hit` is gated): in `echo <path> | xargs git add`
    the echo feeds a write."""
    cache: dict = {}
    segs = [(_ho_strip_lead(w), r) for w, r in segs]
    if all(not r and (not w or _ho_seg_reads(w)) for w, r in segs):
        return False
    for words, redirect in segs:
        if not words:
            continue
        if _HO_RECORDS.search(" ".join(_drop_commit_message(words) if drop_msg else words)):
            return True
        if _ho_git_dir_hit(words, cwd, cache):
            return True
        if not rootrel or (not redirect and (_ho_seg_reads(words) or _ho_fb_read_shaped(words))):
            continue
        for w in _drop_commit_message(words):
            if not w or w[0] == "-" or not _HO_FB_ROOTREL_HINT.search(w):
                continue
            if w not in cache:
                cache[w] = _protected_doc_via_worktree(w, cwd)
            if cache[w]:
                return True
    return False


class _HoFrame:
    """One command frame (the top level, a `$(...)` or a backtick body)."""

    __slots__ = (
        "closer", "depth", "case_open", "words", "redirect", "parts", "started",
        "bad", "dq", "git", "dyn",
    )

    def __init__(self, closer):
        self.closer = closer   # ")" for $(...), "`" for backticks, None at the top level
        self.depth = 0         # open `(` inside a $(...)
        self.case_open = 0     # `case` words seen, less `esac`: a `)` is a pattern's
        self.words: list[str] = []
        self.redirect = False
        self.parts: list[str] = []
        self.started = False
        self.bad = False
        self.dq = False
        self.git = False       # a `git` word in the current segment
        self.dyn = False       # the current word holds a `$(..)` or backtick value


class _HoRichFrame(_HoFrame):
    """A command frame that also captures shell write-redirect target words."""

    __slots__ = ("write_targets", "pending_write_redirect", "skip_redirect_operand")

    def __init__(self, closer):
        super().__init__(closer)
        self.write_targets: list[str] = []
        self.pending_write_redirect = False
        self.skip_redirect_operand = False


def _ho_dequoted_segments_impl(command: str, capture_targets: bool = False):
    """(segments, unreadable_git): each segment is (dequoted words, has a
    write redirect, optionally write-redirect targets); `unreadable_git` is True if a word
    that could not be dequoted follows a `git` word in its segment. See the
    T-4482 comment above."""
    segs = []
    unreadable_git = False
    stack: list[_HoFrame] = []
    Frame = _HoRichFrame if capture_targets else _HoFrame
    f = Frame(None)
    heredocs: list[tuple[str, bool]] = []
    n = len(command)

    if capture_targets:
        def flush(fr: _HoRichFrame) -> None:
            nonlocal unreadable_git
            if fr.dq:
                fr.bad = True  # an unterminated double quote
                fr.dq = False
            if fr.parts or fr.started:
                word = "".join(fr.parts)
                if fr.bad and fr.git:
                    unreadable_git = True
                word_obj = None
                if not fr.words and not fr.redirect:
                    if word == "case":
                        fr.case_open += 1
                    elif word == "esac" and fr.case_open:
                        fr.case_open -= 1
                if fr.dyn:
                    word_obj = _HoDynWord(word)
                elif word:
                    word_obj = word
                if word_obj is not None:
                    if fr.skip_redirect_operand:
                        fr.skip_redirect_operand = False
                    else:
                        fr.words.append(word_obj)
                        if fr.pending_write_redirect:
                            fr.write_targets.append(word_obj)
                            fr.pending_write_redirect = False
                        if not isinstance(word_obj, _HoDynWord):
                            word = word_obj
                        fr.git = fr.git or word.rsplit("/", 1)[-1] == "git"
            fr.parts, fr.started, fr.bad, fr.dyn = [], False, False, False

        def end_segment(fr: _HoRichFrame) -> None:
            flush(fr)
            if fr.words:
                segs.append((fr.words, fr.redirect, fr.write_targets))
            fr.words, fr.redirect, fr.write_targets = [], False, []
            fr.pending_write_redirect, fr.skip_redirect_operand, fr.git = False, False, False
    else:
        def flush(fr: _HoFrame) -> None:
            nonlocal unreadable_git
            if fr.dq:
                fr.bad = True  # an unterminated double quote
                fr.dq = False
            if fr.parts or fr.started:
                word = "".join(fr.parts)
                if fr.bad and fr.git:
                    unreadable_git = True
                if not fr.words and not fr.redirect:
                    if word == "case":
                        fr.case_open += 1
                    elif word == "esac" and fr.case_open:
                        fr.case_open -= 1
                if fr.dyn:
                    fr.words.append(_HoDynWord(word))
                elif word:
                    fr.words.append(word)
                    fr.git = fr.git or word.rsplit("/", 1)[-1] == "git"
            fr.parts, fr.started, fr.bad, fr.dyn = [], False, False, False

        def end_segment(fr: _HoFrame) -> None:
            flush(fr)
            if fr.words:
                segs.append((fr.words, fr.redirect))
            fr.words, fr.redirect, fr.git = [], False, False

    i = 0
    while i < n:
        c = command[i]
        if f.dq:
            m = _HO_FB_DQ_PLAIN.match(command, i)
            if m:
                f.parts.append(m.group(0))
                i = m.end()
                continue
            if c == '"':
                f.dq = False
                i += 1
            elif c == "\\":
                nx = command[i + 1:i + 2]
                if nx == "\n":
                    pass
                elif nx in ("$", "`", '"', "\\"):
                    f.parts.append(nx)
                else:
                    f.parts.append("\\" + nx)
                i += 2
            elif c == "$" and command.startswith("$(", i):
                f.dyn = True  # T-4497: the word's value is dynamic
                stack.append(f)
                f = Frame(")")
                i += 2
            elif c == "`":
                f.dyn = True
                stack.append(f)
                f = Frame("`")
                i += 1
            else:
                f.parts.append(c)
                i += 1
            continue
        m = _HO_FB_PLAIN.match(command, i)
        if m:
            f.parts.append(m.group(0))
            i = m.end()
            continue
        if c == "'":
            j = command.find("'", i + 1)
            f.started = True
            if j < 0:
                f.bad = True
                f.parts.append(command[i + 1:])
                i = n
            else:
                f.parts.append(command[i + 1:j])
                i = j + 1
        elif c == '"':
            f.dq = f.started = True
            i += 1
        elif c == "$":
            nx = command[i + 1:i + 2]
            if nx == "'":
                j = i + 2
                while j < n and command[j] != "'":
                    j += 2 if command[j] == "\\" else 1
                f.started = True
                body = command[i + 2:min(j, n)]
                val = _py_ansi_c(body) if j < n else None
                if val is None:
                    f.bad = True
                    val = body
                f.parts.append(val)
                i = j + 1
            elif nx == '"':
                f.dq = f.started = True
                i += 2
            elif nx == "(":
                f.dyn = f.started = True  # T-4497: the word's value is dynamic
                stack.append(f)
                f = Frame(")")
                i += 2
            else:
                f.parts.append(c)
                i += 1
        elif c == "\\":
            nx = command[i + 1:i + 2]
            if nx != "\n":
                f.parts.append(nx)
                f.started = True
            i += 2
        elif c == "#" and not f.parts and not f.started:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif c == "#":
            f.parts.append(c)
            i += 1
        elif c in " \t":
            flush(f)
            i += 1
        elif c == "\n":
            end_segment(f)
            i += 1
            for delim, strip_tabs in heredocs:  # skip each pending body, in order
                while i < n:
                    j = command.find("\n", i)
                    line = command[i:] if j < 0 else command[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
            heredocs = []
        elif c in ";&|":
            end_segment(f)
            i += 1
        elif c in "<>":
            fd_prefix = False
            if capture_targets:
                fd_prefix = c == ">" and f.parts and not f.bad and not f.dyn \
                    and "".join(f.parts).isdigit()
                if fd_prefix:
                    f.parts, f.started = [], False
                else:
                    flush(f)
            else:
                flush(f)
            hm = _HO_FB_HEREDOC.match(command, i) if command.startswith("<<", i) and not command.startswith("<<<", i) else None
            if hm:
                heredocs.append((hm.group(2).translate(_QUOTE_STRIP), bool(hm.group(1))))
                i = hm.end()
            else:
                nm = _HO_FB_NULL_REDIRECT.match(command, i)
                if nm:
                    i = nm.end()
                elif capture_targets and c == ">" and command.startswith(">&", i):
                    j = i + 2
                    if fd_prefix:
                        f.skip_redirect_operand = True
                    elif j < n and command[j:j + 9] == "/dev/null":
                        i = j + 9
                        continue
                    elif j < n and (command[j].isdigit() or command[j] == "-"):
                        while j < n and command[j].isdigit():
                            j += 1
                        if j < n and command[j] == "-":
                            j += 1
                        i = j
                        continue
                    else:
                        f.redirect = True
                        f.pending_write_redirect = capture_targets
                    i = j
                else:
                    if c == ">":
                        f.redirect = True  # a write to a file (not an fd or /dev/null)
                        if capture_targets:
                            f.pending_write_redirect = True
                    i += 2 if command.startswith(">|", i) else 1
        elif c == "(":
            end_segment(f)
            if f.closer == ")":
                f.depth += 1
            i += 1
        elif c == ")":
            if f.closer == ")" and not f.depth and not f.case_open:
                end_segment(f)
                f = stack.pop()
            else:
                end_segment(f)
                if f.depth:
                    f.depth -= 1
            i += 1
        elif c == "`":
            if f.closer == "`":
                end_segment(f)
                f = stack.pop()
            else:
                f.dyn = f.started = True  # T-4497: the word's value is dynamic
                stack.append(f)
                f = Frame("`")
            i += 1
        else:
            f.parts.append(c)
            i += 1
    end_segment(f)
    while stack:
        f = stack.pop()
        end_segment(f)
    return segs, unreadable_git


def _ho_dequoted_segments(command: str) -> tuple[list[tuple[list[str], bool]], bool]:
    """Each segment is (dequoted words, has a write redirect)."""
    return _ho_dequoted_segments_impl(command, False)


def _ho_dequoted_segments_rich(command: str) -> tuple[list[tuple[list[str], bool, list[str]]], bool]:
    """Each segment is (dequoted words, has a write redirect, write-redirect targets)."""
    return _ho_dequoted_segments_impl(command, True)


def _ho_records_fallback(command: str, cwd: str) -> bool:
    """`_ho_records_match` for a command it can't read as one invocation: the
    raw-text match, plus the dequoted readings and per-segment checks of the
    T-4482 comment above. Only adds denials to the raw match."""
    if _HO_RECORDS.search(command):
        return True
    quoted = any(ch in command for ch in "'\"\\")
    # A file-writing quoted heredoc body is data (T-4328's reading): not stripped.
    blind = (_data_heredoc_text(command).replace("$'", "'").replace('$"', '"').translate(_QUOTE_STRIP)
             if quoted else command)
    blind_hit = quoted and bool(_HO_RECORDS.search(blind))
    rootrel = bool(cwd) and bool(_HO_FB_ROOTREL_HINT.search(blind))
    if not quoted and not rootrel and not _HO_GIT_DIR_HINT.search(command):
        return False
    segs, unreadable_git = _ho_dequoted_segments(command)
    if unreadable_git:
        return True
    # T-4497: the structure-free dequoted match stands unless the lexer reads
    # every segment as a read (`cat ~/pantheon-config/'adr'/x.md | head`).
    if blind_hit and not segs or blind_hit and not all(
            not r and (not w or _ho_seg_reads(w)) for w, r in ((_ho_strip_lead(w), r) for w, r in segs)):
        return True
    return _ho_segments_hit(segs, cwd, rootrel)


def _ho_records_segmented(command: str, cwd: str) -> bool:
    """T-4497: a single invocation with a newline or `$'`, read as bash does
    (`_ho_dequoted_segments`); adds denials to the shlex reading only."""
    segs, unreadable_git = _ho_dequoted_segments(command)
    if unreadable_git:
        return True
    rootrel = bool(cwd) and any(_HO_FB_ROOTREL_HINT.search(w) for ws, _r in segs for w in ws)
    return _ho_segments_hit(segs, cwd, rootrel, drop_msg=True)


# --- T-4578: a bare protected-DIR operand, and relative operands after a `cd` chain ---
# Two holes in the rules above, both closed here by one additive pass (it only adds denials):
#   1. `_PROTECTED_ROOTREL` matches root-relative FILE paths and `os.path.relpath` drops a
#      trailing `/`, so `cp x sop/`, `cp x architecture/adr/` and `git add sop` were allowed
#      for every protected dir. A writer verb's operand is now judged through
#      `_ho_protected_path`, which reads a path as both a file and a directory.
#   2. A relative operand was resolved against the payload cwd only, so `cd <R>/architecture/adr
#      && echo x > probe.md` and `cd <main> && git add sop/x.md` were allowed. The segments
#      are walked in order with a literal cwd tracked through each `cd`, `pushd`, `command cd`
#      and `builtin cd` (one literal operand: absolute, `~` or relative); a dynamic, glob, `-`
#      or no-operand one, and any `popd`, makes the cwd unknown and the rest is skipped
#      (fail-open) until an absolute cd. The lexer drops operator identity, so a `cd` after
#      `||` or in a subshell counts as taken: it can only over-deny, and the payload-cwd
#      rules above still run.
# In THIS pass, verbs are judged by role, so a copy OUT of a protected dir adds no denial here.
# That does not make copy-out allowed: `_ho_records_match_base` still denies any non-read-only
# command, or any redirect, that names a protected path in any position. So `cp sop/x.md /tmp/`
# and `cat tools/README.md > /tmp/x` are denied (over-deny, kept on purpose per T-4664; a worker
# reads with `cat` and writes the copy with the Write tool). cp/install/rsync/ln are judged here
# by destination only (the -t value, else the last operand), mv/rm/touch/mkdir/tee/truncate/
# rmdir by every operand, git add/rm/mv/restore/checkout/commit/apply/clean by every pathspec
# (and by a protected cwd). The values of value-taking options are read per verb
# (`_HO_OPT_SPECS`), so `rsync x sop/ --exclude PAT` finds `sop/` and `touch -r sop/ref x` does
# not count the reference. A cp/install/rsync/ln option the table does not know may take a
# value, so every non-first operand then counts as a destination (over-deny). `git -C DIR`
# is resolved against the tracked cwd (`--git-dir`/`--work-tree` and a dynamic DIR are
# `_ho_git_dir_hit`'s). After a `cd`, any other writer (a redirect, or a verb that is not
# read-only) is judged by every word, as the literal-path rule is (so `cd <protected> &&
# cat x > /tmp/out` is denied, as `cat <pc>/architecture/adr/x > /tmp/out` already is).
_HO_DEST_VERBS = frozenset({"cp", "install", "rsync", "ln"})
_HO_ALL_OPERAND_VERBS = frozenset({"mv", "rm", "rmdir", "touch", "mkdir", "tee", "truncate"})
# verb -> (value-taking short chars, REQUIRED-value long names, `-t` is a target dir,
#          known no-value short chars | None, known no-value long names | None,
#          OPTIONAL-value long names)
# Arity (GNU coreutils / rsync man pages): a required option takes the next word or `=VAL`; an
# optional one (`mkdir --context`) takes `=VAL` only and never the next word; a flag takes
# nothing. Anything not sure is a flag, which never consumes and so judges more operands.
# `None`: unknown options are not tracked (an all-operand verb only over-denies on one).
_HO_OPT_SPECS = {
    "cp": ("tS", frozenset({"target-directory", "suffix", "no-preserve", "sparse"}), True,
           "arRfinpvulsLPHdxTbzZ",
           frozenset({"recursive", "force", "interactive", "no-clobber", "archive", "verbose",
                      "link", "symbolic-link", "dereference", "no-dereference", "parents",
                      "attributes-only", "copy-contents", "remove-destination",
                      "no-target-directory", "one-file-system", "strip-trailing-slashes",
                      "keep-directory-symlink", "debug"}),
           frozenset({"context", "backup", "preserve", "update", "reflink"})),
    "install": ("ogmSt", frozenset({"owner", "group", "mode", "suffix", "target-directory",
                                    "strip-program"}), True, "bcCdDpsTvPZ",
                frozenset({"compare", "directory", "create-directories", "preserve-timestamps",
                           "strip", "verbose", "no-target-directory", "preserve-context"}),
                frozenset({"context", "backup"})),
    "ln": ("St", frozenset({"suffix", "target-directory"}), True, "sfnisvLPrTdFb",
           frozenset({"symbolic", "force", "interactive", "no-dereference", "verbose", "relative",
                      "logical", "physical", "no-target-directory", "directory"}), frozenset({"backup"})),
    "rsync": ("efBTM@", frozenset({
        "rsh", "exclude", "include", "exclude-from", "include-from", "filter", "chmod", "chown",
        "usermap", "groupmap", "rsync-path", "backup-dir", "suffix", "partial-dir", "log-file",
        "log-file-format", "temp-dir", "compare-dest", "copy-dest", "link-dest", "files-from",
        "max-size", "min-size", "bwlimit", "port", "timeout", "contimeout", "address", "sockopts",
        "out-format", "info", "debug", "iconv", "protocol", "block-size", "compress-level",
        "skip-compress", "checksum-choice", "cc", "write-batch", "read-batch", "only-write-batch",
        "stop-after", "stop-at", "early-input", "password-file", "remote-option", "modify-window",
        "max-alloc", "compress-choice", "zc", "outbuf"}),
              False, "arvzcnhPulptogDHxiqbRSWXAUNymkKLOJIE0468",
              frozenset({"archive", "recursive", "verbose", "delete", "delete-after", "delete-before",
                         "delete-during", "delete-delay", "delete-excluded", "dry-run", "checksum",
                         "compress", "times", "perms", "owner", "group", "links", "hard-links", "update",
                         "progress", "partial", "quiet", "force", "ignore-existing", "existing", "inplace",
                         "whole-file", "relative", "one-file-system", "human-readable", "stats",
                         "itemize-changes", "copy-links", "safe-links", "size-only", "ignore-times",
                         "omit-dir-times", "prune-empty-dirs", "remove-source-files", "numeric-ids",
                         "mkpath", "super", "devices", "specials", "executability", "acls", "xattrs",
                         "backup", "append", "append-verify", "dirs", "no-motd", "list-only", "fuzzy",
                         "copy-dirlinks", "keep-dirlinks", "copy-unsafe-links", "munge-links", "sparse",
                         "preallocate", "delay-updates", "ignore-errors", "ignore-missing-args",
                         "delete-missing-args", "stop", "cvs-exclude", "from0", "protect-args",
                         "secluded-args", "8-bit-output", "ipv4", "ipv6", "blocking-io", "archive-links",
                         "open-noatime", "atimes", "crtimes", "no-implied-dirs", "fake-super",
                         "msgs2stderr", "trust-sender", "partial-progress", "write-devices"}), frozenset()),
    "mv": ("tS", frozenset({"target-directory", "suffix"}), True, None, None, frozenset()),
    "touch": ("rdt", frozenset({"reference", "date", "time"}), False, None, None, frozenset()),
    "mkdir": ("m", frozenset({"mode"}), False, None, None, frozenset({"context"})),
    "truncate": ("sr", frozenset({"size", "reference"}), False, None, None, frozenset()),
}
_HO_CD_GATE_WORD = re.compile(
    r"\b(?:cd|pushd|command|builtin|git|cp|install|rsync|ln|mv|rm|rmdir|touch|mkdir|tee|truncate|sed|awk|dd|find)\b|>")
# Substring tests (C speed), a superset of the names: the alternation regex cost ~10ms on a
# 120 KB command that names none.
_HO_CD_GATE_NAMES = ("rulebook", "ledger", "notes", "bugs", "incidents", "lessons", "sop", "architecture",
                     "adr", "engineering-practices", "BACKLOG", "goals", "README", "topics.md", "knowledge")
_HO_DESTROY_GATE_WORD = re.compile(r"\b(?:rm|rmdir|mv|find)\b")
_HO_GIT_WRITE_SUBS = frozenset({"add", "rm", "mv", "restore", "checkout", "commit", "apply", "clean"})
_HO_PUSHD_ROTATE = re.compile(r"[+-]\d+$")
_HO_DYN_WRITE_COMBINED_GATE = re.compile(r"(?:^|[^\d<])>&[ \t]*[\"']?[$`*?\[]")
_HO_DYN_WRITE_NAMED_GATE = re.compile(r"\b(?:tee|mv)\b[^;&|\n]*[$`*?\[]")
_HO_DYN_WRITE_SED_GATE = re.compile(r"\bsed\b[^;&|\n]*-[^-;&|\n]*i[^;&|\n]*[$`*?\[]")
_HO_DYN_WRITE_GIT_GATE = re.compile(r"\bgit\b[^;&|\n]*\b(?:add|commit)\b[^;&|\n]*[$`*?\[]")
_HO_DYN_WRITE_CP_CMD_GATE = re.compile(r"(?:^|[\s({!])(?:\S*/)?cp(?=$|[\s<>()])")
_HO_DYN_WRITE_MARKER_GATE = re.compile(r">|(?:^|[\s({!='\"])(?:\S*/)?(?:git|cp|mv|tee|sed|env|exec|nohup|timeout)(?=$|[\s<>()])")
_HO_DYN_WRITE_CODE_GATE = re.compile(
    r"(?:^|[\s({!])(?:\S*/)?(?:r?bash|sh|zsh|dash|ksh|ash|mksh|hush|static-sh|script|flock)(?=$|[\s<>()])")
_HO_DYN_WRITE_CODE_NAMES = ("bash", "sh", "zsh", "dash", "ksh", "ash", "mksh", "hush", "script", "flock")


def _ho_dynamic_write_gate(view: str) -> bool:
    """Cheap no-name prefilter for T-4785 dynamic write-target surfaces."""
    has_redirect = ">" in view
    has_cp = "cp" in view
    has_git = "git" in view
    has_sed = "sed" in view
    has_named = "tee" in view or "mv" in view
    has_wrapper = "env" in view or "exec" in view or "nohup" in view or "timeout" in view
    has_code = any(name in view for name in _HO_DYN_WRITE_CODE_NAMES) and bool(
        _HO_DYN_WRITE_CODE_GATE.search(view))
    if not (has_redirect or has_cp or has_git or has_sed or has_named or has_wrapper or has_code):
        return False
    if not _HO_DYN_PATH.search(view):
        return False
    return bool((has_redirect and (_ho_dynamic_redirect_gate(view)
                                   or (">&" in view and _HO_DYN_WRITE_COMBINED_GATE.search(view))))
                or (has_named and _HO_DYN_WRITE_NAMED_GATE.search(view))
                or (has_sed and _HO_DYN_WRITE_SED_GATE.search(view))
                or (has_git and _HO_DYN_WRITE_GIT_GATE.search(view))
                or ((has_cp or has_wrapper) and _ho_dynamic_cp_gate(view))
                or (has_code and _HO_DYN_WRITE_MARKER_GATE.search(view)))


def _ho_dynamic_redirect_gate(view: str) -> bool:
    """Linear redirect prefilter: only segments with both `>` and dynamic syntax matter."""
    start, n = 0, len(view)
    while start < n:
        end = start
        while end < n:
            c = view[end]
            if c in ";&\n" or (c == "|" and not (end > start and view[end - 1] == ">")):
                break
            end += 1
        segment = view[start:end]
        if ">" in segment and _HO_DYN_PATH.search(segment):
            return True
        start = end + 1
    return False


def _ho_dynamic_cp_gate(view: str) -> bool:
    """Cheap cp prefilter; the rich wrapper/operand pass decides source vs destination."""
    start, n = 0, len(view)
    while start < n:
        end = start
        while end < n and view[end] not in ";&|\n":
            end += 1
        segment = view[start:end]
        if _HO_DYN_PATH.search(segment) and _ho_dynamic_cp_candidate(segment):
            return True
        start = end + 1
    return False


def _ho_dynamic_cp_candidate(segment: str) -> bool:
    """True when a segment might contain a valid cp invocation with enough operands."""
    if not _HO_DYN_WRITE_CP_CMD_GATE.search(segment):
        return False
    body = segment.split("#", 1)[0].split(">", 1)[0].strip()
    words = body.split()
    for i, word in enumerate(words):
        if word.rsplit("/", 1)[-1] != "cp":
            continue
        rest = words[i + 1:]
        if not rest:
            return False
        if any((" " in w or "\t" in w) for w in rest):
            return True
        if any(w == "-t" or w == "--target-directory" or w.startswith("--target-directory=")
               for w in rest):
            return len([w for w in rest if w and not w.startswith("-")]) >= 1
        if any(w.startswith("-") for w in rest):
            return True
        return len(rest) >= 2 and _ho_dynamic_word(rest[-1])
    return True


def _ho_cd_gate(command: str, cwd: str) -> bool:
    """False only when `command` cannot reach a hit: no writer/`cd` word, no protected-dir
    name in command/cwd, and no T-4785 dynamic named write-target prefilter hit. A `$'..'`
    spelling can decode to any name, so it always passes."""
    if "$'" in command or '$"' in command:
        return True
    view = command
    if "\\" in command or "'" in command or '"' in command:
        # T-5360: an `env -S` string's `\_` is env's word separator (`env -S 'rm\_-rf\_D'`):
        # a blank here, so the word gates below see `rm` (a superset elsewhere).
        view = command.replace("\\\n", "").replace("\\_", " ").translate(_QUOTE_STRIP)
    cw = cwd or ""
    if not any(n in view or n in cw for n in _HO_CD_GATE_NAMES):
        if _ho_dynamic_write_gate(view):
            return True
        # T-5326: a destroy can name no protected dir (`rm -rf <root>`, `rm -rf *`, `env -C
        # "$D" rm -rf l*`): rm/rmdir/mv/find always pass.
        return bool(_HO_DESTROY_GATE_WORD.search(view))
    return bool(_HO_CD_GATE_WORD.search(view))


def _ho_long_opt(name: str, vals: frozenset, flags: frozenset | None, opt: frozenset) -> str:
    """`name` with a GNU unique-prefix abbreviation (`--target`, `--cont`) resolved."""
    allv = vals | opt | (flags or frozenset())
    if name in allv or len(name) < 2:
        return name
    cands = [o for o in allv if o.startswith(name)]
    return cands[0] if len(cands) == 1 else name


def _ho_operands(verb: str, words: list[str]) -> tuple[list[str], list[str], bool, bool]:
    """(operands, target-directory values, has an install `-d`, an option the table does not
    know) of `verb`'s words; the values of the other value-taking options are dropped."""
    sv, lv, tdir, sf, lf, lo = _HO_OPT_SPECS.get(verb, ("", frozenset(), False, None, None, frozenset()))
    ops: list[str] = []
    tvals: list[str] = []
    dflag = unknown = False
    opts = True
    i, n = 1, len(words)
    while i < n:
        w = words[i]
        i += 1
        if not opts or w[:1] != "-" or w == "-":
            ops.append(w)
        elif w == "--":
            opts = False
        elif w[:2] == "--":
            name, eq, val = w[2:].partition("=")
            full = _ho_long_opt(name, lv, lf, lo)
            if full in lv:
                if not eq and i < n:
                    val = words[i]
                    i += 1
                if full == "target-directory":
                    tvals.append(val)
            elif full in lo:
                pass  # optional value: only `=VAL`, never the next word
            elif lf is not None and full not in lf and not full.startswith("no-"):
                unknown = True
                if eq:
                    tvals.append(val)
        else:
            for k in range(1, len(w)):
                c = w[k]
                if c in sv:
                    val = w[k + 1:]
                    if not val and i < n:
                        val = words[i]
                        i += 1
                    if c == "t" and tdir:
                        tvals.append(val)
                    break
                dflag = dflag or (c == "d" and verb == "install")
                if sf is not None and c not in sf:
                    unknown = True
    return ops, tvals, dflag, unknown


def _ho_cd_operand_hit(op: str, cwd: str, cache: dict) -> bool:
    """True if `op`, joined to the tracked `cwd`, is a protected path or dir."""
    if not op or isinstance(op, _HoDynWord) or _HO_DYN_PATH.search(op):
        return False
    o = os.path.expanduser(op)
    if not os.path.isabs(o):
        if not cwd:
            return False
        o = os.path.join(cwd, o)
    o = os.path.normpath(o)
    hit = cache.get(o)
    if hit is None:
        hit = cache[o] = _ho_protected_path(o)
    return hit


def _ho_dynamic_word(word: str) -> bool:
    """True if `word` has unresolved shell expansion/indirection/glob reachability."""
    return isinstance(word, _HoDynWord) or bool(_HO_DYN_PATH.search(word))


def _ho_dynamic_write_target_hit(target: str, cwd: str, cache: dict) -> bool:
    """True if a write target is runtime-computed and could reach a protected path."""
    del cwd, cache
    return isinstance(target, _HoDynWord) or (bool(target) and _ho_dynamic_word(target))


_HO_T4785_WRAPPERS = frozenset({"env", "exec", "nohup", "timeout"})
# T-5325: the static protected-path rules (the T-4578 bare-dir/cd pass and the code-string
# pass) peel every wrapper that runs its arguments as a LOCAL command, using the shared
# `_WRAPPER_GRAMMAR` option tables. Left out: eval (judged by `_eval_joined_text`), ssh (a
# remote command), machinectl, and script (its operand is a log file; `-c` code is a code
# string). `command`/`builtin` keep their own `-v` handling (`_ho_peel_lead`). The set is
# `_HO_STATIC_WRAPPERS`, defined after `_WRAPPER_GRAMMAR` below.


def _ho_peel_lead(words: list[str], time_opts: bool = False) -> list[str]:
    """Leading `NAME=v`/reserved words and a `command`/`builtin` prefix dropped
    (`command -v X` is a query: nothing runs, so nothing is left)."""
    words = _ho_strip_lead(words, time_opts)
    while words and words[0] in ("command", "builtin"):
        k = 1
        while k < len(words) and words[k][:1] == "-":
            if "v" in words[k] or "V" in words[k]:
                k = len(words)  # `command -v cd D`: a query, nothing runs
            k += 1
        words = _ho_strip_lead(words[k:], time_opts)
    return words


def _ho_static_command_words(words: list[str], chdirs: list | None = None,
                             trace: dict | None = None) -> list[str]:
    """T-5325: the words of the command a wrapper chain runs (`nice -n 5 timeout 3 env
    FOO=1 rm X` -> `rm X`), for the static protected-path rules. An option a wrapper's
    table does not know stops the peel there (the wrapper word stays the verb).
    T-5326: a `time` keyword's options are peeled too (`time -p rm X`), and each `env`
    -C/--chdir value is appended to `chdirs` (outermost first) when a list is given.
    T-5341: `trace` (when given, a dict) gets "peeled", the (word, index in `words`) pairs
    the peel consumed in order (an `env -S` value as the placeholder `-S`, its split words
    following as words of their own), and "src", each returned word's index in `words`."""
    src = list(range(len(words))) if trace is not None else None
    if trace is not None:
        trace["peeled"] = []
    while True:
        lead = _ho_peel_lead(words, time_opts=True)
        if trace is not None:
            cut = len(words) - len(lead)
            trace["peeled"].extend(zip(words[:cut], src[:cut]))
            src = src[cut:]
        words = lead
        peeled = _ho_t4785_wrapper_words(words, _HO_STATIC_WRAPPERS, chdirs,
                                         None if trace is None else (trace["peeled"], src))
        if peeled == words:
            if trace is not None:
                trace["src"] = src
            return words
        words = peeled


# T-5360: GNU env's -S/--split-string grammar (coreutils `info coreutils 'env invocation'`,
# "-S/--split-string syntax"), the one splitter every -S reader uses (the wrapper peel, so
# the fed-shell consumer and the code-string reader's peeled argv; the escalation walk;
# `_py_wrapper_codes`; `_py_env_chdirs`). Words split at unquoted blanks (space, tab, newline,
# CR, VT, FF). `'...'` and `"..."` are removed; inside single quotes only `\\` and `\'` are
# escapes. Outside single quotes: `\_` is a separator (a space inside double quotes), `\c`
# ends the string (an error inside double quotes), `\f \n \r \t \v` are those characters,
# `\" \# \$ \' \\` are literal, any other `\X` or a trailing `\` is an error. A `#` that
# starts a word (unquoted) ends the string. `${NAME}` (outside single quotes) is env's own
# run-time expansion: the word is kept with the `${NAME}` text, as a `_HoDynWord` (judged
# fail-closed like any run-time word); any other `$` is an error. An unterminated quote is an
# error. On an error env refuses the string and runs nothing: the splitter returns None.
_ENV_S_BLANKS = frozenset(" \t\n\r\v\f")
_ENV_S_ESCAPES = {"f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                  '"': '"', "#": "#", "$": "$", "'": "'", "\\": "\\"}
_ENV_S_VAR = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _env_s_split(value: str, opaque: set | None = None) -> list[str] | None:
    """T-5360: `value` split as GNU env splits an -S/--split-string value (see above), or
    None when env rejects it. A word holding a `${NAME}` expansion is a `_HoDynWord`.
    `opaque` (when given) gets the index of each word env built from a quote or a backslash
    escape (not a plain literal, as T-4426 reads a shell word). Linear."""
    out: list[str] = []
    cur: list[str] = []
    started = dyn = sq = dq = quoted = False
    i, n = 0, len(value)

    def end() -> None:
        nonlocal started, dyn, quoted
        if started:
            word = "".join(cur)
            if quoted and opaque is not None:
                opaque.add(len(out))
            out.append(_HoDynWord(word) if dyn else word)
        cur.clear()
        started = dyn = quoted = False

    while i < n:
        ch = value[i]
        if ch == "'" and not dq:
            sq = not sq
            started = quoted = True
            i += 1
        elif ch == '"' and not sq:
            dq = not dq
            started = quoted = True
            i += 1
        elif ch in _ENV_S_BLANKS and not sq and not dq:
            end()
            i += 1
        elif ch == "#" and not sq and not dq and not started:
            break  # a comment to the end of the string
        elif ch == "$" and not sq:
            m = _ENV_S_VAR.match(value, i + 1)
            if m is None:
                return None  # only `${NAME}` is supported
            cur.append(value[i:m.end()])
            started = dyn = True
            i = m.end()
        elif ch == "\\" and (not sq or value[i + 1:i + 2] in ("\\", "'")):
            if i + 1 >= n:
                return None  # a backslash at the end of the string
            e = value[i + 1]
            i += 2
            if e == "_":
                if dq:
                    cur.append(" ")
                    started = quoted = True
                else:
                    end()
            elif e == "c":
                if dq:
                    return None
                break  # the rest of the string is ignored
            elif e in _ENV_S_ESCAPES:
                cur.append(_ENV_S_ESCAPES[e])
                started = quoted = True
            else:
                return None  # an invalid sequence
        else:
            cur.append(ch)
            started = True
            i += 1
    if sq or dq:
        return None
    end()
    return out


_ENV_S_VALUE_OPTS = frozenset({"-u", "-C", "-S", "-a", "--unset", "--chdir", "--split-string",
                               "--argv0"})


def _env_s_head_opaque(words: list[str], opaque: set) -> bool:
    """T-5360: True if an env option word or the command word of the -S `words` (the first
    word that is not an option, an option's separate value or an assignment) is one env built
    from a quote or an escape (`opaque`, from `_env_s_split`): T-4426's fail-closed reading of
    a command word that is not a plain literal (`env -S '"su"do x'`, `env -S 'sudo\\tid'`,
    `env -S '-i\\tbash'`, `env -S '\\$X bash'`), which the `\\`-is-unknown reading denied
    before. An argument (`env -S 'printf "%s\\n" "a b"'`) is not judged here."""
    k, n = 0, len(words)
    while k < n:
        w = words[k]
        if k in opaque:
            return True
        if w == "--":
            return k + 1 < n and k + 1 in opaque
        if w[:1] == "-" and w != "-":
            if w in _ENV_S_VALUE_OPTS or (w[1:2] != "-" and w[-1] in "uCSa"):
                k += 1  # its value
            k += 1
            continue
        if _ASSIGN_WORD.match(w):
            k += 1
            continue
        return False
    return False


def _env_s_words(value: str) -> list[str]:
    """T-5360: `value`'s -S words for a reader that must split it: the GNU split, or (a
    string env rejects, so nothing runs; or a value built at run time) the pre-T-5360 blank
    split, which keeps every judgement that reading made."""
    if isinstance(value, _HoDynWord):
        return value.split()
    words = _env_s_split(value)
    return value.split() if words is None else words


# T-5326 r2: the `env -S` re-split cap, and the `chdirs` marker it leaves (an unknown dir
# under which every destroy verb counts).
_HO_ENV_S_RESPLIT_MAX = 64
_HO_CHDIR_CAPPED = _HoDynWord("$(env -S cap)")
_HO_DESTROY_VERBS = frozenset({"rm", "rmdir", "mv", "find"})


def _ho_t4785_wrapper_words(words: list[str], names: frozenset = _HO_T4785_WRAPPERS,
                            chdirs: list | None = None, track: tuple | None = None) -> list[str]:
    """Peel transparent simple wrappers (`names`) before classifying write targets.
    T-5326: `chdirs` (when given) collects each env's -C/--chdir value (GNU env keeps the
    last of one invocation's), and an `env -S` value that starts with an option or an
    assignment is read as more env arguments (`env -S '-C D rm X'`).
    T-5341: `track` (when given) is (peeled, src): `src` holds each word's source index and
    is updated in place to match the returned words; each word peeled is appended to
    `peeled` as (word, source index), an `env -S` value as `-S` (its words follow)."""
    out = list(words)
    resplits = 0
    srcs = list(track[1]) if track is not None else None

    def _peel(k: int, value_at: int = -1) -> None:
        # T-5341: record out[:k] as peeled (out[value_at] is an -S value: `-S`).
        if track is not None:
            track[0].extend(("-S" if j == value_at else out[j], srcs[j]) for j in range(k))

    def _done(res: list[str], res_src: list[int]) -> list[str]:
        if track is not None:
            track[1][:] = res_src
        return res
    while out:
        before = len(out)
        if not out:
            return _done(out, srcs)
        name = out[0].rsplit("/", 1)[-1]
        if name not in names:
            return _done(out, srcs)
        shorts, longs, operands = _WRAPPER_GRAMMAR[name]
        i = 1
        split_words = None
        chdir = None
        while i < len(out):
            v = str(out[i])
            if name == "env" and (v == "-" or _ASSIGN_WORD.match(v)):
                i += 1
                continue
            if v == "--":
                i += 1
                break
            if len(v) < 2 or not v.startswith("-"):
                break
            nw = str(out[i + 1]) if i + 1 < len(out) else None
            at, take_next, key = _option_parse(name, v, nw, shorts, longs, False)
            if key == "?":
                return _done(out, srcs)
            value = v[at:] if at is not None else None
            i += 1
            if take_next:
                value = out[i]
                i += 1
            if name == "env" and key in ("C", "chdir") and value is not None:
                chdir = value
            if name == "env" and key in ("S", "split-string") and value:
                pieces = _env_s_words(value)  # T-5360: GNU -S grammar
                split_words = pieces + out[i:]
                if track is not None:
                    split_src = [srcs[i - 1]] * len(pieces) + srcs[i:]
                break
        else:
            _peel(len(out))
            return _done([], [])
        if chdirs is not None and chdir is not None:
            chdirs.append(chdir)
        if split_words is not None:
            if chdirs is not None and split_words and (split_words[0][:1] == "-"
                                                       or _ASSIGN_WORD.match(split_words[0])):
                # T-5326: env reads the -S words as its own arguments; the text shrinks by
                # at least the `-S` itself, so this terminates.
                split_words = [out[0]] + split_words
                if track is not None:
                    split_src = [srcs[0]] + split_src
                if sum(map(len, split_words)) >= sum(map(len, out)):
                    return _done(out, srcs)
                resplits += 1
                if resplits > _HO_ENV_S_RESPLIT_MAX:
                    # r2: each re-split costs O(len): cap them (`env -S '-S -S ...'`). Past
                    # the cap the directory is unknown and any destroy verb counts (fail
                    # closed); a command without one is returned as is.
                    for j, w in enumerate(split_words):
                        if str(w).rsplit("/", 1)[-1] in _HO_DESTROY_VERBS:
                            chdirs.append(_HO_CHDIR_CAPPED)
                            _peel(i, i - 1)
                            if track is not None:
                                track[0].extend(zip(split_words[:j], split_src[:j]))
                            return _done(split_words[j:], split_src[j:] if track is not None else [])
                    return _done(out, srcs)
            elif sum(map(len, pieces)) > len(value):
                # T-5360: the text shrinks by at least the `env` and `-S` words while the split
                # words are no longer than the value (the GNU split never lengthens one), so
                # this terminates; a word count would refuse a string that splits into more
                # words than it removes (`env -S 'bash -c' CODE`).
                return _done(out, srcs)
            _peel(i, i - 1)
            out = split_words
            if track is not None:
                srcs = split_src
            continue
        for _operand in range(operands):
            if i < len(out):
                i += 1
        if len(out[i:]) >= before:
            return _done(out, srcs)
        _peel(i)
        out = out[i:]
        if track is not None:
            srcs = srcs[i:]
    return _done(out, srcs)


def _ho_sed_inplace_write_operands(words: list[str]) -> list[str]:
    """Best-effort sed -i edited-file operands, excluding option values and script text."""
    if not words or words[0].rsplit("/", 1)[-1] != "sed":
        return []
    inplace = False
    files: list[str] = []
    saw_script = False
    i, n = 1, len(words)
    while i < n:
        w = words[i]
        i += 1
        if w == "--":
            files.extend(words[i:])
            break
        if w == "-i" or w == "--in-place" or w.startswith("--in-place=") \
                or (w.startswith("-") and not w.startswith("--") and "i" in w[1:]):
            inplace = True
            continue
        if w in ("-e", "--expression", "-f", "--file"):
            if i < n:
                i += 1
            saw_script = True
            continue
        if w.startswith(("-e", "-f")) and len(w) > 2:
            saw_script = True
            continue
        if w.startswith("-"):
            continue
        if not saw_script:
            saw_script = True
            continue
        files.append(w)
    return files if inplace else []


def _ho_commit_pathspecs(args: list[str]) -> list[str]:
    """Best-effort git commit pathspec operands; message/control option values are skipped."""
    specs: list[str] = []
    value_opts = {
        "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
        "--fixup", "--squash", "--author", "--date", "--template", "--cleanup",
        "-t", "--trailer", "--pathspec-from-file",
    }
    opts = True
    i, n = 0, len(args)
    while i < n:
        w = args[i]
        i += 1
        if opts and w == "--":
            opts = False
            continue
        if opts and (w in value_opts or _is_short_cluster_ending_in_m(w)):
            if i < n:
                i += 1
            continue
        if opts and any(w.startswith(opt + "=") for opt in value_opts if opt.startswith("--")):
            continue
        if opts and w.startswith("--message="):
            continue
        if opts and w.startswith("-"):
            continue
        specs.append(w)
    return specs


def _ho_git_pathspec_file_values(args: list[str]) -> list[str]:
    """Values whose file contents supply pathspecs to git add/commit."""
    vals: list[str] = []
    i, n = 0, len(args)
    while i < n:
        w = args[i]
        i += 1
        if w == "--":
            break
        if w == "--pathspec-from-file":
            if i < n:
                vals.append(args[i])
                i += 1
            continue
        if w.startswith("--pathspec-from-file="):
            vals.append(w.partition("=")[2])
            continue
    return vals


def _ho_dynamic_git_write_hit(words: list[str], cwd: str, cache: dict) -> bool | None:
    """Dynamic-pathspec sibling of `_ho_git_write_hit` for named protected write surfaces."""
    if not words or words[0].rsplit("/", 1)[-1] != "git":
        return None
    i, n = 1, len(words)
    while i < n and words[i][:1] == "-" and words[i] != "--":
        w = words[i]
        if w in ("-C", "-c"):
            i += 2
        elif w.startswith(("--git-dir", "--work-tree")) or (w.startswith("-C") and len(w) > 2):
            return False
        else:
            i += 1
    if i >= n:
        return None
    sub = words[i]
    if sub == "add":
        ops = _ho_git_pathspec_file_values(words[i + 1:]) + [
            w for w in words[i + 1:] if w and w[0] != "-"]
    elif sub == "commit":
        ops = _ho_git_pathspec_file_values(words[i + 1:]) + _ho_commit_pathspecs(words[i + 1:])
    elif sub == "hash-object" and _git_hash_object_write_mode(["git"] + words[i:]):
        ops = [w for w in words[i + 1:] if w and w[0] != "-"]
    else:
        return None
    return any(_ho_dynamic_write_target_hit(o, cwd, cache) for o in ops)


def _ho_git_write_hit(words: list[str], cwd: str, cache: dict) -> bool | None:
    """A git write subcommand with its `-C DIR` resolved against the tracked `cwd`: True on a
    hit, False when judged clean (or left to `_ho_git_dir_hit`), None for any other git command."""
    i, n, base = 1, len(words), cwd
    while i < n and words[i][:1] == "-" and words[i] != "--":
        w = words[i]
        if w == "-C":
            val = words[i + 1] if i + 1 < n else ""
            base = _ho_dir_join(base, val) if base and val else None
            i += 2
        elif w == "-c":
            i += 2
        elif w.startswith(("--git-dir", "--work-tree")) or (w.startswith("-C") and len(w) > 2):
            return False  # `_ho_git_dir_hit`'s
        else:
            i += 1
    if i >= n or words[i] not in _HO_GIT_WRITE_SUBS:
        return None
    if not base or not os.path.isabs(base):
        return False  # a dynamic or unresolvable -C, or no cwd
    if _ho_cd_operand_hit(".", base, cache):
        return True
    ops = _drop_commit_message(["git", words[i]] + words[i + 1:])[2:]
    return any(o and o[0] != "-" and _ho_cd_operand_hit(o, base, cache) for o in ops)


# --- T-5326: the destroy rule's remaining shapes ---
# Added to the T-4578/T-5325 pass (adds denials only; the segment as written, and against the
# cwd, is still judged as before):
#   (a) `time` options: `time -p|-v|-f FMT|-o FILE|--format=..` CMD judges CMD
#       (`_ho_skip_time_options`, in the static peel only).
#   (b) `env -C DIR` / `--chdir[=]DIR` / `-CDIR` (and an `env -S '-C DIR ...'` value): CMD's
#       relative operands are judged against DIR too. A dynamic DIR (`$D`, `$(..)`) is
#       unknown: a relative rm/rmdir/mv/`find -delete` operand naming a protected dir, an
#       ancestor of one, a protected-name glob, `.` or `..` counts (as a dynamic `git -C`
#       does, T-4497); any other relative operand stays allowed (`env -C "$D" rm -rf build`).
#   (c) bfs `find`: start points anywhere (below).
#   (d) a recursive destroy (`rm -r|-R|-d`, rmdir, mv sources, `find -delete`) of an ANCESTOR
#       of a protected dir: a checkout root (`rm -rf <root>`, `rm -rf .` there, `rm -rf ..`
#       from a subdir) or its `architecture/`. `git clean` at the root is NOT counted: it
#       removes untracked files only, and the protected dirs are tracked.
#   (e) glob/brace operands of those verbs: the last component is matched (fnmatch, braces
#       expanded) against the protected names at that level; a glob inside a protected dir
#       counts; a plain `rm` glob counts on a root's protected top-level files.
# RESIDUALS (not judged): a glob above the last component (`<root>/*/adr`), extglob
# (`@(ledger)`), `$VAR` operands, ancestors ABOVE a checkout root (`rm -rf ~`), the
# `.claude/worktrees` dir that holds isolation worktrees, `find -exec rm`, stdin-fed xargs.
# KNOWN OVER-BLOCKS: `find <root> -delete` with any filter but a cache-artefact `-name`
# (`_HO_FIND_CACHE_NAMES`); a quoted glob/brace (read as if unquoted); `env -C "$D" rm -rf .`.
#
# `find` start points (bfs grammar). GNU find wants its starting points before the
# expression; bfs takes a path anywhere (and repeated `--`). With `-delete` present, every
# word that is not an option, an operator, a predicate or a predicate's argument is read as a
# start point. Predicate arity from the GNU findutils 4.9 and bfs 3.x man pages; a predicate
# missing from the table is read as taking NO argument, so the word after it is judged as a
# start point (an over-read only adds denials). -exec/-execdir/-ok/-okdir take words up to
# `;` (or a `+` right after `{}`).
_HO_FIND_ARITY = dict.fromkeys((
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-lname", "-ilname",
    "-regex", "-iregex", "-regextype", "-type", "-xtype", "-newer", "-anewer", "-cnewer",
    "-Bnewer", "-mtime", "-atime", "-ctime", "-Btime", "-mmin", "-amin", "-cmin", "-Bmin",
    "-used", "-size", "-user", "-group", "-uid", "-gid", "-perm", "-inum", "-links",
    "-samefile", "-maxdepth", "-mindepth", "-fstype", "-context", "-printf", "-fprint",
    "-fprint0", "-fls", "-files0-from", "-D", "-S", "-j", "-xattrname", "-limit", "-flags",
    "-since", "-asince", "-csince", "-msince"), 1) | {"-fprintf": 2}
_HO_FIND_NEWER_XY = re.compile(r"-newer[aBcmt][aBcmt]t?$")
_HO_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# The names the destroy rule matches a glob's last component against: every protected dir's
# components (`architecture` is the ancestor of two). Plus the protected top-level files.
_HO_DESTROY_NAMES = ("rulebook", "ledger", "notes", "bugs", "incidents", "lessons", "sop",
                     "architecture", "adr", "engineering-practices")
_HO_ROOT_FILES = ("BACKLOG.md", "goals.md", "README.md")
# `find ROOT -name PAT -delete` deletes only what PAT matches. A build/cache-artefact PAT
# (exact spellings below) is exempt from the ancestor rule, so `find . -name '*.pyc' -delete`
# at a worktree root stays allowed; any other PAT could name protected docs (ledger shards,
# `*.md`) and is not.
_HO_FIND_CACHE_NAMES = frozenset({"*.pyc", "*.pyo", "*.py[co]", "__pycache__", ".pytest_cache",
                                  ".mypy_cache", ".ruff_cache", "*.orig", "*.rej", ".DS_Store"})
_HO_GLOB_CHARS = re.compile(r"[*?\[]")
_HO_BRACE_MAX = 64


def _ho_find_start_points(words: list[str]) -> list[str]:
    """`find`'s starting points, read as bfs reads them (see the T-5326 comment above): every
    word that is not an option, operator, predicate or predicate argument. A superset of
    GNU find's (which stop at the first expression word)."""
    out = []
    i, n = 1, len(words)
    while i < n:
        w = words[i]
        i += 1
        if w in _HO_FIND_EXEC:
            prev = ""
            while i < n and words[i] != ";" and not (words[i] == "+" and prev == "{}"):
                prev = words[i]
                i += 1
            i += 1
        elif w in ("--", "(", ")", "!", ","):
            continue
        elif w[:1] == "-" and len(w) > 1:
            i += _HO_FIND_ARITY.get(w, 1 if _HO_FIND_NEWER_XY.match(w) else 0)
        else:
            out.append(w)
    return out


_HO_FIND_EXEMPT_FLAGS = frozenset({"-depth"})
_HO_FIND_EXEMPT_NUM = frozenset({"-mindepth", "-maxdepth"})
_HO_FIND_EXEMPT_TYPES = frozenset({"d", "f"})
# r4: an exempt find's start points are plain literal paths (words arrive dequoted, with
# `{`, `}`, `$`, backticks and glob characters kept): nothing in them expands. r5: no `~`
# (tilde expansion reads $HOME/$PWD/$OLDPWD: `HOME=-delete; find R ~ ...`), `@` or `+`
# (extglob `@(..)`/`+(..)`; extglob itself is denied, `_EXTGLOB_ENABLE`); a start point
# holding one loses the exemption.
_HO_FIND_PLAIN_START = re.compile(r"(?!-)[A-Za-z0-9._/%:=,-]+\Z")


def _ho_find_name_filtered(words: list[str]) -> bool:
    """True if `find` deletes only cache artefacts (`_HO_FIND_CACHE_NAMES`). T-5326 r3: an
    allowlist of exact shapes, not a parse (an option's argument can spell `-name '*.pyc'`:
    `find R -fprintf -name '*.pyc' -delete` deletes the tree). After the leading start points,
    every word is one of `-type d|f`, `-name|-iname <cache>`, `-depth`, `-mindepth|-maxdepth
    <digits>`, with `-delete` the LAST word and appearing once, and a cache `-name`/`-iname`
    present. Anything else (other predicates, operators, groups, option arguments, -exec, a
    start point after the expression) is no exemption: unusual cache cleanups over-block.
    r4: every start point must be a plain literal path (`_HO_FIND_PLAIN_START`): a word that
    expands at run time (`{R,-delete,-o}`, `$X`, `$(..)`, a glob) can become expression words."""
    i, n = 1, len(words)
    while i < n and words[i][:1] not in ("-", "(", "!", ")", ","):
        w = words[i]
        if isinstance(w, _HoDynWord) or not _HO_FIND_PLAIN_START.match(w):
            return False
        i += 1  # the start points
    if n - i < 3 or words[-1] != "-delete":
        return False
    named = False
    end = n - 1
    while i < end:
        w = words[i]
        arg = words[i + 1] if i + 1 < end else None
        if w in _HO_FIND_EXEMPT_FLAGS:
            i += 1
        elif w in _HO_FIND_EXEMPT_NUM and arg is not None and arg.isascii() and arg.isdigit():
            i += 2
        elif w == "-type" and arg in _HO_FIND_EXEMPT_TYPES:
            i += 2
        elif w in ("-name", "-iname") and arg in _HO_FIND_CACHE_NAMES:
            named = True
            i += 2
        else:
            return False
    return named


def _ho_glob_match(pat: str, name: str) -> bool:
    """bash pathname matching of one component (`[^x]` is `[!x]`). T-5326 r2: a bracket
    expression holding a POSIX class/equivalence/collating element (`[[:alpha:]]`, `[[=l=]]`,
    `[[.l.]]`), which fnmatch does not know, reads as `?` (a superset)."""
    if "[" in pat and ("[:" in pat or "[=" in pat or "[." in pat):
        pat = _ho_glob_posix_classes(pat)
    return fnmatch.fnmatchcase(name, pat.replace("[^", "[!"))


@functools.lru_cache(maxsize=1024)
def _ho_glob_posix_classes(pat: str) -> str:
    """`pat` with each bracket expression that holds `[:..:]`, `[=..=]` or `[...]` replaced
    by `?`. An unclosed `[` is a literal (as bash reads it). A pattern over 64 chars reads
    as `*` (a superset; bounds the rescans after an unclosed `[`)."""
    if len(pat) > 64:
        return "*"
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c != "[":
            out.append(c)
            i += 1
            continue
        j = i + 1
        if j < n and pat[j] in "!^":
            j += 1
        if j < n and pat[j] == "]":
            j += 1  # a leading `]` is a member
        special = False
        while j < n and pat[j] != "]":
            if pat[j] == "[" and j + 1 < n and pat[j + 1] in ":=.":
                end = pat.find(pat[j + 1] + "]", j + 2)
                if end != -1:
                    special = True
                    j = end + 2
                    continue
            j += 1
        if j >= n:
            out.append("[[]")  # unclosed: a literal `[`; bash rescans from the next char
            i += 1
            continue
        out.append("?" if special else pat[i:j + 1])
        i = j + 1
    return "".join(out)


def _ho_brace_alts(word: str) -> list[str]:
    """bash brace expansion of `word`: comma lists expand, a `{a..b}` sequence reads as `*`.
    Past `_HO_BRACE_MAX` words, every innermost brace group reads as `*` (a superset)."""
    if len(word) > 1024 or word.count("{") > 32:
        return [re.sub(r"\{[^{}]*\}", "*", word)]  # bounded work: nesting rescans
    out: list[str] = []
    todo = [word]
    while todo:
        w = todo.pop()
        i = w.find("{")
        done = True
        while i != -1:
            depth, commas, j = 0, [], -1
            for k in range(i, len(w)):
                c = w[k]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        j = k
                        break
                elif c == "," and depth == 1:
                    commas.append(k)
            if j < 0:
                break
            if commas:
                bounds = [i] + commas + [j]
                todo.extend(w[:i] + w[a + 1:b] + w[j + 1:] for a, b in zip(bounds, bounds[1:]))
                done = False
                break
            if ".." in w[i + 1:j]:
                todo.append(w[:i] + "*" + w[j + 1:])
                done = False
                break
            i = w.find("{", i + 1)
        if done:
            out.append(w)
        if len(out) + len(todo) > _HO_BRACE_MAX:
            return [re.sub(r"\{[^{}]*\}", "*", word)]
    return out


def _ho_ancestor_hit(path: str, cache: dict) -> bool:
    """T-5326: True if `path` holds a protected dir: a pantheon-config checkout root (any
    shape `_ho_protected_path` knows) or its `architecture/`."""
    key = ("anc", path)
    hit = cache.get(key)
    if hit is None:
        hit = cache[key] = (_ho_protected_path(os.path.join(path, "ledger"))
                            or _ho_protected_path(os.path.join(path, "adr")))
    return hit


def _ho_destroy_operand_hit(op: str, cwd: str, cache: dict, recursive: bool) -> bool:
    """T-5326: True if destroying operand `op` (resolved from `cwd`) removes protected docs:
    a recursive destroy of an ancestor of a protected dir (`rm -rf <root>`, `.../architecture`),
    or a glob/brace operand whose last component can match one (`<root>/led*r`, `<root>/*`,
    `{ledger,x}`, `ledger/*`). `recursive` False (a plain `rm`): only files go, so a glob
    counts inside a protected dir or when it matches a protected top-level file. `$`/backtick
    operands are not judged here."""
    if not op or isinstance(op, _HoDynWord) or "$" in op or "`" in op:
        return False
    key = ("dop", op, cwd, recursive)
    hit = cache.get(key)
    if hit is None:
        hit = cache[key] = _ho_destroy_operand_judge(op, cwd, cache, recursive)
    return hit


def _ho_destroy_operand_judge(op: str, cwd: str, cache: dict, recursive: bool) -> bool:
    """`_ho_destroy_operand_hit`, uncached."""
    for a in (_ho_brace_alts(op) if "{" in op else [op]):
        a = os.path.expanduser(a)
        if not os.path.isabs(a):
            if not cwd:
                continue
            a = os.path.join(cwd, a)
        a = os.path.normpath(a)
        if not _HO_GLOB_CHARS.search(a):
            if _ho_cd_operand_hit(a, "", cache) or (recursive and _ho_ancestor_hit(a, cache)):
                return True
            continue
        head, base = os.path.split(a)
        if not base or _HO_GLOB_CHARS.search(head):
            continue  # a glob above the last component: not judged (named residual)
        if _ho_cd_operand_hit(head, "", cache):
            return True  # the contents of a protected dir
        if recursive:
            for nm in _HO_DESTROY_NAMES:
                if _ho_glob_match(base, nm):
                    p = os.path.join(head, nm)
                    if _ho_cd_operand_hit(p, "", cache) or _ho_ancestor_hit(p, cache):
                        return True
        if any(_ho_glob_match(base, f) for f in _HO_ROOT_FILES) \
                and _ho_cd_operand_hit(os.path.join(head, "ledger"), "", cache):
            return True  # a protected top-level file of a checkout root
    return False


def _ho_find_dynamic_prefix_hit(op: str, cwd: str, cache: dict) -> bool:
    """T-5326 r4: a `find` start point holding `$`/a backtick (`<root>$X`): unquoted, the
    value word-splits into expression words (X=' -o'), so the start point can be just the
    literal text before it. That prefix is judged by the ancestor/glob rules. A `$(..)`/backtick
    word (`_HoDynWord`) holds only its literal text, which is judged the same way."""
    dyn = isinstance(op, _HoDynWord)
    if not dyn and "$" not in op and "`" not in op:
        return False
    prefix = re.split(r"[$`]", str(op), maxsplit=1)[0]
    return bool(prefix) and _ho_destroy_operand_hit(prefix, cwd, cache, True)


def _ho_rm_recursive(words: list[str]) -> bool:
    """`rm` removes directories: -r/-R/-d (clustered), or --recursive/--dir (any unique
    prefix: rm has no other long option starting with r or d), before a `--`."""
    for w in words[1:]:
        if w == "--":
            return False
        if w[:2] == "--":
            name = w[2:].partition("=")[0]
            if name and ("recursive".startswith(name) or "dir".startswith(name)):
                return True
        elif w[:1] == "-" and any(c in "rRd" for c in w[1:]):
            return True
    return False


def _ho_destroy_hit(verb: str, words: list[str], cwd: str, cache: dict) -> bool:
    """T-5326: the destroy operands of rm/rmdir/mv/`find -delete` against the ancestor and
    glob rules (`_ho_destroy_operand_hit`)."""
    if verb == "find":
        recursive = not _ho_find_name_filtered(words)
        return any(_ho_destroy_operand_hit(o, cwd, cache, recursive)
                   or (recursive and _ho_find_dynamic_prefix_hit(o, cwd, cache))
                   for o in _ho_find_start_points(words))
    ops, tvals, _dflag, _unknown = _ho_operands(verb, words)
    if verb == "mv":
        ops = ops if tvals else ops[:-1]  # the sources; the destination is not destroyed
        recursive = True
    else:
        recursive = verb == "rmdir" or _ho_rm_recursive(words)
    return any(_ho_destroy_operand_hit(o, cwd, cache, recursive) for o in ops)


def _ho_dynamic_chdir_destroy_hit(words: list[str], cache: dict, any_destroy: bool = False) -> bool:
    """T-5326: `env -C <dynamic> VERB OPS`: the directory is unknown, so a relative destroy
    operand that names a protected dir or an ancestor of one (or is `.`/climbs with `..`)
    counts, as a dynamic `git -C` does in `_ho_git_dir_hit` (T-4497). r2: an absolute or `~`
    operand does not depend on the directory: it is judged by the ancestor/glob rules
    (`_ho_destroy_operand_hit`). `any_destroy` (the env -S peel hit its cap): every destroy
    counts."""
    verb = words[0].rsplit("/", 1)[-1] if words else ""
    if verb == "find":
        if "-delete" not in words:
            return False
        ops = _ho_find_start_points(words)
        recursive = not _ho_find_name_filtered(words)
    elif verb in ("rm", "rmdir", "mv"):
        ops, tvals, _d, _u = _ho_operands(verb, words)
        if verb == "mv":
            ops = ops if tvals else ops[:-1]
        recursive = verb != "rm" or _ho_rm_recursive(words)
    else:
        return False
    if any_destroy:
        return True
    for op in ops:
        if not op:
            continue
        if os.path.isabs(op) or op[:1] == "~":
            if _ho_destroy_operand_hit(op, "", cache, recursive):
                return True
            if verb == "find" and recursive and _ho_find_dynamic_prefix_hit(op, "", cache):
                return True
            continue
        for a in (_ho_brace_alts(op) if "{" in op else [op]):
            if os.path.isabs(a) or a[:1] == "~":
                # r4: a brace alternative can be absolute (`{<root>,-delete,-o}`)
                if _ho_destroy_operand_hit(a, "", cache, recursive):
                    return True
                continue
            parts = [p for p in a.split("/") if p and p != "."]
            if not parts or ".." in parts or _HO_FB_ROOTREL_HINT.search(a.rstrip("/") + "/"):
                return True
            if any(_ho_glob_match(parts[0], nm) for nm in _HO_DESTROY_NAMES + _HO_ROOT_FILES):
                return True
    return False


def _ho_chdir_resolve(cur: str | None, chdirs: list) -> str | None:
    """The directory after `env -C` values `chdirs`, from `cur`; None when unknown."""
    for v in chdirs:
        if cur is None:
            return None
        new = _ho_dir_join(cur or None, v)
        cur = new if new and os.path.isabs(new) else None
    return cur


def _ho_cd_segment_hit(words: list[str], redirect: bool, cwd: str, moved: bool, cache: dict,
                       destroy: bool = True) -> bool:
    """One non-cd segment against the tracked cwd (see the T-4578 comment above). `destroy`
    False: without the T-5326 ancestor/glob rules (an `env -C` command read against the cwd)."""
    verb = words[0].rsplit("/", 1)[-1]
    if verb == "git":
        got = _ho_git_write_hit(words, cwd, cache)
        if got is not None:
            return got
    elif verb == "find" and "-delete" in words:
        # T-5325: `find <protected> -delete` removes the tree (a bare-dir operand, like `rm -r`).
        # T-5326: start points anywhere (bfs), an ancestor of a protected dir, a glob.
        if any(_ho_cd_operand_hit(o, cwd, cache) for o in _ho_find_start_points(words)):
            return True
        if destroy and _ho_destroy_hit(verb, words, cwd, cache):
            return True
    elif verb in _HO_DEST_VERBS or verb in _HO_ALL_OPERAND_VERBS:
        if destroy and verb in ("rm", "rmdir", "mv") and _ho_destroy_hit(verb, words, cwd, cache):
            return True  # T-5326: an ancestor of a protected dir, or a glob/brace operand
        ops, tvals, dflag, unknown = _ho_operands(verb, words)
        if verb not in _HO_DEST_VERBS or (verb == "install" and dflag):
            dests = ops + tvals
        elif unknown:
            dests = tvals + ops[1:]  # an option the table lacks may take a value: over-deny
        else:
            dests = tvals or ops[-1:]
        if any(_ho_cd_operand_hit(o, cwd, cache) for o in dests):
            return True
        if not (moved and redirect):
            return False
    if not moved:
        return False  # the payload-cwd rules above already read this segment
    if not redirect and (_ho_seg_reads(words) or _ho_fb_read_shaped(words)):
        return False
    return any(w and w[0] != "-" and _ho_cd_operand_hit(w, cwd, cache) for w in _drop_commit_message(words))


def _ho_without_redirect_targets(words: list[str], redirect_targets: list[str]) -> list[str]:
    """Command words with shell redirect target words removed for operand classifiers."""
    out = list(words)
    for target in redirect_targets:
        for i in range(len(out) - 1, -1, -1):
            if out[i] is target or out[i] == target:
                del out[i]
                break
    return out


def _ho_cd_segment_dynamic_write_hit(words: list[str], redirect_targets: list[str], cwd: str,
                                     moved: bool, cache: dict) -> bool:
    """True if a write-class segment has a dynamic target that may reach a protected path."""
    del moved
    if any(_ho_dynamic_write_target_hit(t, cwd, cache) for t in redirect_targets):
        return True
    if not words:
        return False
    words = _ho_without_redirect_targets(words, redirect_targets)
    if not words:
        return False
    words = _ho_t4785_wrapper_words(words)
    if not words:
        return False
    verb = words[0].rsplit("/", 1)[-1]
    if verb == "git":
        got = _ho_dynamic_git_write_hit(words, cwd, cache)
        return bool(got)
    if verb == "sed":
        return any(_ho_dynamic_write_target_hit(o, cwd, cache) for o in _ho_sed_inplace_write_operands(words))
    if verb == "cp" or verb in ("mv", "tee"):
        ops, tvals, dflag, unknown = _ho_operands(verb, words)
        if verb in ("mv", "tee"):
            dests = ops + tvals
        elif unknown:
            dests = tvals + ops[1:]
        else:
            dests = tvals if tvals else (ops[-1:] if len(ops) >= 2 else [])
        return any(_ho_dynamic_write_target_hit(o, cwd, cache) for o in dests)
    return False


def _ho_cd_dir_hit(command: str, cwd: str) -> bool:
    """T-4578/T-4785: True if `command` writes a protected dir/doc through a bare
    directory operand, through a relative operand after a `cd` chain, or through an
    unresolved target on a named dynamic write surface. Fail-open (False)."""
    try:
        if not _ho_cd_gate(command, cwd):
            return False
        segs, _unreadable = _ho_dequoted_segments_rich(command)
        cur: str | None = cwd or ""
        moved = False
        cache: dict = {}
        if _ho_code_string_dynamic_write_hit(command, cur):
            return True
        for raw, redirect, redirect_targets in segs:
            words = _ho_peel_lead(raw)
            if not words:
                continue
            verb = words[0]
            if verb == "popd":
                cur = None
                continue
            if verb == "pushd" and "-n" in words[1:]:
                continue  # `pushd -n D` does not change the directory
            if verb in ("cd", "pushd"):
                args = [w for w in words[1:] if w not in ("-L", "-P", "--", "-e", "-@")]
                t = args[0] if len(args) == 1 else ""
                if not t or t == "-" or isinstance(t, _HoDynWord) or _HO_DYN_PATH.search(t) \
                        or (verb == "pushd" and _HO_PUSHD_ROTATE.match(t)):
                    cur = None
                    continue
                new = _ho_dir_join(cur or None, t)
                cur = new if new and os.path.isabs(new) else None
                moved = True
                continue
            if cur is None:
                continue
            if _ho_cd_segment_dynamic_write_hit(words, redirect_targets, cur, moved, cache):
                return True
            if _ho_cd_segment_hit(words, redirect, cur, moved, cache):
                return True
            # T-5325: and the command the wrappers run (`nice rm -rf <protected>`). Judged in
            # addition, never instead: after a `cd`, a wrapper word is not read-only, so the
            # unpeeled segment is judged by every word, and that denial stays.
            # T-5326: from the segment as written (a `time` keyword's options are peeled
            # here), and against an `env -C DIR`'s directory.
            # The cwd reading stays (adds denials only); the -C reading is judged in addition.
            chdirs: list = []
            inner = _ho_static_command_words(raw, chdirs)
            if inner and inner != words and _ho_cd_segment_hit(inner, redirect, cur, moved, cache,
                                                               destroy=not chdirs):
                return True
            if inner and chdirs:
                icur = _ho_chdir_resolve(cur, chdirs)
                if icur is None:
                    if _ho_dynamic_chdir_destroy_hit(inner, cache, any(c is _HO_CHDIR_CAPPED for c in chdirs)):
                        return True
                elif _ho_cd_segment_hit(inner, redirect, icur, moved, cache):
                    return True
        return False
    except Exception:
        return False


# --- T-4397 part B: code that runs as, or before, this guard ---
# A write to a Python startup-hook path is a WHOLE-guard bypass (T-1345): the
# interpreter runs a `usercustomize`/`sitecustomize` module, or any `.pth` file
# in a site dir, at startup, BEFORE the shim, on every later hook call. So
# can a module planted in the RUNNING guard's own directory: the shim puts it
# first on sys.path, so a `json.py`/`re.py` there shadows the stdlib, and
# `worker_guard_impl.py` or `__pycache__` there is the guard itself. Half A of
# T-4397 runs the hook with `python3 -S`/`-I`; this is defence in depth for a
# seat wired without it. Protected, for any subagent, via Write/Edit/
# NotebookEdit/apply_patch AND the Bash write surface:
#   - the user site: a `.local` segment ending the path, `.local/lib` ending it,
#     or `.local/lib/python*/...` (any X.Y; `~`, `$HOME`, absolute, relative).
#     `~/.local/bin` and `~/.local/share` stay open;
#   - the system site: `/usr[/local]/lib/python*/(site|dist)-packages/...`;
#   - any `usercustomize`/`sitecustomize` module, anywhere (`.py`, `.pyc`, `.so`,
#     or a package dir): a fabricated one in scratch is the attack shape;
#   - `_HOOK_DIR`, the running guard's directory, and everything under it.
#     A worker's OWN worktree copy of tools/hooks is a different path, so
#     editing it stays allowed;
#   - in a writing Bash command: a site dir computed at run time
#     (`$(python3 -m site --user-site)`, `site.getusersitepackages()`, ...),
#     and pip installs outside a venv (`--user`, `--break-system-packages`,
#     `PIP_USER`, `PIP_BREAK_SYSTEM_PACKAGES`; PEP 668 already refuses a bare
#     system `pip install`).
# A virtualenv's `<venv>/lib/pythonX.Y/site-packages` is NOT protected: the hook
# never runs a venv interpreter, and venv installs are routine worker work.
# The pattern set is stable (not computed from `site.*`): identical under -S/-I
# and on every account.
# The Bash side lexes the command into SIMPLE commands (`_PyLexer`: quotes,
# `$'..'`, comments, separators, redirect operators, heredocs, arithmetic,
# `$(...)`/backticks, and `bash -c`/`eval` strings) and judges each on its own,
# so a read-only first line cannot carry a writing second (the T-4400 shape
# does not apply here):
#   - every write-redirect target is judged;
#   - a read-only command (grep/cat/diff/sha256sum/git show|diff|log/find
#     without a writing primary, report-only linters) is judged on nothing
#     else, so reviewers can read and copy out the live guard; sed/awk only on
#     their SCRIPT text (`w FILE`, `print > "FILE"`, `-v`), and not read-only at
#     all with an in-place flag or a script file; cp only on its destination
#     (every `-t`, clustered or abbreviated, else the last operand);
#   - any other command: every word (split at `= : , ( )` and quotes), plus the
#     computed-site-dir and pip-outside-a-venv spellings.
# A word is protected if it names a site path or customize module as written,
# or if, resolved against the command's candidate cwds (`_py_walk`; `$PWD`,
# `${PWD}` and `$(pwd)` read as those), it lands in `_HOOK_DIR` or a site path;
# a word starting with a short-option cluster (`mv -vtDIR`, `sort -oFILE`) is
# also judged from after each option letter.
# RESIDUALS (named, not closed; half A is the real fix): glob- or variable-built
# spellings (`~/.loca?/lib`, `F=.lo; "$F"cal`, `D=PC; cd $D`); `HOME=` then a
# bare `cd` (the narrowing refuses a text with `HOME=`); a dynamic chdir's own
# target (`cd "$D"`: it only stops the narrowing); an inherited CDPATH;
# sed's `e` command / `s///e` and `sed -f FILE`; `less -o`; a
# path only in a patch file's or a script file's content (`source`, `git apply`);
# a symlink planted elsewhere pointing into a protected dir is caught for tool
# writes (realpath) but not for Bash (textual); renaming an ANCESTOR of
# `_HOOK_DIR`; git checkout/pull/reset/apply run in the live checkout without
# naming a path; a `case` pattern's `)` inside a `( )` group; a non-default
# PYTHONUSERBASE in the hook's own environment.
# KNOWN OVER-BLOCKS (fail closed): running the live guard or pytest from the
# live checkout (both write `__pycache__` there); a relative write after any
# cd but a leading literal one (`ls; cd W; cp x tools/hooks/y`, `if cd W`), or
# after a dynamic one (the deny then says so and suggests a literal
# `cd /abs/worktree && ...`); multi-cd builds with `..` (`cd src && make &&
# cd .. && cd docs ...`: the candidate set overflows); a sed/awk script run with its cwd inside
# `_HOOK_DIR`; running text that mentions these paths in a writing command
# (a `--note`, a `python3 -c`); an unterminated quote or heredoc whose rest
# names a protected path; an arithmetic `((`/`$((` holding `;`/`&`/`|`/a
# newline or not closed by `))` (bash may re-read it as subshells), up to the
# end of the text.
_HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
_HOOK_DIR_LEXICAL = os.path.dirname(os.path.abspath(__file__))
_PY_WORD_SPLIT = re.compile(r"[\s'\"=:;&|()<>`,]+")
_PY_SITE_QUERY = re.compile(
    r"\b(?:getusersitepackages|getsitepackages|getuserbase|USER_SITE|USER_BASE"
    r"|PYTHONUSERBASE|purelib|platlib|PIP_USER|PIP_BREAK_SYSTEM_PACKAGES)\b"
    r"|--user-(?:site|base)\b"
    r"|break-system-packages"
)
_PIP_WORD = re.compile(r"(?<![\w.-])pip[\d.]*(?![\w-])|\beasy_install\b|\bsetup\.py\b")
_PIP_INSTALL = re.compile(r"(?<![\w-])install(?![\w-])")
# `--user`, or an optparse prefix of `--break-system-packages` (pip accepts one).
_PIP_OUTSIDE_VENV_FLAG = re.compile(r"(?:^|\s)--(?:user|br[a-z-]*)(?=[\s=]|$)")
_CUSTOMIZE_PREFIXES = ("usercustomize", "sitecustomize")


def _py_startup_segment_kind(word: str) -> bool:
    """True if path-like `word` names a user/system site dir or a customize
    module. `word` is normalized first (`//`, `/./`, `..` folded)."""
    if ".local" not in word and "customize" not in word and "-packages" not in word:
        return False  # fast path: none of the three shapes can match
    if "/" in word:
        word = os.path.normpath(word)
    segs = word.split("/")
    n = len(segs)
    for i, seg in enumerate(segs):
        if seg == ".local":
            if i + 1 == n or (segs[i + 1] == "lib" and (i + 2 == n or segs[i + 2].startswith("python"))):
                return True
        elif seg.startswith(_CUSTOMIZE_PREFIXES) and seg[13:14] in ("", "."):
            return True
        elif (
            seg in ("site-packages", "dist-packages") and i >= 2
            and segs[i - 1].startswith("python") and segs[i - 2] == "lib"
            and segs[: i - 2] in (["", "usr"], ["", "usr", "local"])
        ):
            return True
    return False


def _under_hook_dir(abspath: str) -> bool:
    for d in (_HOOK_DIR, _HOOK_DIR_LEXICAL):
        if abspath == d or abspath.startswith(d + "/"):
            return True
    return False


def _abs_word(word: str, cwd: str) -> str | None:
    """`word` as a normalized absolute path: `~`/`$HOME`/`${HOME}` expanded,
    a relative word joined to `cwd`. None when it can't be anchored."""
    if word.startswith("~"):
        word = os.path.expanduser(word)
    else:
        for var in ("${HOME}", "$HOME"):
            if word == var or word.startswith(var + "/"):
                word = os.path.expanduser("~") + word[len(var):]
                break
    if os.path.isabs(word):
        return os.path.normpath(word)
    if cwd and os.path.isabs(cwd):
        return os.path.normpath(os.path.join(cwd, word))
    return None


# --- The Bash side: a small shell lexer into simple commands (r2) ---
# TODO(T-4397 follow-up): `_PyLexer` duplicates what `_scan` (the escalation
# walk's lexer) models. Unify it onto `_scan` once T-4413 and T-4389 part B(1)
# (the heredoc spans) land there, so there is one shell lexer to review.
#
# One forward pass with an explicit frame stack (no recursion, so no depth
# limit can fail OPEN): command frames (the top level, `$(...)`, backticks),
# double quotes, arithmetic (`$(( ))`, `(( ))`, `$[ ]`: a `<<` there is a
# shift, never a heredoc) and heredoc bodies. It models: quotes (`'..'`,
# `".."`, `$'..'`), backslashes and `\`+newline, comments, separators (`;`,
# `&`, `|`, newline, parens), redirect operators with their target word,
# heredocs (`<<` / `<<-`, quoted or not, several per line: the body lines up
# to the delimiter line are skipped; in an UNQUOTED body `$(...)` and
# backticks still run, so they are lexed), `${...}` (whose `<<`, `;`, `|` are
# literal), and command substitutions. What it
# cannot finish (an unterminated quote, substitution or heredoc, or frames
# nested past `_PY_MAX_FRAMES`) is kept as a fail-closed tail: denied if any
# protected path appears in it.
_PY_PLAIN_RUN = re.compile(r"[^\s;&|()<>`'\"\\$#]+")
_PY_DQ_RUN = re.compile(r"[^\"\\$`]+")
_PY_HD_RUN = re.compile(r"[^\\$`]+")
_PY_ARITH_RUN = re.compile(r"[^()\[\]$`;&|\n]+")
_PY_PE_RUN = re.compile(r"[^}'\"\\$`]+")
_PY_REDIR_OP = re.compile(r"<<<|<<-|<<|<>|<&|<|>>|>&|>\||>|&>>|&>")
_PY_FD = re.compile(r"\d+|-")
_PY_STDOUT_PATHS = frozenset({"/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"})  # T-5571
# T-5430: bash's named fd, `{fd}>f`: a whole unquoted word right before a
# redirect operator is an io-number, like `2>f` (`{1}>f`, `a{fd}>f` are words)
_PY_NAMED_FD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_PY_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_PY_RESERVED = frozenset({
    "{", "}", "then", "do", "else", "fi", "done", "!", "time", "if", "while", "until",
    "elif", "coproc", "esac", "in",
})
_PY_CODE_SHELLS = _SHELL_NAMES  # T-5332: rbash, hush, static-sh too
_PY_SUBST = "\0"  # stands in for a `$(...)` result inside a word: output, not literal text
_PY_PWD_MARK = "\1"  # stands in for a `$(pwd)` / `` `pwd` `` result: the cwd
_PY_MAX_FRAMES = 64
_PY_EMPTY_PARENS = re.compile(r"(?:\s|\\\n)*")  # `( )`, `(\<newline>)`: a definition's parens
# `$HOME`/`$PWD` (word-bounded) or exactly `${HOME}`/`${PWD}`: read as a known path.
_PY_STATIC_VAR = re.compile(r"\$(?:HOME|PWD)(?![A-Za-z0-9_])|\$\{(?:HOME|PWD)\}")
_PY_CMD, _PY_DQF, _PY_ARITHF, _PY_HDF, _PY_PEF = range(5)


_PY_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                   "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_PY_ANSI_NUM = re.compile(r"[0-7]{1,3}|x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}")


def _py_ansi_c(body: str) -> str | None:
    """The value of a `$'...'` body, decoded as bash 5.2 does (r6; checked with
    printf in bash): `\\n`-style escapes, `\\NNN` octal (1-3 digits), `\\xHH`,
    `\\uHHHH`, `\\UHHHHHHHH`, `\\cX` (a control char), an unknown `\\q` kept
    as-is, and the value cut at a NUL. None when the result can't be carried
    in a word (it holds the lexer's marker chars, or a code point past
    U+10FFFF): the caller reads that as unknown text."""
    if "\\" not in body:
        return body
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        e = body[i + 1]
        if e in _PY_ANSI_SIMPLE:
            out.append(_PY_ANSI_SIMPLE[e])
            i += 2
            continue
        if e == "c":
            # `\cX` works on bytes (a non-ASCII X keeps its tail bytes) and
            # `\c\\` is one unit: not modelled, so unknown text (r7).
            return None
        m = _PY_ANSI_NUM.match(body, i + 1)
        if m:
            g = m.group(0)
            cp = int(g, 8) if g[0] in "01234567" else int(g[1:], 16)
            if cp > 0x10FFFF:
                return None
            out.append(chr(cp & 0xFF) if g[0] in "01234567" else chr(cp))
            i = m.end()
            continue
        out.append("\\" + e)  # bash keeps an unknown escape as written
        i += 2
    val = "".join(out).split("\0", 1)[0]  # bash's value ends at a NUL
    if _PY_SUBST in val or _PY_PWD_MARK in val:
        return None
    return val


def _heredoc_line_spans(src: str, pos: int, delims, join: bool):
    """The bodies of the heredocs pending on one line, whose text starts at
    `src[pos]` (just past that line's newline), found as bash finds them: by
    lines, before anything in a body is expanded. `delims`: (delimiter,
    quoted, strip) per operator, in order. Each body runs to the first line
    equal to its delimiter (leading tabs stripped for `<<-`), and the next
    body starts after that line.

    Returns (spans, open_at). spans: (start, end, resume, expands) per body,
    with [start, end) the body, [end, resume) its delimiter line and
    `expands` an unquoted delimiter. open_at: where the body that reached the
    end of `src` with no delimiter line starts (bash takes the rest of the
    input as its body), else None.

    `join` (T-4389 B(1), for `_scan`): in an UNQUOTED body bash joins a line
    ending in an odd run of backslashes to the next before it compares the
    delimiter (bash 5.2: `ab\\<nl>E<nl>E` ends at the second `E`; only the
    first physical line's tabs are stripped). `_PyLexer` passes False and
    keeps its line-by-line reading (T-4438 to reconcile). Each character of
    `src` is read a bounded number of times: linear."""
    n = len(src)
    spans: list[tuple[int, int, int, bool]] = []
    for delim, quoted, strip in delims:
        j = pos
        cap = len(delim) + 1  # a longer logical line is not the delimiter
        while True:
            k = src.find("\n", j)
            end = n if k < 0 else k
            logical = src[j:end]
            if strip:
                logical = logical.lstrip("\t")
            if join and not quoted:
                # Bash pairs backslashes afresh on each physical line (the
                # removed `\`+newline leaves no escape pending), so each
                # line's own trailing run decides; only `cap` chars are kept.
                piece = src[j:end]
                while k >= 0 and (len(piece) - len(piece.rstrip("\\"))) % 2 == 1:
                    k2 = src.find("\n", k + 1)
                    end2 = n if k2 < 0 else k2
                    piece = src[k + 1:end2]
                    logical = logical[:-1][:cap] + piece[:cap]
                    k, end = k2, end2
            if logical == delim:
                spans.append((pos, j, end + 1, not quoted))
                pos = end + 1
                break
            if k < 0:  # no closing delimiter: the body runs to the end
                spans.append((pos, n, n, not quoted))
                return spans, pos
            j = k + 1
        if pos >= n:
            break
    return spans, None


class _PyCmd:
    """One simple command: dequoted `words`, write-redirect `targets`,
    `badredir` (any redirect other than to /dev/null or an fd), and `dyn`:
    the indexes of the words built at run time (an unquoted or double-quoted
    expansion other than a word-bounded `$HOME`/`$PWD`, `${HOME}`/`${PWD}`).
    T-5332: `herestrs` (each `<<<` word; None if built at run time) and `heredocs`
    (each heredoc's raw body text, whether it expands, whether `<<-`) are recorded
    for the stdin-fed-shell reading (`_stdin_fed_codes`); no other reader uses them.
    T-5505: `infiles` (each stdin `<`/`0<` redirect's word; None if built at run time) is
    recorded for the script-file reading (`_sf_collect`) only."""

    __slots__ = ("words", "targets", "badredir", "dyn", "argv", "herestrs", "heredocs", "infiles",
                 "errout", "stdindup", "qwords")

    def __init__(self):
        self.words: list[str] = []
        self.targets: list[str] = []
        self.badredir = False
        self.dyn: set[int] = set()
        self.argv = None
        self.herestrs: list = []
        self.heredocs: list = []
        self.infiles: list = []
        # T-5571 (read by `_decide_noncomputable` only): stderr reaches stdout (`|&` after it,
        # `2>&1`, `2>/dev/stdout`, `&>/dev/stdout`), and a `<&N`/`0<&N` stdin duplication
        self.errout = False
        self.stdindup = False
        # T-5662: the indexes of the words that had a quote character (`"$x"`, not `$x`),
        # read by the run-time command-word reading (`_sf_dynhead`) only
        self.qwords: set[int] | None = None  # (made on the first quoted word)


class _PyFrame:
    __slots__ = ("kind", "cmd", "parts", "started", "quoted", "pending", "hdocs", "groups",
                 "closer", "outer", "first_cmd", "depth", "end", "resume", "lex", "start", "dyn",
                 "open_at", "hdoc_cmds", "rfd", "wfd")

    def __init__(self, kind, start):
        self.kind = kind
        self.start = start
        self.cmd = None
        self.parts: list[str] = []
        self.started = False
        self.quoted = False
        self.dyn = False
        self.pending = None
        self.hdocs: list = []
        self.groups = 0
        self.closer = None  # ")" for $(...), "`" for backticks, None at the top level
        self.outer = None   # the frame whose word receives this substitution's value
        self.first_cmd = 0
        self.depth = 0
        self.end = 0
        self.resume = 0
        self.lex = True
        self.open_at = -1  # where this frame's last grouping `(` was (T-4444)
        self.hdoc_cmds: list = []  # T-5332: the command owning each `hdocs` entry
        self.rfd = None  # T-5505: the fd word of a pending `<` redirect (None: stdin)
        self.wfd = None  # T-5571: (operator, fd word) of a pending `>`/`>&`/`<>`/`&>` redirect


class _PyLexer:
    def __init__(self, src: str):
        self.src = src
        self.n = len(src)
        self.cmds: list[_PyCmd] = []
        self.events: list = []  # the top level: _PyCmd objects and separator strings, in order
        self.tail_start: int | None = None
        # T-4444: a function definition (`NAME ( )`, the parens holding only
        # blanks and `\`+newline) in any command frame; `_py_lexed_defs`.
        self.defs = False
        self.code_tails: list[str] | None = None  # set by `_py_expand_code`: code strings' tails too
        # T-5140 r3: a push refused at `_PY_MAX_FRAMES`; the rest of the text
        # was never lexed, so its words (and code strings) are unknown.
        self.frame_limited = False
        top = _PyFrame(_PY_CMD, 0)
        top.cmd = _PyCmd()
        self.frames = [top]

    def _tail(self, start: int):
        # Every unfinished construct runs to the end of the text, so the
        # fail-closed tail is one suffix: keep the earliest start only.
        if self.tail_start is None or start < self.tail_start:
            self.tail_start = start

    @property
    def tails(self) -> list[str]:
        return [] if self.tail_start is None else [self.src[self.tail_start:]]

    # -- words and commands --
    def _cmd_frame(self):
        for f in reversed(self.frames):
            if f.kind == _PY_CMD:
                return f
        return self.frames[0]

    def _end_word(self, f):
        if not f.started:
            return
        w = "".join(f.parts)
        p = f.pending
        if p in ("w", "d"):
            # T-5571 (recorded only): `<>` on stdin reads it too; stderr sent to stdout
            op, lfd = f.wfd or (None, None)
            f.wfd = None
            if op == "<>" and lfd in (None, "0"):
                f.cmd.infiles.append(None if f.dyn or _PY_SUBST in w or _PY_PWD_MARK in w else w)
            if (p == "d" and lfd == "2" and w == "1") or (
                    w in _PY_STDOUT_PATHS and (lfd == "2" or op in ("&>", "&>>"))):
                f.cmd.errout = True
        if p == "w" or (p == "d" and not _PY_FD.fullmatch(w)):
            if w != "/dev/null":
                f.cmd.targets.append(w)
                f.cmd.badredir = True
        elif p == "r" or p == "s":
            if w != "/dev/null":
                f.cmd.badredir = True
            if p == "s":  # T-5332: a here-string's word, recorded only
                f.cmd.herestrs.append(None if f.dyn or _PY_SUBST in w or _PY_PWD_MARK in w else w)
            elif f.rfd in (None, "0"):  # T-5505: a stdin `<` redirect's word, recorded only
                f.cmd.infiles.append(None if f.dyn or _PY_SUBST in w or _PY_PWD_MARK in w else w)
            elif f.rfd in ("&", "&0") and w not in ("-", "0"):
                f.cmd.stdindup = True  # T-5571: `<&N` / `0<&N`, recorded only
            f.rfd = None
        elif p and p[0] == "h":
            f.hdocs.append((w, f.quoted, p == "h-"))
            f.hdoc_cmds.append(f.cmd)
            f.cmd.badredir = True
        elif p is None:
            if f.dyn:
                f.cmd.dyn.add(len(f.cmd.words))
            if f.quoted:
                if f.cmd.qwords is None:
                    f.cmd.qwords = set()
                f.cmd.qwords.add(len(f.cmd.words))
            f.cmd.words.append(w)
        f.pending = None
        f.parts, f.started, f.quoted, f.dyn = [], False, False, False

    def _end_cmd(self, f, sep):
        self._end_word(f)
        f.pending = None
        c = f.cmd
        top = f is self.frames[0]
        if c.words or c.targets:
            self.cmds.append(c)
            if top:
                self.events.append(c)
        if top and sep:
            self.events.append(sep)
        f.cmd = _PyCmd()

    def _push_cmd(self, closer, start, outer):
        if len(self.frames) >= _PY_MAX_FRAMES:
            self.frame_limited = True
            self._tail(start)
            return False
        f = _PyFrame(_PY_CMD, start)
        f.closer = closer
        f.outer = outer
        f.cmd = _PyCmd()
        f.first_cmd = len(self.cmds)
        self.frames.append(f)
        return True

    def _close_cmd(self, f):
        self._end_cmd(f, None)
        self.frames.pop()
        inner = self.cmds[f.first_cmd:]
        mark = _PY_SUBST
        if len(inner) == 1 and inner[0].words and os.path.basename(inner[0].words[0]) == "pwd" \
                and all(a in ("-L", "-P") for a in inner[0].words[1:]):
            mark = _PY_PWD_MARK
        o = f.outer
        o.parts.append(mark)
        o.started = o.dyn = True

    def _push(self, kind, start, **kw):
        if len(self.frames) >= _PY_MAX_FRAMES:
            self.frame_limited = True
            self._tail(start)
            return False
        f = _PyFrame(kind, start)
        for k, v in kw.items():
            setattr(f, k, v)
        self.frames.append(f)
        return True

    # -- the loop --
    def run(self):
        i = 0
        while True:
            if i >= self.n:
                break
            f = self.frames[-1]
            if f.kind == _PY_CMD:
                i = self._cmd_step(f, i)
            elif f.kind == _PY_DQF:
                i = self._dq_step(f, i)
            elif f.kind == _PY_ARITHF:
                i = self._arith_step(f, i)
            elif f.kind == _PY_PEF:
                i = self._pe_step(f, i)
            else:
                i = self._hd_step(f, i)
            if i < 0:  # frame limit hit: the rest is a fail-closed tail
                break
        # Anything still open at the end is unterminated: its text is a tail.
        while len(self.frames) > 1:
            f = self.frames.pop()
            if f.kind != _PY_HDF:
                self._tail(f.start)
            if f.kind == _PY_CMD:
                self._end_cmd(f, None)
        self._end_cmd(self.frames[0], None)
        return self

    def _dollar(self, f, i, owner):
        """`$...` at `i` inside a word of `owner`'s command: returns the new i."""
        s = self.src
        if not s.startswith("$'", i) and not _PY_STATIC_VAR.match(s, i):
            owner.dyn = True  # a value this text does not spell
        if s.startswith("$((", i):
            owner.started = True
            return i + 3 if self._push(_PY_ARITHF, i, depth=2, parts=owner.parts) else -1
        if s.startswith("$(", i):
            return i + 2 if self._push_cmd(")", i, owner) else -1
        if s.startswith("$[", i):
            owner.started = True
            return i + 2 if self._push(_PY_ARITHF, i, depth=-1, parts=owner.parts) else -1
        if s.startswith("${", i):
            owner.parts.append("${")
            owner.started = True
            return i + 2 if self._push(_PY_PEF, i, parts=owner.parts) else -1
        if s.startswith("$'", i) and f.kind == _PY_CMD:
            j = i + 2
            while j < self.n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j >= self.n:
                self._tail(i)
            body = _py_ansi_c(s[i + 2:j])
            if body is None:
                owner.dyn = True
                body = _PY_SUBST  # text this lexer can't spell: unknown (r6)
            owner.parts.append(body)
            owner.started = owner.quoted = True
            return j + 1
        owner.parts.append("$")
        owner.started = True
        return i + 1

    def _cmd_step(self, f, i):
        s = self.src
        c = s[i]
        m = _PY_PLAIN_RUN.match(s, i)
        if m:
            f.parts.append(m.group(0))
            f.started = True
            return m.end()
        if c in " \t\r\f\v":
            self._end_word(f)
            return i + 1
        if c == "\n":
            self._end_cmd(f, "\n")
            if f.hdocs:
                return self._heredoc_bodies(f, i + 1)
            return i + 1
        if c in "<>" or (c == "&" and s.startswith("&>", i)):
            if f.started and not f.quoted and (
                    all(p.isdigit() for p in f.parts)
                    or (not f.dyn and _PY_NAMED_FD.fullmatch("".join(f.parts)))):
                # an fd number, not a word; T-5430: or a named fd, `{fd}>f`
                rfd = "".join(f.parts)
                f.parts, f.started = [], False
            else:
                rfd = None
                self._end_word(f)
            op = _PY_REDIR_OP.match(s, i).group(0)
            if op in ("<<", "<<-"):
                f.pending = "h" if op == "<<" else "h-"
            elif op == "<<<":
                f.pending = "s"  # read exactly as "r" (T-5332 records the word)
            elif op in ("<", "<&"):
                f.pending = "r"
                # T-5505: `<&N` duplicates, reads no file (T-5571: `&` + its fd, recorded)
                f.rfd = rfd if op == "<" else "&" + (rfd or "")
            elif op == ">&":
                f.pending = "d"
                f.wfd = (op, rfd)
            else:
                f.pending = "w"
                f.wfd = (op, rfd)
            return i + len(op)
        if c == "|" and s.startswith("|&", i):
            f.cmd.errout = True  # T-5571: stderr into the pipe too
            self._end_cmd(f, "|")  # `|&` is a pipe (stderr too), not a background `&`
            return i + 2
        if c in ";&|":
            self._end_cmd(f, c)
            return i + 1
        if c == "(":
            if not f.started and s.startswith("((", i):
                f.started = True
                return i + 2 if self._push(_PY_ARITHF, i, depth=2, parts=f.parts) else -1
            self._end_cmd(f, "(")
            f.groups += 1
            f.open_at = i
            return i + 1
        if c == ")":
            if f.closer == ")" and f.groups == 0:
                self._close_cmd(f)
                return i + 1
            if f.open_at >= 0 and _PY_EMPTY_PARENS.fullmatch(s, f.open_at + 1, i):
                self.defs = True
            f.open_at = -1  # each `(`'s span is read once: linear
            self._end_cmd(f, ")")
            f.groups = max(0, f.groups - 1)
            return i + 1
        if c == "`":
            if f.closer == "`":
                self._close_cmd(f)
                return i + 1
            return i + 1 if self._push_cmd("`", i, f) else -1
        if c == "'":
            k = s.find("'", i + 1)
            if k < 0:
                self._tail(i)
                k = self.n
            f.parts.append(s[i + 1:k])
            f.started = f.quoted = True
            return k + 1
        if c == '"':
            f.started = f.quoted = True
            return i + 1 if self._push(_PY_DQF, i, parts=f.parts) else -1
        if c == "\\":
            if i + 1 < self.n and s[i + 1] != "\n":
                f.parts.append(s[i + 1])
                f.started = f.quoted = True
            return i + 2
        if c == "$":
            return self._dollar(f, i, f)
        if c == "#":
            if not f.started:
                k = s.find("\n", i)
                return self.n if k < 0 else k
            f.parts.append("#")
            return i + 1
        f.parts.append(c)
        f.started = True
        return i + 1

    def _dq_step(self, f, i):
        s = self.src
        m = _PY_DQ_RUN.match(s, i)
        if m:
            f.parts.append(m.group(0))
            return m.end()
        c = s[i]
        if c == '"':
            self.frames.pop()
            return i + 1
        if c == "\\":
            if i + 1 < self.n:
                nxt = s[i + 1]
                if nxt != "\n":
                    f.parts.append(nxt if nxt in '$`"\\' else "\\" + nxt)
            return i + 2
        owner = self._cmd_frame()
        if c == "`":
            return i + 1 if self._push_cmd("`", i, owner) else -1
        return self._dollar(f, i, owner)

    def _arith_step(self, f, i):
        s = self.src
        m = _PY_ARITH_RUN.match(s, i)
        if m:
            f.parts.append(m.group(0))
            return m.end()
        c = s[i]
        if c in "&|" and f.depth > 0:
            # T-4444: `(( a && b ))`, `$(( a | b ))`: operators, not a list.
            # Bash reads `((` as arithmetic only when it closes as `))`; a
            # paren that closes it any other way is a tail below.
            f.parts.append(c)
            return i + 1
        if c in ";&|\n":
            # Never arithmetic in practice: bash may re-read `((a); b)` as
            # nested subshells. Fail closed on the rest.
            self._tail(f.start)
            f.parts.append(c)
            return i + 1
        if f.depth > 0:  # `(( ))` / `$(( ))`: parens count
            if c == "(":
                f.depth += 1
            elif c == ")":
                if f.depth == 2 and not s.startswith(")", i + 1):
                    # bash needs `))` here, else it re-reads the text as nested
                    # subshells (`((true); cmd)`): fail closed on the rest.
                    self._tail(f.start)
                f.depth -= 1
                if f.depth == 0:
                    self.frames.pop()
                    return i + 1
        else:  # `$[ ]`: brackets count (depth runs negative)
            if c == "[":
                f.depth -= 1
            elif c == "]":
                f.depth += 1
                if f.depth == 0:
                    self.frames.pop()
                    return i + 1
        if c == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
            return i + 2 if self._push_cmd(")", i, self._cmd_frame()) else -1
        if c == "`":
            return i + 1 if self._push_cmd("`", i, self._cmd_frame()) else -1
        f.parts.append(c)
        return i + 1

    def _pe_step(self, f, i):
        """Inside `${...}`: `;&|()<>` and `<<` are literal; quotes, `\\`,
        `$...` and backticks nest. A bare `{` is not counted (bash may count
        it: then it reads MORE as literal, so this only over-judges)."""
        s = self.src
        m = _PY_PE_RUN.match(s, i)
        if m:
            f.parts.append(m.group(0))
            return m.end()
        c = s[i]
        if c == "}":
            f.parts.append("}")
            self.frames.pop()
            return i + 1
        if c == "'":
            k = s.find("'", i + 1)
            if k < 0:
                self._tail(i)
                k = self.n
            f.parts.append(s[i + 1:k])
            return k + 1
        if c == '"':
            return i + 1 if self._push(_PY_DQF, i, parts=f.parts) else -1
        if c == "\\":
            if i + 1 < self.n and s[i + 1] != "\n":
                f.parts.append(s[i + 1])
            return i + 2
        owner = self._cmd_frame()
        if c == "`":
            return i + 1 if self._push_cmd("`", i, owner) else -1
        return self._dollar(f, i, owner)

    def _hd_step(self, f, i):
        s = self.src
        if i >= f.end or not f.lex:
            self.frames.pop()
            return max(i, f.resume)
        m = _PY_HD_RUN.match(s, i, f.end)
        if m:
            return m.end()
        c = s[i]
        if c == "\\":
            return i + 2
        # A substitution's value lands in this (discarded) frame, not in a word.
        if c == "`":
            return i + 1 if self._push_cmd("`", i, f) else -1
        if s.startswith("$(", i):
            return self._dollar(f, i, f)
        return i + 1

    def _heredoc_bodies(self, f, pos):
        """Skip the bodies of `f`'s pending heredocs, which start at `pos`;
        an unquoted body is lexed for `$(...)`/backticks. Returns the new i."""
        spans, open_at = _heredoc_line_spans(self.src, pos, f.hdocs, join=False)
        if open_at is not None:  # no closing delimiter: fail closed on the rest
            self._tail(open_at)
        for (start, end, _resume, expands), owner, (_w, _q, strip) in zip(spans, f.hdoc_cmds, f.hdocs):
            owner.heredocs.append((self.src[start:end], expands, strip))  # T-5332: recorded only
        f.hdocs = []
        f.hdoc_cmds = []
        for start, end, resume, lex in reversed(spans):
            if not self._push(_PY_HDF, start, end=end, resume=resume, lex=lex):
                return -1
        return spans[0][0] if spans else pos


def _py_lex(src: str) -> _PyLexer:
    return _PyLexer(src).run()


def _py_head_index(words: list[str]) -> int:
    """Index of the command word: past reserved words, `NAME=value`s, and a
    `builtin`/`command` prefix (`builtin cd X` is a cd)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in _PY_RESERVED or _PY_ASSIGN.match(w):
            i += 1
        elif w in ("builtin", "command"):
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "-":
                i += 1  # `command -p cd`: the options, then the command
        else:
            break
    return i


# --- Candidate working directories (order-insensitive, mason's r1 ruling) ---
# A relative operand is resolved against EVERY directory the command could be
# running in: the payload cwd plus every literal chdir target anywhere in the
# text (cd/pushd, also behind `builtin`/`command`; `git -C`, `make -C`/
# `--directory`, `tar -C`/`--directory`, `env -C`/`--chdir`; every `CDPATH=`
# dir), each relative target resolved against every candidate, to a bounded
# depth. Order, subshells, pipes, `&`, `{ }`, if/case/loops and `$(...)` are
# not modelled: a cd anywhere only ADDS candidates, so none of them can hide
# the payload cwd (r4: the r3 sequential walk was attacked through exactly
# those; dropped). `cd -`, `popd`, `pushd` with no dir or `+N`/`-N`, `~-`/
# `~+`/`~N`, `$OLDPWD`, `source`/`.` and any other dynamic target add nothing
# the set lacks (every literal dir is already in it) but stop the narrowing.
# One sound narrowing (`_py_narrow`) drops the payload cwd.
_PY_MAX_CANDS = 32
_PY_CAND_DEPTH = 4
_PY_CD_FLAGS = frozenset({"-L", "-P", "-e", "-@", "--"})
_PY_DEFS = re.compile(r"\(\s*\)|(?<![\w-])(?:function|alias)(?![\w-])")
_PY_CHDIR_EXTRA = frozenset({"git", "make", "tar", "env"})
_PY_STACK_N = re.compile(r"[+-]\d+")
_PY_OPT_CLUSTER = re.compile(r"-[A-Za-z0-9#]+")  # `-4o`, `-#o` too (curl, wget)


def _py_lexed_defs(lex: _PyLexer) -> bool:
    """A function or alias definition may run (T-4444: read from the lexed
    commands, so `()`/`function`/`alias` inside quoted text is data): a
    `NAME ( )` in any command frame or code string, a command whose word is
    `function` or `alias`, or `_PY_DEFS` in the fail-closed tail (unlexed text)."""
    if lex.defs:
        return True
    for c in lex.cmds:
        i = _py_head_index(c.words)
        if i < len(c.words) and os.path.basename(c.words[i]) in ("function", "alias"):
            return True
    return any(_PY_DEFS.search(t) for t in (lex.tails if lex.code_tails is None else lex.code_tails))


class _PyCand:
    """A normalized absolute directory, with what a relative word joined to it
    can reach, precomputed once (so a word costs O(len(word)), not O(len(cwd)))."""

    __slots__ = ("path", "comps", "under", "hrels", "all_prot", "warm", "tail")

    def __init__(self, path: str):
        self.path = path
        self.comps = path.split("/")
        self.under = _under_hook_dir(path)
        prefix = path.rstrip("/") + "/"
        self.hrels = tuple({d[len(prefix):] for d in (_HOOK_DIR, _HOOK_DIR_LEXICAL) if d.startswith(prefix)})
        self.all_prot = _py_startup_segment_kind(path.rstrip("/") + "/x_")
        self.warm = ".local" in path or "customize" in path or "-packages" in path
        if path.startswith("/usr") and len(self.comps) <= 7:
            self.tail = path.rstrip("/")
        else:
            # The site rules only look a few segments back from the join; keep
            # the markers they read, blank everything else (bounded length).
            keep = [c if c in (".local", "lib") or c.startswith("python") else "x" for c in self.comps[-3:]]
            self.tail = "x/" + "/".join(keep)

    def relevant(self) -> bool:
        return self.under or bool(self.hrels) or self.all_prot or self.warm


_PY_STATIC_PREFIXES = ("${HOME}", "${PWD}", "$HOME", "$PWD", _PY_PWD_MARK)


def _py_static_prefix(word: str) -> str:
    """The leading `$HOME`/`${HOME}`/`$PWD`/`${PWD}`/`$(pwd)` of `word` when it
    ends the word or is followed by `/` (r5: `$PWD_SAVE`, `$HOMEX`, `${PWD}X`
    are other variables or values), else ""."""
    if word[:1] not in ("$", _PY_PWD_MARK):
        return ""
    for pre in _PY_STATIC_PREFIXES:
        if word.startswith(pre) and (len(word) == len(pre) or word[len(pre)] == "/"):
            return pre
    return ""


def _py_dynamic(target: str) -> bool:
    """A chdir target whose directory the text does not say: `-`, `~-`,
    `~+`, `~N`, a variable (bar a word-bounded leading $HOME/$PWD), a
    substitution."""
    if target == "-" or "`" in target or _PY_SUBST in target or "{}" in target:
        return True  # `{}`: xargs -I{} / find -exec fill it in at run time (r6)
    if target[:1] == "~" and target[1:2] in ("+", "-") or (target[:1] == "~" and target[1:2].isdigit()):
        return True
    rest = target[len(_py_static_prefix(target)):]
    return "$" in rest or _PY_PWD_MARK in rest


def _py_norm(path: str) -> str:
    """`os.path.normpath`, and a leading `//` read as `/` (POSIX keeps two
    leading slashes; Linux, and bash's cd, reach the same dir: T-4444)."""
    p = os.path.normpath(path)
    return "/" + p.lstrip("/") if p.startswith("//") else p


def _py_literal_alt(word: str, base: str | None) -> str | None:
    """A quoted `~`/`$HOME`/`$PWD` is literal text (`'~/x'`, `'$PWD'/..`) and
    the lexer does not keep quoting: the word read as a plain relative path
    from `base`, when it starts with `~` or `$`."""
    if base and word[:1] in ("~", "$"):
        return os.path.normpath(os.path.join(base, word))
    return None


def _py_resolve(word: str, base: str | None) -> str | None:
    """`word` as a normalized absolute path from directory `base`: `~`/`$HOME`
    expanded, `$PWD`/`$(pwd)` read as `base`. None when it can't be anchored."""
    pre = _py_static_prefix(word)
    if pre in ("$PWD", "${PWD}", _PY_PWD_MARK):
        word = "." + word[len(pre):]
    if word[:1] == "~" or pre in ("$HOME", "${HOME}"):
        a = _abs_word(word, "")
        return a if a and a[:1] == "/" else None
    if word[:1] == "/":
        return _py_norm(word)
    if base:
        return _py_norm(os.path.join(base, word))
    return None


def _py_chdir(words: list[str]) -> list[str]:
    """The directories a command changes to or names as a cwd (`-` for one
    the text does not say; `source`/`.` may run a cd too)."""
    i = _py_head_index(words)
    if i >= len(words):
        return []
    head = words[i]
    args = words[i + 1:]
    if head in ("popd", "source", "."):
        return ["-"]
    if os.path.basename(head) == "find" and any(a in ("-execdir", "-okdir") for a in args):
        return ["-"]  # runs its command in each match's directory: dynamic (r6)
    if head in ("cd", "pushd"):
        ops = [a for a in args if a == "-" or not a.startswith("-")]
        if head == "pushd" and (not ops or any(_PY_STACK_N.fullmatch(a) for a in args)):
            return ["-"]  # swaps or rotates the dir stack: dynamic
        return [ops[0] if ops else os.path.expanduser("~")]  # a bare `cd`: HOME, never a literal `~`
    out = []
    if head == "env":
        out.extend(_py_env_chdirs(args))
    elif head in _PY_CHDIR_EXTRA:
        for j, a in enumerate(args):
            if a == "-C" or (head != "git" and a in ("--directory", "--chdir")):
                if j + 1 < len(args):
                    out.append(args[j + 1])
            elif a.startswith("-C") and len(a) > 2:
                out.append(a[2:])
            elif a.startswith(("--directory=", "--chdir=")):
                out.append(a.split("=", 1)[1])
    return out


_PY_ENV_TOKEN_SPLIT = re.compile(r"[\s=]+")


def _py_env_tok(t: str) -> str:
    """A single-dash cluster glued to a long option (`-S--chdir`, `-iS--c`):
    env reads the -S value `--chdir` as its own option, so read it as that."""
    if t.startswith("-") and not t.startswith("--") and "--" in t:
        return t[t.index("--"):]
    return t


def _py_env_chdirs(args: list[str]) -> list[str]:
    """env's chdir targets, as a SUPERSET (r9): every argument is split into
    tokens at blanks and `=` (an -S string carries more options, and its
    `-C` can take the NEXT argument as its dir: `env -S -C DIR cmd`,
    `env -S '-i -C' DIR cmd`, `--split-string=-C DIR`), and in that one
    token stream any prefix of `--chdir` from `--c` takes the next token,
    and any single-dash token holding `C` takes its rest after the `C`, or
    the next token. No option value is skipped (`--u -C -CDIR`: `--u` is
    --unset), so a misread only adds a candidate."""
    toks = [_py_env_tok(t) for a in args for t in _PY_ENV_TOKEN_SPLIT.split(a) if t]
    out = []
    # r11: an argument (or an -S / --split-string value inside it) whose LAST
    # token is a chdir option still waiting for its value takes the WHOLE next
    # argument, unsplit (`--split-string=-C 'D/k v/..'`, `-S '-i --chdir' D`,
    # `-S--chdir D`).
    for j in range(len(args) - 1):
        parts = [_py_env_tok(t) for t in _PY_ENV_TOKEN_SPLIT.split(args[j]) if t]
        last = parts[-1] if parts else ""
        if (last.startswith("--") and len(last) >= 3 and "--chdir".startswith(last)) or (
                last.startswith("-") and not last.startswith("--") and last.endswith("C")):
            out.append(args[j + 1])
    # The unsplit readings too (r10): a dir may hold a blank or `=`
    # (`env -C 'D/k=v/..'`), so each original argument's rest after its C,
    # the whole next argument, and a `--c...=` option's whole value count.
    for j, a in enumerate(args):
        nxt = args[j + 1] if j + 1 < len(args) else None
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if len(name) >= 3 and "--chdir".startswith(name):
                if eq:
                    out.append(val)
                elif nxt:
                    out.append(nxt)
        elif a.startswith("-") and "C" in a[1:]:
            rest = a[a.index("C", 1) + 1:]
            if rest:
                out.append(rest)
            if nxt:
                out.append(nxt)
    _py_env_chdir_toks(toks, out)
    # T-5360: the same reading with each argument split as GNU env splits an -S string
    # (`_env_s_split`: quotes removed, `\_`, escapes, a `#` comment), a long option's word at
    # its first `=`: `env -S '-C "D/k v/.."'`, `env -S '--chdir\_D'`. A `${VAR}` stays in its
    # word (`_py_dynamic` reads it as a dir the text does not say). Only adds candidates.
    gparts = []
    for a in args:
        split = _env_s_split(a)
        parts = []
        for w in split or ():
            name, eq, val = w.partition("=")
            parts.extend((_py_env_tok(name), val) if eq and w[:2] == "--" else (_py_env_tok(w),))
        gparts.append([p for p in parts if p])
    for j in range(len(args) - 1):
        last = gparts[j][-1] if gparts[j] else ""
        if (last.startswith("--") and len(last) >= 3 and "--chdir".startswith(last)) or (
                last.startswith("-") and not last.startswith("--") and last.endswith("C")):
            out.append(args[j + 1])
    _py_env_chdir_toks([p for parts in gparts for p in parts], out)
    return out


def _py_env_chdir_toks(toks: list[str], out: list[str]) -> None:
    """`_py_env_chdirs`' token-stream reading: any prefix of `--chdir` from `--c` takes the
    next token, any single-dash token holding `C` its rest after the `C`, or the next token."""
    n = len(toks)
    for j, t in enumerate(toks):
        nxt = toks[j + 1] if j + 1 < n else None
        if t.startswith("--"):
            if len(t) >= 3 and "--chdir".startswith(t) and nxt:
                out.append(nxt)
        elif t.startswith("-") and "C" in t[1:]:
            rest = t[t.index("C", 1) + 1:]
            if rest:
                out.append(rest)
            elif nxt:
                out.append(nxt)


def _py_cdpath(cmds: list, start: str | None) -> list[str]:
    """Every directory any `CDPATH=` assignment in the command names (as a
    prefix, a statement or after `export`): a relative cd may land in any."""
    out = []
    for c in cmds:
        for w in c.words:
            if w.startswith("CDPATH="):
                for v in w[7:].split(":"):
                    r = _py_resolve(v, start) if v else None
                    if r:
                        out.append(r)
    return out


def _py_narrow(lex: _PyLexer, start: str, text: str) -> list[str] | None:
    """The directories that replace the payload cwd (the cd's target, plus its
    literal reading for a `~`/`$` operand: `_py_literal_alt`), or None. Only when the
    FIRST command (after blank lines and plain `NAME=value` statements) is a
    top-level `cd DIR` (optionally `builtin`/`command [-p]`), flags only
    `-L -P -e -@ --`, one literal operand, no redirect but /dev/null, with no
    CDPATH/HOME assignment or function/alias definition in the text, no lone
    `&` anywhere after it (a backgrounded and-or list runs its cd in a
    subshell), not piped, and either
      (a) `cd DIR && ...` with no later `;`/newline/`||`/`&` (a failed cd
          skips the rest), or
      (b) DIR is an existing directory this user can enter (os.access X_OK)
          and the cd is followed by `;`, newline, `&&` or `||`."""
    if _py_lexed_defs(lex) or "CDPATH" in text or "HOME=" in text:
        return None
    ev = lex.events
    n = len(ev)
    k = 0
    while k < n:
        e = ev[k]
        if e == "\n":
            k += 1
            continue
        if isinstance(e, _PyCmd) and e.words and not e.badredir and all(
                _PY_ASSIGN.match(w) and _PY_SUBST not in w and _PY_PWD_MARK not in w for w in e.words):
            if ev[k + 1:k + 2] in (["\n"], [";"]):
                k += 2
                continue
            if ev[k + 1:k + 3] == ["&", "&"]:
                k += 3
                continue
        break
    if k >= n or not isinstance(ev[k], _PyCmd):
        return None
    c0 = ev[k]
    w = c0.words
    j = 0
    while j < len(w) and w[j] in ("builtin", "command"):
        j += 1
        if w[j - 1] == "command" and j < len(w) and w[j] == "-p":
            j += 1
    if j >= len(w) or w[j] != "cd" or c0.badredir:
        return None
    args = w[j + 1:]
    ops = [a for a in args if a == "-" or not a.startswith("-")]
    if len(ops) != 1 or any(a not in _PY_CD_FLAGS for a in args if a not in ops) or _py_dynamic(ops[0]):
        return None
    dest = _py_resolve(ops[0], start)
    if not dest:
        return None
    dests = [dest]
    alt = _py_literal_alt(ops[0], start)
    if alt:
        dests.append(alt)
    rest = ev[k + 1:]
    if not rest:
        return dests
    for m, e in enumerate(rest):
        if e in (";", "\n"):
            break  # T-4444: the cd's and-or list has ended; a later `&` is another list's
        if e == "&" and (m == 0 or rest[m - 1] != "&") and (m + 1 == len(rest) or rest[m + 1] != "&"):
            return None
    pair = rest[:2]
    if pair == ["&", "&"] and not any(e in (";", "\n") for e in rest) and not any(
            rest[m] == "|" and rest[m + 1] == "|" for m in range(len(rest) - 1)):
        return dests  # (a)
    if rest[0] in (";", "\n") or pair in (["&", "&"], ["|", "|"]):
        if os.path.isdir(dest) and os.access(dest, os.X_OK):
            return dests  # (b)
    return None


def _py_walk(lex: _PyLexer, cwd: str, text: str):
    """(candidate cwd set, or None to deny on overflow; dynamic_seen). Past
    `_PY_MAX_CANDS` only relevant candidates are kept (in, or a parent of,
    `_HOOK_DIR`, or on a site path); with a `..` in the text any candidate can
    climb to a relevant one, so overflow denies."""
    start = _py_norm(cwd) if cwd and os.path.isabs(cwd) else None
    targets: list[str] = []
    seen: set[str] = set()
    dynamic = False
    for c in lex.cmds:
        for t in _py_chdir(c.words):
            if _py_dynamic(t):
                dynamic = True
            elif t not in seen:
                seen.add(t)
                targets.append(t)
    dests = _py_narrow(lex, start, text) if start and not dynamic else None
    paths = set(dests or [start]) if start else set()
    paths |= set(_py_cdpath(lex.cmds, start))
    frontier = set(paths)
    # A literal reading (`_py_literal_alt`) descends below its base, so it can
    # reach a protected path only through a later `..`, or from a base already
    # in `_HOOK_DIR` or a site path: only then is it added.
    climbs = ".." in text
    for _ in range(_PY_CAND_DEPTH):
        new: set[str] = set()
        for t in targets:
            if t[:1] in ("/", "~") or _py_static_prefix(t) in ("$HOME", "${HOME}"):
                a = _py_resolve(t, None)
                if a:
                    new.add(a)
                if t[:1] == "/":
                    continue
            for base in frontier:
                if t[:1] not in ("~", "$"):
                    r = _py_resolve(t, base)
                    if r:
                        new.add(r)
                elif t[:1] == "$" and _py_static_prefix(t) not in ("$HOME", "${HOME}"):
                    r = _py_resolve(t, base)  # `$PWD/x`: from each candidate
                    if r:
                        new.add(r)
                if climbs or _under_hook_dir(base) or _py_startup_segment_kind(base.rstrip("/") + "/x_"):
                    alt = _py_literal_alt(t, base)
                    if alt:
                        new.add(alt)
        new = {p for p in new if len(p) <= 4096} - paths
        if not new:
            break
        paths |= new
        frontier = new
        if len(paths) > _PY_MAX_CANDS:
            if ".." in text:
                return None, dynamic
            paths = {p for p in paths if _PyCand(p).relevant()}
            frontier &= paths
            if len(paths) > _PY_MAX_CANDS:
                return None, dynamic
    return paths, dynamic


_PY_UPS = re.compile(r"(?:\.\./)+")


def _py_up(cand: _PyCand, k: int, memo: dict) -> _PyCand:
    key = (cand.path, k)
    got = memo.get(key)
    if got is None:
        comps = cand.comps[: max(1, len(cand.comps) - k)]
        got = memo[key] = _PyCand("/".join(comps) or "/")
    return got


def _py_word_hits(piece: str, cands: list, rel: list, memo: dict) -> bool:
    """True if path-like `piece` names a site path / customize module, or, as
    written or resolved against any candidate cwd, lands in one or in `_HOOK_DIR`.
    `rel` is the candidates a plain descent (no `..`) can reach anything from.
    `$PWD`, `${PWD}` and `$(pwd)` read as each candidate cwd."""
    if not piece or piece == _PY_SUBST:
        return False
    if _py_startup_segment_kind(piece):
        return True
    word = piece
    pre = _py_static_prefix(word)
    if pre in ("$PWD", "${PWD}", _PY_PWD_MARK):
        word = "." + word[len(pre):]
    elif word[0] == "~" or pre:
        word = _abs_word(word, "") or word
    if word.startswith("/"):
        p = _py_norm(word)
        if _under_hook_dir(p) or _py_startup_segment_kind(p):
            return True
        if piece[0] not in ("~", "$"):
            return False
    elif _py_rel_hits(word, cands, rel, memo):
        return True
    # A quoted `~`/`$HOME`/`$PWD` is literal text: its relative reading too.
    return piece[0] in ("~", "$") and word != piece and _py_rel_hits(piece, cands, rel, memo)


def _py_rel_hits(word: str, cands: list, rel: list, memo: dict) -> bool:
    """`_py_word_hits` for a relative `word`, against every candidate cwd."""
    r = os.path.normpath(word)
    k = 0
    if r == "..":
        k, r = 1, "."
    else:
        m = _PY_UPS.match(r)
        if m:
            k, r = len(m.group(0)) // 3, r[m.end():]
            if r == "..":
                k, r = k + 1, "."
    site_hint = ".local" in r or "customize" in r or "-packages" in r
    for c in cands if (k or site_hint) else rel:
        b = _py_up(c, k, memo) if k else c
        if b.under or b.all_prot:
            return True
        for hrel in b.hrels:
            if r == hrel or r.startswith(hrel + "/"):
                return True
        if (b.warm or site_hint) and r != "." and _py_startup_segment_kind(b.tail + "/" + r):
            return True
    return False


# Commands that only READ their operands: for them only the redirect targets
# (and a sed/awk script, which can write with `w`/`print >`) are judged, so
# reviewers can inspect the live guard (`git show REV:tools/hooks/x > /tmp/y`).
# `git`/`find` count only in their T-1600 read-only forms; sed/awk not with an
# in-place flag or a script FILE. Linters that only report (shellcheck,
# pyflakes, mypy, `ruff check` without a fix flag, `ruff format --check`) too.
_PY_READ_ONLY_HEADS = _READ_ONLY_VERBS | frozenset({
    "sha256sum", "sha1sum", "md5sum", "stat", "file", "diff", "cmp", "cut", "tr", "nl", "jq",
    "shellcheck", "pyflakes", "mypy",
})
_PY_INPLACE_FLAG = re.compile(r"^(?:-[a-zA-Z]*i|--in-place|inplace$)")
_PY_SED_WRITE = re.compile(r"[wW]")
_PY_AWK_WRITE = re.compile(r"[>|]|system")


def _py_read_only(head: str, args: list[str]):
    """True: reads only (judge nothing but redirects). A list: sed/awk that
    reads its files, the list holding the script texts to judge. None: may write."""
    if head in ("sed", "awk", "gawk", "mawk"):
        if any(_PY_INPLACE_FLAG.match(a) for a in args):
            return None
        scripts: list[str] = []
        first = None
        j = 0
        while j < len(args):
            a = args[j]
            if a in ("-f", "--file") or a.startswith(("--file=", "-f")):
                return None  # the script is in a file this hook can't read
            if a in ("-e", "--expression", "-v", "--assign"):
                if j + 1 < len(args):
                    scripts.append(args[j + 1])
                j += 2
                continue
            if a == "-F" or (head == "sed" and a in ("-l", "--line-length")):
                j += 2
                continue
            if a.startswith(("--expression=", "--assign=")):
                scripts.append(a.split("=", 1)[1])
            elif a.startswith(("-e", "-v")) and not a.startswith("--") and len(a) > 2:
                scripts.append(a[2:])
            elif first is None and (a == "-" or not a.startswith("-")):
                first = a
            j += 1
        if first is not None and (head != "sed" or not scripts):
            scripts.append(first)
        # Only a script that CAN write (sed's `w`/`W`, awk's `>`/`|`/system())
        # has anything to judge; `sed -n 95,352p f` run inside the dir is a read.
        writes = _PY_SED_WRITE if head == "sed" else _PY_AWK_WRITE
        return scripts if any(writes.search(x) for x in scripts) else []
    if head in ("git", "find"):
        return True if _is_read_only_verb([head] + args) else None
    if head == "ruff":
        if args[:1] == ["check"] and not any(a.startswith(("--fix", "--unsafe-fixes")) for a in args):
            return True
        if args[:1] == ["format"] and any(a in ("--check", "--diff") for a in args):
            return True
        return None
    if head.startswith("python") and args[:1] == ["-m"] and args[1:2] in (["pyflakes"], ["mypy"]):
        return True
    return True if head in _PY_READ_ONLY_HEADS else None


# cp's options that take the NEXT word (GNU coreutils 9.4): -S/--suffix,
# -t/--target-directory. Long options match any unambiguous prefix
# (getopt_long): `--t`..`--target-directory`, `--su`..`--suffix`.
def _py_cp_dests(args: list[str]) -> list[str]:
    """cp's destination operands: every `-t DIR` (also clustered, `-rt DIR`,
    `-rtDIR`) or `--target-directory=DIR` (or a prefix of it), else the last
    operand. Its sources are only read."""
    dests, ops = [], []
    j = 0
    n = len(args)
    while j < n:
        a = args[j]
        j += 1
        if a == "--":
            ops.extend(args[j:])
            break
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name and "target-directory".startswith(name):
                if eq:
                    dests.append(val)
                elif j < n:
                    dests.append(args[j])
                    j += 1
            elif len(name) >= 2 and "suffix".startswith(name) and not eq:
                j += 1
            continue
        if a.startswith("-") and a != "-":
            body = a[1:]
            for x, ch in enumerate(body):
                if ch in "tS":
                    rest = body[x + 1:]
                    val = rest if rest else (args[j] if j < n else "")
                    if not rest:
                        j += 1
                    if ch == "t":
                        dests.append(val)
                    break
            continue
        ops.append(a)
    return dests or ops[-1:]


# r5: a code string runs as commands wherever its shell word sits (`timeout
# 60 bash -c CODE`, `env`/`nohup`/`xargs`/`find -exec`/`exec ... sh -c CODE`,
# `bash -c "bash -c '...'"`, `eval "bash -c ..."`): a shell word (any path
# form) with a `-c` option cluster, at ANY word position, runs its first
# operand. Each code string is lexed with the same lexer, its commands join
# the candidate-cwd set and the judgement, to `_PY_CODE_DEPTH` nested levels
# (the escalation walk allows benign nesting that deep: `_CODE_DEPTH_MAX` and
# past it); deeper nesting, re-reading more than `_PY_CODE_BUDGET` times the
# command's length (each level re-reads a part of the one around it), or a
# code string built at run time (`bash -c "$X"`, an `eval` of a
# substitution), fails closed. `eval eval ... WORDS` of plain words (no
# quote, expansion or operator: re-reading them changes nothing) is read once.
_PY_CODE_DEPTH = 16
_PY_CODE_BUDGET = 4
_PY_PLAIN_WORD = re.compile(r"[^\s;&|()<>`'\"\\$#]+")
_PY_POSITIONAL_REF = re.compile(r"\$([0-9]|[@*#?$!-])|\$\{(\d+|[@*#?$!-])\}")
_PY_POSITIONAL_EXACT = re.compile(r"(?:\$([0-9]|[@*#?$!-])|\$\{(\d+|[@*#?$!-])\})")
_PY_SAFE_SPECIAL_PARAMS = frozenset({"#", "?", "$", "!", "-"})


def _py_positional_values(key: str, argv: list | None) -> list:
    """Values for one shell positional/special parameter from visible shell argv."""
    if key in _PY_SAFE_SPECIAL_PARAMS:
        return [""]
    if argv is None:
        return []
    if key in ("@", "*"):
        return list(argv[1:])
    idx = int(key)
    return [argv[idx]] if idx < len(argv) else []


def _py_normal_word_value(word: str, dynamic: bool) -> str:
    return _HoDynWord(word) if dynamic or _py_dynamic(word) else word


def _py_expand_positional_word(word: str, dynamic: bool, argv: list | None,
                               missing_empty: bool = False) -> list:
    """A lexed word with shell positional refs resolved against `argv`.

    Exact `$@`/`$*` expands to the finite visible argv tail. Other refs compose
    within this one word only; this never rewrites or reparses source code.
    """
    if not _PY_POSITIONAL_REF.search(word):
        return [_py_normal_word_value(word, dynamic)]
    exact = _PY_POSITIONAL_EXACT.fullmatch(word)
    if exact:
        key = exact.group(1) or exact.group(2)
        vals = _py_positional_values(key, argv)
        if vals:
            return vals
        return [""] if missing_empty and key not in ("@", "*") else []

    out: list[str] = []
    pos = 0
    dyn = _py_dynamic(_PY_POSITIONAL_REF.sub("", word))
    missing = False
    for m in _PY_POSITIONAL_REF.finditer(word):
        out.append(word[pos:m.start()])
        key = m.group(1) or m.group(2)
        vals = _py_positional_values(key, argv)
        if not vals:
            missing = True
            val = ""
        elif key in ("@", "*"):
            if any(isinstance(v, _HoDynWord) or _py_dynamic(str(v)) for v in vals):
                dyn = True
            val = " ".join(str(v) for v in vals)
        else:
            val = vals[0]
            if isinstance(val, _HoDynWord) or _py_dynamic(str(val)):
                dyn = True
        out.append(str(val))
        pos = m.end()
    out.append(word[pos:])
    checked = "".join(out)
    if missing and not missing_empty and not checked:
        return []
    return [_HoDynWord(checked) if dyn or _py_dynamic(checked) else checked]


def _py_command_argv(c: _PyCmd, shell_word: str, words: list[str], start: int) -> list:
    """Visible argv for `SHELL -c CODE [ARG0 ARG...]`, resolved from parent argv."""
    if start >= len(words):
        return [os.path.basename(shell_word) or "sh"]
    out: list = []
    for idx in range(start, len(words)):
        out.extend(_py_expand_positional_word(words[idx], idx in c.dyn, c.argv, missing_empty=True))
    return out or [""]


def _py_shell_codes(c: _PyCmd, i: int, add, with_argv: bool, keep_dynamic: bool = False) -> bool:
    """`add` each `SHELL [opts] -c... CODE` operand among `c`'s words (`i`: the head index);
    False if one is built at run time (with `keep_dynamic`, T-5140 r3: its lexed static
    text is added instead, and this is never False)."""
    words = c.words
    n = len(words)
    j = 0
    while j < n:
        # a shell word, or a command word built at run time (`"$SHELL" -c CODE`)
        if os.path.basename(words[j]) not in _PY_CODE_SHELLS and not (j == i and j in c.dyn):
            j += 1
            continue
        k = j + 1
        code = False
        while k < n:
            w = words[k]
            if w == "--":
                k += 1
                break
            if len(w) < 2 or w[0] not in "-+":
                break
            if w[1] != "-" and "c" in w:
                code = True  # `-c`, clustered `-xc`, and `+c` (bash runs it too)
            if w in ("--rcfile", "--init-file"):
                k += 1  # `--rcfile FILE`: a value
            elif w[1] != "-":
                # `-o NAME`, `-euo pipefail`, `-oc posix CODE`, `-Oc extglob
                # CODE`: one value word per `o`/`O` in the cluster (r6).
                k += w.count("o") + w.count("O")
            k += 1
        if code and k < n:
            if not keep_dynamic and (k in c.dyn or _PY_SUBST in words[k] or _PY_PWD_MARK in words[k]):
                return False
            add(words[k], _py_command_argv(c, words[j], words, k + 1) if with_argv else None)
        # Resume at the first word past this shell's options: a shell word
        # among them is an option value whose own scan would end at `k` too
        # (and rescanning each would be quadratic: `bash -o bash -o ...`).
        j = max(j + 1, k)
    return True


def _py_env_s_peeled(c: _PyCmd) -> _PyCmd | None:
    """T-5360: the command `c`'s first `env` word runs, as the shared wrapper peel reads it
    (`_ho_static_command_words`), when that peel split an -S string: the words after the
    peel, a word built at run time where its source word is one (or it holds env's own
    `${VAR}`), and `c`'s argv. None when no -S string was split. The peel's option-head
    re-split stays off (no `chdirs`): an env option heading the -S string stays a word of
    the result (`env -S '-i bash -c' CODE` -> `-i bash -c CODE`), where the shell scan finds
    the shell word anyway, and the peel stays linear (`env -S '-S -S ...'`)."""
    words = c.words
    j = next((k for k, w in enumerate(words) if os.path.basename(w) == "env"), None)
    if j is None or not any(w[:1] == "-" and ("S" in w or w[:3] == "--s") for w in words[j + 1:]):
        return None
    # Cheap skip: every shell name holds `sh` (once env's quotes and escapes are dropped), and
    # a command word built at run time holds `$` or a backtick or is one of `c.dyn`.
    if not any(k > j for k in c.dyn) and not any(
            "sh" in w.translate(_QUOTE_STRIP) or "$" in w or "`" in w for w in words[j + 1:]):
        return None
    trace: dict = {}
    got = _ho_static_command_words(list(words[j:]), None, trace)
    peeled = trace.get("peeled", [])
    if not any(w == "-S" for w, _ in peeled):
        return None
    src = trace.get("src")
    sub = _PyCmd()
    sub.words = [str(w) for w in got]
    sub.argv = c.argv
    if src is None or len(src) != len(got):
        sub.dyn = set(range(len(got)))  # fail closed: every word may be built at run time
    else:
        sub.dyn = {k for k, s in enumerate(src) if s + j in c.dyn or isinstance(got[k], _HoDynWord)}
    return sub


def _py_code_strings(c: _PyCmd, with_argv: bool = False, keep_dynamic: bool = False) -> list | None:
    """The code strings simple command `c` runs (`eval ARGS`; the operand of
    every `SHELL [opts] -c... CODE`), or None if one is built at run time.
    With `keep_dynamic` (T-5140 r3), a run-time-built one is returned as its
    lexed static text instead (substitutions as `_PY_SUBST`): never None."""
    words = c.words
    n = len(words)
    out: list[str] = []

    def add(code: str, argv: list | None = None):
        out.append((code, argv) if with_argv else code)

    i = _py_head_index(words)
    if i < n and words[i] == "eval":
        if not keep_dynamic and any(j in c.dyn or _PY_SUBST in words[j] or _PY_PWD_MARK in words[j]
                                    for j in range(i + 1, n)):
            return None
        k = i + 1
        if all(_PY_PLAIN_WORD.fullmatch(w) for w in words[k:]):
            while k < n and words[k] == "eval":
                k += 1
        if k < n:
            add(" ".join(words[k:]), c.argv)
    if not _py_shell_codes(c, i, add, with_argv, keep_dynamic):
        return None
    # T-5360: the shell argv as it stands after the shared `env -S` peel: a `-c` (or an
    # `-o`/`-O` value) from inside the -S string, its code word after it (`env -S 'bash -c'
    # CODE`, `env -S '-i sh -c' CODE`, `nice env --split-string='bash -c' CODE`).
    peeled = _py_env_s_peeled(c)
    if peeled is not None:
        seen = {x[0] if with_argv else x for x in out}

        def add_new(code: str, argv: list | None = None):
            if code not in seen:
                seen.add(code)
                add(code, argv)
        if not _py_shell_codes(peeled, _py_head_index(peeled.words), add_new, with_argv, keep_dynamic):
            return None
    wrapped = _py_wrapper_codes(c, keep_dynamic)
    if wrapped is None:
        return None
    if with_argv:
        out.extend((code, None) for code in wrapped)
    else:
        out.extend(wrapped)
    return out


# r6: wrappers that take a command string as their own option's value.
# `script [opts] [-c|--command] CODE [file]` (clustered `-qc CODE`), `flock
# [opts] FILE -c|--command CODE`, and `env -S|--split-string STRING` (the
# string is split into a command line: read here as code, a superset).
# (option letter, long option, shortest accepted prefix of it: script and
# env take any unique abbreviation, `--c`/`--s` and up; flock takes none).
_PY_CODE_WRAPPERS = {"script": ("c", "--command", 3), "flock": ("c", "--command", 9),
                     "env": ("S", "--split-string", 3)}


def _py_wrapper_codes(c: _PyCmd, keep_dynamic: bool = False) -> list[str] | None:
    """The code strings `c`'s script/flock/env -S words carry, or None if one
    is built at run time (with `keep_dynamic`, its static text instead).
    One forward scan per option letter, from the first wrapper word using
    it (linear; a `-c` of a later command word is read as code too: a
    superset, it can only add judgement)."""
    words = c.words
    n = len(words)
    out: list[str] = []
    first: dict = {}
    for j, w in enumerate(words):
        spec = _PY_CODE_WRAPPERS.get(os.path.basename(w))
        if spec is not None and spec not in first:
            first[spec] = j
    for (letter, long_opt, min_pre), j in first.items():
        env_s = letter == "S"

        def code(at: int, val: str, env_s: bool = env_s) -> str | None:
            # T-5140 r3: with `keep_dynamic`, a value this reading cannot form is returned
            # as its static text instead of None.
            got = strict(at, val, env_s)
            return val if got is None and keep_dynamic else got

        def strict(at: int, val: str, env_s: bool) -> str | None:
            # The code string option value `val` (word `at`) carries; None if it is built
            # at run time.
            if at in c.dyn or _PY_SUBST in val or _PY_PWD_MARK in val:
                return None
            if not env_s:
                return val
            # env reads options (`-C DIR`, `-i`, `-u NAME`) at the head of an -S
            # string before the command: read it as an `env` command line (r8).
            # env -S has its own escapes (`\_` is a separator) and `${VAR}` expansion: a
            # value using either is not what env runs (r7). T-5360 r2: this refusal stays
            # (later gates read the raw text, so it is the deny for every escaped -S string);
            # the GNU split (`_env_s_split`: quotes, `#` comments) only adds denials.
            if "\\" in val or "$" in val:
                return None
            opaque: set = set()
            split = _env_s_split(val, opaque)
            if split is None or any(isinstance(x, _HoDynWord) for x in split):
                return None
            # r2: a quoted blank joins words the blank-split readings (the escalation walk's
            # `_dequote`d words, main's) keep apart (`env -S 'nice "sudo id"'`): fail closed.
            if any(ch in _ENV_S_BLANKS for x in split for ch in x):
                return None
            if opaque and _env_s_head_opaque(split, opaque):
                return None
            return " ".join(["env"] + [shlex.quote(x) for x in split])

        for k in range(j + 1, n):
            w = words[k]
            val_at = None
            name, eq, val = w.partition("=")
            if len(name) >= min_pre and name.startswith("--") and long_opt.startswith(name):
                if not eq:
                    val_at = k + 1
                else:
                    got = code(k, val)
                    if got is None:
                        return None
                    out.append(got)
                    continue
            elif len(w) >= 2 and w[0] == "-" and w[1] != "-" and letter in w[1:]:
                x = w.index(letter, 1)
                if x + 1 < len(w):  # `-cCODE`, `-SSTRING`: glued
                    got = code(k, w[x + 1:])
                    if got is None:
                        return None
                    out.append(got)
                    continue
                val_at = k + 1
            if val_at is not None and val_at < n:
                got = code(val_at, words[val_at])
                if got is None:
                    return None
                out.append(got)
    return out


def _py_expand_code(lex: _PyLexer, skip_unknown: bool = False, with_argv: bool = False) -> list[str] | None:
    """Lex every code string of `lex`'s commands (and theirs, to
    `_PY_CODE_DEPTH` levels) into `lex.cmds`. Returns the fail-closed tails
    (`lex`'s and the code strings'), or None to deny (a dynamic code string,
    one nested past the bound, or past the re-read budget). With
    `skip_unknown`, unreadable branches are skipped for supplemental scans.
    With `with_argv`, appended commands carry shell `-c` positional metadata."""
    tails = lex.tails
    budget = _PY_CODE_BUDGET * lex.n + 4096
    work = [(c, 0) for c in lex.cmds]
    while work:
        c, depth = work.pop()
        codes = _py_code_strings(c, with_argv=with_argv)
        if codes is None:
            if skip_unknown:
                continue
            return None
        for item in codes:
            if with_argv:
                code, argv = item
            else:
                code, argv = item, None
            budget -= len(code)
            if depth >= _PY_CODE_DEPTH or budget < 0:
                if skip_unknown:
                    continue
                return None
            sub = _py_lex(code)
            if with_argv:
                for x in sub.cmds:
                    x.argv = argv
            lex.defs = lex.defs or sub.defs
            lex.cmds.extend(sub.cmds)
            tails.extend(sub.tails)
            work.extend((x, depth + 1) for x in sub.cmds)
    lex.code_tails = tails
    return tails


def _ho_py_dyn_word(word: str, dynamic: bool = False) -> str:
    """`word` adapted to the HO dynamic-target classifier's provenance."""
    return _HoDynWord(word) if dynamic or _py_dynamic(word) else word


def _ho_py_cmd_words(c: _PyCmd) -> tuple[list[str], list[str]]:
    """A `_PyCmd` as HO words and write-redirect targets, preserving dynamic targets."""
    words: list[str] = []
    for i, w in enumerate(c.words):
        words.extend(_py_expand_positional_word(w, i in c.dyn, c.argv))
    targets: list[str] = []
    for t in c.targets:
        targets.extend(_py_expand_positional_word(t, _py_dynamic(t), c.argv))
    return words, targets


def _ho_static_write_hit_in_py_cmd(words: list[str], redirect_targets: list[str], cwd: str, cache: dict) -> bool:
    """Static protected targets for the same guarded write surfaces as the dynamic scan."""
    if any(_ho_cd_operand_hit(t, cwd, cache) for t in redirect_targets):
        return True
    if not words:
        return False
    words = _ho_without_redirect_targets(words, redirect_targets)
    if not words:
        return False
    chdirs: list = []
    words = _ho_static_command_words(words, chdirs)  # T-5325: every local wrapper, not only T-4785's
    if not words:
        return False
    if chdirs:  # T-5326: `env -C DIR cmd` runs cmd in DIR; the cwd reading stays too
        icwd = _ho_chdir_resolve(cwd, chdirs)
        if icwd is None:
            if _ho_dynamic_chdir_destroy_hit(words, cache, any(c is _HO_CHDIR_CAPPED for c in chdirs)):
                return True
        elif _ho_static_verb_hit(words, icwd, cache):
            return True
    return _ho_static_verb_hit(words, cwd, cache, destroy=not chdirs)


def _ho_static_verb_hit(words: list[str], cwd: str, cache: dict, destroy: bool = True) -> bool:
    """`_ho_static_write_hit_in_py_cmd`'s per-verb rules for the peeled command `words`."""
    verb = words[0].rsplit("/", 1)[-1]
    if verb == "git":
        return bool(_ho_git_write_hit(words, cwd, cache))
    if verb == "sed":
        return any(_ho_cd_operand_hit(o, cwd, cache) for o in _ho_sed_inplace_write_operands(words))
    if destroy and verb == "mv" and _ho_destroy_hit(verb, words, cwd, cache):
        return True  # T-5326: a moved ancestor of a protected dir, or a glob source
    if verb == "cp" or verb in ("mv", "tee"):
        ops, tvals, dflag, unknown = _ho_operands(verb, words)
        del dflag
        if verb in ("mv", "tee"):
            dests = ops + tvals
        elif unknown:
            dests = tvals + ops[1:]
        else:
            dests = tvals if tvals else (ops[-1:] if len(ops) >= 2 else [])
        return any(_ho_cd_operand_hit(o, cwd, cache) for o in dests)
    if verb in _HO_ALL_OPERAND_VERBS or verb in _HO_DEST_VERBS or verb == "find":
        # T-5325: rm/rmdir/touch/mkdir/truncate/install/rsync/ln and `find -delete` in a
        # code string, judged by the same per-verb rule as at the top level.
        return _ho_cd_segment_hit(words, False, cwd, False, cache, destroy)
    return False


def _ho_dynamic_write_hit_in_py_cmds(cmds: list[_PyCmd], first: int, cwd: str) -> bool:
    """True if selected `_PyCmd`s contain guarded write families with dynamic targets."""
    cache: dict = {}
    for c in cmds[first:]:
        raw, targets = _ho_py_cmd_words(c)
        words = _ho_peel_lead(raw)
        if _ho_cd_segment_dynamic_write_hit(words, targets, cwd, False, cache):
            return True
        # T-5326: the raw words, so a `time` keyword's options are peeled too
        if _ho_static_write_hit_in_py_cmd(raw, targets, cwd, cache):
            return True
    return False


def _ho_literal_shell_stdin_codes(command: str) -> list[str]:
    """Literal heredoc bodies proven to be stdin code for plain shell consumers
    (`_SHELL_NAMES`, T-5332: was bash/sh only)."""
    if "<<" not in command:
        return []
    try:
        toks, docs = _hd_lex(command)
    except _HeredocBail:
        return []
    if _hd_global_bail([t[1] for t in toks if t[0] == "W"]):
        return []

    codes: list[str] = []
    doc_by_pos = {pos: (strip, start, end, depth) for strip, start, end, depth, pos in docs}
    i, n = 0, len(toks)
    while i < n:
        j = i
        while j < n and not (toks[j][0] == "O" and toks[j][1] in _HD_SEPARATORS):
            j += 1
        seg = toks[i:j]
        hpos = [k for k in range(i, j) if toks[k][0] == "H"]
        if len(hpos) == 1 and hpos[0] in doc_by_pos:
            strip, start, end, depth = doc_by_pos[hpos[0]]
            nxt = toks[j][1] if j < n else None
            if not strip and start is not None and depth == 0 and nxt not in ("|", "|&"):
                words: list[str] = []
                plain = True
                for kind, value, dynamic, is_plain in seg:
                    if kind == "H":
                        continue
                    if kind != "W" or dynamic or not is_plain:
                        plain = False
                        break
                    words.append(value)
                if plain and words and os.path.basename(words[0]) in _SHELL_NAMES:
                    pending = [("word", w, False) for w in reversed(words[1:])]

                    def nxttok():
                        return pending.pop() if pending else None

                    smode: list[bool] = []
                    cmode, tok = _skip_shell_options(nxttok, smode)
                    if not cmode and tok is None:
                        codes.append(command[start:end])
        i = j + 1
    return codes


def _ho_code_string_dynamic_write_hit(command: str, cwd: str) -> bool:
    """Dynamic guarded write targets inside proven executable shell code strings/bodies."""
    lex = _py_lex(command)
    first = len(lex.cmds)
    # T-5332 r3: the stdin-fed walk reads this command's own commands and top-level events
    # (which `_py_expand_code` leaves as they are) instead of lexing it again.
    _FED_LEX_CACHE[0] = (command, lex, first)
    _py_expand_code(lex, skip_unknown=True, with_argv=True)
    if _ho_dynamic_write_hit_in_py_cmds(lex.cmds, first, cwd):
        return True
    for body in _ho_literal_shell_stdin_codes(command):
        sub = _py_lex(body)
        _py_expand_code(sub, skip_unknown=True, with_argv=True)
        if _ho_dynamic_write_hit_in_py_cmds(sub.cmds, 0, cwd):
            return True
    return False


# --- T-5332: shells fed their commands on stdin from static, fully visible text ---
# `printf '<lit>' | bash`, `echo '<lit>' | sh`, `bash <<< '<lit>'`, `bash <<'E' ... E`,
# `cat <<E | bash`: the shell runs the fed text as commands, so `_decide_readings` judges each
# fed text as a command of its own, once at every directory the command can run it in (the
# payload cwd, and for each chain of literal chdir targets the directory after each step in
# sequence and each target from the payload cwd: `_fed_cands`; r4/r5: a target inside an
# `eval` string counts in its place, a `-c` string's does not outlive it: `_fed_chain`). At
# the payload cwd every rule judges the copy; at the other candidates only the rules that
# read the cwd do (`_decide_cwd_rules`, r5: the rest give the same verdict anywhere). Each
# copy only adds denials.
# A feed is read from `_PyLexer`'s top-level pipes and each command's recorded
# here-strings/heredocs, in the command and (to `_FED_DEPTH`) in its static code strings, its
# `$(...)`/backtick bodies and the fed texts themselves.
#   Producers: `printf` (format escapes; %s %b %c %q %d %i %u %o %x %X %% with flags, width
# and precision; the format reused while arguments remain), `echo` (read as bash's, as bash's
# with `xpg_echo` and as dash's: every reading is judged), `cat` (any options), `cat -`,
# `tee` passing their stdin on, `xargs printf|echo ...`, behind any local wrapper (`env
# printf`), and a `{ ...; }` or `( ... )` group of them (with the group's own here-string or
# heredoc as its stdin). A word built at run time is kept as written (`$X`), so the text is
# judged as the `-c` walk judges a code string holding the expansion.
#   Consumers: a shell (behind a local wrapper, or `busybox`) with no `-c` and no script
# operand, or `-s`, or `/dev/stdin`-style operand; `source`/`.` [`--`] of `/dev/stdin`;
# `eval` of a static string, judged as the commands it runs (r5: `| eval bash`); and
# `xargs CMD` (r3: the input split into arguments as GNU xargs splits it, each one quoted:
# one command per `-I` line, or per argument for a shell's `-c`, else CMD with them all;
# `-a /dev/stdin` reads the pipe too; r5: with `-a FILE` CMD itself reads the pipe and is
# judged as a direct consumer (`| xargs -a /dev/null bash`); r4: options parsed as getopt_long
# does, clusters and unique long prefixes included, an unknown one refused).
#   Here-strings and heredocs: a quoted body as written; an unquoted heredoc body processed
# in one left-to-right pass (`\`+newline dropped, `\\`, `\$`, `\`` unescaped, expansions
# kept) and, when it holds a `\`+newline, also as written; a here-string built at run time
# is read from the raw text (`_shell_word`). An even backslash run before a newline gets a
# blank line after it (the same program; the guard's own reading would join the lines).
#   Refused (fail closed: the text is the worker's own, so a plainer spelling is always
# available): a printf whose words are all static but whose output this cannot compute
# (`%f`, a `*` width, a width on `%q`, an invalid or out-of-range number, `%#o`, a negative
# `%x`, `%c` of an empty argument, an option) feeding a shell; feeds past the depth/size
# bounds, a relay chain past 16 hops, more than 8 readings of a group, more than 64 raw
# here-strings; and more than `_FED_MAX_CANDS` directories (its own message).
# OUT of scope (OS isolation, T-1347): a feed from a file or another program (`bash < f`,
# `curl ... | sh`, `grep ... | bash`, `sed`/`dd`/`head` relays), a process substitution (`tee
# >(bash)`, `bash < <(...)`, `bash <(...)`, `BASH_ENV=<(...)`), a pipe into an `if`/`while`
# body, `bash -c bash`, `bash -c 'source /dev/stdin'`, `cd -`/`$OLDPWD`/`CDPATH`/a dynamic
# chdir target, and a shell under a name this hook does not know.
# T-5571: those feeds (a file or another program, a process substitution, an `if`/`while`
# body, `bash -c bash`) are now refused when they reach a shell (`_decide_noncomputable`);
# this reading still judges only the computable ones.
_FED_DEPTH = 4
_FED_MAX_CODES = 32
_FED_MAX_CANDS = 32
_FED_MAX_OUT = 65_536
# r3: (text, lexer, its own command count) of the last command `_ho_code_string_dynamic_write_hit`
# lexed, read back by the fed walk for the same text (`_FedLexView`).
_FED_LEX_CACHE: list = [None]


class _FedLexView:
    """A lexer's own commands and top-level events: all the fed walk reads of one."""

    __slots__ = ("cmds", "events")

    def __init__(self, cmds: list, events: list):
        self.cmds = cmds
        self.events = events
# Wrappers that read their own stdin (as argument lines) instead of passing it on.
_FED_STDIN_TAKERS = frozenset({"parallel", "sem", "watch"})
# r6: every spelling `_py_chdir` reads as a chdir: cd/pushd, any `--c...`/`--d...` long
# option (env takes any prefix of --chdir from `--c`; make/tar --directory), and any
# single-dash word holding a `C` (`-C`, `-CDIR`, env's `-iC`).
_FED_CHDIR_HINT = re.compile(r"cd|pushd|--[cd]|(?<![\w-])-[^\s=-][^\s=]*C|(?<![\w-])-C")
_FED_STDIN_PATHS = frozenset({"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0", "-"})
_FED_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
               "t": "\t", "v": "\v", "\\": "\\"}
_FED_HEX = re.compile(r"x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})")
_FED_OCT2 = re.compile(r"[0-7]{0,2}")
_FED_OCT3 = re.compile(r"[0-7]{0,3}")
_FED_SPEC = re.compile(r"%([-+ #0]*)(\d*)(?:\.(\d*))?([sbcdiouxXq%])")
_FED_INT = re.compile(r"\s*([+-]?)(0[xX][0-9A-Fa-f]+|0[0-7]*|[1-9][0-9]*)\Z")
_FED_HD_UNESCAPE = re.compile(r"\\(\n|[\\$`])")
_FED_EVEN_BS_NL = re.compile(r"(?<!\\)((?:\\\\)+)\n")
_FED_HERESTR = re.compile(r"<<<[ \t]*")
_FED_PREFILTER = ("sh", "stdin", "fd/0", "xargs", "sg", "newgrp", "firejail")  # T-5571: + wrappers


class _FedUnknown(Exception):
    """A static printf whose output this hook cannot compute."""


def _fed_escapes(s: str, mode: str) -> tuple[str, bool]:
    """`s` with bash's backslash escapes expanded, as a printf format ("fmt"), a `%b`
    argument ("b") or `echo -e` ("echo") reads them (bash 5.2, checked against it).
    Returns (text, stopped): `\\c` ends all output in "b"/"echo"; in "fmt" it is literal."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        k = s.find("\\", i)
        if k < 0 or k + 1 >= n:
            out.append(s[i:])
            break
        out.append(s[i:k])
        c = s[k + 1]
        i = k + 2
        if c in _FED_SIMPLE:
            out.append(_FED_SIMPLE[c])
        elif mode == "fmt" and c in "'\"?":
            out.append(c)
        elif mode != "fmt" and c == "c":
            return "".join(out), True
        elif c == "0" and mode != "fmt":
            m = _FED_OCT3.match(s, i)  # `\0NNN`
            out.append(chr(int(m.group(0) or "0", 8) & 0xFF))
            i = m.end()
        elif c in "01234567" and mode != "echo":
            m = _FED_OCT2.match(s, i)  # `\NNN`
            out.append(chr(int(c + m.group(0), 8) & 0xFF))
            i = m.end()
        else:
            m = _FED_HEX.match(s, k + 1)
            if m:
                out.append(chr(min(int(m.group(1) or m.group(2) or m.group(3), 16), 0x10FFFF)))
                i = m.end()
            else:
                out.append("\\" + c)
    return "".join(out), False


def _fed_int(arg: str) -> int:
    """bash printf's integer argument (`0x1f`, `017`, `'c`), or _FedUnknown (also for a
    value past 64 bits, which bash clamps)."""
    if arg == "":
        return 0
    if arg[0] in "'\"":
        return ord(arg[1]) if len(arg) > 1 else 0
    m = _FED_INT.match(arg)
    if not m:
        raise _FedUnknown
    g = m.group(2)
    v = int(g, 16) if g[:2] in ("0x", "0X") else int(g, 8) if len(g) > 1 and g[0] == "0" else int(g)
    if v >= 2 ** 63:
        raise _FedUnknown
    return -v if m.group(1) == "-" else v


def _fed_printf(args: list[str]) -> str:
    """bash `printf ARGS`'s output, or _FedUnknown. Bounded: the output (literal text
    included) past `_FED_MAX_OUT` raises."""
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        return ""
    fmt, rest = args[0], args[1:]
    if fmt[:1] == "-" or "\\%" in fmt:
        raise _FedUnknown  # `-v NAME` prints nothing; `\%` is not modelled
    out: list[str] = []
    size = pos = 0
    while True:
        used = 0
        i = 0
        while True:
            k = fmt.find("%", i)
            text, _stop = _fed_escapes(fmt[i:] if k < 0 else fmt[i:k], "fmt")
            out.append(text)
            size += len(text)
            if size > _FED_MAX_OUT:
                raise _FedUnknown
            if k < 0:
                break
            m = _FED_SPEC.match(fmt, k)
            if not m:
                raise _FedUnknown  # `%q`, `%f`, a `*` width, a lone `%`
            flags, width, prec, conv = m.groups()
            i = m.end()
            if conv == "%":
                if flags or width or prec is not None:
                    raise _FedUnknown
                out.append("%")
                continue
            if (width and int(width) > 4096) or (prec and int(prec) > 4096):
                raise _FedUnknown
            arg = ""
            if pos < len(rest):
                arg = rest[pos]
                pos += 1
                used += 1
            spec = "%" + flags + width + ("." + prec if prec is not None else "")
            if conv == "q":
                # r3: bash's own quoting differs in bytes (`a\ b`), but a shell reads both
                # back as the same word, which is all a fed shell sees.
                if flags or width or prec is not None:
                    raise _FedUnknown
                out.append(shlex.quote(arg))
            elif conv in "sbc":
                if flags.strip("-"):
                    raise _FedUnknown
                stop = False
                if conv == "b":
                    arg, stop = _fed_escapes(arg, "b")
                elif conv == "c":
                    if not arg:
                        raise _FedUnknown  # bash prints a NUL
                    arg, spec = arg[:1], "%" + flags + width
                out.append((spec + "s") % arg)
                if stop:
                    return "".join(out)
            else:
                v = _fed_int(arg)
                if (conv in "uoxX" and v < 0) or ("#" in flags and conv in "oxX"):
                    raise _FedUnknown  # two's complement; `%#o` is `010`, not `0o10`
                out.append((spec + {"i": "d", "u": "d"}.get(conv, conv)) % v)
            size += len(out[-1])
            if size > _FED_MAX_OUT:
                raise _FedUnknown
        if not used or pos >= len(rest):
            return "".join(out)


def _fed_echo(args: list[str]) -> list[str]:
    """`echo ARGS`'s output as bash reads it, and (when an argument holds a backslash) as
    bash with `xpg_echo` and as dash (`\\0NNN`, `\\c`; `-e` is text) read it."""
    escapes, newline = False, True
    k = 0
    while k < len(args) and len(args[k]) > 1 and args[k][0] == "-" and not args[k][1:].strip("neE"):
        for ch in args[k][1:]:
            if ch == "n":
                newline = False
            else:
                escapes = ch == "e"
        k += 1
    nl = "\n" if newline else ""
    text = " ".join(args[k:])
    outs = []
    if escapes:
        e, stop = _fed_escapes(text, "echo")
        outs.append(e if stop else e + nl)
    else:
        outs.append(text + nl)
    if any("\\" in a for a in args):
        e, stop = _fed_escapes(text, "echo")  # xpg_echo
        outs.append(e if stop else e + nl)
        d_nl = "" if args[:1] == ["-n"] else "\n"
        d_text = " ".join(args[1:] if args[:1] == ["-n"] else args)
        e, stop = _fed_escapes(d_text, "echo")  # dash
        outs.append(e if stop else e + d_nl)
    return list(dict.fromkeys(outs))


def _fed_heredoc_text(body: str, expands: bool, strip: bool) -> str:
    """The text an unquoted (`expands`) or quoted heredoc feeds: tabs stripped for `<<-`;
    unquoted: one left-to-right pass as bash reads it (r3: `\\\\` then a newline is a
    backslash and a real newline), `\\`+newline dropped and `\\\\`, `\\$`, `\\`` unescaped,
    expansions kept."""
    if strip:
        body = "".join(line.lstrip("\t") for line in body.splitlines(True))
    if expands:
        body = _FED_HD_UNESCAPE.sub(lambda m: "" if m.group(1) == "\n" else m.group(1), body)
    return body


def _fed_own_stdin(c: _PyCmd, raw: list) -> list:
    """The texts `c`'s own here-strings and heredocs feed it. A here-string built at run
    time is read from `raw` (the text's here-string words, as written). An unquoted heredoc
    is read processed and (r3) as written too."""
    out: list = []
    for w in c.herestrs:
        if w is not None:
            out.append(w + "\n")
        else:
            out.extend(x + "\n" for x in raw)
    for body, expands, strip in c.heredocs:
        text = _fed_heredoc_text(body, expands, strip)
        out.append(text)
        if expands and "\\\n" in body:
            out.append(body)  # r3: the body as written, an extra reading
    return out


def _fed_words(c: _PyCmd) -> list[str]:
    """`c`'s words from its command word on, with the lexer's markers for a `$(...)` /
    `$(pwd)` value spelled as an expansion (`$(:)`, `$PWD`)."""
    return [w.replace(_PY_SUBST, "$(:)").replace(_PY_PWD_MARK, "$PWD")
            for w in c.words[_py_head_index(c.words):]]


def _fed_dynamic(c: _PyCmd) -> bool:
    return bool(c.dyn) or any(_PY_SUBST in w or _PY_PWD_MARK in w for w in c.words)


def _fed_xargs_split(full: list[str]):
    """(xargs option words, the command words it runs) when `full` runs its command
    through `xargs` (behind any local wrapper), else None."""
    words = _ho_static_command_words(full)
    prefix = [os.path.basename(w) for w in full[:len(full) - len(words)]]
    if "xargs" not in prefix:
        return None
    return full[prefix.index("xargs"):len(full) - len(words)], words


def _fed_eval_code(c: _PyCmd, words: list[str]) -> str | None:
    """The code `eval WORDS` runs (`words[0]` the eval word), or None if built at run time."""
    if _fed_dynamic(c):
        return None
    args = words[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    return " ".join(args)


def _fed_eval_consumer(c: _PyCmd, words: list[str], depth: int):
    """r5: `eval STRING` with a static string reads stdin as the commands it runs do
    (`| eval bash`, `| eval 'bash -s'`, `eval bash <<< ...`): the first of them that
    consumes stdin, judged as `_fed_consumer` judges it."""
    code = _fed_eval_code(c, words)
    if code is None or depth >= _FED_DEPTH:
        return None
    for sub in _py_lex(code).cmds:
        run = _fed_consumer(sub, depth + 1)
        if run is not None:
            return run
    return None


def _fed_peeled(full: list[str]) -> tuple[list[str], list[str], int, list[int]]:
    """(words, base, n, src): the command `_ho_static_command_words` reads in `full` (with
    the `env -S` re-split on), and the words as the peel consumed them: `base[:n]` the
    peeled wrapper words in order (an -S value as `-S`, its split words after it), `base[n:]`
    the command words, and `src[k]` the index in `full` that `base[k]` came from. T-5341:
    read from the peel itself, so a split -S string lines up with the words around it
    (`env -S '-i xargs -0' bash -c`). One that does not line up is refused (fail closed)."""
    trace: dict = {}
    words = _ho_static_command_words(full, [], trace)
    peeled = trace.get("peeled", [])
    src_words = trace.get("src")
    base = [w for w, _ in peeled] + list(words)
    src = [s for _, s in peeled] + list(src_words or [])
    # T-5360: a word GNU's -S grammar split out of a value need not be a substring of it
    # (`"a\_b"` -> `a b`): one whose source is an -S value (the `-S` placeholder's) lines up.
    s_values = {s for w, s in peeled if w == "-S"}
    if src_words is None or len(src_words) != len(words) or any(
            not 0 <= s < len(full) or not (w == full[s] or w == "-S" or s in s_values
                                           or str(w) in str(full[s]))
            for w, s in zip(base, src)):
        raise _FedUnknown
    return words, base, len(peeled), src


def _fed_consumer(c: _PyCmd, depth: int = 0):
    """None, or a function from a text fed on `c`'s stdin to the code `c` runs: a shell
    reading commands from stdin, `source`/`.` of `/dev/stdin`, or `xargs CMD`."""
    if not any("sh" in w or "xargs" in w or w in (".", "source", "eval") or "sg" in w
               or "newgrp" in w or "firejail" in w for w in c.words):
        return None
    i = _py_head_index(c.words)
    if i >= len(c.words) or i in c.dyn or _PY_SUBST in c.words[i] or _PY_PWD_MARK in c.words[i]:
        return None
    # A later word built at run time reads as an option or argument (`bash -s "$X"`).
    full = c.words[i:]
    # T-5341: the peel's `env -S` re-split is on, so an env option that starts the -S
    # string (`env -S '-i' bash`, `--split-string='--chdir ..' bash`) is read as env's own
    # and the head is the command after it. The chdir itself reaches the candidates through
    # `_fed_chain` (`_py_env_chdirs` reads -S strings).
    words, base, n, src = _fed_peeled(full)
    prefix = [os.path.basename(w) for w in base[:n]]
    if any(w in _FED_STDIN_TAKERS for w in prefix):
        return None  # `... | parallel bash`: the wrapper reads the pipe, not the shell
    if "xargs" in prefix:
        spec = _fed_xargs_spec(base[prefix.index("xargs"):n])
        if spec is not None:
            return _fed_xargs_run(spec, words) if words else None
        # r5: `xargs -a FILE CMD` reads its arguments from FILE and leaves CMD's stdin the
        # pipe: CMD is judged as a direct consumer (`| xargs -a /dev/null bash`), and (r6)
        # as a consumer of any kind when it is one (`| xargs -a /dev/null xargs -0 sh -c`).
        at = prefix.index("xargs")
        end = at + _fed_xargs_end(base[at:])
        if end < len(base) and depth < _FED_DEPTH:
            sub = _PyCmd()
            sub.words = list(base[end:])
            sub.dyn = {k - end for k in range(end, len(base)) if src[k] + i in c.dyn}
            return _fed_consumer(sub, depth + 1)
        return None
    if words and os.path.basename(words[0]) == "busybox":
        words = words[1:]
    if not words:
        return None
    head = os.path.basename(words[0])
    if head == "eval":
        return _fed_eval_consumer(c, words, depth)
    if head in (".", "source"):
        ops = words[1:2] if words[1:2] != ["--"] else words[2:3]
        return (lambda t: t) if ops and _fed_stdin_operand(ops[0]) else None
    name = None if head in _SHELL_NAMES else _wc_name(words[0])
    if name is not None:
        # T-5571: `sg GROUP`, `newgrp`, `firejail [opts]` with no command start a shell that
        # reads stdin; `firejail [opts] CMD` passes its stdin to CMD (a code string, `sg -c
        # CODE`, or a word built at run time is `_decide_wrapcode`'s)
        strip = len(base) - n - len(words)  # (a `busybox` word dropped above)
        wdyn = [src[n + strip + k] + i in c.dyn or _PY_SUBST in words[k] or _PY_PWD_MARK in words[k]
                for k in range(len(words))]
        got = _wc_parse(name, words, lambda k: wdyn[k])
        if got[0] == "stdin":
            return lambda t: t
        if got[0] == "argv" and depth < _FED_DEPTH:
            at = len(words) - len(got[1])
            sub = _PyCmd()
            sub.words = list(got[1])
            sub.dyn = {k - at for k in range(at, len(words)) if wdyn[k]}
            return _fed_consumer(sub, depth + 1)
        return None
    if head not in _SHELL_NAMES:
        return None
    k, smode = 1, False
    while k < len(words):
        w = words[k]
        if w == "-":
            return lambda t: t
        if w == "--":
            k += 1
            break
        if len(w) < 2 or w[0] not in "-+":
            break
        if w[:2] == "--":
            k += 2 if w in ("--rcfile", "--init-file") else 1
            continue
        if "c" in w[1:]:
            return None  # a code string: `_py_code_strings` reads it
        smode = smode or (w[0] == "-" and "s" in w[1:])
        k += 1 + w.count("o", 1) + w.count("O", 1)
    if smode or k >= len(words) or _fed_stdin_operand(words[k]):
        return lambda t: t
    return None


_FED_SLASHES = re.compile(r"/+")


def _fed_stdin_operand(word: str) -> bool:
    """A shell's or source's operand that names its own stdin: a `_FED_STDIN_PATHS` spelling
    or (T-5571, a superset) any other path under /dev/ or /proc/ but /dev/null (`/dev//stdin`,
    `/dev/./stdin`, `/dev/fd/00`, `/proc/self/fd//0`): no script lives there."""
    if word in _FED_STDIN_PATHS:
        return True
    norm = _FED_SLASHES.sub("/", word)
    return norm.startswith(("/dev/", "/proc/")) and norm != "/dev/null"


_FED_XARGS_FLAGS = frozenset("0rtpxo")
_FED_XARGS_VALUE = frozenset("nLPsdaEI")  # the rest of the word, else the next word
_FED_XARGS_OPTVAL = frozenset("ile")  # an optional value, only glued to the letter
# long option -> (short letter it stands for, value: "req" | "opt" | "")
_FED_XARGS_LONG = {
    "null": ("0", ""), "arg-file": ("a", "req"), "delimiter": ("d", "req"), "eof": ("e", "opt"),
    "replace": ("i", "opt"), "max-lines": ("l", "opt"), "max-args": ("n", "req"),
    "max-procs": ("P", "req"), "max-chars": ("s", "req"), "process-slot-var": ("", "req"),
    "interactive": ("p", ""), "verbose": ("t", ""), "no-run-if-empty": ("r", ""),
    "exit": ("x", ""), "open-tty": ("o", ""), "show-limits": ("", ""), "help": ("", ""),
    "version": ("", ""),
}


def _fed_xargs_spec(xs: list[str]) -> dict | None:
    """How `xargs` (option words `xs`, `xs[0]` the xargs word) reads its input, parsed as
    getopt_long does (r4): a short cluster's `0 r t p x o` are flags, `n L P s d a E I` take
    the rest of the word or else the next word, `i l e` take only a glued value; a long
    option may be any unique prefix. None when it reads no stdin (`-a FILE`)."""
    spec = {"zero": False, "delim": None, "rep": None, "eof": None}

    def apply(letter: str, value) -> bool:
        if letter == "0":
            spec["zero"] = True
        elif letter == "a":
            return value in _FED_STDIN_PATHS
        elif letter == "d":
            d = _fed_escapes(value or "", "fmt")[0][:1]
            if not d:
                raise _FedUnknown
            spec["delim"] = d
        elif letter == "E":
            spec["eof"] = value
        elif letter == "e":
            spec["eof"] = value or None
        elif letter == "I":
            spec["rep"] = value
        elif letter == "i":
            spec["rep"] = value or "{}"
        return True

    j = 1
    while j < len(xs):
        w = xs[j]
        nxt = xs[j + 1] if j + 1 < len(xs) else ""
        if w == "--" or w[:1] != "-" or w == "-":
            break
        if w[:2] == "--":
            name, eq, val = w[2:].partition("=")
            hits = [k for k in _FED_XARGS_LONG if k.startswith(name)] if name else []
            if name in _FED_XARGS_LONG:
                hits = [name]
            if len(hits) != 1:
                raise _FedUnknown  # unknown or ambiguous: fail closed
            letter, kind = _FED_XARGS_LONG[hits[0]]
            value = val if eq else None
            if kind == "req" and not eq:
                value = nxt
                j += 1
            if letter and not apply(letter, value):
                return None
            j += 1
            continue
        for k in range(1, len(w)):
            ch = w[k]
            if ch in _FED_XARGS_FLAGS:
                if ch == "0":
                    apply("0", None)
                continue
            if ch in _FED_XARGS_VALUE:
                value = w[k + 1:]
                if not value:
                    value = nxt
                    j += 1
                if not apply(ch, value):
                    return None
                break
            if ch in _FED_XARGS_OPTVAL:
                apply(ch, w[k + 1:])
                break
            raise _FedUnknown  # a letter xargs does not take
        j += 1
    return spec


def _fed_xargs_end(xs: list[str]) -> int:
    """The index in `xs` (`xs[0]` the xargs word) of the command xargs runs: past its
    options as `_fed_xargs_spec` reads them (r6)."""
    j = 1
    while j < len(xs):
        w = xs[j]
        if w == "--":
            return j + 1
        if w[:1] != "-" or w == "-":
            return j
        if w[:2] == "--":
            name, eq, _val = w[2:].partition("=")
            hits = [k for k in _FED_XARGS_LONG if k.startswith(name)] if name else []
            if name in _FED_XARGS_LONG:
                hits = [name]
            if len(hits) == 1 and _FED_XARGS_LONG[hits[0]][1] == "req" and not eq:
                j += 1
        else:
            for k in range(1, len(w)):
                if w[k] in _FED_XARGS_VALUE:
                    if not w[k + 1:]:
                        j += 1
                    break
                if w[k] in _FED_XARGS_OPTVAL:
                    break
        j += 1
    return j


def _fed_xargs_items(text: str, spec: dict) -> list[str]:
    """The arguments `xargs` reads from `text` (GNU xargs, checked against it): split at a
    NUL (`-0`) or the `-d` delimiter as is; else at blanks and newlines (only newlines with
    `-I`, whose lines lose their leading blanks), with quotes and `\\` honoured; an
    unmatched quote ends the input (the arguments before it still run); `-E EOF` ends it."""
    sep = "\0" if spec["zero"] else spec["delim"]
    if sep is not None:
        items = text.split(sep)
        if items and items[-1] == "":
            items.pop()
        return items
    line = spec["rep"] is not None
    items: list[str] = []
    cur: list[str] = []
    started = False
    quote = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\n":
                return items  # unmatched quote
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote, started = ch, True
        elif ch == "\\" and i + 1 < n:
            cur.append(text[i + 1])
            started = True
            i += 1
        elif ch == "\n" or (not line and ch in " \t"):
            if started:
                items.append("".join(cur))
                if spec["eof"] is not None and items[-1] == spec["eof"]:
                    return items[:-1]
                cur, started = [], False
        elif line and not started and ch in " \t":
            pass
        else:
            cur.append(ch)
            started = True
        i += 1
    if quote:
        return items
    if started:
        items.append("".join(cur))
        if spec["eof"] is not None and items[-1] == spec["eof"]:
            items.pop()
    return items


def _fed_xargs_invocations(spec: dict, cmd: list[str], text: str) -> list[list[str]]:
    """The command lines `xargs CMD` runs on input `text`: one per item with `-I`, or for
    a shell's `-c` with no code (each item a code string: any `-n` batching), else one."""
    items = _fed_xargs_items(text, spec)
    cmd = cmd or ["echo"]
    rep = spec["rep"]
    if rep:
        return [[w.replace(rep, it) for w in cmd] for it in items]
    if os.path.basename(cmd[0]) in _SHELL_NAMES and any(
            len(w) > 1 and w[0] == "-" and w[1] != "-" and "c" in w for w in cmd[1:]):
        return [cmd + [it] for it in items]
    return [cmd + items]


def _fed_xargs_run(spec: dict, cmd: list[str]):
    def run(text: str) -> str:
        return "\n".join(" ".join(shlex.quote(w) for w in inv)
                         for inv in _fed_xargs_invocations(spec, cmd, text))
    run.nc_xargs = True  # T-5571: xargs reads the pipe as arguments, not as a shell's code
    return run


def _fed_zip(outs: list, texts: list) -> list:
    """Readings concatenated index by index (bash/xpg/dash echo readings line up)."""
    n = max(len(outs), len(texts))
    if n > 8:
        raise _FedUnknown  # r3: fail closed, never truncate
    return [outs[min(i, len(outs) - 1)] + texts[min(i, len(texts) - 1)] for i in range(n)]


def _fed_producer_texts(unit: list, src: dict, raws: list, depth: int = 0) -> list:
    """The texts the commands of `unit` (one command, or a `{ }`/`( )` group's) write into
    the pipe after it, concatenated in order. Raises _FedUnknown for a static printf this
    does not model (one built at run time is skipped) and past the bounds (r3)."""
    if depth > 16:
        raise _FedUnknown
    gin = None  # `{ cat; } <<< x | bash`: the group's own stdin
    last = unit[-1]
    if not last.words[_py_head_index(last.words):] and (last.herestrs or last.heredocs):
        gin = _fed_own_stdin(last, raws)
    outs = [""]
    for c in unit:
        texts = _fed_one_producer(c, src, raws, depth, gin)
        if texts:
            outs = _fed_zip(outs, texts)
    return [t for t in outs if t]


def _fed_upstream(c: _PyCmd, src: dict, raws: list, depth: int, gin) -> list:
    """What `c` reads on stdin: its own here-strings/heredocs, else its pipe, else its
    group's stdin."""
    own = _fed_own_stdin(c, raws)
    if own:
        return own
    unit = src.get(id(c))
    if unit is not None:
        return _fed_producer_texts(unit, src, raws, depth + 1)
    return list(gin or [])


def _fed_one_producer(c: _PyCmd, src: dict, raws: list, depth: int, gin=None) -> list:
    full = _fed_words(c)
    xs = _fed_xargs_split(full)
    words = xs[1] if xs else _ho_static_command_words(full)
    if xs:
        spec = _fed_xargs_spec(xs[0])
        if spec is None:
            return []
        outs = [""]
        for text in _fed_upstream(c, src, raws, depth, gin):
            for inv in _fed_xargs_invocations(spec, words, text):
                texts = _fed_command_output(inv, _fed_dynamic(c))
                if texts:
                    outs = _fed_zip(outs, texts)
        return [t for t in outs if t]
    if not words:
        return []
    head = os.path.basename(words[0])
    if head in ("printf", "echo"):
        return _fed_command_output(words, _fed_dynamic(c))
    if not (head == "tee" or (head == "cat" and all(w[:1] == "-" or w in _FED_STDIN_PATHS for w in words[1:]))):
        return []
    # `printf x | cat | bash`: cat (any options) and tee pass their stdin on
    return _fed_upstream(c, src, raws, depth, gin)


def _fed_command_output(words: list[str], dyn: bool) -> list:
    head = os.path.basename(words[0])
    if head == "printf":
        try:
            return [_fed_printf(words[1:])]
        except _FedUnknown:
            if dyn:
                return []
            raise
    if head == "echo":
        return _fed_echo(words[1:])
    return []


def _fed_group(ev: list, j: int) -> list:
    """The commands of the producer ending at event `j`: one command, or every command of
    the `( ... )` (j at `)`) or `{ ...; }` (j at a `}` command) group closing there."""
    e = ev[j]
    if e == ")":
        depth, k = 0, j
        while k >= 0:
            if ev[k] == ")":
                depth += 1
            elif ev[k] == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        return [x for x in ev[k + 1:j] if isinstance(x, _PyCmd)]
    if isinstance(e, _PyCmd):
        if e.words[_py_head_index(e.words):] or "}" not in e.words:
            return [e]
        depth, k = 0, j
        while k >= 0:
            x = ev[k]
            if isinstance(x, _PyCmd):
                depth += x.words.count("}") - x.words.count("{")
                if depth <= 0:
                    break
            k -= 1
        return [x for x in ev[max(k, 0):j + 1] if isinstance(x, _PyCmd)]
    return []


def _fed_raw_herestrings(text: str) -> list[str]:
    """Every `<<<` word in `text` as written (one quoting level removed): the read of a
    here-string the lexer could not spell (a superset: one inside quotes counts too).
    Past 64 of them: refused (r3)."""
    out = []
    for m in _FED_HERESTR.finditer(text):
        if len(out) >= 64:
            raise _FedUnknown
        out.append(_shell_word(text, m.end()))
    return out


def _fed_lex_codes(lex: _PyLexer, text: str) -> list:
    """The texts fed on stdin to the commands of `lex` that run them, as code."""
    ev = lex.events
    src: dict = {}  # id(cmd) -> the producer unit piped into it
    out: list = []
    top = set()
    raws = None
    memo: dict = {}
    for idx, e in enumerate(ev):
        if not isinstance(e, _PyCmd):
            continue
        top.add(id(e))
        j = idx - 1
        while j >= 0 and ev[j] in ("\n", "("):
            j -= 1
        if j >= 0 and ev[j] == "|" and not (j >= 1 and ev[j - 1] == "|"):
            unit = _fed_group(ev, j - 1) if j >= 1 else []
            if unit:
                src[id(e)] = unit
        key = (tuple(e.words), tuple(sorted(e.dyn)))  # r3: same words, same consumer
        run = memo[key] if key in memo else memo.setdefault(key, _fed_consumer(e))
        if run is None:
            continue
        if raws is None:
            raws = _fed_raw_herestrings(text) if any(w is None for x in lex.cmds for w in x.herestrs) else []
        texts = _fed_own_stdin(e, raws)
        if id(e) in src:
            texts += _fed_producer_texts(src[id(e)], src, raws)
        out.extend(run(t) for t in texts if t)
    for c in lex.cmds:  # a shell inside `$(...)` with its own here-string or heredoc
        if id(c) not in top and (c.herestrs or c.heredocs):
            run = _fed_consumer(c)
            if run is not None:
                if raws is None:
                    raws = _fed_raw_herestrings(text)
                out.extend(run(t) for t in _fed_own_stdin(c, raws) if t)
    return out


def _fed_subst_bodies(text: str) -> list[str]:
    """The outermost `$(...)` and backtick bodies of `text` (quotes skipped; a superset:
    one inside single quotes counts too). Linear: each body is skipped once found."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            out.append(text[i + 1:j])
            i = j + 1
            continue
        if c == "$" and text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d in "'\"":
                    k = text.find(d, j + 1)
                    j = n if k < 0 else k + 1
                    continue
                depth += (d == "(") - (d == ")")
                j += 1
            out.append(text[i + 2:j - 1 if not depth else j])
            i = j
            continue
        i += 1
    return out


def _fed_may_run_code(c: _PyCmd) -> bool:
    """Cheap prefilter: `c` has a word `_py_code_strings` could read a code string after."""
    # r3: every code string follows `eval` or an option word (`-c`, `--command`, `-S`)
    return any(w[:1] in ("-", "+") or w == "eval" for w in c.words) and any(
        "sh" in w or "eval" in w or "script" in w or "flock" in w or "env" in w for w in c.words)


def _fed_wanted(text: str) -> bool:
    return ("|" in text or "<<" in text) and any(k in text for k in _FED_PREFILTER)


@functools.lru_cache(maxsize=8)  # decide() reads the same command in up to two passes
def _stdin_fed_codes(command: str) -> tuple[tuple[str, ...], tuple, bool]:
    """(texts, chdir chains, refused): the texts the shells in `command` read their commands
    from (see the block comment above); for each text read on the way, its literal chdir
    targets in order (None for one this cannot read); and whether a text could not be
    computed or the walk passed its bounds (fail closed)."""
    codes, chains, refused = _stdin_fed_codes_impl(command)
    return tuple(dict.fromkeys(codes)), tuple(dict.fromkeys(chains)), refused


def _stdin_fed_codes_impl(command: str) -> tuple[list[str], list[tuple], bool]:
    if not _fed_wanted(command):
        return [], [], False
    codes: list[str] = []
    chains: list[tuple] = []
    budget = 4 * len(command) + 4096
    work = [(command, 0)]
    queued = {command}
    while work:
        text, depth = work.pop()
        cached = _FED_LEX_CACHE[0]
        if cached is not None and cached[0] == text:
            lex = _FedLexView(cached[1].cmds[:cached[2]], cached[1].events)
        else:
            lex = _py_lex(text)
        try:
            found = [x for x in _fed_lex_codes(lex, text) if x]
        except _FedUnknown:
            return codes, chains, True
        nested = [code for c in lex.cmds if _fed_may_run_code(c) for code in (_py_code_strings(c) or [])
                  if _fed_wanted(code)]
        if "$(" in text or "`" in text:
            nested += [b for b in _fed_subst_bodies(text) if _fed_wanted(b)]
        # r5: a chain needs a word `_py_chdir` reads (cd, pushd, -C, --directory/--chdir)
        # or an eval holding one; `popd`/`source` alone only end a chain
        if (found or nested) and ("$'" in text or '$"' in text or _FED_CHDIR_HINT.search(
                text.replace("\\\n", "").translate(_QUOTE_STRIP))):
            chain = _fed_chain(lex.cmds, 0)
            if chain:
                chains.append(tuple(chain))
        for code in found + nested:
            if code in queued:
                continue  # each distinct text is read once
            queued.add(code)
            budget -= len(code)
            if depth >= _FED_DEPTH or budget < 0:
                return codes, chains, True
            work.append((code, depth + 1))
        # r3: the guard's own reading of a top-level command takes any `\`+newline as a
        # continuation, so `echo x\\` then a newline would hide the next line. An even run
        # of backslashes before a newline ends the line in bash; a blank line after it is
        # the same program, which every reading below then sees as two lines.
        codes.extend(_FED_EVEN_BS_NL.sub(r"\1\n\n", x) if "\\\n" in x else x for x in found)
        if len(set(codes)) > _FED_MAX_CODES:
            return codes, chains, True
    return codes, chains, False


def _fed_chain(cmds: list, depth: int) -> list:
    """The literal chdir targets of `cmds` in order (None for one this cannot read), with
    (r4) those of an `eval`'s string in its place (`eval 'cd ..'`, `eval cd ..`). r5: a
    `-c` string's own cd does not outlive it, so it is not counted here (that string's
    text, read as a command when it feeds a shell, carries its own cd). Past `_FED_DEPTH`
    nested strings, or past the candidate cap's worth of targets: None (unknown)."""
    out: list = []
    for c in cmds:
        out.extend(None if t == "-" or _py_dynamic(t) else t for t in _py_chdir(c.words))
        i = _py_head_index(c.words)
        if i < len(c.words) and c.words[i] == "eval":
            code = _fed_eval_code(c, c.words[i:])
            if code is None or depth >= _FED_DEPTH or len(out) > 4 * _FED_MAX_CANDS:
                out.append(None)
            elif "cd" in code or "-C" in code or "pushd" in code or "--" in code:
                out.extend(_fed_chain(_py_lex(code).cmds, depth + 1))
    return out


def _fed_cands(cwd: str, chains: tuple) -> list[str] | None:
    """The payload cwd and, for each chain of literal chdir targets (in text order), the
    directory after each step taken in sequence and each target from the payload cwd
    (r3: linear in the targets; a 6-cd chain is 13 candidates), or None past
    `_FED_MAX_CANDS`. A target this cannot read leaves the sequence unknown until an
    absolute one."""
    start = _py_norm(cwd) if cwd and os.path.isabs(cwd) else cwd
    paths = [start]
    seen = {start}
    base = start if start and os.path.isabs(start) else None
    for chain in chains:
        cur = base
        for t in chain:
            if t is None:
                cur = None
                continue
            absolute = t[:1] in ("/", "~") or _py_static_prefix(t) in ("$HOME", "${HOME}")
            step = _py_resolve(t, None) if absolute else (_py_resolve(t, cur) if cur else None)
            direct = None if absolute or not base else _py_resolve(t, base)
            for r in (step, direct):
                if r and r not in seen and len(r) <= 4096:
                    seen.add(r)
                    paths.append(r)
            cur = step
            if len(paths) > _FED_MAX_CANDS:
                return None
    return paths


# T-4491: the lexer walk below costs a full second pass over every command, so
# `_py_cannot_match` first rules out, on the text alone, the commands for which
# `_py_startup_write_match` cannot return anything but None. Every character
# of a lexed word is text from the command, and two characters next to each
# other in a word are separated in the text by nothing but quote characters,
# backslashes and `\`+newline pairs (`$(...)` values are marker characters;
# `$'...'` is the one construct that makes new text, so it is never skipped).
# So once those are dropped, anything a word, a `bash -c`/`eval` string or a
# fail-closed tail spells is a substring of the stripped text. A non-None
# result needs one of:
#   - a code string (`eval`, or a `-c`/`+c` option, or a wrapper's `-c`/`-S`/
#     `--...` value): `eval`, `-` or `+`;
#   - a candidate cwd beyond the payload cwd: `cd`/`pushd`, a `-C`/`--...`
#     option (git, make, tar, env) or `CDPATH` (a `popd`/`source`/`.` target
#     is dynamic: it adds no candidate);
#   - a word that names a site path or customize module: `.local`,
#     `customize` or `-packages`; one that climbs (`..`) to a relevant dir; a
#     `~`/`$HOME` path; an absolute path in the running guard's dir, which
#     spells that dir's last component;
#   - the computed-site-dir or pip spellings (`_PY_SITE_QUERY`, whose `--`
#     forms need `-`; `_PIP_OUTSIDE_VENV_FLAG` needs `--`);
#   - a payload cwd that is itself relevant (`_PyCand.relevant`): a plain
#     relative word is judged against that cwd only then.
# Anything else keeps the full walk.
_PY_SKIP_TRIGGERS = (
    "-", "+", "~", "..", "cd", "pushd", "eval", "CDPATH", "HOME", ".local", "customize",
    "sitepackages", "getuserbase", "USER_SITE", "USER_BASE", "USERBASE", "purelib", "platlib", "PIP_",
)


def _py_cannot_match(command: str, cwd: str) -> bool:
    """True only if `_py_startup_write_match(command, cwd)` is certainly None
    (see the comment above); False means "walk it"."""
    joined = command.replace("\\\n", "")
    # `$'..'` decodes, and the arithmetic forms drop or replace their text
    # (`x$[]y` lexes to `xy`, `x$((0))y` to `x0)y`), so a lexed word need
    # not be a substring of the stripped text: walk those (T-4491 r2).
    if "$'" in joined or "$[" in joined or "$((" in joined:
        return False
    names = {os.path.basename(d) for d in (_HOOK_DIR, _HOOK_DIR_LEXICAL)}
    if "" in names:
        return False
    text = joined.translate(_QUOTE_STRIP)
    if any(t in text for t in _PY_SKIP_TRIGGERS) or any(n in text for n in names):
        return False
    if cwd and os.path.isabs(cwd):
        return not _PyCand(_py_norm(cwd)).relevant()
    return True


def _py_judge_cmd(c: _PyCmd, cands: list, rel: list, memo: dict) -> int:
    """0, or why simple command `c` may write a protected path when it runs
    in one of `cands`: 1 an operand or redirect lands there (the caller's
    tag), 2 a computed site dir or a pip install outside a venv ("hit")."""
    for t in c.targets:
        if _py_word_hits(t, cands, rel, memo):
            return 1
    i = _py_head_index(c.words)
    if i >= len(c.words):
        return 0
    head = os.path.basename(c.words[i])
    args = c.words[i + 1:]
    if head in ("cd", "pushd", "popd"):
        return 0  # changes directory only; modelled by `_py_walk` / `_py_chain_clears`
    ro = _py_read_only(head, args)
    if ro is True:
        return 0
    if ro is not None:
        judged = ro
    elif head == "cp":
        judged = _py_cp_dests(args)
    else:
        judged = _drop_commit_message(c.words[i:])[1:] if head == "git" else args
    for w in judged:
        for piece in _PY_WORD_SPLIT.split(w):
            if _py_word_hits(piece, cands, rel, memo):
                return 1
            # A value glued to a short option (`mv -vtDIR`, `sort -oF`,
            # `curl -sSLoF`): the text after each option letter may be a path.
            m = _PY_OPT_CLUSTER.match(piece)
            if m:
                if m.end() > 65:
                    return 1  # a padded cluster: fail closed
                for k in range(2, m.end() + 1):
                    if _py_word_hits(piece[k:], cands, rel, memo):
                        return 1
    if ro is None:
        text = " ".join(c.words)
        if _PY_SITE_QUERY.search(text):
            return 2
        if _PIP_WORD.search(text) and _PIP_INSTALL.search(text) and _PIP_OUTSIDE_VENV_FLAG.search(text):
            return 2
    return 0


# --- T-4444: a plain list of `&&`-chains, read step by step ---
# The candidate set above ignores order, so `cd src && make && cd .. && cd
# docs && make && cd ..` is refused (its `..`s climb from every candidate).
# `_py_chain_clears` may only turn such a refusal into an allow, and only for
# a fail-closed shape whose cwd at each step bash fixes: top-level statements
# (`;`, newline, or a trailing lone `&` for one that changes no dir) of
# pipelines joined by `&&` only. A failed `&&` step stops the statement, so a
# command after `cd DIR &&` runs in DIR; after the statement ends, the next
# one may run in any directory it passed through. Anything else keeps the
# candidate set's verdict: `||`, a subshell, group or definition paren, a
# reserved word (if/for/while/case/`{`/`!`/time/coproc/...), a command word
# built at run time, a `builtin`/`command` prefix, eval/source/
# `.`/exec/pushd/popd/alias/shopt/trap/enable/declare/local, `set` with
# -P/physical, a cd with options other than `-L`/`--` or more than one
# operand, a cd inside a pipeline or a backgrounded statement, a CDPATH,
# HOME or PWD assignment, a fail-closed tail. A command that runs a code
# string is judged against the candidate set. A cd whose target the text
# does not spell exactly (a variable, `~`, `-`, a glob or brace) makes the
# cwd unknown until an absolute literal cd: unknown steps, a command's own
# `-C`-style dir that is not one literal, and every command not at the top
# level (`$(...)`, code strings) are judged against the candidate set, as
# before.
_PY_CHAIN_STOP_HEADS = frozenset({
    "pushd", "popd", "dirs", "source", ".", "eval", "exec", "alias", "unalias", "shopt", "trap",
    "enable", "declare", "typeset", "local", "readonly", "function", "for", "case", "select",
    "coproc",
})
_PY_CHAIN_FUZZY = re.compile(r"[*?\[\]{}\\]")  # a glob, a brace expansion, an escape


def _py_chain_stmts(ev: list) -> list | None:
    """Top-level events as statements `(pipelines, terminator)`, each
    pipeline a list of commands, the pipelines joined by `&&` only; None for
    any other shape (a paren, `||`, an empty step)."""
    stmts, pipes, pipe = [], [], []
    need = True  # a command must come next
    n = len(ev)
    k = 0
    while k < n:
        e = ev[k]
        if isinstance(e, _PyCmd):
            if not need:
                return None
            pipe.append(e)
            need = False
            k += 1
            continue
        if e == "&" and k + 1 < n and ev[k + 1] == "&":
            if need:
                return None
            pipes.append(pipe)
            pipe, need = [], True
            k += 2
        elif e == "|":
            if need or (k + 1 < n and ev[k + 1] == "|"):
                return None
            need = True
            k += 1
        elif e in (";", "\n", "&"):
            if need:
                if pipe or pipes or e == "&":
                    return None
                k += 1
                continue
            pipes.append(pipe)
            stmts.append((pipes, e))
            pipes, pipe, need = [], [], True
            k += 1
            continue
        else:
            return None  # `(` / `)`
        while k < n and ev[k] == "\n":
            k += 1  # `a &&` / `a |` may continue on the next line
    if pipe:
        pipes.append(pipe)
        stmts.append((pipes, None))
    elif pipes:
        return None
    return stmts


def _py_chain_cd(c: _PyCmd, i: int):
    """For a cd at word `i` of `c`: its exact target, "" when the target is
    not spelled exactly (the cwd becomes unknown), None to refuse the chain."""
    args = c.words[i + 1:]
    j = 0
    while j < len(args) and args[j] in ("-L", "--"):
        j += 1
        if args[j - 1] == "--":
            break
    ops = args[j:]
    if len(ops) != 1 or any(a not in ("-L", "--") for a in args[:j]):
        return None
    t = ops[0]
    if (i + 1 + j) in c.dyn or not t or t[0] in "~$-" or _py_dynamic(t) or _PY_CHAIN_FUZZY.search(t) \
            or _PY_SUBST in t or _PY_PWD_MARK in t:
        return ""
    return t


def _py_chain_clears(lex: _PyLexer, cwd: str, text: str, old_paths) -> bool:
    """True when `lex` has the shape above and no command, judged at the
    cwds bash can run it in, writes a protected path. `old_paths` is the
    candidate set (None: it overflowed) for the commands judged against it."""
    tails = lex.tails if lex.code_tails is None else lex.code_tails
    if not cwd or not os.path.isabs(cwd) or tails or "CDPATH" in text or "HOME=" in text \
            or "PWD=" in text or _py_lexed_defs(lex):
        return False
    stmts = _py_chain_stmts(lex.events)
    if not stmts:
        return False
    memo: dict = {}
    cand_of: dict = {}

    def cand(p):
        x = cand_of.get(p)
        if x is None:
            x = cand_of[p] = _PyCand(p)
        return x

    def clean(c, paths) -> bool:
        if paths is None:
            return False
        cs = [cand(p) for p in paths]
        return _py_judge_cmd(c, cs, [x for x in cs if x.relevant()], memo) == 0

    top = set()
    cur = frozenset({_py_norm(cwd)})  # None: not known (judge against `old_paths`)
    for pipes, term in stmts:
        passed = set(cur) if cur is not None else None
        for pipe in pipes:
            for c in pipe:
                top.add(id(c))
                w = c.words
                i = _py_head_index(w)
                if any(x in _PY_RESERVED or x in ("builtin", "command") for x in w[:i + 1]):
                    return False  # `command -v cd DIR` changes nothing
                head = w[i] if i < len(w) else ""
                if i in c.dyn or _PY_SUBST in head or _PY_PWD_MARK in head:
                    return False  # `$CD DIR`: a command word built at run time may be cd
                if head in _PY_CHAIN_STOP_HEADS or os.path.basename(head) in _PY_CHAIN_STOP_HEADS:
                    return False
                if head == "set" and any("P" in a or "physical" in a for a in w[i + 1:]):
                    return False
                if head == "cd":
                    if len(pipe) > 1 or term == "&":
                        return False  # a subshell's cd
                    if not clean(c, cur if cur is not None else old_paths):
                        return False  # its redirects
                    t = _py_chain_cd(c, i)
                    if t is None:
                        return False
                    if not t:
                        cur = None
                    elif t[0] == "/":
                        cur = frozenset({_py_norm(t)})
                    elif cur is not None:
                        cur = frozenset(_py_norm(os.path.join(b, t)) for b in cur)
                    if cur is not None and any(len(p) > 4096 for p in cur):
                        return False
                    if passed is not None:
                        if cur is None:
                            passed = None
                        else:
                            passed |= cur
                            if len(passed) > _PY_MAX_CANDS:
                                return False
                    continue
                # A command's own dir (`git -C`, `make -C`, `tar -C`, `env -C`).
                # One that runs a code string (`bash -c 'cd D && cp x "$1"' _ ARG`)
                # may use its words wherever that code goes: the candidate set.
                own = _py_chdir(w) if head else []
                if cur is None or len(own) > 1 or any(_py_dynamic(t) for t in own) \
                        or _py_code_strings(c) != []:
                    paths = old_paths
                elif own:
                    paths = set(cur) | {r for b in cur for r in [_py_resolve(own[0], b)] if r}
                else:
                    paths = cur
                if not clean(c, paths):
                    return False
        if term != "&":
            cur = frozenset(passed) if passed is not None else None
    # Commands not at the top level: `$(...)` bodies and code strings.
    for c in lex.cmds:
        if id(c) not in top and not clean(c, old_paths):
            return False
    return True


def _py_startup_write_match(command: str, cwd: str = "") -> str | None:
    """A reason tag ("hit"; "dyncd" when a dynamic cd kept the payload cwd
    in play; "dyncode" for a code string built at run time) if `command` may write a Python startup-hook path or the running
    guard's directory (see the block comment above); None otherwise. Every
    simple command is judged on its own, against its own candidate cwds; a
    refusal is lifted only by `_py_chain_clears` (T-4444)."""
    if _py_cannot_match(command, cwd):
        return None
    lex = _py_lex(command)
    tails = _py_expand_code(lex)
    if tails is None:
        return "dyncode"
    paths, dynamic_seen = _py_walk(lex, cwd, command)
    why = _py_judge_all(lex, tails, paths, dynamic_seen)
    if why is not None and _py_chain_clears(lex, cwd, command, paths):
        return None
    return why


def _py_judge_all(lex: _PyLexer, tails: list[str], paths, dynamic_seen: bool) -> str | None:
    """The candidate-set verdict: every command and tail against `paths`."""
    if paths is None:
        return "hit"
    memo: dict = {}
    cands = [_PyCand(p) for p in paths]
    rel = [c for c in cands if c.relevant()]
    tag = "dyncd" if dynamic_seen else "hit"
    for c in lex.cmds:
        got = _py_judge_cmd(c, cands, rel, memo)
        if got:
            return tag if got == 1 else "hit"
    for t in tails:
        for src in {t, t.translate(_QUOTE_STRIP)}:
            for piece in _PY_WORD_SPLIT.split(src):
                if _py_word_hits(piece, cands, rel, memo):
                    return "hit"
    return None


def _py_startup_path(path: str, cwd: str) -> bool:
    """True if an Edit/Write/patch target `path`, resolved against `cwd` and
    through symlinks, is a protected startup path or lies in `_HOOK_DIR`."""
    try:
        resolved = _abs_word(path, cwd) or path
        candidates = [path, resolved]
        if os.path.isabs(resolved):
            candidates.append(os.path.realpath(resolved))
        return any(
            _py_startup_segment_kind(c) or (os.path.isabs(c) and _under_hook_dir(os.path.normpath(c)))
            for c in candidates
        )
    except Exception:
        return True  # fail closed: an unresolvable target is not waved through


_PY_STARTUP_WHAT = (
    "to write a Python startup-hook path: the user or system site-packages, a "
    "`usercustomize`/`sitecustomize` module, a `.pth` file in a site dir, or the "
    "running guard's own directory ({hook_dir}); or to pip-install outside a venv "
    "(`--user`, `--break-system-packages`), or to write a site dir computed at run "
    "time. Code there runs at interpreter start, BEFORE this guard, on every later "
    "hook call (T-4397, a whole-guard bypass of T-1345). Install into a venv "
    "instead (`python3 -m venv D && D/bin/pip install ...`); edit tools/hooks in "
    "your own worktree. A single ls/cat/grep with no redirect passes. If these "
    "words are only file content, write it with the Write tool, or pass a note as "
    "`--note \"$(cat file)\"`"
)
# Added for a code string this hook cannot read (r5).
_PY_DYNCODE_HINT = (
    ". A `bash -c`/`sh -c`/`eval` code string built at run time (`bash -c \"$CMD\"`, "
    "`eval \"$(...)\"`) or nested more than " + str(_PY_CODE_DEPTH) + " levels deep "
    "can't be checked: spell the command out literally"
)
_PY_UNREADABLE_HINT = (
    ". This check could not read the command (e.g. a `~` joined to a `$(...)`), "
    "so it denies: spell the path out literally"
)
# Added when a cd this hook cannot follow kept the starting directory in play.
_PY_DYNCD_HINT = (
    ". A `cd` whose target is not a literal path (`cd \"$D\"`, `cd -`, `popd`, "
    "`~-`) left the starting directory as a possible cwd for the relative path: "
    "use `cd /abs/worktree && ...` with a literal absolute path"
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
#
# T-4145: git's global options may precede `push` (`git -C /x push`, `git -c k=v
# push`, `git --git-dir=... push`, `git --work-tree=... push`), the same
# leading-flag class as `gh -R o/r pr merge`, so they are skipped before `push`.
# That skip is a single forward token walk (`_git_push_attempt`), not a regex:
# a regex quantifier over option/value pairs backtracked exponentially on
# `git -C -C -C ...` (review round 2), and a hook timeout is non-blocking, so a
# slow guard fails OPEN.
# The `gh pr create|merge` half stays as a raw-text floor UNDER the gh read
# allowlist (`_gh_cli_violation`, below): defense in depth for spellings the
# command-position parser does not recognize (T-4145 review round 1).
_PUSH = re.compile(r"\bgh\s+pr\s+(create|merge)\b")
# One shell word, or one separator as its own token, for the linear walkers.
_WALK_TOKEN = re.compile(r"[;&|()`\n]|[^\s;&|()`]+")
_GIT_WORD = re.compile(r"\bgit$")
_GIT_VALUE_OPTS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env",
})
_GIT_FLAG_OPT = re.compile(r"-{1,2}[A-Za-z][\w-]*(?:=\S+)?")
_PUSH_WORD = re.compile(r"push\b")
# T-5425: one shell redirection with its target (`>/dev/null`, `2>&1`, `&>>log`,
# `<<<x`, `> "a b"`). bash lets a redirection sit anywhere in a simple command, so
# `git 2>/dev/null push` pushes, but the walk stopped at `2>` and never reached
# `push`. An fd number, or a bash named fd (`{fd}>f`, r2), counts only at a word
# start, so `a{fd}>f` keeps `a{fd}` as a word. Possessive and atomic throughout,
# so each match reads its run once (a hook timeout fails OPEN).
_GIT_WALK_REDIRECT = re.compile(
    r"(?:(?<![^\s;&|()`<>])(?:\d++|\{[A-Za-z_]\w*+\})|&)?"
    r"(?>&>>|&>|<<<|<<-|<<|<>|<&|<|>>|>&|>\||>)[ \t]*+"
    r"(?:\"[^\"]*+\"|'[^']*+'|[^\s;&|()`<>\"'])++")


def _git_push_attempt(surface: str) -> bool:
    """True if `surface` holds `git [global options] push`, as written or with
    its shell redirections removed (T-5425)."""
    # T-5425: judge BOTH copies. The stripped copy finds `git >/dev/null push`;
    # the original keeps every deny stripping would lose (`echo >git push`
    # strips to `echo  push`), so the change can only add denies.
    if _git_push_walk(surface):
        return True
    stripped = _GIT_WALK_REDIRECT.sub(" ", surface)
    return stripped != surface and _git_push_walk(stripped)


def _git_push_walk(surface: str) -> bool:
    """One forward pass: after a `git` word the options are skipped, and the
    scan resumes at the first word that is not an option (an option's value is
    never a command)."""
    toks = _WALK_TOKEN.findall(surface.replace("\\\n", " "))
    i, n = 0, len(toks)
    while i < n:
        if not _GIT_WORD.search(toks[i]):
            i += 1
            continue
        i += 1
        while i < n:
            tok = toks[i]
            if tok in _GIT_VALUE_OPTS:
                i += 2
            elif _GIT_FLAG_OPT.fullmatch(tok):
                i += 1
            else:
                break
        if i < n and _PUSH_WORD.match(toks[i]):
            return True
    return False


# T-2716 (2026-08-09): `_PUSH.search(command)` used to run on the RAW command
# string, so a `git commit` whose -m/--message text merely DISCUSSES push/PR/merge
# (e.g. documenting a `--no-verify-push` flag) false-positived as a push attempt.
# Same shape-vs-substance fix T-1600 already established for `_ho_records_match`:
# for a recognized `git commit` (or `git -C <dir> commit`), drop the message value
# before matching — message text is not a file operand or a real subcommand.
# `_drop_commit_message` is a no-op for any shape that isn't a plain `git commit`,
# so a real `git push` / `gh pr create` / `gh pr merge` invocation is untouched and
# still matches normally. Falls back to the raw-string match on redirection,
# compound shell structure, or unparseable quoting — fail-closed, identical
# discipline to `_ho_records_match`.
def _push_match(command: str) -> bool:
    """True if `command`'s genuine argument surface attempts a push/PR/merge.
    Excludes a `git commit`'s -m/--message text (T-2716 false positive)."""
    surface = _argument_surface(command)
    return _git_push_attempt(surface) or bool(_PUSH.search(surface))


def _argument_surface(command: str) -> str:
    """The text a raw-text rule matches: a single plain invocation's tokens
    rejoined with any `git commit` message dropped (T-2716), or the whole raw
    command on redirection, compound structure or unparseable quoting
    (fail-closed)."""
    if _REDIRECT_RE.search(command):
        return command
    tokens = _tokens_or_none(command)
    if not tokens:
        return command
    return " ".join(_drop_commit_message(tokens))

# --- Git identity/config: blocked for ANY subagent (T-1687) ---
# 2026-07-12: a worker ran `git config --global user.*` (on/before 07-06),
# misattributing ~6 days of commits account-wide as `reviewer <reviewer@local>`
# (bugs/subagent-git-config-global-identity-pollution.md). Workers never touch git
# identity: any --global/--system config WRITE surface is blocked outright (workers
# have no legitimate need even for the read form via Bash — over-block is harmless
# under the drift model), and user.name/user.email config aimed at a SHARED main
# checkout (cwd under /home/claude/<repo>, or an explicit `git -C /home/claude/...`)
# is blocked too. Worktree-local identity under /tmp stays ALLOWED — a dispatch
# environment may legitimately set per-worktree identity there.
#
# T-2323 — KNOWN OVER-BLOCK, deliberately not "fixed" by parsing: `cwd` is the
# SESSION's directory as the harness reports it, not the command's effective
# target. So a worker whose session sits in a shared checkout is blocked even for
# `cd /tmp/scratch && git config user.email ...`, because the `cd` is invisible
# here. Measured (T-2323): cwd=/home/claude/pantheon-config + a /tmp-targeted
# command -> BLOCKED; the same command from a /tmp cwd -> allowed.
#
# Tightening this by parsing a leading `cd` was considered and REJECTED: `cd /tmp
# && cd /home/claude/repo` defeats naive parsing, and a false NEGATIVE here
# re-opens the exact account-wide misattribution T-1687 exists to prevent. An
# over-block that names its own workaround is strictly safer than an under-block
# that does not know it happened.
#
# The over-block is therefore made SURVIVABLE rather than removed: the denial text
# names `git -c user.email=...` and the GIT_AUTHOR_*/GIT_COMMITTER_* env vars,
# both of which pass this guard in every cwd and both of which set identity for a
# single invocation WITHOUT writing it to any config file — which is the property
# being protected, so they are the sanctioned route, not a loophole. Two workers
# hit this on 2026-07-29 doing Amendment H falsification in throwaway repos; both
# correctly surfaced the block instead of routing around it, and both then had to
# rediscover the alternative themselves. The message now carries it.
# Matching stays inside one command segment (bounded by ;, |, &) so
# `git log; foo --global` style compounds don't cross-match (drift model, not
# adversarial).
#
# T-4317 — LINEAR TIME. This rule used to be two regexes of the shape
# `\bgit\b[^;|&]*\bconfig\b[^;|&]*<key>`. Two unbounded gaps between three
# anchors backtrack: every `git` start rescans the rest of the segment for
# every `config` candidate, so `'git -C '*3000` took ~5s and `'git config '*3000`
# ran past 10s. Claude Code treats a hook TIMEOUT as non-blocking, so a slow
# guard fails OPEN. The rule is now a walk over segments: in each segment take
# the FIRST `git`, the first `config` after it, then look for the key after
# that. Taking the earliest anchor is exact, not a heuristic: any later `git`
# (or `config`) that could complete a match leaves a suffix the earliest one
# also sees, so existence is unchanged. Each segment is scanned a constant
# number of times, so the whole walk is linear. `search(pos, endpos)` keeps
# `\b` identical to the old whole-string regex: it reads the character before
# `pos`, and `endpos` always sits on a separator (a non-word character) or the
# end of the string.
_IDENTITY_SEGMENT_SEP = re.compile(r"[;|&]")
_IDENTITY_GIT_WORD = re.compile(r"\bgit\b")
_IDENTITY_CONFIG_WORD = re.compile(r"\bconfig\b")
# T-5817: the identity rule's `config` anchor is the `git config` command word,
# never the `--config-env` OPTION. `git --config-env=user.email=VAR commit` sets
# identity for that one invocation from an environment variable, exactly like
# `git -c user.email=... commit` (which never matched: it has no `config` word),
# and writes no config file, so it is the sanctioned one-shot route the denial
# text names, not a persistent write. Every persistent spelling still carries a
# real `config` word (`git config`, `git-config`, `git --config-env=... config
# user.email x`: the scan skips the option and anchors on the later word), so
# nothing that writes config is newly allowed. Only the exact `--config-env`
# option is skipped: a bare `config-env` word still anchors, since it could be a
# git alias for `config`. The account-wide-scope rule
# keeps the plain `\bconfig\b` anchor unchanged (no decision there moves).
_IDENTITY_CONFIG_SUBCOMMAND = re.compile(r"\bconfig\b(?!(?<=--config)-env\b)")
_GIT_CONFIG_GLOBAL = re.compile(r"--(?:global|system)\b")
_GIT_CONFIG_IDENTITY = re.compile(r"\buser\.(?:name|email)\b")
# T-5817: the head orchestrator's account home. Its main checkouts
# (`<home>/<repo>`) are the SHARED checkouts T-1687 protects; every other seat
# works in its own home. One definition, read at CALL time by
# `_under_ho_home` and `_shared_checkout_res`, so a test points it elsewhere
# (unittest.mock.patch.object) and covers the shared-checkout branch on any
# machine, instead of only on the seat whose checkout happens to sit there.
# Not read from the environment: a worker controls its own env.
_HO_HOME = "/home/claude"
# T-4963: the `-C` operand itself, read again after normalisation (quotes
# stripped, `_py_norm` folding `//`, `/./` and `..`), so another spelling of
# the same shared checkout classifies the same. `\S+` keeps the scan linear.
_GIT_C_OPERAND = re.compile(r"\bgit\s+-C\s+(\S+)")


def _under_ho_home(path: str) -> bool:
    """True if `path` lies under the head orchestrator's home (`_HO_HOME`)."""
    return path.startswith(_HO_HOME.rstrip("/") + "/")


@functools.lru_cache(maxsize=4)
def _shared_checkout_res(ho_home: str) -> tuple[re.Pattern, re.Pattern]:
    """(`git -C <home>/<repo>` in raw text, `<home>/<repo>` at the start of a
    normalised operand) for `ho_home`: the pre-T-5817 literals, built from it."""
    home = re.escape(ho_home.rstrip("/"))
    return (re.compile(r"\bgit\s+-C\s+" + home + r"/[^/\s]+"),
            re.compile(home + r"/[^/]+"))


def _targets_shared_checkout(command: str) -> bool:
    """True if a `git -C <dir>` in `command` names a `<_HO_HOME>/<repo>` checkout."""
    raw_c, path_re = _shared_checkout_res(_HO_HOME)
    if raw_c.search(command):
        return True
    for m in _GIT_C_OPERAND.finditer(command):
        op = m.group(1).strip("'\"")
        if op.startswith("/") and path_re.match(_py_norm(op)):
            return True
    return False


def _git_config_segment_has(command: str, key: re.Pattern,
                            config_word: re.Pattern = _IDENTITY_CONFIG_WORD) -> bool:
    """True iff one ;|&-free segment of `command` holds `git`, then `config`, then `key`.

    Same answer as the pre-T-4317 `\\bgit\\b[^;|&]*\\bconfig\\b[^;|&]*<key>` search,
    in linear time (see the T-4317 note above). `config_word` is the anchor
    (T-5817: the identity rule passes one that skips the `--config-env` option);
    the earliest-anchor argument holds for it too, since whether a `config`
    occurrence qualifies depends only on the few characters around it.
    """
    pos, n = 0, len(command)
    while pos <= n:
        git = _IDENTITY_GIT_WORD.search(command, pos)
        if git is None:
            return False
        sep = _IDENTITY_SEGMENT_SEP.search(command, git.end())
        end = sep.start() if sep else n
        config = config_word.search(command, git.end(), end)
        if config is not None and key.search(command, config.end(), end) is not None:
            return True
        pos = end + 1
    return False


def _git_identity_match(command: str, cwd: str) -> str | None:
    """Reason string if `command` touches worker-forbidden git config surface, else None.

    Read-only single invocations (grep/cat/... mentioning these strings) stay allowed
    via the same allowlist machinery `_ho_records_match` uses.
    """
    tokens = _tokens_or_none(command)
    if tokens and _is_read_only_verb(tokens):
        return None
    if _git_config_segment_has(command, _GIT_CONFIG_GLOBAL):
        return (
            "to touch --global/--system git config (account-wide state; workers never "
            "touch git identity — T-1687 after the 6-day misattribution incident)"
        )
    if _git_config_segment_has(command, _GIT_CONFIG_IDENTITY, _IDENTITY_CONFIG_SUBCOMMAND):
        in_shared_cwd = _under_ho_home(cwd)
        targets_shared = _targets_shared_checkout(command)
        if in_shared_cwd or targets_shared:
            return (
                "to set persistent git user identity (`git config user.name/user.email`)"
                " — T-1687, after a worker's identity write misattributed ~6 days of "
                "commits account-wide. "
                "USE ONE OF THESE INSTEAD, both allowed here and both sufficient to "
                "commit in a throwaway repo: `git -c user.email=you@example.com "
                "-c user.name=you commit ...`, or export GIT_AUTHOR_EMAIL / "
                "GIT_AUTHOR_NAME / GIT_COMMITTER_EMAIL / GIT_COMMITTER_NAME. Both set "
                "identity for the single invocation without writing it to any config "
                "file, which is the whole property this guard protects. "
                "NOTE the cwd caveat: this fires on your SESSION's cwd, so it triggers "
                "even when your command targets a scratch repo elsewhere (T-2323) — "
                "that is a known over-block, and the two forms above are the sanctioned "
                "route through it, not a workaround"
            )
    return None


# --- gh api: ANY mutating REST write ---
# T-4135 (row 13, rulebook/policies/access-by-operation-risk.md): `_PUSH` above only
# catches the `gh pr merge`/`gh pr create` CLI shapes. A subagent can reach the SAME
# HO-reserved outcomes (T-1673 push/merge; O10 visibility/admin) through the
# lower-level `gh api` escape hatch. Every REST write is HO-tier: workers push
# nothing, so they write nothing to GitHub. An endpoint allowlist was tried first
# and missed push-equivalents — `contents/<path>` PUT (a direct commit to any
# branch, main included), `git/refs/heads/main` PATCH (a force-update of main),
# `/merges` POST (a branch merge with no PR). That is the enumeration-fails shape
# this ticket exists to close, so the rule keys on the METHOD, not the endpoint.
#
# Method resolution, per gh's own semantics: the LAST -X/--method decides (gh
# honors the final occurrence); GET/HEAD stay reads even with -f, which then
# become query params. With no explicit method, a body flag (-f/-F/--field/
# --raw-field/--input) makes it POST; otherwise it is a GET. Evaluated per
# top-level shell segment holding the `gh api` call (see `_scan`), so a
# `| grep -F x` or `| jq -f prog` on a read doesn't count as a body flag, while
# `$(...)`, quotes and backslash-newline continuations stay inside the segment.
# Fail-closed on what this hook can't see: a `$(...)`/backtick substitution or a
# standalone `$VAR` word in the segment can expand to a method or body flag, so
# it counts as a write. A worker that needs a computed value in a read assigns it
# first (`PR=$(...); gh api repos/o/r/pulls/$PR`); a path-embedded `$VAR` is fine.
# The graphql endpoint is left to `_gh_api_graphql_mutation` below (every
# graphql call is a POST, reads included). HO is exempt (subagent-only rule), so
# HO's own mutating calls (tools/echo-merge.sh's ref DELETE, the push-hang
# workaround's git/refs POST) are unaffected.
# T-4145: gh takes its global -R/--repo/--hostname flags BEFORE the group, so
# `gh --hostname h api -X DELETE ...` is still a `gh api` call and must reach
# the method rule. (Any other leading flag is denied by the gh allowlist below.)
# Detected by a linear token walk (`_gh_word_positions`), not a regex: the
# regex form `(?:=|\s+)?\S+` parsed `--repo=o/r` two ways and backtracked
# exponentially on a run of them (review round 2).
_GH_WORD_TOKEN = re.compile(r"\bgh$")
_API_WORD = re.compile(r"api\b")


_QUOTE_STRIP = str.maketrans("", "", "'\"\\")


def _gh_word_positions(text: str, gh_word: re.Pattern = _GH_WORD_TOKEN, dequote: bool = False):
    """Yield (tokens, i) for each gh word in `text`, where tokens[i] is the
    first token after gh's leading -R/--repo/--hostname flags. One forward
    pass: the scan resumes at tokens[i], since a flag's value is never a
    command. `dequote` drops quote and backslash characters from every token
    first, so `'gh'`, `"gh"` and `\\gh` read as the gh word the shell runs."""
    toks = _WALK_TOKEN.findall(text.replace("\\\n", " "))
    if dequote:
        toks = [t.translate(_QUOTE_STRIP) for t in toks]
    i, n = 0, len(toks)
    while i < n:
        if not gh_word.search(toks[i]):
            i += 1
            continue
        i += 1
        while i < n:
            tok = toks[i]
            if tok in ("-R", "--repo", "--hostname"):
                i += 2
            elif tok.startswith(("--repo=", "--hostname=")) or (tok.startswith("-R") and len(tok) > 2):
                i += 1
            else:
                break
        yield toks, i


def _has_gh_api_call(text: str) -> bool:
    return any(i < len(toks) and _API_WORD.match(toks[i]) for toks, i in _gh_word_positions(text))
_GH_API_EXPLICIT_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)(?:=|\s*)(\S+)")
_GH_API_BODY_FLAGS = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:[=\s]|$)")
_GH_API_OPAQUE = re.compile(r"\$\(|`|(?:^|\s)['\"]?\$\{?[A-Za-z_]")
_GH_API_GRAPHQL_ENDPOINT = re.compile(r"(?:^|\s)['\"]?/?graphql['\"]?(?:\s|$)")
_GH_API_READ_METHODS = frozenset({"GET", "HEAD"})

# After one of these (unescaped, unquoted), a `#` starts a comment (`_scan`).
_SCAN_WORD_BREAK = frozenset(" \t\n;&|()<>")

# A quoted string handed to one of these is a command, not data.
_EXEC_QUOTE_PREFIX = re.compile(
    r"(?:\b(?:ba|z|da|k)?sh|\bsu)\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*c\s*$"
    r"|\beval\s*$"
    r"|\bssh\b[^;&|\n]*\s$"
)


# The `case` keyword as bash reads it: a backslash-newline may split or
# follow it (`ca\<nl>se a in`), since bash joins those before reading words (r5).
_SCAN_CASE_KW = re.compile(r"c(?:\\\n)*a(?:\\\n)*s(?:\\\n)*e(?:\\\n)*[ \t\n]")


class _Scanned(tuple):
    """`_scan`'s 5-tuple, unchanged, plus `comments` (T-4426 r3) and
    `bodies` (T-4389 B(1))."""

    comments: tuple[tuple[int, int], ...] = ()
    bodies: tuple[tuple[int, int, int, bool], ...] = ()


# T-5140: `_scan` is a state-machine over executable shell frames.  An input
# can otherwise make its stack proportional to attacker-controlled nesting and
# feed later readers an expensive malformed surface.  Keep this distinct from
# ordinary unbalanced syntax: callers must turn this sentinel into a specific
# fail-closed verdict, while malformed syntax retains its existing `None` path.
_SCAN_NEST_MAX = 128


class _ScanTooDeep:
    pass


_SCAN_TOO_DEEP = _ScanTooDeep()


# T-5140 r2: set whenever ANY scan made while judging a command (the command
# itself, or a text derived from it by a reader) reaches `_SCAN_TOO_DEEP`.
# Callers of `_scan` see `None` and fall back to a raw reading, which is not
# guaranteed to judge the shape as main did, so `decide()` turns a set latch
# into `_SHELL_NESTING_TOO_DEEP` on every allow.  Reset at the start of each
# `decide()` call.
_SCAN_OVERFLOWED = [False]


def _scan_depth_may_exceed(command: str) -> bool:
    """A necessary condition for `_scan_cached(t)` returning `_SCAN_TOO_DEEP`
    for `command` and for every text the overflow walk derives from it.

    Every frame `_scan_cached` pushes consumes one character of its input,
    and a character is pushed at most once (the index only moves forward):
    `$(` and a nested `(` consume a `(`; a backtick frame (unquoted or inside
    double quotes) consumes a backtick; every quote frame (data, executable
    `x` or dequoted-command-word `d`) consumes a `'` or `"`.  Arithmetic
    `$((` is a `$(` frame plus a `(` frame, one `(` each; `${...}` is a
    counter, not a frame.  Overflow needs more than `_SCAN_NEST_MAX` open
    frames, so more than that many of these four characters.

    r3: the walk's child texts are words as `_PyLexer` forms them (quote
    removal: characters only drop out) and heredoc bodies (slices), except
    that an ANSI-C `$'...'` word decodes escapes, and an escape can spell
    one of the four characters.  Each decoded character consumes at least
    one backslash, so when `$'` occurs the backslashes are counted too, and
    no derived text holds more frame characters than this bound."""
    if len(command) <= _SCAN_NEST_MAX:
        return False  # each counted character is one character of `command`
    n = (command.count("(") + command.count("`") + command.count("'")
         + command.count('"'))
    if "$'" in command:
        n += command.count("\\")
    return n > _SCAN_NEST_MAX


# T-4389 B(1): heredoc body text in `_scan`: one run of characters that are
# plain text there (no quote, escape, substitution or separator).
_SCAN_BODY_RUN = re.compile(r"[^'\"\\$`;&|\n]+")
_SCAN_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_SCAN_NAME_CHARS = _SCAN_NAME_START | frozenset("0123456789")
# What may follow a modelled delimiter word ("" is the end of the text).
_SCAN_DELIM_END = frozenset({"", " ", "\t", "\n", ";", "&", "|", ")", "<", ">"})


def _scan_heredoc_op(command: str, i: int) -> tuple[str, bool, bool] | None:
    """(delimiter, quoted, strip) for the heredoc operator `<<`/`<<-` at
    command[i], or None for a delimiter word this does not model, which
    leaves the operator unrecognized (the text scans as before T-4389 B(1)):
    no word, a substitution, `$'`/`$"`, a quote left open within the word's
    run, a run ending in an odd backslash run (it escapes what follows:
    `<<E\\ x` is the delimiter `E x`), or a run that stops at anything but a
    plain word end (`<<$(E)`, `<<$((1))` stop at `(`; a CRLF line's `\\r` is
    part of bash's delimiter). The delimiter is the word with quotes
    removed; any quoting makes the body unexpanded (bash)."""
    m = _NL_HEREDOC.match(command, i)
    run = _NL_WORD_RUN.match(command, m.end()).group()
    after = command[m.end() + len(run):m.end() + len(run) + 1]
    if (not run or "$(" in run or "`" in run or "$'" in run or '$"' in run
            or run.count("'") % 2 or run.count('"') % 2
            or (len(run) - len(run.rstrip("\\"))) % 2
            or after not in _SCAN_DELIM_END):
        return None
    word = _shell_word(run, 0)
    quoted = any(ch in "'\"\\" for ch in run)
    if not word and not quoted:
        return None
    return word, quoted, bool(m.group(1))


# T-4389 B(1) MF3 (see `decide()`): [False] while `decide()` judges a
# command a second time in base's reading (no heredoc model). Its ONLY
# readers are `_scan` (below) and `_NonLiteralLevel._skip_ranges`; a new
# reader must keep `_SCAN_SAW_LTLT` exact, or `decide()` may skip a pass
# that would differ.
_SCAN_HEREDOCS = [True]
# Set by `_scan` when a text it reads, raw or masked, holds `<<`: the only
# texts on which the two readings can differ. Reset by `decide()` before
# each pass and after the call.
_SCAN_SAW_LTLT = [False]


def _scan(command: str, overflow: list[bool] | None = None) -> tuple[tuple[tuple[str, str], ...], str, tuple[int, ...], dict[int, int], dict[int, int]] | None:
    """`_scan_cached` in the current reading (`_SCAN_HEREDOCS`). The `<<`
    observation is made here, outside the cache, so a cache hit records it
    too."""
    out = _scan_cached(command, _SCAN_HEREDOCS[0])
    if out is _SCAN_TOO_DEEP:
        _SCAN_OVERFLOWED[0] = True
        if overflow is not None:
            overflow[0] = True
        if "<<" in command:
            _SCAN_SAW_LTLT[0] = True
        return None
    if "<<" in command or (out is not None and "<<" in out[1]):
        _SCAN_SAW_LTLT[0] = True
    return out


@functools.lru_cache(maxsize=8)  # decide() scans the same command up to 3 times
def _scan_cached(command: str, heredocs: bool = True) -> _Scanned | _ScanTooDeep | None:
    """One pass over `command`, tracking quotes, `$(...)` and backticks.

    Returns (segments, masked, rawmap, spans, xspans):
      segments: `command` split on ; & | and newline only where they sit at top
        level (outside quotes and substitutions), as (raw, masked) pairs. A
        backslash-newline is a line continuation, not a split.
      masked: `command` with the text of every non-executable quoted string
        blanked, so a word inside a grep pattern or a commit message never reads
        as a command. A quoted string handed to an interpreter (sh/bash -c,
        su -c, eval, ssh) stays visible, and its opening quote becomes `;` so its
        first word sits at command position. A quote inside the COMMAND WORD
        itself (`"sudo" x`, `g"h" api`, `env 'sudo' x`) is dequoted instead:
        the shell runs `"sudo"` exactly as `sudo`, so its text stays visible and
        its quote characters drop out. `$(...)` and backtick bodies stay visible
        even inside double quotes: they run.
      rawmap (T-4145): for each character of `masked`, its index in `command`,
        so a command found in the masked text can be read back from the raw text.
      spans (T-4154): for each data string, the index in `masked` of its opening
        quote mapped to the index of its closing quote, so a walker can read a
        quoted word (`"a b"`) as one word and fetch its text through rawmap.
      xspans (T-4154): the same for each interpreter string, from its opening
        `;` to its closing quote, so a walker knows which words sit inside one.
    None if quoting or nesting is unbalanced; callers fall back to the raw
    command (fail-closed, like `_tokens_or_none`).
    The result also carries `.comments` (T-4426 r3): the raw [start, end)
    range of each comment the scan read, from its `#` to where it ended.

    Heredoc bodies (T-4389 B(1)) are read as bash reads them, and the result
    carries `.bodies`: (start, end, resume, expands) per body, raw indices,
    with [start, end) the body, [end, resume) its delimiter line, `expands`
    an unquoted delimiter. An operator `<<`/`<<-` (not `<<<`) counts where
    bash reads one: unquoted, outside comments and `${...}`, and not in
    arithmetic, where `<<` is a shift (`(( ))` at command position,
    `$(( ))`, `$[ ]`, a `NAME[...]` subscript). Its bodies start after the
    next unquoted newline of the same frame and are found by lines
    (`_heredoc_line_spans`). Body text is TEXT: a quote or backslash in it
    opens nothing (it is dropped from `masked`, as a dequoted command
    word's quotes are), `#` starts no comment and `)` closes no frame; only
    in an unquoted body do `$(...)` and backticks open (they run), and one
    still open at the delimiter line is a bash syntax error: None. The
    words stay visible in `masked`, so every rule still judges them as
    commands (the fail-closed reading kept from before B(1)). A body with
    no delimiter line runs to the end, as in bash (not None: that would
    mute the T-4426 rule for the commands before it). What this does not
    model is left unrecognized and scans as before: an operator inside
    backticks, an odd delimiter word (`_scan_heredoc_op`), an operator
    whose `$(`/`(` frame closes before its newline, a heredoc inside a
    body's substitution. The arithmetic tracking leans the same way: one
    that never closes only suppresses operators (the text scans as before).
    `heredocs=False` is the reading from before B(1) (no operator is
    recognized), which `decide()` adds as a second pass (MF3).
    """
    segs: list[tuple[str, str]] = []
    seg: list[str] = []
    masked: list[str] = []
    origin: list[int] = []  # raw index of each `masked` chunk (T-4145)
    spans: dict[int, int] = {}  # data string open -> close, masked indices (T-4154)
    data_open: list[int] = []
    xspans: dict[int, int] = {}  # interpreter string open -> close (T-4154)
    x_open: list[int] = []
    mlen = 0  # len("".join(masked))

    def emit(chunk: str) -> None:
        nonlocal mlen
        masked.append(chunk)
        origin.append(i)
        mlen += len(chunk)
    def close_backtick() -> None:
        """Pop the innermost backtick frame and every frame above it."""
        nonlocal ansi, in_comment, brace
        k, pk, bk = bt_idx.pop()
        brace = bsave[bk]
        del bsave[bk:]
        for fr in stack[k + 1:]:
            if fr in ("'", '"'):
                data_open.pop()
            elif fr in ("x'", 'x"'):
                x_open.pop()
        while q_idx and q_idx[-1] > k:
            q_idx.pop()
        del stack[k:]
        del parens[pk:]
        ansi = in_comment = False
        trim()

    # T-4389 B(1): heredocs. `hd_pending`: (frame depth, delimiter, quoted,
    # strip) per operator awaiting its line's newline, sorted by depth.
    # `bodies`: every body found; `hq` indexes the one being read (none when
    # hq == len(bodies)), at stack depth `hd_base`. Arithmetic, where `<<` is
    # a shift: `arith_marks` (a `$((`, or a `((` in a `$(`/`(` frame, open
    # while the stack is deeper than the mark), `arith_top` (a top-level
    # `((`'s paren depth: top-level parens push no frame), `arith_br` (`$[`
    # brackets) and `sub_br` (a `NAME[` subscript's brackets, to the line end).
    hd_pending: list[tuple[int, str, bool, bool]] = []
    bodies: list[tuple[int, int, int, bool]] = []
    hq = 0
    hd_on = False  # hq < len(bodies)
    hd_base = 0
    arith_marks: list[int] = []
    arith_top = arith_br = sub_br = 0

    def trim() -> None:
        """Drop what a closed frame left: its pending operators (never
        recognized, as before B(1)) and its arithmetic marks."""
        while hd_pending and hd_pending[-1][0] > len(stack):
            hd_pending.pop()
        while arith_marks and arith_marks[-1] >= len(stack):
            arith_marks.pop()
    mstart = 0  # index in `masked` where the current segment began
    # "'" / '"' (data), "x'" / 'x"' (executable), "d'" / 'd"' (dequoted command
    # word), "$(", "(", "`"
    stack: list[str] = []
    # T-4393 follow-up: bash's comment rule. A `#` that begins a word (after
    # the start, an unescaped unquoted metacharacter, `$(` or an opening
    # backtick) outside quotes and `${...}` starts a comment, and a quote
    # character in a comment is text, not a quote: before this, the `'` in
    # `true # it's` opened a quote that blanked the next line's command.
    # The comment text itself is NOT blanked (T-4375 r1: blanking regressed
    # other rules): inside a comment every character is plain text (nothing
    # is pushed or popped) until a newline (outside backticks, also an
    # escaped one) or a backtick that closes an open backtick (below).
    # Where the scan can fall out of step with bash (an unmatched `)`, the
    # `\\`/`\$` de-escaping of a backtick body) it returns None: the
    # conservative raw fallback beats modelling more of bash (T-4413).
    # Backticks: bash finds the end of `...` by scanning for the next
    # unescaped backtick, blind to quotes, comments and `$(` inside it (the
    # body is parsed later), so any unescaped backtick closes the innermost
    # open backtick, with every frame opened inside it (bash-verified:
    # `` echo `true # c` 'a<nl> b' ; printf x `` runs printf, and
    # `` `echo 'a` `` ends at the second backtick).
    word_start = True
    in_comment = False
    comments: list[tuple[int, int]] = []  # raw [start, end) of each comment (T-4426 r3)
    cstart = 0
    # Each open backtick: (stack index, len(parens), len(bsave)).
    bt_idx: list[tuple[int, int, int]] = []
    q_idx: list[int] = []  # stack index of each open quote frame
    # One entry per unquoted `(`/`$(` open: does its `)` end a word? Only a
    # subshell/group `)` does; after the `)` of `$(...)`, `$((...))` or
    # `<(...)` a `#` is mid-word (`$(echo a)#x` keeps `#x`, bash-verified).
    parens: list[bool] = []
    paren_popped = False  # some `)` has popped a `parens` entry
    redir_at = -2  # index of the last plain `<` or `>`
    brace = 0  # unquoted `${` depth: ` #` inside `${x// #/y}` is no comment
    # `brace` counts per command frame: a `$(` or backtick inside `${...}` is
    # parsed as commands, where `#` does start a comment (bash-verified:
    # `${ x $(true # it's<nl>)b }` prints `$(true)`), so each saves it here.
    bsave: list[int] = []
    ansi = False  # the open single quote is ANSI-C `$'...'`: `\'` is escaped
    plain_dollar = -2  # index of the last `$` emitted as a plain character
    i, n = 0, len(command)
    while i < n:
        if len(stack) > _SCAN_NEST_MAX:
            return _SCAN_TOO_DEEP
        c = command[i]
        top = stack[-1] if stack else None
        if hd_on:
            b0, e0, r0, exp = bodies[hq]
            if i >= r0:
                hq += 1
                hd_on = hq < len(bodies)
                if not hd_on:  # past the last body: a new line
                    word_start = True
                    plain_dollar = redir_at = -2
                continue
            if len(stack) > hd_base:
                if i >= e0:
                    # A `$(`/backtick opened in the body runs past its
                    # delimiter line: a bash syntax error (nothing runs).
                    return None
            elif i >= b0:
                # Body text: plain text to bash (no quotes, no comments, no
                # frames closing), but kept visible in `masked`.
                lim = e0 if i < e0 else r0
                m = _SCAN_BODY_RUN.match(command, i, lim)
                if m:
                    emit(m.group())
                    seg.append(m.group())
                    i = m.end()
                    continue
                live = exp and i < e0
                if not (live and (c == "`" or command.startswith("$(", i))):
                    if live and c == "\\" and i + 1 < e0:
                        nx = command[i + 1]
                        # `\`+newline joins (bash); `\'`, `\"` stay text.
                        emit(" " if nx == "\n" else "" if nx in "'\"" else command[i:i + 2])
                        seg.append(command[i:i + 2])
                        i += 2
                        continue
                    if c in ";&|\n" and not stack:
                        segs.append(("".join(seg), "".join(masked[mstart:])))
                        seg = []
                        emit(c)
                        mstart = len(masked)
                        i += 1
                        continue
                    # A quote or backslash is dropped, not blanked: a shell
                    # reading the body removes it (`pr\intf` runs printf).
                    emit("" if c in "'\"\\" else c)
                    seg.append(c)
                    i += 1
                    continue
                # else: a substitution in an unquoted body runs: read on below
        if c == "`" and bt_idx and (top in ("'", "x'", "d'") or in_comment):
            if in_comment:
                comments.append((cstart, i))
            close_backtick()
            emit(c)
            seg.append(c)
            word_start = False
            i += 1
            continue
        if top in ("'", "x'", "d'"):
            if bt_idx and c == "\\" and i + 1 < n and command[i + 1] in "\\$":
                return None  # backtick de-escaping of `\\`/`\$`: not modelled
            if bt_idx and c == "\\" and command.startswith("`", i + 1):
                seg.append(command[i:i + 2])  # `\`` inside backticks: escaped
                emit("  " if top == "'" else command[i:i + 2])
                i += 2
                continue
            if ansi and c == "\\" and i + 1 < n:
                seg.append(command[i:i + 2])
                emit("  " if top == "'" else command[i:i + 2])
                i += 2
                continue
            if c == "'":
                stack.pop()
                q_idx.pop()
                ansi = False
                if top == "'":
                    spans[data_open.pop()] = mlen
                elif top == "x'":
                    xspans[x_open.pop()] = mlen
                emit("" if top == "d'" else c)
                word_start = False
            else:
                emit(" " if top == "'" else c)
            seg.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if bt_idx and command[i + 1] in "\\$":
                # T-4413: bash reads a backtick body turning `\\` into `\`
                # and `\$` into `$` before parsing it; that second pass is
                # not modelled, so fail closed.
                return None
            if command[i + 1] == "\n":
                seg.append(" ")
                emit(" ")
                if in_comment and not bt_idx:
                    # Outside backticks only, bash ends a comment at this
                    # newline (T-4393): the text is still joined here, but
                    # quotes are live again. Inside backticks bash joins the
                    # lines while reading the body, so the comment runs on
                    # (`` `echo A #\<nl>"<nl>echo B` `` runs `echo B`).
                    in_comment = False
                    comments.append((cstart, i + 1))
                    word_start = True
            else:
                seg.append(command[i:i + 2])
                emit("  " if top == '"' else command[i:i + 2])
                if top not in ('"', 'x"', 'd"'):
                    word_start = False
            i += 2
            continue
        if in_comment:  # `top` is an unquoted frame: none opens in a comment
            if c == "\n":
                in_comment = False
                comments.append((cstart, i))
            else:
                emit(c)
                seg.append(c)
                i += 1
                continue
        if command.startswith("$(", i) and top != "'":
            if command.startswith("$((", i):
                arith_marks.append(len(stack))  # B(1): `<<` inside is a shift
            stack.append("$(")
            parens.append(False)
            bsave.append(brace)
            brace = 0
            seg.append("$(")
            emit("$(")
            i += 2
            word_start = True
            continue
        if top in (None, "$(", "(", "`"):
            ws = word_start
            if (word_start and top is not None and not brace and c == "c"
                    and _SCAN_CASE_KW.match(command, i)):
                # T-4413 r4: a `case` inside `$(`/`(`/backticks. Its pattern
                # `)` pops that frame here (bash reads it as a pattern), and
                # inside `"$(...)"` no unmatched `)` is left for the check
                # below to see (`echo "$(case a in a) x;; esac)"` runs x).
                return None
            if c == "#" and word_start and not brace:
                in_comment = True
                cstart = i
            elif c == "{" and plain_dollar == i - 1:
                # Every `${` reads to its `}` with quotes live, `${ x` too
                # (bash 5.2, the fleet's: `${ # it's<nl>}' }; cmd # '` runs
                # cmd), so no `#` inside one starts a comment.
                brace += 1
            elif c == "}" and brace:
                brace -= 1
            if c == "$":
                # `$$` is a parameter: `$${x}` is `$$` then a literal `{x}`.
                plain_dollar = -2 if plain_dollar == i - 1 else i
            if c in "()" and brace:
                word_start = False  # `(`/`)` inside `${...}` is text
            elif c == "(":
                # After `=` it opens an array assignment (`x=(a b)`,
                # `x+=(`): its `)` ends no word, like `$(...)`'s.
                parens.append(redir_at != i - 1 and command[i - 1:i] != "=")
                word_start = True
            elif c == ")":
                if parens:
                    word_start = parens.pop()
                    paren_popped = True
                elif paren_popped:
                    # T-4413: a `case` pattern `)` pops the `(`/`$(` of the
                    # frame around it, so that frame's own `)` arrives
                    # unmatched: the paren stack is out of step with bash
                    # (`$(case a in a) x;; esac)#` is mid-word). Fail closed.
                    return None
                else:
                    # No `)` has popped anything yet, so none can have
                    # popped the wrong frame: a top-level `case` pattern.
                    word_start = True
            else:
                word_start = c in _SCAN_WORD_BREAK or (c == "`" and not bt_idx)
            if c in "()[]\n<" and not brace:
                # T-4389 B(1): heredoc operators, outside arithmetic.
                if c == "(":
                    if top is None and arith_top:
                        arith_top += 1
                    elif ws and command.startswith("((", i):
                        if top is None:
                            arith_top = 1  # its second `(` makes 2
                        else:
                            arith_marks.append(len(stack))
                elif c == ")":
                    if top is None and arith_top:
                        arith_top -= 1
                elif c == "[":
                    if arith_br or plain_dollar == i - 1:
                        arith_br += 1
                    elif sub_br:
                        sub_br += 1
                    else:
                        # `NAME[`: read back over the name run (each run once,
                        # as the `[` after it stops the next read: linear).
                        # Any run counts, word start or not: over-reading
                        # only suppresses.
                        j = i
                        while j > 0 and command[j - 1] in _SCAN_NAME_CHARS:
                            j -= 1
                        if j < i and command[j] in _SCAN_NAME_START:
                            sub_br = 1
                elif c == "]":
                    if arith_br:
                        arith_br -= 1
                    elif sub_br:
                        sub_br -= 1
                elif c == "\n":
                    sub_br = 0
                    if hd_pending and hd_pending[-1][0] == len(stack) and not hd_on \
                            and not bt_idx:
                        # The line ends: its operators' bodies follow, in order.
                        k = len(hd_pending)
                        while k and hd_pending[k - 1][0] == len(stack):
                            k -= 1
                        due = [(d, q, s) for _dep, d, q, s in hd_pending[k:]]
                        del hd_pending[k:]
                        found, _open_at = _heredoc_line_spans(command, i + 1, due, join=True)
                        bodies.extend(found)
                        hd_on = hq < len(bodies)
                        hd_base = len(stack)
                elif (c == "<" and command.startswith("<<", i) and not command.startswith("<<<", i)
                        and not (redir_at == i - 1 and command[i - 1] == "<")
                        and heredocs and not hd_on and not bt_idx and not in_comment
                        and not (arith_top or arith_br or sub_br
                                 or (arith_marks and len(stack) > arith_marks[-1]))):
                    hd = _scan_heredoc_op(command, i)
                    if hd is not None:
                        hd_pending.append((len(stack),) + hd)
            if c in "<>":
                redir_at = i
        if top in ('"', 'x"', 'd"'):
            if c == '"':
                stack.pop()
                q_idx.pop()
                if top == '"':
                    spans[data_open.pop()] = mlen
                elif top == 'x"':
                    xspans[x_open.pop()] = mlen
                emit("" if top == 'd"' else c)
                word_start = False
            elif c == "`":
                if bt_idx:
                    close_backtick()
                    word_start = False
                else:
                    bt_idx.append((len(stack), len(parens), len(bsave)))
                    bsave.append(brace)
                    brace = 0
                    stack.append("`")
                    word_start = True
                emit(c)
            else:
                emit(" " if top == '"' else c)
            seg.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            recent = "".join(masked[-120:])
            ansi = c == "'" and plain_dollar == i - 1
            if _EXEC_QUOTE_PREFIX.search(recent) or (
                    plain_dollar == i - 1 and _EXEC_QUOTE_PREFIX.search(recent[:-1])):
                stack.append("x" + c)
                x_open.append(mlen)
                emit(";")
            elif _in_command_word(recent, truncated=len(masked) > 120):
                stack.append("d" + c)
                emit("")
            else:
                stack.append(c)
                data_open.append(mlen)
                emit(c)
            q_idx.append(len(stack) - 1)
        elif c == "(" and top in ("$(", "(") and not brace:
            stack.append("(")
            emit(c)
        elif c == ")" and top in ("$(", "(") and not brace:
            stack.pop()
            if top == "$(":
                brace = bsave.pop()
            trim()
            emit(c)
        elif c == "`":
            if bt_idx:
                close_backtick()
            else:
                bt_idx.append((len(stack), len(parens), len(bsave)))
                bsave.append(brace)
                brace = 0
                stack.append("`")
            emit(c)
        elif c in ";&|\n" and top is None:
            segs.append(("".join(seg), "".join(masked[mstart:])))
            seg = []
            emit(c)
            mstart = len(masked)
            i += 1
            continue
        else:
            emit(c)
        seg.append(c)
        i += 1
    if len(stack) > _SCAN_NEST_MAX:
        return _SCAN_TOO_DEEP
    if stack:
        return None
    if in_comment:
        comments.append((cstart, n))
    segs.append(("".join(seg), "".join(masked[mstart:])))
    rawmap = tuple(o + j for chunk, o in zip(masked, origin) for j in range(len(chunk)))
    out = _Scanned((tuple(s for s in segs if s[0].strip()), "".join(masked), rawmap, spans, xspans))
    out.comments = tuple(comments)
    out.bodies = tuple(bodies)
    return out


_scan.cache_clear = _scan_cached.cache_clear


def _gh_api_segments(command: str) -> list[str]:
    """Raw text of each top-level segment that really invokes `gh api`.

    A segment counts only if `gh api` is visible in its MASKED text, so a commit
    message, grep pattern or echo that merely mentions `gh api -X PUT ...` is
    data, not a call (same rule as `_escalation`). The selected segment is then
    judged on its RAW text: a quoted `-X 'PUT'` or a quoted graphql query body is
    a real argument to the call and must stay visible. Unbalanced quoting falls
    back to the whole raw command (fail-closed)."""
    scanned = _scan(command)
    if scanned is None:
        # The raw fallback reads a quoted gh word (`"gh" api`, `g"h" api`) as
        # the shell does, too: it can only add denials (T-4413 r4).
        hit = _has_gh_api_call(command) or any(
            i < len(toks) and _API_WORD.match(toks[i])
            for toks, i in _gh_word_positions(command, dequote=True))
        return [command] if hit else []
    return [raw for raw, masked in scanned[0] if _has_gh_api_call(masked)]


def _gh_api_mutating_rest_write(command: str) -> bool:
    """True if any `gh api` REST call in `command` resolves to a non-GET/HEAD
    method, or hides its arguments behind a substitution. See the module comment
    above for the method-resolution rule."""
    for seg in _gh_api_segments(command):
        if _GH_API_GRAPHQL_ENDPOINT.search(seg):
            continue
        if _GH_API_OPAQUE.search(seg):
            return True
        methods = _GH_API_EXPLICIT_METHOD.findall(seg)
        if methods:
            if methods[-1].strip("'\"").upper() not in _GH_API_READ_METHODS:
                return True
        elif _GH_API_BODY_FLAGS.search(seg):
            return True
    return False


# --- gh api graphql: the same reserved outcomes via a different transport ---
# HO addition to the T-4135 brief: every `gh api graphql` call is a POST, reads
# included, so the REST method rule above can't judge it — the actual operation
# lives in the QUERY TEXT (the -f/-F query=... value), not the method or path.
# `mergePullRequest`/`updateRepository`(visibility)/`deleteRepository`/
# `enablePullRequestAutoMerge`/branch-protection-rule/repository-ruleset/deploy-key
# mutations reach the identical HO-reserved outcomes as their REST counterparts
# above, just spelled differently — exactly the "every spelling of an operation,
# not just the common ones" intent this ticket (T-4135 `serves:`) names.
#
# Three fail-closed tiers (HO's explicit call on the boundary; see the worker's
# DECISIONS):
#   1. Query text VISIBLE inline and names one of the 12 reserved mutation fields
#      by identifier -> block. Precise, the brief's primary ask.
#   2. Query text VISIBLE inline and contains the literal `mutation` GraphQL
#      operation keyword but none of the 12 identifiers matched -> ALSO block.
#      Deliberately broader than the 12 named fields: a mutation is a write by
#      GraphQL's own type system, and restricting the block to only the
#      enumerated 12 would leave any other admin-shaped mutation (a future
#      renameBranch/archiveRepository/etc., or the same 12 reached via an alias
#      that defeats the identifier match) uncovered by the identical reasoning
#      that motivates this whole ticket. A `query {...}` read has no `mutation`
#      keyword, so this tier does not touch it (brief: "a query {...} read stays
#      allowed").
#   3. Query text NOT visible — sourced from a file or stdin (`-F query=@path`,
#      `--raw-field query=@path`, `--input <path>`) or from the shell
#      (`query="$(cat op.graphql)"`, `query=$Q`, a backtick) -> block outright.
#      This hook does not open arbitrary files to inspect their content (a much
#      larger surface than matching a command string, and outside "don't invent
#      a full shell parser"), so a file-sourced body can never be proven a safe
#      read — the brief's "file path itself can't be judged" case. GraphQL's own
#      `$variable` syntax inside a single-quoted query is literal text, not a
#      shell expansion, so it stays allowed.
# Anything that falls through all three (inline, visible, no `mutation` keyword,
# no reserved identifier, not file-sourced) is a plain `query {...}` read and
# stays ALLOWED.
_GRAPHQL_SENSITIVE_MUTATIONS = (
    "mergePullRequest",
    "updateRepository",
    "deleteRepository",
    "enablePullRequestAutoMerge",
    "createBranchProtectionRule",
    "updateBranchProtectionRule",
    "deleteBranchProtectionRule",
    "createRepositoryRuleset",
    "updateRepositoryRuleset",
    "deleteRepositoryRuleset",
    "addDeployKey",
    "deleteDeployKey",
)
_GRAPHQL_SENSITIVE_MUTATION_RE = re.compile(r"\b(?:" + "|".join(_GRAPHQL_SENSITIVE_MUTATIONS) + r")\b")
_GRAPHQL_MUTATION_KEYWORD_RE = re.compile(r"\bmutation\b")
_GRAPHQL_FILE_SOURCED_RE = re.compile(r"query=@|--input\b|query=['\"]?(?:\$|`)")
_SHELL_SUBSTITUTION_RE = re.compile(r"\$\(|`")


def _gh_api_graphql_mutation(command: str) -> bool:
    """True if a `gh api graphql` call should be blocked for a subagent. See the
    module comment above for the three fail-closed tiers."""
    # The endpoint token, not `gh api graphql` adjacency: flags may precede it
    # (`gh api -H 'X: y' graphql -f query=...`).
    segs = [seg for seg in _gh_api_segments(command) if _GH_API_GRAPHQL_ENDPOINT.search(seg)]
    if not segs:
        return False
    # Scoped to the graphql call's own segments: a query body sourced from
    # elsewhere on the line (`$Q`, a file, stdin) is tier 3 below, so an
    # unrelated quoted mention in another command is not the query.
    for seg in segs:
        if _GRAPHQL_SENSITIVE_MUTATION_RE.search(seg) or _GRAPHQL_MUTATION_KEYWORD_RE.search(seg):
            return True
    for seg in segs:
        scanned = _scan(seg)
        if _GRAPHQL_FILE_SOURCED_RE.search(seg) or _SHELL_SUBSTITUTION_RE.search(scanned[1] if scanned else seg):
            return True
    return False


# --- gh repo edit / delete / archive / unarchive / rename (T-4135) ---
# Row 13's other named `gh` CLI escape hatch: `gh repo` can flip visibility or
# delete a repository without `gh api`. O10 (repo visibility/admin changes) is
# the operator's; repo deletion is irreversible. The ticket named `edit --visibility`;
# every other `gh repo edit` flag and the archive/rename verbs are repo-admin
# settings writes of the same class, so the whole admin verb set is blocked.
# T-4145: the gh read allowlist below admits only `repo view/list/clone`; this
# raw-text match stays as a floor under it (defense in depth, review round 1).
_GH_REPO_ADMIN = re.compile(r"\bgh\s+repo\s+(?:edit|delete|archive|unarchive|rename)\b")


# --- sudo: blocked for ANY subagent, directly or behind a command wrapper ---
# T-4135: workers hold no elevated credentials of their own (T-1345); `sudo` is the
# direct escalation path. Matched at COMMAND POSITION: after a separator
# (; & | newline backtick `$(`), a subshell/group opener `(`/`{`, negation `!`,
# a shell keyword (then/do/else/elif/if/while/until) that is itself at command
# position, or find's -exec; an
# absolute-path spelling (/usr/bin/sudo) counts too. `su`/`doas`/`pkexec` are the
# same operation (privilege escalation) under a different name, so they share
# the rule — "every spelling of an operation" (T-4135 `serves:`).
#
# The match runs on `_scan`'s MASKED command, so the word inside a grep pattern,
# an echo or a commit message (`grep "sudo" notes`, `git commit -m "sudo ..."`)
# is data, not a command. A quoted string handed to an interpreter
# (`bash -c "cd x && sudo y"`, `sh -c 'sudo ...'`, `eval`, `ssh host '...'`)
# stays visible with its opening quote read as a separator, so it is still
# caught. If the quoting is unbalanced the raw command is matched instead, with
# a bare quote also counted as command position (fail-closed). A heredoc body is
# unquoted text, so a script written through a heredoc whose line starts with
# `sudo` is blocked; the deny reason points at the Write tool.
#
# Coordinator addition 2: also catch `sudo` preceded by a command wrapper (env /
# nohup / time / exec / xargs / command / nice / ionice / timeout / stdbuf) —
# these don't change what runs, only how it's launched, and a subagent one
# wrapper-hop away from `sudo` is exactly as unauthorized as one typing it bare.
# Matched by requiring the wrapper word at command position, then the escalator
# as a later word of the SAME command segment (never crossing a separator — that
# would be a different, unrelated command, not the wrapper's target).
_SUDO_WRAPPERS = ("env", "nohup", "time", "exec", "xargs", "command", "nice", "ionice", "timeout", "stdbuf")
_WRAPPER = r"(?:" + "|".join(_SUDO_WRAPPERS) + r")\b"
# Separators that put the next word at command position. A shell keyword keeps
# it there only when the keyword itself is at command position, chained after a
# separator: `if sudo -n true`, `; then sudo x`, `; do if sudo ...` run sudo;
# `echo if sudo` and `journalctl --until "sudo"` do not (T-4135 review round 4).
_CMD_ANCHORS = r"^|[;&|`\n(){!]|\$\("
_KEYWORDS = r"(?:then|do|else|elif|if|while|until)"
_ESCALATOR = r"(?:(?:/usr)?/bin/)?(?:sudo|su|doas|pkexec)\b"


def _cmd_position(anchors: str) -> str:
    return r"(?:(?:" + anchors + r")\s*(?:" + _KEYWORDS + r"\s+)*|-exec(?:dir)?\s+)"


# T-4145 review round 1: `VAR=value` words may precede the command word
# (`X="a b" sudo ls`, `GH_TOKEN=x gh ...`). Matched on MASKED text, where a
# quoted value is its quotes around blanks; a backslash escape (`X=a\ b`) is
# kept verbatim (round 2, N3). Each alternative starts with a different
# character and both loops are possessive, so the pattern matches one way
# only and cannot backtrack (T-4145 review round 2).
#
# T-4457: bash runs the command after any assignment word, and the old
# `NAME=value` shape missed three kinds of them, so the word after one was
# never at command position:
#   - an array value with more of the word glued on: `x=(a)#x sudo id`,
#     `x=( a 'b c' )b sudo id` (the `)` is a separator in `_scan`'s masked
#     text, so the glued `#x` read as the command word; see below);
#   - `NAME+=value` (the escalation walk knew it; this prefix did not);
#   - a subscript, `a[1]=x sudo id`, `a["k"]+=x ...`. bash reads a subscript
#     as part of the word even across blanks and separators, to its `]`
#     (`a[1 2]=x cmd`, `a[x y z]=x cmd`, `a[1;2 3]=x cmd` all run cmd). So
#     after an open piece (`NAME[` with no `]` before a blank), every piece
#     up to and including the first one holding an unescaped `]=` or `]+=`
#     (the tail piece, `z]=x`) is part of the assignment word; with no such
#     piece, the open piece alone is skipped and the next word is at
#     command position. A word whose first `=` follows a `]` (`2]=x`, after
#     an anchor inside the subscript) counts as an assignment word too.
#     Fail-closed: skipping such a word only makes the NEXT word a command.
#   - (round 2) a value holding `${...}`: bash reads it to its `}` across
#     blanks and `;` (`x=${y:-a b} cmd`, `x=${y:-a;b} cmd` run cmd).
#     `_AssignRuns.brace_end` reads it as bash does (nesting, quotes,
#     `$(...)`); the window regex, `_ASSIGN_BRACE`, only a flat one. With
#     no `}` the `$` is a plain character, so the value ends at the blank as
#     before.
# `_ASSIGN_HEAD` is the word up to its value. As a regex it is read only on
# `_scan`'s 120-character window (`_CMD_WORD_PREFIX`): its quoted-subscript
# and multi-piece alternatives can scan to the end of the text from each
# start, so on a whole command it would be quadratic (round-1 review:
# `'a["' * n`). `_AssignRuns` matches the same shapes in linear time.
_ASSIGN_Q = r"\"[^\"]*\"|'[^']*'|\\."
_ASSIGN_SUB_CHAR = r"(?:" + _ASSIGN_Q + r"|\$\{[^\s{}'\"\\]*+\}|[^\s\]'\"\\])"
_ASSIGN_PIECE = r"(?:\\.|[^\s\]\\]|\](?!\+?=))*+"
_ASSIGN_HEAD = (
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:\[" + _ASSIGN_SUB_CHAR + r"*+(?:\]\+?="
    r"|\s+(?:" + _ASSIGN_PIECE + r"\s+)*+" + _ASSIGN_PIECE + r"\]\+?=|(?=\s))|\+?=)"
    r"|[^\s;&|`(){}!=\]'\"\\]*+\]\+?=)"
)
# The window's `${...}`: no quote, escape or nested `${` inside (a quoted
# part is a unit of the value anyway), so a scan from one `${` stops at the
# next `{`. With blanks allowed, a nested or quoted form would cost a
# whole-window scan from every `{` anchor (`x=${"` * n).
_ASSIGN_BRACE = r"\$\{[^{}\"'\\]*+\}"
_ASSIGN_VALUE = r"(?:" + _ASSIGN_BRACE + r"|" + _ASSIGN_Q + r"|[^\s;&|`()'\"\\])*+"
_ASSIGN_PREFIX = r"(?:" + _ASSIGN_HEAD + _ASSIGN_VALUE + r"\s+)*+"
# An array value is not parsed here: its `(` and `)` are command-position
# anchors already (`x=(a b) sudo`), and a word glued to the `)` is skipped by
# `_AssignRuns.after_glued_close`, `_CMD_WORD_AFTER_ARRAY` and the walk, in
# a text that holds `=(`. That reads a nested body (`x=(a $(b))#x`) too,
# which a body pattern would not, and adds no `x=(a) x=(a) ...` run for the
# `$`-anchored prefix search to rescan from each `)`.


# `_scan` asks this at each opening quote: does the quote sit inside the command
# word (at command position, optionally right after a wrapper like `env`)? The
# word's unquoted part so far (`g` in `g"h"`) is skipped first. Reversed match
# keeps it linear.
_WORD_TAIL_REVERSED = re.compile(r"[^\s;&|`()<>{}!]*")
_CMD_WORD_PREFIX = re.compile(
    _cmd_position(_CMD_ANCHORS) + _ASSIGN_PREFIX + r"(?:" + _WRAPPER + r"\s+)?$")
# T-4457: after a `)` with a word glued on, in a text holding an array
# assignment (`x=(a)#x "gh" ...`), as `_AssignRuns.after_glued_close`. Like
# `_CMD_WORD_PREFIX` (on `A={ A={ ...`), a `$`-anchored search rescans an
# assignment run from each start in it, so both are read only on `_scan`'s
# 120-character window.
_CMD_WORD_AFTER_ARRAY = re.compile(
    r"\)(?:" + _ASSIGN_Q + r"|[^\s;&|`()'\"\\])++\s+" + _ASSIGN_PREFIX
    + r"(?:" + _WRAPPER + r"\s+)?$")


def _in_command_word(recent: str, truncated: bool) -> bool:
    tail_len = _WORD_TAIL_REVERSED.match(recent[::-1]).end()
    # An assignment word (`X="a b"`) is not the command word: its quotes are a
    # real value's, and dequoting them would make `b` read as the command
    # (T-4145 review round 1).
    if "=" in recent[len(recent) - tail_len:]:
        return False
    head = recent[:len(recent) - tail_len]
    if truncated:  # the window starts mid-command, so `^` is not a command start
        head = "\0" + head
    return bool(_CMD_WORD_PREFIX.search(head)) or (
        "=(" in recent and bool(_CMD_WORD_AFTER_ARRAY.search(head)))


# T-4154: two layers, both linear in the command length (a hook timeout is
# non-blocking, so a slow guard fails OPEN).
#
# 1. The T-4135 rule, decided exactly as before but without a regex search
#    (`_cmd_positions`, `_regex_rule_escalates`): an escalator at command
#    position, or a wrapper there followed by an escalator word later in the same
#    segment. As one regex it was quadratic twice over. Its wrapper half
#    rescanned to the segment's end from every wrapper, so a run of `{env ` /
#    `(env ` / `!env ` (each one an anchor, none a stop) took 2-3 s at 3000
#    tokens. And both halves rescanned the `VAR=value` run from every anchor
#    inside it (`A={ A={ ...`, 2.6 s). Now the command positions are computed
#    once, with each assignment run and value end memoized, and each wrapper
#    looks up the first escalator hit after it. Its decisions are unchanged,
#    including the known over-block of an escalator word that is only an
#    argument under a wrapper (`timeout 60 pytest -k sudo`).
#
# 2. `_escalation_walk`, for spellings layer 1 cannot see. Layer 1 reads the
#    MASKED text, so it misses a quoted escalator after a wrapper's own arguments
#    (`env FOO=bar "sudo" x`, `timeout 60 "sudo" x`, `env -S "sudo x"`). It also
#    misses a path-prefixed wrapper (`/usr/bin/env sudo x`), `eval sudo x`, a
#    backslash-escaped word (`\sudo x`), and a quote nested in an interpreter
#    string (`bash -c "x && 'sudo' y"`). The walk visits each word at command
#    position once. It skips `VAR=` assignments, keywords and redirections,
#    and skips each wrapper's options by that wrapper's own argument grammar
#    (`_WRAPPER_GRAMMAR`: layer 1's list plus eval, watch, setsid and ssh;
#    T-4351 adds taskset, chrt, flock, strace, systemd-run, script, parallel,
#    coproc and machinectl, the escalators sudoedit/run0/runuser, and a
#    shell fed its commands through a pipe or here-string). Each command word is read from the RAW text with its
#    quotes and backslashes dropped and its path prefix cut. Because the
#    grammar stops at the wrapped command's own word, `timeout 60 grep "sudo" f`
#    stays allowed: grep is the command, and "sudo" is its data. A command word
#    the hook cannot read (`$(which sudo) x`, `` `which sudo` x ``, `$SUDO x`)
#    denies when the command mentions an escalator anywhere (the same fail-closed
#    shape as gh's `_GH_DYNAMIC_CMD`).
_CMD_ANCHOR_CHARS = re.compile(r"[;&|`\n(){!]")
_CMD_ANCHOR_CHARS_RAW = re.compile(r"[;&|`\n(){!'\"]")
_KEYWORD_RUN = re.compile(r"\s*(?:" + _KEYWORDS + r"\s+)*")
# `_KeywordRuns` (T-4389 part B): `_KEYWORD_RUN` computed from these two.
_WS_RUN = re.compile(r"\s+")
_KEYWORD_THEN_SPACE = re.compile(_KEYWORDS + r"(?=\s)")
_FIND_EXEC = re.compile(r"-exec(?:dir)?\s+")
# T-4457 round 2: `_ASSIGN_HEAD`'s pieces, for `_AssignRuns.head_end`.
_NAME_AT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TAIL_WORD_AT = re.compile(r"[^\s;&|`(){}!=\]'\"\\]*+\]\+?=")
# A `${...}` in a subscript. It stops at a `{`, so the scans from two `${`
# never overlap (`a[${${${...`).
_SUB_BRACE_AT = re.compile(r"\$\{[^\s{}'\"\\]*+\}")
_DQ_AT = re.compile(r'"(?:[^"\\]|\\.)*+"', re.S)
# Each unescaped `]=` / `]+=` (the end of a subscript's tail piece), with or
# without quoted strings skipped.
_SUB_TAILS = re.compile(r"\\.|\](\+?=)", re.S)
_SUB_TAILS_Q = re.compile(r"\\.|'[^']*+'|\"(?:[^\"\\]|\\.)*+\"|\](\+?=)", re.S)
# T-4457: a `)` with more of its word glued on (`x=(a)#x`, `x=(a $(b))c`).
_GLUED_CLOSE = re.compile(r"\)(?=[^\s;&|`()])")
_SPACE_RUN = re.compile(r"\s*")
_ESCALATOR_AT = re.compile(_ESCALATOR)
_WRAPPER_AT = re.compile(_WRAPPER)
_ESCALATOR_AFTER_SPACE = re.compile(r"(?=\s" + _ESCALATOR + r")")
_SEGMENT_STOP = re.compile(r"[;&|`\n]")
# A character that ends an unquoted assignment value (`_ASSIGN_PREFIX`).
_VALUE_STOP = frozenset(";&|`()")


class _AssignRuns:
    """`_ASSIGN_PREFIX` as memoized functions of the start position: where
    the `VAR=value` run that starts at `p` ends. Each value end and each run
    end is computed once, so every anchor sharing a run costs O(1).

    T-4457 round 2: the word's head (`_ASSIGN_HEAD`) is matched by
    `head_end`, not by the regex, which rescanned a quoted subscript to the
    end of the text from each start (`'a["' * n`, 30 s at 64K). Each scan
    here is memoized per start (`sub_end`, `brace_end`, `value_end`) or done
    once per text (`_SUB_TAILS`), so the whole is linear. `quotes`: a quoted
    string hides a `]=` (the walk's raw text; masked text has blanked them).
    """

    def __init__(self, text: str, quotes: bool = False):
        self.text = text
        self.quotes = quotes
        self.value_ends: dict[int, int] = {}
        self.run_ends: dict[int, int] = {}
        self.sub_ends: dict[int, int] = {}
        self.brace_ends: dict[int, int | None] = {}
        self.tail_at: list[int] | None = None
        self.tail_value: list[int] = []
        # T-4457: whether the text holds an array assignment (`=(`, `+=(`).
        self.arrays = "=(" in text

    def head_end(self, p: int) -> int | None:
        """Where the value of an assignment word starting at p begins (after
        its `=` / `+=`), else None: `_ASSIGN_HEAD`'s alternatives in order.
        For an open piece (`a[x` before a blank) that is the value of the
        first tail piece after it (`z]=v`, found by `_SUB_TAILS`, one scan
        per text), so every piece between is skipped; with none, the open
        piece's own end, an empty value, so the next word is judged as
        usual (fail-closed)."""
        text, n = self.text, len(self.text)
        m = _NAME_AT.match(text, p)
        if m:
            k = m.end()
            if text.startswith("=", k):
                return k + 1
            if text.startswith("+=", k):
                return k + 2
            if text.startswith("[", k):
                e = self.sub_end(k + 1)
                if text.startswith("]=", e):
                    return e + 2
                if text.startswith("]+=", e):
                    return e + 3
                if e < n and text[e].isspace():
                    if self.tail_at is None:
                        tails = _SUB_TAILS_Q if self.quotes else _SUB_TAILS
                        found = [t for t in tails.finditer(text) if t.group(1)]
                        self.tail_at = [t.start() for t in found]
                        self.tail_value = [t.end() for t in found]
                    i = bisect.bisect_right(self.tail_at, e)
                    return self.tail_value[i] if i < len(self.tail_at) else e
        m = _TAIL_WORD_AT.match(text, p)
        return m.end() if m else None

    def word_end(self, p: int) -> int | None:
        """The end of an assignment word starting at p (the walk's view of
        it, whatever follows), else None."""
        h = self.head_end(p)
        return None if h is None else self.value_end(h)

    def sub_end(self, x: int) -> int:
        """End of `_ASSIGN_SUB_CHAR*+` from x (a subscript's text)."""
        text, n, memo = self.text, len(self.text), self.sub_ends
        path = []
        while x not in memo:
            nx = None
            if x < n:
                c = text[x]
                if c in "\"'":
                    close = text.find(c, x + 1)
                    nx = close + 1 if close >= 0 else None
                elif c == "\\":
                    nx = x + 2 if x + 1 < n and text[x + 1] != "\n" else None
                elif c == "$" and text.startswith("{", x + 1):
                    m = _SUB_BRACE_AT.match(text, x)
                    nx = m.end() if m else x + 1
                elif not (c.isspace() or c == "]"):
                    nx = x + 1
            if nx is None:
                memo[x] = x
                break
            path.append(x)
            x = nx
        end = memo[x]
        for q in path:
            memo[q] = end
        return end

    def brace_end(self, x: int) -> int | None:
        """For a `${` at x: the index after its `}`, read as bash reads it
        (quotes, escapes, a nested `${...}`, `$(...)` or backticks inside),
        else None. Only a `${` nests: `${y:-{a} b}` ends at `a}` in bash
        5.2. One scan per `${`: every `${` it passes is memoized too, and
        one with no `}` has none in any enclosing scan either."""
        memo = self.brace_ends
        if x in memo:
            return memo[x]
        text, n = self.text, len(self.text)
        frames: list[tuple[str, int]] = [("}", x)]  # (closer, `${` start or -1)
        i = x + 2
        while frames and i < n:
            c = text[i]
            close = frames[-1][0]
            if close == "`":  # a backtick body: only `\` and a backtick count
                if c == "`":
                    frames.pop()
                i += 2 if c == "\\" else 1
                continue
            if c == "\\":
                i += 2
            elif c == "'":
                k = text.find("'", i + 1)
                if k < 0:
                    break
                i = k + 1
            elif c == '"':
                m = _DQ_AT.match(text, i)
                if not m:
                    break
                i = m.end()
            elif text.startswith("${", i):
                if i in memo:
                    if memo[i] is None:
                        break
                    i = memo[i]
                else:
                    frames.append(("}", i))
                    i += 2
            elif text.startswith("$(", i):
                frames.append((")", -1))
                i += 2
            elif c == "`" or (c == "(" and close == ")"):
                frames.append((c if c == "`" else ")", -1))
                i += 1
            elif c == close:
                s = frames.pop()[1]
                i += 1
                if s >= 0:
                    memo[s] = i
            else:
                i += 1
        for _c, s in frames:
            if s >= 0:
                memo[s] = None
        return memo[x]

    def after_glued_close(self, k: int) -> int | None:
        """T-4457: for a `)` ending at k, in a text that holds an array
        assignment: if more of the word is glued on (`x=(a)#x sudo`), where
        the assignment run after that word ends, else None. The `)` is taken
        to close the array without matching parens, so a nested body
        (`x=(a $(b))#x`) or a `(` that `_scan` unquoted (`x=("(" b)#x`) is
        covered too (fail-closed: at worst a `$(...)x` word's next word is
        also judged as a command)."""
        if not self.arrays or k >= len(self.text) or not _GLUED_CLOSE.match(self.text, k - 1):
            return None
        e = self.value_end(k)
        if e >= len(self.text) or not self.text[e].isspace():
            return None
        return self.run_end(_SPACE_RUN.match(self.text, e).end())

    def value_end(self, x: int) -> int:
        """End of an assignment value starting at x: a possessive run of
        "..." / '...' / backslash-escape / `${...}` / plain character."""
        text, n, memo = self.text, len(self.text), self.value_ends
        path = []
        while x not in memo:
            nx = None
            if x < n:
                c = text[x]
                if c in "\"'":
                    close = text.find(c, x + 1)
                    nx = close + 1 if close >= 0 else None
                elif c == "\\":
                    nx = x + 2 if x + 1 < n and text[x + 1] != "\n" else None
                elif c == "$" and text.startswith("{", x + 1):
                    # T-4457 round 2: `${...}` to its `}`, blanks and `;`
                    # included; with none, a plain `$` as before.
                    nx = self.brace_end(x) or x + 1
                elif not (c.isspace() or c in _VALUE_STOP):
                    nx = x + 1
            if nx is None:
                memo[x] = x
                break
            path.append(x)
            x = nx
        end = memo[x]
        for p in path:
            memo[p] = end
        return end

    def run_end(self, p: int) -> int:
        text, n, memo = self.text, len(self.text), self.run_ends
        path = []
        while p not in memo:
            h = self.head_end(p)
            e = self.value_end(h) if h is not None else n
            if e >= n or not text[e].isspace():
                memo[p] = p
                break
            path.append(p)
            p = _SPACE_RUN.match(text, e).end()
        end = memo[p]
        for x in path:
            memo[x] = end
        return end


class _KeywordRuns:
    """`_KEYWORD_RUN.match(text, p).end()` as a memoized function of p
    (T-4389 part B). Matching `_KEYWORD_RUN` from every anchor rescanned a
    whitespace run once per newline in it (24K blank lines: ~5 s), and a
    keyword chain (`if\\n` * N) once per keyword in it. Here each whitespace
    run is indexed once (by `\\s+`, the regex's own `\\s`), and each keyword
    chain's end is memoized per position, so every anchor costs O(log n).

    The regex `\\s*(?:KW\\s+)*` matches one way only: `\\s*` and `\\s+` end
    at a non-space (a keyword starts with a letter), and at most one keyword
    of `_KEYWORDS` is followed by a space at any position. So its end is:
    skip the whitespace at p, then, while a keyword followed by whitespace
    starts there, skip the keyword and that whitespace. This relies on every
    keyword in `_KEYWORDS` being letters only: a keyword containing a
    non-letter or whitespace could make two alternatives match at one
    position (the verbatim-oracle test in TestKeywordRunsMatchOldRegex would
    then fail)."""

    def __init__(self, text: str):
        self.text = text
        self.ws_starts: list[int] = []
        self.ws_ends: list[int] = []
        for m in _WS_RUN.finditer(text):
            self.ws_starts.append(m.start())
            self.ws_ends.append(m.end())
        self.chain_ends: dict[int, int] = {}

    def skip_space(self, p: int) -> int:
        """`\\s*` matched at p: the end of the whitespace run holding p, else p."""
        i = bisect.bisect_right(self.ws_starts, p) - 1
        return self.ws_ends[i] if i >= 0 and self.ws_ends[i] > p else p

    def end(self, p: int) -> int:
        text, memo = self.text, self.chain_ends
        q = self.skip_space(p)
        path = []
        while q not in memo:
            m = _KEYWORD_THEN_SPACE.match(text, q)
            if not m:
                memo[q] = q
                break
            path.append(q)
            q = self.skip_space(m.end())
        end = memo[q]
        for x in path:
            memo[x] = end
        return end


def _cmd_positions(text: str, anchors: re.Pattern) -> list[int]:
    """Every index where `_cmd_position(...) + _ASSIGN_PREFIX` can end in
    `text`, in increasing order. `anchors` is `_CMD_ANCHOR_CHARS` (masked text)
    or `_CMD_ANCHOR_CHARS_RAW` (raw text, where a bare quote counts too).
    (`$(` needs no entry of its own: its `(` is an anchor.)"""
    runs = _AssignRuns(text)
    keywords = _KeywordRuns(text)
    starts = [0] + [m.end() for m in anchors.finditer(text)]
    out = {runs.run_end(keywords.end(a)) for a in starts}
    out.update(runs.run_end(m.end()) for m in _FIND_EXEC.finditer(text))
    if runs.arrays:  # T-4457: the word after a glued array close
        out.update(q for m in _GLUED_CLOSE.finditer(text)
                   if (q := runs.after_glued_close(m.end())) is not None)
    return sorted(out)


def _regex_rule_escalates(text: str, anchors: re.Pattern) -> bool:
    """Layer 1: the T-4135 rule (escalator at command position, or a wrapper
    there and an escalator word later in its segment), in linear time."""
    positions = _cmd_positions(text, anchors)
    if any(_ESCALATOR_AT.match(text, q) for q in positions):
        return True
    hits = [m.start() for m in _ESCALATOR_AFTER_SPACE.finditer(text)]
    if not hits:
        return False
    stops = [m.start() for m in _SEGMENT_STOP.finditer(text)]
    for q in positions:
        m = _WRAPPER_AT.match(text, q)
        if not m:
            continue
        e = m.end()
        i = bisect.bisect_left(hits, e)
        if i == len(hits):
            continue
        h = hits[i]
        j = bisect.bisect_left(stops, e)
        s = stops[j] if j < len(stops) else len(text)
        # The whitespace before the escalator may itself be the segment's
        # newline (`env x\nsudo`), as the regex's `\s` allowed.
        if h < s or (h == s and text[s] == "\n"):
            return True
    return False


# T-4351: sudoedit (sudo -e), run0 (systemd's sudo) and runuser (su without
# PAM auth) are escalators too. They are walk-only: layer 1 keeps the T-4135
# list, so no new wrapper-argument over-block appears.
_ESCALATOR_WORDS = frozenset({"sudo", "su", "doas", "pkexec", "sudoedit", "run0", "runuser"})
# Mentioned anywhere in the command: a dynamic command word may expand to it.
_ESCALATOR_MENTION = re.compile(r"(?<![\w.])(?:sudo(?:edit)?|su|doas|pkexec|run0|runuser)(?![\w.-])")
# T-4351: `coproc cmd` / `coproc NAME { cmd; }` keeps command position.
_KEYWORD_WORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "coproc"})
_FIND_EXEC_WORDS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# T-4457: on a dequoted word, the assignment shapes of `_ASSIGN_HEAD`: a
# subscript (`a[1]=x`, `a[k]+=x`), a subscript split at a blank or `;`
# (`a[1` then `2]=x`, both skipped), and `NAME+=`.
_ASSIGN_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*(?:\]\+?=|$)|\+?=)|[^=]*\]\+?=")
_REDIRECT_WORD = re.compile(r"\d*(?:[<>]|&>)")
_REDIRECT_OPERATOR = re.compile(r"\d*(?:[<>]+&?|&>>?)")
# Each wrapper's argument grammar: (short options that take a value, long
# options that take a separate value, operands before the command). An option
# not listed is a flag; `--` ends the options. From GNU coreutils/findutils
# 9.x/4.9 and bash (time/exec/command/eval). `env` also skips `VAR=value`
# words and `-`, and runs its -S/--split-string value as more of its arguments.
# GNU parallel (and `sem`): options that take a value.
_PARALLEL_GRAMMAR = ("adEIjJLnNPsSC", frozenset({
    "arg-file", "delimiter", "jobs", "max-procs", "max-args", "max-replace-args",
    "max-chars", "sshlogin", "sshloginfile", "colsep", "joblog", "results", "tmpdir",
    "workdir", "halt", "delay", "timeout", "retries", "env", "basefile", "return",
    "transferfile", "memfree", "load", "nice", "tagstring", "arg-sep", "arg-file-sep",
    "termseq", "rpl", "block", "recend", "recstart", "sshdelay", "header", "profile",
    "id", "semaphorename", "semaphoretimeout", "limit", "memsuspend", "ssh", "template",
    "plus-rpl"}), 0)
_WRAPPER_GRAMMAR: dict[str, tuple[str, frozenset[str], int]] = {
    "env": ("uCSa", frozenset({"unset", "chdir", "split-string", "argv0"}), 0),
    "nohup": ("", frozenset(), 0),
    "time": ("fo", frozenset({"format", "output"}), 0),
    "exec": ("a", frozenset(), 0),
    "xargs": ("adEILnPs", frozenset({
        "arg-file", "delimiter", "max-args", "max-procs", "max-chars", "process-slot-var"}), 0),
    "command": ("", frozenset(), 0),
    "nice": ("n", frozenset({"adjustment"}), 0),
    "ionice": ("cnpPu", frozenset({"class", "classdata", "pid", "pgid", "uid"}), 0),
    "timeout": ("ks", frozenset({"kill-after", "signal"}), 1),  # then DURATION
    "stdbuf": ("ioe", frozenset({"input", "output", "error"}), 0),
    "eval": ("", frozenset(), 0),  # its arguments are code
    # T-4154 round 2 (walk only; layer 1 keeps the T-4135 wrapper list):
    # procps watch; util-linux setsid (-f/-w/-c are flags); OpenSSH, whose
    # first operand is the destination and the rest the remote command.
    "watch": ("nq", frozenset({"interval", "equexit"}), 0),
    "setsid": ("", frozenset(), 0),
    "ssh": ("piloFJLRDWbceEmOQSwBIP", frozenset(), 1),
    # T-4351 (walk only), from util-linux 2.39 / strace 6.8 / systemd 255
    # --help and GNU parallel's man page:
    # taskset MASK|CPU-LIST (-a/-c/-p are flags); chrt PRIORITY (optional
    # for some policies, so only a numeric word is taken as it); flock FILE,
    # then its own -c; strace; systemd-run (an escalator unless --user,
    # below); script, whose operand is a log file, not a command; parallel;
    # machinectl, whose `shell` verb is a root shell by default.
    "taskset": ("", frozenset(), 1),
    "chrt": ("TPD", frozenset({"sched-runtime", "sched-period", "sched-deadline"}), 1),
    "flock": ("wE", frozenset({"timeout", "conflict-exit-code"}), 1),
    "strace": ("abeEIoOpPsSuUX", frozenset({
        "env", "attach", "user", "detach-on", "interruptible", "trace-path", "columns",
        "output", "string-limit", "const-print-style", "summary-syscall-overhead",
        "summary-sort-by", "summary-columns", "signal", "status", "trace", "trace-fds",
        "abbrev", "verbose", "raw", "read", "write", "kvm", "inject", "fault", "argv0"}), 0),
    "systemd-run": ("HMupE", frozenset({
        "host", "machine", "unit", "property", "description", "slice", "expand-environment",
        "service-type", "uid", "gid", "nice", "working-directory", "setenv", "path-property",
        "socket-property", "timer-property", "on-active", "on-boot", "on-startup",
        "on-unit-active", "on-unit-inactive", "on-calendar"}), 0),
    "script": ("IOBTmEo", frozenset({
        "log-in", "log-out", "log-io", "log-timing", "logging-format", "echo", "output-limit"}), 0),
    "parallel": _PARALLEL_GRAMMAR,
    # GNU `sem` is `parallel --semaphore`: the same options, then a command.
    "sem": _PARALLEL_GRAMMAR,
    "machinectl": ("pMHnosE", frozenset({
        "property", "machine", "host", "lines", "output", "signal", "kill-whom", "uid",
        "setenv", "max-addresses", "verify", "format"}), 0),
    # T-5505 r1: pass-through wrappers whose next word (after their options) is the command:
    # moreutils chronic [-ev] CMD; expect's unbuffer [-p] CMD; libfaketime faketime [-m] [-f]
    # [-p PID] [--exclude-monotonic] [--date-prog PROG] TIMESTAMP CMD; nocache [-n N] CMD.
    # (firejail's option table and sg's single code-string argument are not this shape: T-5571
    # reads both in a derived reading of their own, `_decide_wrapcode`.)
    "chronic": ("", frozenset(), 0),
    "unbuffer": ("", frozenset(), 0),
    "faketime": ("p", frozenset({"date-prog"}), 1),
    "nocache": ("n", frozenset(), 0),
}
assert set(_WRAPPER_GRAMMAR) == set(_SUDO_WRAPPERS) | {
    "eval", "watch", "setsid", "ssh", "taskset", "chrt", "flock", "strace", "systemd-run",
    "script", "parallel", "sem", "machinectl", "chronic", "unbuffer", "faketime", "nocache"}
# T-5325: the wrappers the static protected-path rules peel (see `_ho_static_command_words`).
_HO_STATIC_WRAPPERS = frozenset(_WRAPPER_GRAMMAR) - {"eval", "ssh", "machinectl", "script", "command"}
# Wrappers that run their arguments as a shell command line, so a single
# quoted word there is a whole command (`watch "sudo x"`, `ssh h "sudo x"`).
_CODE_STRING_WRAPPERS = frozenset({"watch", "ssh", "parallel", "sem"})
# T-4351: options whose value is a shell command line (short letters, long
# names), checked like a `bash -c` string: `flock f -c 'sudo x'`,
# `script -qc 'sudo x' /dev/null`.
_CODE_OPTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "flock": ("c", frozenset({"command"})),
    "script": ("c", frozenset({"command"})),
}
# T-4361: each wrapper's FULL long-option table, as its own parser reads
# it (`_long_option`), so an abbreviated option takes its value as the tool
# does (`flock --tim 5 f CMD`: 5 is the timeout, f the lock file). "name" is
# a flag, "name=r" takes a value (the next word, or `=value`), "name=o" takes
# one only as `=value`; "a|b" are aliases of one option (a prefix of both is
# not ambiguous). GNU parallel reads its options with Perl Getopt::Long
# (`_PERL_GETOPT`): long names ignore case, and "=s" takes the next word
# unless it looks like an option, "=i"/"=f" only a number. Derived per tool
# (versions below) by probing the binary on this box, or the unpacked Ubuntu
# noble deb, with an exhaustive prefix cross-check against it (glibc
# getopt_long; parallel's GetOptions spec against Getopt::Long). exec,
# command, eval (bash builtins) and ssh (plain getopt) have no long options.
def _opt_table(spec: str) -> tuple[dict[str, str], dict[str, str]]:
    table, aliases = {}, {}
    for item in spec.split():
        names, _, kind = item.partition("=")
        names = names.split("|")
        for n in names:
            table[n] = kind
            aliases[n] = names[0]
    return table, aliases


_OPTION_TABLES = {
    "chrt": _opt_table(  # util-linux 2.39.3
        "all-tasks batch deadline fifo help idle max other pid reset-on-fork rr "
        "sched-deadline=r sched-period=r sched-runtime=r verbose version"),
    # T-5360: plus `argv0=r`, newer coreutils' `-a/--argv0 NAME` (the `a` short is in
    # `_WRAPPER_GRAMMAR` already): `env --argv0 x bash` takes `x` as its value, so the command
    # is `bash` (9.4 refuses the word, and runs nothing).
    "env": _opt_table(  # coreutils 9.4
        "argv0=r block-signal=o chdir=r debug default-signal=o help ignore-environment "
        "ignore-signal=o list-signal-handling null split-string=r unset=r version"),
    "flock": _opt_table(  # util-linux 2.39.3
        "close conflict-exit-code=r exclusive help nb no-fork nonblocking shared "
        "timeout=r unlock verbose version wait=r"),
    "ionice": _opt_table(  # util-linux 2.39.3
        "class=r classdata=r help ignore pgid=r pid=r uid=r version"),
    "machinectl": _opt_table(  # systemd-container 255.4 (deb)
        "all force format=r full help host=r kill-whom=r lines=r machine=r "
        "max-addresses=r mkdir no-ask-password no-legend no-pager now output=r "
        "property=r quiet read-only setenv=r signal=r uid=r value verify=r version"),
    "nice": _opt_table(  # coreutils 9.4
        "adjustment=r help version"),
    "nohup": _opt_table(  # coreutils 9.4
        "help version"),
    "script": _opt_table(  # util-linux (bsdutils) 2.39.3
        "append command=r echo=r flush force help log-in=r log-io=r log-out=r "
        "log-timing=r logging-format=r output-limit=r quiet return timing=o version"),
    "setsid": _opt_table(  # util-linux 2.39.3
        "ctty fork help version wait"),
    "stdbuf": _opt_table(  # coreutils 9.4
        "error=r help input=r output=r version"),
    "strace": _opt_table(  # strace 6.8
        "abbrev=r absolute-timestamps=o argv0=r attach=r columns=r const-print-style=r "
        "daemonize|daemonised|daemonized=o debug decode-fds=o decode-pids=r detach-on=r "
        "env=r failed-only|failing-only fault=r follow-forks help inject=r "
        "instruction-pointer interruptible=r kill-on-exit kvm=r no-abbrev output=r "
        "output-append-mode output-separately pidns-translation quiet=o raw=r read=r "
        "relative-timestamps=o seccomp-bpf secontext=r signal|signals=r "
        "silence|silent=o stack-trace=o stack-trace-frame-limit=r stack-traces=o "
        "status=r string-limit=r strings-in-hex=o successful-only summary "
        "summary-columns=r summary-only summary-sort-by=r summary-syscall-overhead=r "
        "summary-wall-clock syscall-limit=r syscall-number syscall-times=o timestamps=o "
        "tips=o trace=r trace-fds=r trace-path=r user=r verbose=r version write=r"),
    "systemd-run": _opt_table(  # systemd 255.4
        "collect description=r expand-environment=r gid=r help host=r machine=r nice=r "
        "no-ask-password no-block on-active=r on-boot=r on-calendar=r on-clock-change "
        "on-startup=r on-timezone-change on-unit-active=r on-unit-inactive=r "
        "path-property=r pipe property=r pty quiet remain-after-exit same-dir scope "
        "send-sighup service-type=r setenv=r shell slice=r slice-inherit "
        "socket-property=r system timer-property=r tty uid=r unit=r user version wait "
        "working-directory=r"),
    "taskset": _opt_table(  # util-linux 2.39.3
        "all-tasks cpu-list help pid version"),
    "time": _opt_table(  # GNU time 1.9 (deb)
        "append format=r help output|output-file=r portability quiet verbose version"),
    "timeout": _opt_table(  # coreutils 9.4
        "foreground help kill-after=r preserve-status signal=r verbose version"),
    "watch": _opt_table(  # procps 4.0.4
        "beep chgexit color differences=o equexit=r errexit exec help interval=r "
        "no-color no-rerun no-title no-wrap precise version"),
    "xargs": _opt_table(  # findutils 4.9.0
        "arg-file=r delimiter=r eof=o exit help interactive max-args=r max-chars=r "
        "max-lines=o max-procs=r no-run-if-empty null open-tty process-slot-var=r "
        "replace=o show-limits verbose version"),
    "parallel": _opt_table(  # GNU parallel 20231122 (deb), Getopt::Long 2.54
        "_bug _parset=r _pipe-means-argfiles _test=r _unsafe arg-file|a|argfile=r "
        "arg-file-sep|argfilesep=r arg-sep|argsep=r bar basefile|bf=r "
        "basenameextensionreplace|bner=r basenamereplace|bnr=r bg bin=r "
        "block-size|block|blocksize=r block-timeout|blocktimeout|bt=r cat cleanup "
        "col-sep|colsep=r color|colour "
        "color-failed|cf|color-fail|colorfail|colorfailed|colour-fail|colour-failed|colourfail|colourfailed "
        "compress controlmaster csv ctag ctag-string|ctagstring=r ctrl-c|ctrlc debug=r "
        "delay=r delimiter|d=r dirnamereplace|dnr=r dry-run|dr|dryrun embed env=r "
        "eof|e=s eta exit|x extensionreplace|er=r fg fifo filter=r "
        "filter-hosts|filter-host|filterhosts g gnu group group-by|groupby=r "
        "halt-on-error|halt|haltonerror=r header=r help|h "
        "hgrp|hostgroup|hostgroups|hostgrp interactive|p joblog|jl=r "
        "keep-order|k|keeporder latest-line|latestline|ll limit=r "
        "line-buffer|lb|line-buffered|linebuffer|linebuffered link|xapply "
        "linkinputsource|xapplyinputsource=r load=r m max-args|maxargs|n=r "
        "max-chars|maxchars|s=r max-line-length-allowed|maxlinelengthallowed "
        "max-lines|l|maxlines=f max-procs|j|jobs|maxprocs=r "
        "max-replace-args|maxreplaceargs=r memfree=r memsuspend=r "
        "min-version|minversion=r nice=r no-ctrl-c|no-ctrlc|noctrlc "
        "no-keep-order|no-k|nok|nokeeporder no-run-if-empty|norunifempty|r nonall "
        "noswap null|0 number-of-cores|numberofcores number-of-cpus|numberofcpus "
        "number-of-sockets|numberofsockets number-of-threads|numberofthreads onall "
        "open-tty|o output-as-files|files|outputasfiles "
        "output-as-files0|files0|outputasfiles0 parens=r pipe|spreadstdin "
        "pipe-part|pipepart plain plus process-slot-var|processslotvar=r profile=r "
        "progress quote|q recend=r recordenv|record-env recstart=r regexp|regex "
        "remove-rec-sep|removerecsep|rrs replace|i=s results|res|result=r resume "
        "resume-failed|resumefailed retries=r retry-failed|retryfailed return=r "
        "round-robin|round|roundrobin rpl=r rsync-opts|rsyncopts=r semaphore "
        "semaphore-name|id|semaphorename=r semaphore-timeout|semaphoretimeout|st=r "
        "seqreplace=r session shard=r shebang|hashbang "
        "shell-completion|shellcompletion=r shell-quote|shell_quote|shellquote "
        "show-limits|showlimits shuf silent skip-first-line|skipfirstline slotreplace=r "
        "sql=r sql-and-worker|sqlandworker=r sql-master|sqlmaster=r "
        "sql-worker|sqlworker=r ssh=r ssh-delay|sshdelay=r sshlogin=r "
        "sshloginfile|slf=r tag tag-string|tagstring=r tee template|tmpl=r "
        "term-seq|termseq=r timeout=r tmpdir|tempdir=r tmux tmux-pane|tmuxpane tollef "
        "total-jobs|total|totaljobs=r transfer "
        "transfer-file|tf|transfer-files|transferfile|transferfiles=r trc=r trim=r tty "
        "ungroup|u "
        "use-compress-program|compress-program|compressprogram|usecompressprogram=r "
        "use-cores-instead-of-threads|usecoresinsteadofthreads "
        "use-cpus-instead-of-cores|usecpusinsteadofcores "
        "use-decompress-program|decompress-program|decompressprogram|usedecompressprogram=r "
        "use-sockets-instead-of-threads|usesocketsinsteadofthreads v verbose|t version "
        "wait will-cite|nn|no-notice|nonotice|willcite work-dir|wd|workdir=r xargs"),
}
_OPTION_TABLES["sem"] = _OPTION_TABLES["parallel"]  # `parallel --semaphore`
for _name in ("exec", "command", "eval", "ssh", "chronic", "unbuffer", "nocache"):
    _OPTION_TABLES[_name] = ({}, {})
_OPTION_TABLES["faketime"] = _opt_table(  # libfaketime 0.9.10 (no abbreviations)
    "date-prog=r disable-shm exclude-monotonic help version")
assert set(_OPTION_TABLES) == set(_WRAPPER_GRAMMAR)
_LONG_OPTIONS = {k: v[0] for k, v in _OPTION_TABLES.items()}
_LONG_ALIASES = {k: v[1] for k, v in _OPTION_TABLES.items()}
_LONG_SORTED = {k: sorted(v) for k, v in _LONG_OPTIONS.items()}
_PERL_GETOPT = frozenset({"parallel", "sem"})
# Short options that take a value, by kind, where they differ from the
# grammar's `shorts` (from the same GetOptions spec): parallel's -i/-e take
# the next word unless it looks like an option, -l only a number.
_SHORT_KINDS = {"parallel": dict.fromkeys("BCDEHIJLNPSUWadjns", "r") | {"i": "s", "e": "s", "l": "f"}}
_SHORT_KINDS["sem"] = _SHORT_KINDS["parallel"]
# ssh -o options that run a LOCAL command line (`ssh -o ProxyCommand='...'
# host`), or a remote one like ssh's own command operand (RemoteCommand).
_SSH_CODE_OPTION = re.compile(
    r"\s*(?:proxycommand|localcommand|knownhostscommand|remotecommand)(?:\s*=\s*|\s+)", re.I)
# A wrapper whose operand must look numeric (chrt's optional PRIORITY).
_NUMERIC_OPERAND = frozenset({"chrt"})
# Names this arc made deny on their own; `command -v NAME` only looks one up.
_T4351_LOOKUP_EXEMPT = frozenset({"sudoedit", "run0", "runuser", "systemd-run", "machinectl"})
# A here-string redirection word (`<<<"x"`, `0<<< x`).
_HERE_STRING = re.compile(r"0?<<<")
# Separators that do not change whether the next command reads a pipe
# (`a | (bash)`, `a |\n bash`, `a | { bash; }`).
_PIPE_RUN_STRIP = str.maketrans("", "", "\n({!")


def _sep_tail(tail: str, word: str) -> str:
    """The last two characters of `"".join(run).translate(_PIPE_RUN_STRIP)`
    after `word` joins the run, given `tail` for the run before it: all that
    `pipe_ended`'s `endswith` checks read (T-4389 part B: re-joining the whole
    run at each group opener was quadratic on `{ ` * N)."""
    return (tail + word.translate(_PIPE_RUN_STRIP))[-2:]

# T-4363: a compound command's opening keyword and the word that closes it.
# Its commands read the compound's stdin, like a `{ }` / `( )` group's do.
_COMPOUND_OPEN = {"if": "fi", "while": "done", "until": "done", "for": "done",
                  "select": "done", "case": "esac"}
_COMPOUND_CLOSE = frozenset({"}", "fi", "done", "esac"})
_WALK_SPACE = " \t\r\f\v"
_WALK_SEP = ";&|\n()`"
_WALK_PLAIN = re.compile(r"[^ \t\r\f\v\n;&|()`$\\'\"]+")


def _walk_words(text: str, spans: dict[int, int] | None, raw: str, rawmap: tuple[int, ...] | None,
                xspans: dict[int, int] | None = None):
    """Tokens of `text` for `_escalation_walk`, in one pass.

    ("sep", s, glued, code_at, nested, at): a separator. `$(` and a backtick
      count too, and `glued` says it touches the word before it (`X=$(...)`,
      `a$(b)`). `code_at`: for an interpreter string's opening (`xspans`),
      the index in `raw` of its quote, else None. `at` (T-4457 round 2): its
      index in `raw`.
    ("word", w, dynamic, raw_start, nested): a word, with `w` its RAW text (via `rawmap`). A
      blanked data string (`spans`) is part of its word, so `"a b"` is one
      word. `dynamic`: it is computed at run time ($VAR, $(...), backticks).
    `{` and `!` are separators only as whole words (`{ cmd;}`, `! cmd`).
    raw_start: the index in `raw` where the word starts (the start of any
    quote dropped before it), for `_shell_word`. `nested`: the token sits
    inside an interpreter string, where `raw` is one quoting level too deep
    to read a word from (the string's own check reads it instead).
    The `&` of a redirection (`2>&1`, `<&0`, `&>f`, `&>>f`) is part of its
    word, not a separator (T-4154 round 3).
    """
    j, n = 0, len(text)
    word_end = -1
    x_ends: list[int] = []  # closing indices of the interpreter strings we are in

    def is_sep(k: int) -> bool:
        c = text[k]
        if c == "&" and ((k > 0 and text[k - 1] in "<>") or text.startswith(">", k + 1)):
            return False
        return c in _WALK_SEP or text.startswith("$(", k)

    while j < n:
        c = text[j]
        if c in _WALK_SPACE:
            j += 1
            continue
        while x_ends and x_ends[-1] < j:
            x_ends.pop()
        nested = bool(x_ends)
        if is_sep(j):
            k = 2 if c == "$" else 1
            close = xspans.get(j) if xspans else None
            code_at = rawmap[j] if close is not None and rawmap is not None else None
            yield ("sep", text[j:j + k], word_end == j, code_at, nested,
                   rawmap[j] if rawmap is not None else j)
            if close is not None:
                x_ends.append(close)
            j += k
            continue
        if c in "{!" and (j + 1 == n or text[j + 1] in _WALK_SPACE or text[j + 1] in _WALK_SEP):
            yield ("sep", c, False, None, nested, rawmap[j] if rawmap is not None else j)
            j += 1
            continue
        start = j
        while j < n:
            m = _WALK_PLAIN.match(text, j)
            if m:
                j = m.end()
                if j == n:
                    break
            c = text[j]
            if c in _WALK_SPACE or is_sep(j):
                break
            close = spans.get(j) if spans else None
            if close is not None and (close == j + 1 or text[j + 1:close].isspace()):
                j = close + 1  # a blanked data string: part of this word
            elif c == "\\" and j + 1 < n:
                j += 2
            else:
                j += 1  # `$`, or a quote that is not a blanked data string
        word_end = j
        word = raw[rawmap[start]:rawmap[j - 1] + 1] if rawmap is not None else text[start:j]
        # A word glued to a `$(` (`"$(which sudo)"` after a wrapper's arguments
        # lexes as `"` + `$(`) is computed too. Not a glued backtick: that is
        # usually a closing one (`` `date` ``).
        dynamic = "$" in word or "`" in word or text.startswith("$(", j)
        if rawmap is None:
            raw_start = start
        else:
            raw_start = rawmap[start - 1] + 1 if start > 0 else 0
        yield ("word", word, dynamic, raw_start, nested)


def _dequote(word: str) -> str:
    return word.translate(_QUOTE_STRIP)


def _skip_wrapper_options(name: str, nxt, pending: list, codes: list | None = None,
                          seen: list | None = None, budget: _CodeBudget | None = None,
                          note_assign=None):
    """Consume `name`'s options and operands (`_WRAPPER_GRAMMAR`); return the
    first token after them, the wrapped command's word. Values of options
    that are command lines go to `codes`, and each option word to `seen`."""
    shorts, longs, operands = _WRAPPER_GRAMMAR[name]
    codes = [] if codes is None else codes
    tok = _skip_options(name, shorts, longs, nxt(), nxt, pending, codes, seen, budget,
                        note_assign)
    for _ in range(operands):
        if tok is not None and tok[0] == "word":
            if name in _NUMERIC_OPERAND and not _dequote(tok[1])[:1].isdigit():
                break
            tok = nxt()
    # OpenSSH reads options after the destination too; flock reads its -c
    # after the lock file.
    if name in ("ssh", "flock"):
        tok = _skip_options(name, shorts, longs, tok, nxt, pending, codes, seen, budget,
                            note_assign)
    return tok


def _long_prefix_of(word: str, names: tuple[str, ...]) -> bool:
    """Whether `word` is `--X` or `--X=v` where X is a non-empty prefix of one of `names`."""
    if word[:2] != "--":
        return False
    opt = word[2:].partition("=")[0]
    return bool(opt) and any(n.startswith(opt) for n in names)


_NUMBER = {"i": re.compile(r"[-+]?[0-9]+"),
           "f": re.compile(r"[-+]?(?=[0-9.])[0-9]*(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?")}


def _takes_next(kind: str, nw: str | None) -> bool:
    """Whether an option of `kind` (`_LONG_OPTIONS`) with no attached value
    takes the next word `nw` (None: no next word) as its value."""
    if nw is None or kind not in ("r", "s", "i", "f"):
        return False
    if kind == "s":  # Getopt::Long `:s`: unless the word looks like an option
        return not (nw.startswith("-") and nw != "-")
    if kind in _NUMBER:  # Getopt::Long `:i` / `:f`: only a number
        return bool(_NUMBER[kind].fullmatch(nw))
    return True


def _long_option(name: str, word: str) -> tuple[str, str]:
    """(the option `--word` resolves to, its kind) for wrapper `name`, as
    its own parser resolves it (T-4361): an exact name wins, else a unique
    prefix (aliases of one option count once). Kind "?": the tool refuses
    the word (unknown, an ambiguous prefix, or a value given to a flag).
    `word` is the text after `--`, with any `=value`."""
    opt, eq, _ = word.partition("=")
    if opt in _CODE_OPTIONS.get(name, ("", frozenset()))[1]:
        return opt, "r"  # flock matches its `--command` itself, in full
    hit, kind = _resolve_long(name, opt)
    if eq and not kind:
        return hit, "?"
    return hit, kind


@functools.lru_cache(maxsize=4096)
def _resolve_long(name: str, opt: str) -> tuple[str, str]:
    table, aliases = _LONG_OPTIONS[name], _LONG_ALIASES.get(name, {})
    if name in _PERL_GETOPT:  # Getopt::Long ignores case in long names
        opt = opt.lower()
    if opt in table:
        hit = opt
    else:
        names = _LONG_SORTED[name]
        i = bisect.bisect_left(names, opt)
        hits = []
        while opt and i < len(names) and names[i].startswith(opt):
            hits.append(names[i])
            i += 1
        if len({aliases.get(h, h) for h in hits}) != 1:
            return opt, "?"
        hit = hits[0]
    return hit, table[hit]


def _code_long_abbrev(name: str, opt: str) -> str | None:
    """T-4501: the full name of wrapper `name`'s code-string long option (env
    --split-string, script/flock --command) when `opt` (the text after `--`,
    no `=value`) spells it at its shortest accepted prefix or longer, per
    `_PY_CODE_WRAPPERS` (the one table of those prefixes); else None."""
    spec = _PY_CODE_WRAPPERS.get(name)
    if spec is None or not opt or len(opt) + 2 < spec[2]:
        return None
    full = spec[1][2:]
    return full if full.startswith(opt) else None


def _option_parse(name: str, v: str, nw: str | None, shorts: str, longs: frozenset[str],
                  legacy: bool) -> tuple[int | None, bool, str]:
    """How wrapper `name` parses option word `v` (`--x[=y]` or a short
    cluster) followed by the word `nw`: (the index in `v` where an attached
    value starts, or None when it takes no value; whether it takes `nw` as
    its value; the option: a long name, or the value-taking short letter).
    `legacy`: the pre-T-4361 grammar, where a long option takes a value only
    when spelled in full (`longs`) and parallel's shorts are `shorts`."""
    code_shorts, code_longs = _CODE_OPTIONS.get(name, ("", frozenset()))
    if v.startswith("--"):
        opt, eq, _ = v[2:].partition("=")
        code_long = _code_long_abbrev(name, opt)
        if legacy and code_long:
            key, kind = code_long, "r"  # T-4501: an accepted abbreviation, read as code
        elif legacy:
            key, kind = opt, "r" if (opt in longs or opt in code_longs) else ""
        else:
            key, kind = _long_option(name, v[2:])
        return (len(opt) + 3 if eq else None), not eq and _takes_next(kind, nw), key
    kinds = None if legacy else _SHORT_KINDS.get(name)
    for k in range(1, len(v)):
        c = v[k]
        kind = kinds.get(c, "") if kinds is not None else ""
        if c in code_shorts or (kinds is None and c in shorts):
            kind = "r"
        if kind:
            return k + 1, k + 1 == len(v) and _takes_next(kind, nw), c
    return None, False, ""


def _env_s_walk_words(value: str, tok, at: int | None) -> list[str]:
    """T-5360: the escalation walk's env -S words: `value` (the dequoted -S value) re-read
    from its token's raw word `tok[1]` with one level of shell quoting removed (a value glued
    to its option, `at` set: the text after the option, when the raw reading starts with the
    same option text), then split by GNU's -S grammar (`_env_s_words`). A spliced word (no
    raw position), a reading that does not line up with `value` once dequoted, or one env
    rejects is split as `value`."""
    sval = value
    if tok is not None and len(tok) > 3 and isinstance(tok[1], str):
        word = _shell_word(tok[1], 0)
        if at is None:
            sval = word
        elif word[:at] == _dequote(tok[1])[:at]:
            sval = word[at:]
    if sval != value and (_dequote(sval) != value or _env_s_split(sval) is None):
        sval = value  # the raw reading lost text (`$(..)`), or env rejects it
    return _env_s_words(sval)


def _skip_options(name: str, shorts: str, longs: frozenset[str], tok, nxt, pending: list,
                  codes: list | None = None, seen: list | None = None,
                  budget: _CodeBudget | None = None, note_assign=None):
    """`note_assign(tok)` (T-4457 round 2) is told of each of env's
    assignment words, which may run on past its token (`x=${y:-a;b}`)."""
    code_shorts, code_longs = _CODE_OPTIONS.get(name, ("", frozenset()))
    legacy = budget is not None and budget.legacy
    while tok is not None and tok[0] == "word":
        v = _dequote(tok[1])
        if v == "--":
            tok = nxt()
            break
        if name == "env" and (v == "-" or _ASSIGN_WORD.match(v)):
            if note_assign is not None and v != "-":
                note_assign(tok)
            tok = nxt()
            continue
        if len(v) < 2 or v[0] != "-":
            break
        if seen is not None:
            seen.append(v)
        opt_nested = len(tok) > 4 and tok[4]
        otok = tok
        tok = nxt()
        nw = _dequote(tok[1]) if tok is not None and tok[0] == "word" else None
        parsed = _option_parse(name, v, nw, shorts, longs, True)
        if not legacy:
            new = _option_parse(name, v, nw, shorts, longs, False)
            # T-4361: THE divergence point. The probed tables and the
            # pre-T-4361 grammar read this word differently, so
            # `_escalation_reason` also runs the old reading if this one allows.
            if new != parsed and budget is not None:
                budget.diverged = True
            parsed = new
        at, take_next, key = parsed
        value = None if at is None else v[at:]
        vtok = None  # the value's own token, when it is a separate word
        if take_next:
            value, vtok = nw, tok
            tok = nxt()
        is_long = v.startswith("--")
        is_code = key in (code_longs if is_long else code_shorts)
        split = key == ("split-string" if is_long else "S")
        if name == "ssh" and not is_long and key == "o" and value:
            m = _SSH_CODE_OPTION.match(value)
            if m and codes is not None:
                codes.append(("str", value[m.end():], opt_nested))
        if is_code and value is not None and codes is not None:
            codes.append(vtok if vtok is not None else ("str", value, opt_nested))
        if name == "env" and split and value:
            # env -S "sudo x": the string's words are env's next arguments.
            if tok is not None:
                pending.append(tok)
            # T-5360: split by GNU's -S grammar, read from the shell word itself (one level
            # of shell quoting removed, `_shell_word`), not `_dequote`, which drops every
            # backslash env reads (`'bash\_-c'`). A word spliced in here (no raw position)
            # is already env's text.
            pending.extend(("word", w, "$" in w or "`" in w)
                           for w in reversed(_env_s_walk_words(
                               value, *((vtok, None) if vtok is not None else (otok, at)))))
            tok = nxt()
    return tok


# T-4154 round 3: a shell's -c code string behind options (`bash -o pipefail
# -c '...'`, `bash -euo pipefail -c`, `bash -c -- '...'`) or nested in
# another (`bash -c "bash -c 'sudo x'"`, `eval "bash -c '...'"`) is read
# here. `_EXEC_QUOTE_PREFIX` sees only `sh -c` followed directly by a quote.
# The operand loses ONE level of quoting (`_shell_word`), and the whole
# escalation check runs on it, to a bounded depth and a bounded total size.
# Past either bound, it denies if the operand mentions an escalator.
_SHELLS = _SHELL_NAMES  # T-5332: rbash, ash, mksh, hush, static-sh too
_SHELL_VALUE_OPTS = frozenset({"-o", "-O", "+o", "+O", "--rcfile", "--init-file"})
_CODE_DEPTH_MAX = 8
_SHELL_WORD_END = frozenset(" \t\r\f\v\n;&|()<>")


def _shell_word(src: str, i: int) -> str:
    """The shell word starting at src[i], with one level of quoting removed.
    An unterminated quote runs to the end of `src` (fail-closed: more text)."""
    out: list[str] = []
    n = len(src)
    while i < n:
        c = src[i]
        if c in _SHELL_WORD_END:
            break
        if c == "'":
            j = src.find("'", i + 1)
            j = n if j < 0 else j
            out.append(src[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\" and i + 1 < n and src[i + 1] in '"\\$`\n':
                    i += 1
                out.append(src[i])
                i += 1
            i += 1
        elif c == "\\" and i + 1 < n:
            out.append(src[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# T-5140 r4: constructs inside which `_scan` splits a segment at `;`, `|`,
# `&` or newline although bash reads one word: `${x:-;}`, `${x[;]}`,
# `${x//;/}`, process substitution `<(...;)`/`>(...)`, an assignment
# subscript `a[x|y]=`, `$[...]`, and extglob `@(a|b)` and its siblings.  The
# other splits (`case` pattern `|`, `a#;b`, an unquoted glob `a[;]`) end the
# word in bash too.  Where any of these occurs the segment gate is skipped.
_SEG_CROSSING = re.compile(r"\$\{|[<>?*+@!]\(|\w\[|\$\[")


def _executable_code_scan_overflow(command: str) -> bool:
    """Whether `command`, or executable shell text inside it, exceeds
    `_scan_cached`'s structural cap (T-5140), or holds such text this walk
    cannot form (unknown: fail closed).

    The outer command is scanned in the current reading; a cap miss there
    is a deny (r2: `_scan`'s callers would otherwise fall back to raw
    readings).  The outer shell treats a quoted `bash -c` operand, and a
    quoted heredoc body a shell reads, as data, but the child shell parses
    those bytes as code.  So the heredoc bodies not proven data
    (`_shell_read_heredoc_bodies`) and every code string (`_py_code_strings`
    with `keep_dynamic`) are walked.  r3: a code string is the WHOLE word
    after quote removal, as `_PyLexer` forms it for the gap-(1) push reader
    (`bash -c ':;''$(...'`, the `'\\''` idiom, `bash -c \\$\\(...`), never
    one quote span of it.

    Only a text passing `_scan_depth_may_exceed` can overflow (nor can any
    text derived from it), so each gate below skips only texts that cannot:
    a top-level segment (r4: used only where no `_SEG_CROSSING` construct
    occurs; outside those a word never crosses a separator `_scan` splits
    at), a
    simple command's joined words (every code string it yields is drawn from
    them), and each child.  A child that does pass is read to the limits
    `_py_expand_code` uses; a lexer frame limit, the depth bound or the
    re-read budget leaves its text unformed, and that is a deny.  Every
    child is formed (lexed) before any is scanned, and the deepest is
    scanned first, so a deep shape stops before its long levels are
    scanned."""
    overflow = [False]
    scanned = _scan(command, overflow)
    if overflow[0]:
        return True
    budget = _PY_CODE_BUDGET * len(command) + 4096
    work: list[tuple[str, int]] = []
    if (scanned is None or _SEG_CROSSING.search(command)
            or any(_scan_depth_may_exceed(raw) for raw, _masked in scanned[0])):
        work.append((command, 0))
    if "<<" in command:
        # Bodies are raw slices of `command`, read whatever the segment gate
        # says; heredocs inside a code string are out of scope (T-5158).
        for a, e in _shell_read_heredoc_bodies(command, scanned):
            if _scan_depth_may_exceed(command[a:e]):
                budget -= e - a
                work.append((command[a:e], 1))
    children: list[str] = []
    while work:
        text, depth = work.pop()
        if depth:
            children.append(text)
        codes = _overflow_code_strings(text)
        if codes is None:
            return True  # words past the lexer's frame limit: unknown
        for code in codes:
            budget -= len(code)
            if depth >= _PY_CODE_DEPTH or budget < 0:
                return True  # an operand past the read bound: unknown
            work.append((code, depth + 1))
    for text in reversed(children):  # deepest first: it overflows soonest
        overflow = [False]
        _scan(text, overflow)
        if overflow[0]:
            return True
    return False


# An ANSI-C string with an escape: its decoded text can spell any word.
_ANSI_C_ESCAPE = re.compile(r"\$'[^']*\\")


def _may_hold_code_strings(text: str) -> bool:
    """A necessary condition for `_py_code_strings` (any `keep_dynamic`) to
    yield a code string for a command lexed from `text`.

    It needs an `eval` command word or an option word (`-`/`+` and then
    `c`; script/flock `--command`, env `-S`/`--split-string` and their
    abbreviations).  `_PyLexer` forms a word by removing quote characters,
    escaping backslashes and backslash-newlines from its raw text (or by
    decoding ANSI-C escapes: then this answers True), so a literal `eval`
    word is a substring of `text` with those characters removed, and an
    option word's leading `-`/`+` comes before its `c`, `S` or `s` there."""
    if _ANSI_C_ESCAPE.search(text):
        return True
    plain = text.replace("\\\n", "").translate(_QUOTE_STRIP)
    if "eval" in plain:
        return True
    starts = [k for k in (plain.find("-"), plain.find("+")) if k >= 0]
    if not starts:
        return False
    rest = plain[min(starts) + 1:]
    return "c" in rest or "S" in rest or "s" in rest


@functools.lru_cache(maxsize=4)
def _overflow_code_strings(text: str) -> tuple[str, ...] | None:
    """The code strings of `text` (`_py_code_strings` with `keep_dynamic`)
    that pass `_scan_depth_may_exceed`, or None when `_PyLexer` stopped at
    its frame limit (the rest of `text` is unread).  A simple command whose
    joined words fail the prefilter yields none that pass, so it is skipped
    unread.  Lexing does not depend on `decide()`'s reading, so this is
    cached: the second (MF3) reading of a `<<` command reuses it."""
    if not _may_hold_code_strings(text):
        return ()
    lex = _py_lex(text)
    if lex.frame_limited:
        return None
    out: list[str] = []
    for c in lex.cmds:
        if _scan_depth_may_exceed("".join(c.words)):
            out += [code for code in _py_code_strings(c, keep_dynamic=True)
                    if _scan_depth_may_exceed(code)]
    return tuple(out)


def _skip_shell_options(nxt, smode: list | None = None):
    """Consume a shell's options. Return (whether -c was given, the next token).
    Whether -s (read commands from stdin) was given is appended to `smode`."""
    cmode = False
    tok = nxt()
    if smode is not None:
        smode.append(False)
    while tok is not None and tok[0] == "word":
        v = _dequote(tok[1])
        if v == "--":
            tok = nxt()
            break
        if v in _SHELL_VALUE_OPTS:
            tok = nxt()
            if tok is not None and tok[0] == "word":
                tok = nxt()
            continue
        if v.startswith("--"):  # --norc, --login, --rcfile=x, ...
            tok = nxt()
            continue
        if len(v) < 2 or v[0] not in "-+":
            break
        if v[0] == "-" and "c" in v[1:]:
            cmode = True
        if smode is not None and v[0] == "-" and "s" in v[1:]:
            smode[0] = True
        tok = nxt()
        if v[-1] in "oO" and tok is not None and tok[0] == "word":
            tok = nxt()  # `-euo pipefail`: the cluster's o/O takes the next word
    return cmode, tok


class _CodeBudget:
    """Shared by one decision's nested code-string checks, so re-reading
    code strings stays linear in the command's length. T-4363 measured deep
    shlex.quote nesting: each level is a third the size of the one around
    it, so all levels together re-read ~0.5x the command, under this cap."""

    def __init__(self, size: int, legacy: bool = False):
        self.chars = 2 * size + _TOKENIZE_MAX_CHARS
        # T-4361: the whole decision reads wrapper options one way, set here:
        # `legacy` = the pre-T-4361 grammar (a long option takes a value
        # only when spelled in full), else the probed tables (`_LONG_OPTIONS`).
        # `diverged`: the two ways parsed some option word differently.
        self.legacy = legacy
        self.diverged = False
        # T-4426 r3: a `_NonLiteralSink` the walks report command words to, or None.
        # T-4506: on a level `_scan` cannot read, `_RawLevel` reports to it.
        self.sink = None


def _escalation_walk(text: str, spans: dict[int, int] | None, raw: str,
                     rawmap: tuple[int, ...] | None, depth: int, budget: _CodeBudget,
                     xspans: dict[int, int] | None = None, stdin: bool = False,
                     bodies: tuple[tuple[int, int, int, bool], ...] = ()) -> str | None:
    """Layer 2: "static" if a word at command position is an escalator after
    dequoting and path-stripping (or a code string holds one), "dynamic" if
    a dynamic command word sits in a command that mentions one, "stdin" if
    a shell reads its commands from a pipe or here-string in a command that
    mentions one, "option" if a wrapper option word is refused (`refused`),
    else None. `stdin`: `text` is a code string whose own
    stdin is a pipe in a command that mentions one (T-4360)."""
    tokens = _walk_words(text, spans, raw, rawmap, xspans)
    pending: list = []  # words spliced in by env -S, next first

    def nxt():
        return pending.pop() if pending else next(tokens, None)

    mention: list[bool] = []

    def mentions() -> bool:
        if not mention:
            mention.append(bool(_ESCALATOR_MENTION.search(raw)))
        return mention[0]

    def feeds() -> bool:
        """T-4360: the current command reads a pipe in a command that
        mentions an escalator, so a code string it runs as a whole command
        inherits that pipe (`echo 'sudo x' | ssh h 'bash -s'`,
        `... | bash -c bash`, `... | eval 'bash'`)."""
        return piped and (stdin or mentions())

    def code(tok, ssh: bool = False) -> str | None:
        """The escalation check on a word that is a whole command line. A
        word inside an interpreter string is left to that string's own check,
        which reads it at the right quoting level."""
        if len(tok) > 4 and tok[4]:
            return None
        start = tok[3] if len(tok) > 3 else None
        # T-5360: a word `env -S` spliced in (no raw position) is already the argv word env
        # passes on (GNU's -S grammar removed its quoting): read as is, not shell-dequoted
        # again (`env -S "bash -c 'rm\_x'"` runs the code `rm\_x`, backslash and all).
        value = _shell_word(raw, start) if start is not None else str(tok[1])
        # `ssh -o 'ProxyCommand sudo x' host`: `_scan` reads ssh's first
        # quoted word as its code string, so the option's command line
        # arrives here (T-4351). Only a leading non-command word is dropped,
        # and only for a string that follows ssh.
        m = _SSH_CODE_OPTION.match(value) if ssh else None
        if m:
            value = value[m.end():]
        return code_value(value)

    def code_value(value: str) -> str | None:
        """The escalation check on a command line read with its quotes
        dropped: a whole word (from `code()`) or an option value
        (`-c'sudo x'`, `-oProxyCommand=sudo x`)."""
        budget.chars -= len(value)
        if depth + 1 >= _CODE_DEPTH_MAX or budget.chars < 0:
            # Past the cap, fail closed: a pipe-fed string is not read, so it
            # may hold a shell reading that pipe.
            if feeds():
                return "stdin"
            return "static" if _ESCALATOR_MENTION.search(value) else None
        sink = budget.sink
        if nl is None or nl.is_text(nl.last_pos):
            # T-4426 r3: a code string in comment or heredoc body text is not
            # judged by that rule. T-4506: one read from a level `_scan` gave
            # up on was judged by `_RawLevel`, which reads that level's code
            # strings itself.
            budget.sink = None
        try:
            return _escalation_reason(value, depth + 1, budget, feeds())
        finally:
            budget.sink = sink

    def check_codes(codes: list) -> str | None:
        for c in codes:
            if c[0] == "str":
                why = None if c[2] else code_value(c[1])
            else:
                why = code(c)
            if why:
                return why
        return None

    def reads_stdin_code(tok) -> bool:
        """T-4351: a shell (or parallel) with no command operand runs the
        command lines on its stdin. Fed by a pipe (`echo 'sudo x' | bash`,
        `sed 's/^/sudo /' f | sh`) or a here-string (`bash <<< "sudo x"`),
        those lines are built at run time, so the hook denies when the
        command mentions an escalator (the dynamic-word rule's shape). A
        heredoc body is already walked as commands; `bash < file` is not
        judged, except after a pipe-fed group has opened, where any shell
        reading stdin denies (T-4363, fail-closed). Inside a code string fed
        such a pipe (`stdin`), the string counts as mentioning one (T-4360).
        A comment after the shell is no operand (`... | bash # c`, T-4375)."""
        if not (stdin or mentions()):
            return False
        if tok is not None and tok[0] == "word" and _HERE_STRING.match(tok[1]):
            return True
        return piped

    sep_run: list[str] = []  # the separators since the last word
    sep_tail = [""]  # `_sep_tail` folded over `sep_run`
    # The current command reads a pipe (`a | cmd`, `a |& cmd`). Cleared past
    # xargs (its child's stdin is /dev/null unless -a) and parallel (unless
    # --pipe/--pipepart), which pass the piped text as ARGUMENTS:
    # `grep -l sudo *.sh | xargs bash -n` only syntax-checks files.
    # Known residual: `... | xargs -0 bash -c` runs piped text as a -c code
    # string, which the hook cannot read (not among T-4351's items).
    # In a code string fed a pipe (`stdin`), every command not fed by a pipe
    # of its own reads the inherited one (`ssh h 'cd x; bash -s'`).
    piped = stdin
    # T-4363: the open groups (`{ }`, `( )`, `$( )`) and compound commands
    # (if/while/until/for/select/case), innermost last, as (closing word,
    # whether it reads an inherited pipe). A group fed by a pipe passes it to
    # each of its commands without a pipe of their own, after `;`, `&&`,
    # `||` and newlines too (`echo 'sudo x' | { cd x; bash; }`), the same way
    # a pipe-fed code string does (`stdin`). A closer is an unquoted literal
    # word (`}`, `done`; not `"}"` or `\}`, as in bash) that matches the
    # innermost group; any other is ignored (a `case` pattern's `)`).
    # Invariant (T-4363 review): once a pipe-fed group has opened, inheritance
    # never drops again for the rest of the walk (`fed_seen`). The walk reads
    # comments and heredoc bodies as commands, so a `)`, `}` or `done` there
    # could close a group early (`... | ( # step 1)\n bash )`); a close can
    # therefore never lower inheritance, and a mismatch fails closed by
    # construction. Known over-block: `echo 'sudo x' | { :; }; bash`.
    # Residual: a function body reads its CALLER's stdin
    # (`f() { bash; }; echo 'sudo x' | f`).
    groups: list[tuple[str, bool]] = []
    fed_seen = False
    # T-4375: a comment at command position (`echo 'sudo x' | # c\nbash`)
    # does not consume the pipe: bash continues the pipeline past the
    # comment to the next command. `comment_pipe` holds the pipe a comment
    # took until the first command (or group opener) on a later line takes
    # it. It only ever adds a pipe, so it cannot lower what the walk judges
    # fed; the comment's own words are still walked as commands.
    # Named residuals (T-4375 review round 2):
    #   - (fixed, T-4389 part B(1)) a heredoc opened BEFORE the pipe: see
    #     `body_pipe` below.
    #   - (fixed, T-4389 part B) `_cmd_positions` and `_GhPrefix.after_anchor`
    #     matched `_KEYWORD_RUN` from every anchor, quadratic on blank-line
    #     runs and keyword chains; they now share the memoized `_KeywordRuns`.
    comment_pipe = False
    # T-4389 B(1): a heredoc opened BEFORE the pipe (`sed 's/^/sudo /' <<E |
    # \nid\nE\nbash`): bash connects the pipe to the first command after the
    # body's delimiter line (bash-verified), not to the body's first line.
    # The body's words are still walked as commands (the fail-closed reading,
    # as for comments), and keep the pipe state they had before; in addition
    # `body_pipe` holds the pipe state on entering a body (`_scan`'s
    # `bodies`, raw spans to each delimiter line's end), and `body_hand`
    # passes it to the first command (or group opener) past the last of the
    # bodies in a row, through `pipe_ended`. Invariant: it only ever adds a
    # pipe, so it cannot lower what the walk judges fed.
    body_spans = bodies or ()
    body_k = 0  # the first span not yet passed
    in_body = body_pipe = body_hand = False

    def track_body(at: int | None) -> None:
        nonlocal body_k, in_body, body_pipe, body_hand
        if at is None:
            return
        while body_k < len(body_spans) and body_spans[body_k][2] <= at:
            body_k += 1
        inside = body_k < len(body_spans) and body_spans[body_k][0] <= at
        if inside and not in_body:
            body_pipe = pipe_ended()
        elif in_body and not inside:
            body_hand = body_hand or body_pipe
            body_pipe = False
        in_body = inside

    def inherited() -> bool:
        return fed_seen or (groups[-1][1] if groups else stdin)

    def pipe_ended() -> bool:
        """The separators since the last word end in a pipe (`|`, `|&`), or
        a comment line after a pipe passed it on (T-4375)."""
        run = sep_tail[0]
        return (comment_pipe or body_hand or (run.endswith("|") and not run.endswith("||"))
                or run.endswith("|&"))

    def raw_start(tok) -> int | None:
        """The index in `raw` where a word token really starts. A word after
        a `\\`+newline continuation can start inside that pair (on its
        newline, where `_scan`'s raw offsets land), so step past
        continuations first (`... | \\<newline>while`)."""
        if len(tok) <= 3 or tok[3] is None:
            return None
        s = tok[3]
        while True:
            if raw.startswith("\\\n", s):
                s += 2
            elif raw.startswith("\n", s) and s > 0 and raw[s - 1] == "\\":
                s += 1
            else:
                return s

    # T-4457 round 2: an assignment word can run on over several tokens
    # (`a[x y z]=v`, `x=${y:-a b}`, `x=${y:-a;b}`, `x=${y:-$(pwd)}`). Its end
    # is read on the raw text (`_AssignRuns.word_end`), and the first token
    # at or past that end is at command position. The tokens inside are
    # still walked as before (a `$(...)` in the value runs), so this only
    # adds command positions.
    assign_runs: list[_AssignRuns] = []
    resume: list[int] = []  # a heap of those ends

    def note_assign(tok) -> None:
        if len(tok) > 4 and tok[4]:  # in an interpreter string: `raw` is a level too deep
            return
        s = raw_start(tok)
        if s is None:
            return
        if not assign_runs:
            assign_runs.append(_AssignRuns(raw, quotes=True))
        e = assign_runs[0].word_end(s)
        if e is not None and e > s + len(tok[1]):
            heapq.heappush(resume, e)

    def comment_word(tok) -> bool:
        """T-4375: `tok` is an unquoted word starting with `#` (read on the
        raw text, so `"#"`, `\\#` and `a#b` are not), which bash reads as a
        comment when it starts a word."""
        if tok is None or tok[0] != "word" or tok[1][:1] != "#":
            return False
        s = raw_start(tok)
        return s is not None and raw.startswith("#", s)

    def refused(name: str, seen: list[str]) -> bool:
        """T-4361: a long option word the wrapper refuses (unknown, an
        ambiguous prefix, a value given to a flag) in a command that mentions
        an escalator or reads a fed pipe. The tool would not run, but this
        hook's table may differ from the installed version's, where the
        word could take a value; fail closed rather than guess. It denies
        at the end of the walk, so a more specific reason is reported first."""
        return (not budget.legacy and (stdin or mentions())
                and any(s[:2] == "--" and s != "--" and _long_option(name, s[2:])[1] == "?"
                        for s in seen))

    refused_seen = False
    in_comment = False  # inside a comment that took a pipe (review round 2)
    ssh_pending = False  # the previous token was ssh's own arguments
    # T-4457: in a command holding an array assignment, a word glued to a
    # `)` (`x=(a)#x sudo id`) is the rest of that assignment word, so the
    # word after it is at command position (`_AssignRuns.after_glued_close`).
    array_text = "=(" in text
    close_seen = False  # the previous token was a `)`
    # T-4426 r3: each word this walk reads at command position (and each
    # separator, for the frames it opens) goes to the non-literal rule.
    nl = (_NonLiteralLevel(budget.sink, text, spans, raw, rawmap, xspans)
          if budget.sink is not None and rawmap is not None else None)
    cmdpos = True
    tok = nxt()
    while tok is not None:
        kind, word, flag = tok[0], tok[1], tok[2]
        if body_spans:
            track_body((tok[5] if len(tok) > 5 else None) if kind == "sep" else raw_start(tok))
        if resume:
            at = (tok[5] if len(tok) > 5 else None) if kind == "sep" else raw_start(tok)
            if at is not None and at >= resume[0]:
                while resume and resume[0] <= at:
                    heapq.heappop(resume)
                cmdpos = True  # T-4457 round 2: the word after a long assignment
        after_ssh, ssh_pending = ssh_pending, False
        after_close, close_seen = close_seen, kind == "sep" and word == ")"
        if kind == "sep":
            sep_run.append(word)
            sep_tail[0] = _sep_tail(sep_tail[0], word)
            if word in ("{", "(", "$("):
                # Right after a word, the group sits in that word's command
                # and reads its stdin (`... | time { cd x; bash; }`,
                # `... | tee $(bash)`); else the command it starts reads the
                # pipe before it, or the one its enclosing group inherits.
                fed = piped if len(sep_run) == 1 else inherited() or pipe_ended()
                groups.append(("}" if word == "{" else ")", fed))
                fed_seen = fed_seen or fed
            elif word == ")" and groups and groups[-1][0] == ")":
                groups.pop()
        elif sep_run:
            piped = inherited() or pipe_ended()
            # A comment runs to the end of its line: a `;` or `|` in it
            # separates nothing, so only the next line's command takes it.
            if "\n" in sep_run:
                comment_pipe = False
            if not in_body:
                body_hand = False  # B(1): taken by this command
            sep_run.clear()
            sep_tail[0] = ""
        if kind == "sep":
            if nl is not None and not tok[4]:
                nl.sep(tok, cmdpos)
            # `$(...) x` / `` `...` x ``: the command word is a substitution.
            if cmdpos and not flag and word in ("$(", "`") and mentions():
                return "dynamic"
            if tok[3] is not None:  # an interpreter string: check it as a command line
                why = code(("word", "", False, tok[3], tok[4]), ssh=after_ssh)
                if why:
                    return why
            cmdpos = True
            in_comment = False
            tok = nxt()
            continue
        if nl is not None and (cmdpos or nl.tail is not None or nl.pause is not None):
            nl.word(tok, cmdpos)
        if after_close and array_text and cmdpos:
            s = raw_start(tok)
            if s is not None and s > 0 and raw[s - 1] == ")":
                tok = nxt()  # T-4457: glued to the `)`; cmdpos stays
                continue
        v = _dequote(word)
        if v in _FIND_EXEC_WORDS:
            cmdpos = True
            tok = nxt()
            continue
        # T-4375 review round 2 (M1): `_scan` joins a `\`+newline inside a
        # comment, which bash does not (`... | # c \<newline>bash`), so the
        # next line's command lands mid-comment. Fail-closed: after a `#` at
        # command position that holds a pipe, each shell word in the rest of
        # that comment is judged as a command (with its own operands), so
        # one with no script operand reads the pipe.
        if not cmdpos and not (in_comment and v.rsplit("/", 1)[-1] in _SHELLS):
            tok = nxt()
            continue
        if _REDIRECT_WORD.match(word):
            tok = nxt()
            if _REDIRECT_OPERATOR.fullmatch(word) and tok is not None and tok[0] == "word":
                tok = nxt()  # the redirection's target
            continue
        # T-4363: a compound command opens a group (`... | while read l; do
        # bash; done`); its closing word, or a `}`, closes the innermost one.
        # Both are matched on the RAW word: bash reads only an unquoted
        # literal as a reserved word or `}` (`"done"`, `\fi`, `''}` are
        # commands). `word` omits a quote `_scan` dropped from a command word,
        # so the raw text at the word's start (`raw_start`) must be the word
        # itself, followed by no quote or escape. T-4375: a `\`+newline right
        # after it is a word boundary, not an escape: bash joins the
        # continuation and still reads the keyword (`... | while\<newline>
        # read l; do bash; done`).
        s = raw_start(tok)
        literal = False
        if s is not None:
            e = s + len(word)
            literal = (raw.startswith(word, s)
                       and (raw[e:e + 1] not in ("'", '"', "\\")
                            or raw.startswith("\\\n", e)))
            # T-4375: an unquoted `#` word at command position starts a
            # comment. It keeps the pipe for the next command (see
            # `comment_pipe`); the walk still reads its words as before.
            if comment_word(tok):
                comment_pipe = comment_pipe or piped
                in_comment = comment_pipe
        if literal and word in _COMPOUND_OPEN:
            groups.append((_COMPOUND_OPEN[word], piped))
            fed_seen = fed_seen or piped
        elif literal and word in _COMPOUND_CLOSE:
            if groups and groups[-1][0] == word:
                groups.pop()
            cmdpos = False
            tok = nxt()
            continue
        if v in _KEYWORD_WORDS or _ASSIGN_WORD.match(v):
            if v not in _KEYWORD_WORDS:
                note_assign(tok)
            tok = nxt()
            continue
        if flag:
            if mentions():
                return "dynamic"
            cmdpos = False
            tok = nxt()
            continue
        base = v.rsplit("/", 1)[-1]
        if base in _ESCALATOR_WORDS:
            return "static"
        if base in _SHELLS:
            smode: list[bool] = []
            cmode, tok = _skip_shell_options(nxt, smode)
            if cmode and tok is not None and tok[0] == "word":
                why = code(tok)
                if why:
                    return why
                tok = nxt()
            # T-4375 review round 2 (M3): a comment is no script operand
            # (`... | bash # c`), so the shell reads its stdin.
            if not cmode and (smode[0] or tok is None or tok[0] == "sep"
                              or _REDIRECT_WORD.match(tok[1]) or comment_word(tok)):
                if reads_stdin_code(tok):
                    return "stdin"
            if not cmode or (tok is not None and tok[0] == "word"):
                cmdpos = False  # a script path, or the code string's $0 and args
            continue
        if base in _WRAPPER_GRAMMAR:
            codes: list = []
            seen: list[str] = []
            tok = _skip_wrapper_options(base, nxt, pending, codes, seen, budget,
                                        note_assign)
            why = check_codes(codes)
            if why:
                return why
            refused_seen = refused_seen or refused(base, seen)
            # `command -v x` / `command -V x` only looks x up (T-4351 replay:
            # `command -v systemd-run`). Only this arc's new names are
            # exempt; every other word is judged exactly as before.
            if (base == "command" and tok is not None and tok[0] == "word" and not tok[2]
                    and _dequote(tok[1]).rsplit("/", 1)[-1] in _T4351_LOOKUP_EXEMPT
                    and any("v" in s or "V" in s for s in seen if s[:2] != "--")):
                cmdpos = False
                tok = nxt()
                continue
            # systemd-run without --user asks the system manager, which runs
            # the command (or -S shell) as root by default (T-4351). With no
            # command it runs nothing (`case $b in tmux|systemd-run) ...`).
            if base == "systemd-run" and "--user" not in seen and (
                    (tok is not None and tok[0] == "word")
                    or any(s == "--shell" or (s[:2] != "--" and "S" in s) for s in seen)
                    or (not budget.legacy and any(
                        s[:2] == "--" and _long_option(base, s[2:])[0] == "shell" for s in seen))):
                return "static"
            if base == "machinectl":  # `machinectl shell` is a root shell
                if tok is not None and tok[0] == "word" and _dequote(tok[1]) == "shell":
                    return "static"
                cmdpos = False
                continue
            if base == "script":
                # Options may follow the log file (getopt permutes); none of
                # its words is a command, only a -c value. Without -c it
                # starts $SHELL, which reads stdin like a bare `bash`.
                shorts, longs, _ops = _WRAPPER_GRAMMAR[base]
                has_code = bool(codes)
                here_string = None
                while tok is not None and tok[0] == "word":
                    if here_string is None and _HERE_STRING.match(tok[1]):
                        here_string = tok
                    tok = _skip_options(base, shorts, longs, nxt(), nxt, pending, codes, seen, budget)
                    has_code = has_code or bool(codes)
                    why = check_codes(codes)
                    if why:
                        return why
                    refused_seen = refused_seen or refused(base, seen)
                    codes.clear()
                    seen.clear()
                if not has_code and reads_stdin_code(here_string):
                    return "stdin"
                cmdpos = False
                continue
            # getopt_long takes an unambiguous prefix (`--arg=f`, `--pip`), so
            # any prefix of a stdin-passing long option keeps the pipe.
            if base == "xargs" and not any(
                    _long_prefix_of(s, ("arg-file",)) or (s[:2] != "--" and "a" in s)
                    for s in seen):
                piped = False
            if base == "parallel":
                if (tok is None or tok[0] == "sep" or _REDIRECT_WORD.match(tok[1])
                        or comment_word(tok)):
                    if reads_stdin_code(tok):  # command lines from stdin
                        return "stdin"
                    cmdpos = False
                    continue
                if not any(_long_prefix_of(s, ("pipe", "pipepart", "spreadstdin"))
                           for s in seen):
                    piped = False
                if _dequote(tok[1]) == ":::":
                    # No command: each argument is a command line.
                    tok = nxt()
                    while tok is not None and tok[0] == "word" and _dequote(tok[1]) != ":::":
                        why = code(tok)
                        if why:
                            return why
                        tok = nxt()
                    cmdpos = False
                    continue
            if base in _CODE_STRING_WRAPPERS and tok is not None and tok[0] == "word":
                inner = tok[1]
                # `watch -n 5 "sudo x"`: one quoted word is a whole command line.
                # T-4363: so is a partly quoted one, read with its quotes
                # dropped (`ssh h s'udo x'`, `echo 'sudo x' | ssh h b'ash -s'`):
                # these wrappers join their words into one line, so a quoted
                # space no longer splits it. Residual: a quoted separator in
                # a LATER word (`ssh h echo x';' 'sudo y'`) is not read.
                if (len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]) or (
                        not tok[2] and ("'" in inner or '"' in inner)):
                    why = code(tok, ssh=base == "ssh")
                    if why:
                        return why
                    cmdpos = False
                    tok = nxt()
            ssh_pending = base == "ssh"
            continue
        cmdpos = False
        tok = nxt()
    return "option" if refused_seen else None


def _escalation_reason(command: str, depth: int = 0, budget: _CodeBudget | None = None,
                       stdin: bool = False, sink: "_NonLiteralSink | None" = None) -> str | None:
    """"static", "dynamic", "stdin" or "option" if `command` escalates for a subagent
    (the two layers above), else None. `stdin`: see `_escalation_walk`. `sink`
    (T-4426 r3): every walk of this decision reports its command words to it."""
    if budget is None:
        # T-4361: read wrapper options as the tools do. If that allows but
        # some option word parsed differently from the pre-T-4361 grammar,
        # the old reading decides too, so this change only adds denials.
        budget = _CodeBudget(len(command))
        budget.sink = sink
        why = _escalation_reason(command, depth, budget, stdin)
        if why is None and budget.diverged:
            legacy = _CodeBudget(len(command), legacy=True)
            legacy.sink = sink
            why = _escalation_reason(command, depth, legacy, stdin)
        return why
    scanned = _scan(command)
    if scanned is None and budget.sink is not None:
        # T-4506: `_NonLiteralLevel` follows `_scan`, which gave up on this
        # level; `_RawLevel` classifies its command words (and those of its
        # code strings) instead.
        _raw_nonliteral(command, depth, budget.sink)
    if scanned is None:  # unbalanced quoting: read the raw text, a bare quote an anchor
        # T-4413: `_scan` also gives up where it would fall out of step with
        # bash (a `case` pattern in a group), so this path must not be the
        # weaker one: the raw walk reads `\`+newline+`bash` as one word, so
        # the text with each continuation joined, as bash reads it, is
        # walked too. Deny if either reading escalates (only adds denials).
        for text in (command, command.replace("\\\n", "  ")) if "\\\n" in command else (command,):
            if _regex_rule_escalates(text, _CMD_ANCHOR_CHARS_RAW):
                return "static"
            why = _escalation_walk(text, None, text, None, depth, budget, stdin=stdin)
            if why is not None:
                return why
        return None
    _segs, masked, rawmap, spans, xspans = scanned
    if _regex_rule_escalates(masked, _CMD_ANCHOR_CHARS):
        return "static"
    return _escalation_walk(masked, spans, command, rawmap, depth, budget, xspans, stdin,
                            scanned.bodies)


def _escalation(command: str) -> bool:
    return _escalation_reason(command) is not None


# --- gh CLI: a (group, verb) READ allowlist for ANY subagent (T-4145) ---
# T-4135 closed named gh write shapes one by one (`gh api` writes, graphql
# mutations, `gh repo` admin verbs); T-1345's `_PUSH` covers `gh pr create/merge`.
# That per-verb blocklist left the rest of the gh write surface open (release
# create, secret/variable set, workflow run, run rerun/cancel, pr comment/review/
# close, issue create, label create/clone, gist create, auth token, ...) and was
# bypassed outright by a leading global flag (`gh -R o/r pr merge 5`). Workers
# write nothing to GitHub (T-1345/T-1673), so the fix is the inverse shape:
# every gh invocation at command position is parsed, and only a listed
# (group, verb) PAIR passes. Pairs, not bare verbs: a bare `clone`/`status`
# would pass `label clone` and `auth status -t`.
#
# Parsing reuses `_scan`: a gh word is found at command position in the MASKED
# text (so a commit message or grep pattern that mentions gh is data), including
# a path prefix (/usr/bin/gh), `VAR=val` prefixes, the escalation rule's wrapper
# list, a dequoted `g"h"`, and code strings handed to bash/sh -c, eval and ssh.
# Its arguments are then read back from the RAW text via `_scan`'s rawmap and
# tokenized with shlex, up to the next top-level separator.
#
# Fail-closed: an unknown group or verb (user aliases and extensions included),
# any leading flag other than -R/--repo/--hostname, a group/verb that comes from
# `$(...)`/backticks/`$VAR`, or a gh segment that cannot be tokenized all DENY.
# `gh api` keeps the T-4135 method rule above; the allowlist only defers to it.
_GH_READ_ALLOWLIST: dict[str, frozenset[str] | None] = {
    # None = any verb (or none) passes. `ls` is gh 2.45's built-in alias of
    # every `list` (from `gh <group> list --help`), so it rides along.
    "pr": frozenset({"view", "list", "ls", "status", "diff", "checks", "checkout", "co"}),
    "issue": frozenset({"view", "list", "ls", "status"}),
    "run": frozenset({"view", "list", "ls", "watch", "download"}),
    "workflow": frozenset({"view", "list", "ls"}),
    "release": frozenset({"view", "list", "ls", "download"}),
    "repo": frozenset({"view", "list", "ls", "clone"}),
    "search": None,
    "status": None,
    "ruleset": frozenset({"list", "ls", "view", "check"}),
    "cache": frozenset({"list", "ls"}),
    "secret": frozenset({"list", "ls"}),
    "variable": frozenset({"list", "ls"}),
    "label": frozenset({"list", "ls"}),
    "auth": frozenset({"status"}),
    "help": None,
    "version": None,
}
# Built-in top-level alias: `gh co` is `gh pr checkout`.
_GH_BUILTIN_ALIASES = {"co": ("pr", "checkout")}
_GH_GLOBAL_VALUE_FLAGS = ("-R", "--repo", "--hostname")
_GH_DYNAMIC = re.compile(r"[$`*?\[]")

_GH_WORD = r"(?:[^\s;&|`()<>{}!'\"=]*/)?gh(?![^\s;&|`()<>])"
# The escalation wrappers, plus `ssh` for an unquoted remote command
# (`ssh host gh pr merge 5`) and the command-running `coproc`/`watch`/`parallel`.
# Lax like the sudo rule: gh as any later word of the wrapper's segment.
_GH_WRAPPER = r"(?:" + "|".join(_SUDO_WRAPPERS + ("ssh", "coproc", "watch", "parallel")) + r")\b"
# Wrappers that append their own arguments to the command: a bare `gh` under
# them (`echo pr merge 5 | xargs gh`) gets its group and verb at run time.
_GH_ARG_APPENDING = frozenset({"xargs", "parallel"})
# The wrapper's lazy scan stops at every character that can start another
# command position (`{`, `!`, quotes, separators), so each scan is bounded by
# the next anchor and the whole search stays linear (review round 2: a run of
# `{env ` or `'env ` anchors was quadratic).
#
# T-4154: the command-position match was one regex,
#   (cmd_position(_CMD_ANCHORS) + VAR=... + [wrapper, lazy scan to a space]
#    | cmd_position(quote) + VAR=...) + gh word,
# and the dynamic-word check the same prefix followed by `$`/backtick. The
# `VAR=value` prefix rescanned the whole assignment run from every anchor
# inside it (`A={ A={ ...`: 2.7 s at 3000 tokens, and a slow guard fails
# OPEN). `_gh_cmd_matches` and `_gh_dynamic_cmd` now compute the same
# matches in linear time. They reuse the escalation rule's memoized
# assignment runs (`_AssignRuns`) and look up, for each wrapper, the first
# space-then-gh (or space-then-`$`/backtick) hit before the scan's stop.
# `_gh_cmd_matches` simulates `finditer`: start positions in order, the
# alternatives in the regex's order, and no match starting inside the
# previous one.
#
# A quote in the MASKED text is either a data string's delimiter (its content
# is blanked, so no gh can follow) or a quote INSIDE an interpreter string,
# i.e. a second level of nesting (`bash -c "bash -c 'gh pr merge 5'"`); count
# it as command position so that nesting stays visible (assignments only; a
# wrapper there is left to the raw-text floor).
#
# A command word this hook cannot read (`$CMD ...`, `eval "$CMD"`, `$(...) ...`,
# `` `echo gh` pr merge ``) in a command that mentions gh: the gh call may be
# staged in a variable or substitution. (No quote anchor there: `echo "$(gh pr
# view 5)"` is a substitution inside data.)
_GH_WORD_AT = re.compile(_GH_WORD)
_GH_WRAPPER_AT = re.compile(_GH_WRAPPER)
_GH_AFTER_SPACE = re.compile(r"(?=\s(" + _GH_WORD + r"))")
_DYNAMIC_AFTER_SPACE = re.compile(r"(?=\s[$`])")
# Where a wrapper's lazy scan stops: every character that can start another
# command position (T-4145 review round 2).
_GH_WRAP_STOP = re.compile(r"[;&|`\n(){}!'\"]")
# Every position where a command-position match can start.
_GH_MATCH_START = re.compile(r"[;&|`\n(){!'\"]|\$(?=\()|-exec(?=(?:dir)?\s)")
_ANCHOR_CHARS = frozenset(";&|`\n(){!")


class _GhPrefix:
    """The command-position prefix shared by `_gh_cmd_matches` and
    `_gh_dynamic_cmd`, computed once per masked text."""

    def __init__(self, text: str):
        self.text = text
        self.runs = _AssignRuns(text)
        self.keywords = _KeywordRuns(text)
        self.stops = [m.start() for m in _GH_WRAP_STOP.finditer(text)]

    def after_anchor(self, a: int) -> int:
        """Where `_cmd_position` + `_ASSIGN_PREFIX` ends for an anchor ending at `a`."""
        return self.runs.run_end(self.keywords.end(a))

    def after_exec(self, p: int) -> int | None:
        m = _FIND_EXEC.match(self.text, p)
        return self.runs.run_end(m.end()) if m else None

    def branches(self, p: int) -> tuple[list[int], list[int]]:
        """(the regex's first-alternative positions, its quote-alternative
        positions) for a match starting at p, each in the regex's order."""
        text = self.text
        c = text[p] if p < len(text) else ""
        first: list[int] = []
        quote: list[int] = []
        if p == 0:
            first.append(self.after_anchor(0))
        if c in _ANCHOR_CHARS:
            first.append(self.after_anchor(p + 1))
            g = self.runs.after_glued_close(p + 1) if c == ")" else None
            if g is not None:  # T-4457: `x=(a)#x gh ...`
                first.append(g)
        elif text.startswith("$(", p):
            first.append(self.after_anchor(p + 2))
        elif c in "'\"":
            quote.append(self.after_anchor(p + 1))
        q = self.after_exec(p) if c == "-" else None
        if q is not None:
            first.append(q)
            quote.append(q)
        return first, quote

    def wrapped(self, q: int, hits: list[int]) -> tuple[int, str] | None:
        """If a wrapper sits at q and its lazy scan reaches a hit before the
        scan's stop: (that hit, the wrapper word)."""
        m = _GH_WRAPPER_AT.match(self.text, q)
        if not m:
            return None
        e = m.end()
        i = bisect.bisect_left(hits, e)
        if i == len(hits):
            return None
        h = hits[i]
        j = bisect.bisect_left(self.stops, e)
        s = self.stops[j] if j < len(self.stops) else len(self.text)
        # The regex's `\s` before the gh word may itself be a newline stop.
        if h < s or (h == s and self.text[s] == "\n"):
            return h, m.group(0)
        return None


def _gh_cmd_matches(text: str) -> list[tuple[int, int, str | None]]:
    """(gh start, gh end, wrapper word or None) for each command-position gh
    word in the masked text, as the T-4145 `_GH_CMD.finditer` found them."""
    prefix = _GhPrefix(text)
    gh_at: dict[int, int] = {}
    hits: list[int] = []
    for m in _GH_AFTER_SPACE.finditer(text):
        hits.append(m.start())
        gh_at[m.start()] = m.end(1)
    starts = [m.start() for m in _GH_MATCH_START.finditer(text)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    out: list[tuple[int, int, str | None]] = []
    last = 0
    for p in starts:
        if p < last:
            continue
        first, quote = prefix.branches(p)
        found = None
        for q in first:
            w = prefix.wrapped(q, hits)
            if w:
                found = (w[0] + 1, gh_at[w[0]], w[1])
                break
            m = _GH_WORD_AT.match(text, q)
            if m:
                found = (q, m.end(), None)
                break
        if found is None:
            for q in quote:
                m = _GH_WORD_AT.match(text, q)
                if m:
                    found = (q, m.end(), None)
                    break
        if found:
            out.append(found)
            last = found[1]
    return out


def _gh_dynamic_cmd(text: str) -> bool:
    """True if a `$`/backtick command word sits at command position in the
    masked text, directly or after a wrapper (the T-4145 `_GH_DYNAMIC_CMD`)."""
    prefix = _GhPrefix(text)
    hits = [m.start() for m in _DYNAMIC_AFTER_SPACE.finditer(text)]
    starts = [0] + [m.start() for m in _GH_MATCH_START.finditer(text)]
    for p in starts:
        for q in prefix.branches(p)[0]:
            if text[q:q + 1] in ("$", "`") or prefix.wrapped(q, hits):
                return True
    return False


_GH_MENTION = re.compile(r"(?<![\w.-])gh(?![\w.-])")
# Unbalanced quoting: `_scan` gives up, so look for gh at any plausible command
# position in the raw text (a bare quote counts too, as in `_CMD_ANCHOR_CHARS_RAW`).
# The path class excludes every start character, so a run of quotes cannot
# make each start rescan the rest of the word (review round 2).
_GH_CMD_RAW = re.compile(r"(?:^|[\s;&|`(){!'\"])(?:[^\s;&|`(){}!'\"]*/)?gh(?![^\s;&|`()<>])")
_GH_SPAN_END = re.compile(r"[;&|\n)]")

# Local writes by allowlisted reads (T-1687 / protected docs).
_SCRATCH_PREFIXES = ("/tmp/", "/var/tmp/")


def _gh_invocations(command: str) -> list[list[str] | None]:
    """Token lists (starting at the gh word) for every gh invocation at command
    position in `command`. A None entry is a gh invocation that could not be
    tokenized, which the caller denies."""
    scanned = _scan(command)
    if scanned is None:
        # A dequoted reading too (`"gh"`, `g"h"`): only adds denials (T-4413 r4).
        hit = _GH_CMD_RAW.search(command) or _GH_CMD_RAW.search(
            command.replace("$'", "'").replace('$"', '"').translate(_QUOTE_STRIP))  # `$'gh'`, `$"gh"` (r5, r6)
        return [None] if hit else []
    _segs, masked, rawmap, _spans, _xspans = scanned
    if _GH_MENTION.search(command) and _gh_dynamic_cmd(masked):
        return [None]
    out: list[list[str] | None] = []
    budget = _TOKENIZE_MAX_CHARS
    for start, gh_end, wrap in _gh_cmd_matches(masked):
        end_m = _GH_SPAN_END.search(masked, gh_end)
        end = end_m.start() if end_m else len(masked)
        # T-4154: spans can nest (`(gh (gh (gh ...`, each running to the
        # end), so tokenizing each one was quadratic: 22 s at 3000. Past one
        # tokenize-limit of span text in total, deny (fail-closed, as for
        # one over-long span).
        budget -= end - start
        if budget < 0:
            out.append(None)
            break
        # Start just after the previous masked char, so a dropped quote of a
        # dequoted command word (`"gh" pr ...`) is kept and balances.
        raw_start = rawmap[start - 1] + 1 if start > 0 else 0
        raw_end = rawmap[end] if end < len(masked) else len(command)
        span = command[raw_start:raw_end]
        tokens = _gh_tokens(span)
        if tokens is not None and len(tokens) == 1 and wrap in _GH_ARG_APPENDING:
            tokens = None  # bare gh under xargs/parallel: group and verb come at run time
        out.append(tokens)
    return out


def _gh_tokens(span: str) -> list[str] | None:
    if len(span) > _TOKENIZE_MAX_CHARS:
        return None
    try:
        return shlex.split(span)
    except ValueError:
        pass
    # A gh call inside an interpreter string (`bash -c "gh pr view 5"`) runs up
    # to that string's closing quote; drop it and retry once.
    trimmed = span.rstrip()
    if trimmed and trimmed[-1] in "'\"":
        try:
            return shlex.split(trimmed[:-1])
        except ValueError:
            return None
    return None


def _gh_skip_global_flags(args: list[str], i: int) -> tuple[int, str | None]:
    """Skip -R/--repo/--hostname (with their values) from args[i]. Returns the
    index of the first other token, or a deny reason for any other flag."""
    while i < len(args) and args[i].startswith("-"):
        tok = args[i]
        if tok in _GH_GLOBAL_VALUE_FLAGS:
            if i + 1 >= len(args):
                return i, f"`gh ... {tok}` with no value"
            i += 2
        elif tok.startswith(("--repo=", "--hostname=")) or (tok.startswith("-R") and len(tok) > 2):
            i += 1
        else:
            return i, f"a gh flag outside the read allowlist before the command (`{tok}`)"
    return i, None


def _gh_help_requested(args: list[str]) -> bool:
    """`--help` that pflag reads as a flag: not after `--`, and not in a value
    slot (the previous token is not itself a flag, so `-b --help` is a body,
    not help). Only `--help`: gh has no `-h` help shorthand (gh 2.45 `--help`
    output), and under `auth` `-h` is --hostname (`gh auth token -h x`)."""
    for j, tok in enumerate(args):
        if tok == "--":
            return False
        if tok == "--help" and (j == 0 or not args[j - 1].startswith("-")):
            return True
    return False


def _gh_positionals(args: list[str], value_flags: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    j = 0
    while j < len(args):
        tok = args[j]
        if tok == "--":
            break
        if tok in value_flags:
            j += 2
            continue
        if not tok.startswith("-"):
            out.append(tok)
        j += 1
    return out


def _gh_flag_values(args: list[str], short: str, long: str) -> list[str]:
    """Every value given to `short`/`long` (separate, `--long=v`, or fused `-Sv`)."""
    vals: list[str] = []
    j = 0
    while j < len(args):
        tok = args[j]
        if tok == "--":
            break
        if tok in (short, long):
            vals.append(args[j + 1] if j + 1 < len(args) else "")
            j += 2
            continue
        if tok.startswith(long + "="):
            vals.append(tok[len(long) + 1:])
        elif tok.startswith(short) and len(tok) > len(short) and not tok.startswith("--"):
            vals.append(tok[len(short):])
        j += 1
    return vals


def _gh_target_reason(path: str, cwd: str, is_dir: bool) -> str | None:
    """Deny reason if a gh download/clone target is dynamic or lands under a
    protected pantheon-config doc path (reusing `_PROTECTED_DOC` and the T-3662
    worktree resolution)."""
    if path == "-":
        return None  # stdout
    if not path or _GH_DYNAMIC.search(path):
        return f"a gh download/clone into a target this hook cannot resolve (`{path}`); use a literal path"
    resolved = _resolve_against_cwd(os.path.expanduser(path), cwd)
    probe = resolved.rstrip("/") + "/_" if is_dir else resolved
    if _PROTECTED_DOC.search(probe) or _protected_doc_via_worktree(probe, cwd):
        return f"a gh download/clone into a protected canonical doc path (`{path}`)"
    return None


def _in_shared_main_checkout(cwd: str) -> bool:
    """True if `cwd` is a shared main checkout, where `gh pr checkout` would
    switch the branch under every other user of it. T-1687's shared-checkout
    rule (a cwd under `_HO_HOME`, outside an Agent-tool isolation worktree),
    plus any git MAIN checkout (`.git` a directory, not a linked worktree's
    file) outside a scratch prefix, which covers the other seats' homes.
    Fail-open without a cwd, like the rest of the module."""
    if not cwd:
        return False
    if _under_ho_home(cwd) and "/.claude/worktrees/" not in cwd + "/":
        return True
    root, _common = _worktree_root_and_common(cwd)
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return False
    return not (root + "/").startswith(_SCRATCH_PREFIXES)


def _gh_judge(tokens: list[str] | None, cwd: str) -> str | None:
    """Deny reason for one gh invocation (tokens start at the gh word), or None."""
    if not tokens:
        return (
            "a gh command this hook cannot read (unbalanced quoting, an over-long "
            "command, a command word taken from `$VAR`/`$(...)`/backticks next to a gh "
            "mention, or a bare `gh` under xargs/parallel, which append its arguments)"
        )
    if tokens[0].rsplit("/", 1)[-1] != "gh":
        return "a gh command this hook cannot parse"
    args = tokens[1:]
    if not args or args == ["--version"] or args[0] == "--help":
        return None  # bare `gh` / `gh --version` / `gh --help`: help and version text only
    i, why = _gh_skip_global_flags(args, 0)
    if why:
        return why
    if i >= len(args):
        return "a gh command with global flags and no command"
    group = args[i]
    if _GH_DYNAMIC.search(group):
        return f"a gh command whose group is computed at run time (`{group}`)"
    if group in _GH_BUILTIN_ALIASES:
        if _gh_help_requested(args):
            return None
        group, verb = _GH_BUILTIN_ALIASES[group]
        rest = args[i + 1:]
    else:
        if group == "api":
            # T-4135's method rule judges it (decide() runs it first); only make
            # sure that rule can see this call.
            return None if _gh_api_segments(" ".join(shlex.quote(t) for t in tokens)) else (
                "a `gh api` call the T-4135 method rule cannot read")
        if group not in _GH_READ_ALLOWLIST:
            return f"`gh {group}`, which is not on the subagent read allowlist"
        # `--help` only once the group is known: gh hands `--help` through to
        # extensions and aliases, which then run (T-4145 review round 1, N1).
        if _gh_help_requested(args):
            return None
        allowed = _GH_READ_ALLOWLIST[group]
        if allowed is None:
            return None
        j, why = _gh_skip_global_flags(args, i + 1)
        if why:
            return why
        if j >= len(args):
            return f"`gh {group}` with no subcommand"
        verb = args[j]
        if _GH_DYNAMIC.search(verb):
            return f"a gh command whose subcommand is computed at run time (`{verb}`)"
        if verb not in allowed:
            return f"`gh {group} {verb}`, which is not on the subagent read allowlist"
        rest = args[j + 1:]
        if verb == "co":
            verb = "checkout"
    if group == "auth":
        for tok in rest:
            if tok == "--":
                break
            if tok.startswith("--show-token") or (
                    tok.startswith("-") and not tok.startswith("--") and "t" in tok[1:]):
                return "`gh auth status --show-token`, which prints the GitHub token"
        return None
    if group == "pr" and verb == "checkout":
        if _in_shared_main_checkout(cwd):
            return (
                "`gh pr checkout` in a shared main checkout (it switches the branch "
                "for every other user of that checkout, T-1687). Run it from your "
                "own worktree; note this judges your SESSION's cwd, not a `cd`"
            )
        return None
    if group == "repo" and verb == "clone":
        # git clone flags after `--` (`--separate-git-dir <p>`, `--template=<p>`)
        # write to their own paths: every value there is checked (N2).
        if "--" in rest:
            for tok in rest[rest.index("--") + 1:]:
                value = tok.split("=", 1)[1] if tok.startswith("-") and "=" in tok else tok
                if value.startswith("-"):
                    continue
                why = _gh_target_reason(value, cwd, is_dir=True)
                if why:
                    return why
        pos = _gh_positionals(rest, ("-u", "--upstream-remote-name"))
        if not pos:
            return None  # no repo: gh errors out
        if len(pos) >= 2:
            return _gh_target_reason(pos[1], cwd, is_dir=True)
        name = pos[0].rstrip("/").rsplit("/", 1)[-1]
        name = name[:-4] if name.endswith(".git") else name
        return _gh_target_reason(name or ".", cwd, is_dir=True)
    if verb == "download" and group in ("run", "release"):
        dirs = _gh_flag_values(rest, "-D", "--dir") or ["."]
        for d in dirs:
            why = _gh_target_reason(d, cwd, is_dir=True)
            if why:
                return why
        if group == "release":
            for out in _gh_flag_values(rest, "-O", "--output"):
                why = _gh_target_reason(out, cwd, is_dir=False)
                if why:
                    return why
    return None


# --- Raw-text floor under the allowlist (T-4145 review round 1) ---
# The allowlist above judges gh only where its command-position parser finds
# it. A spelling the parser does not recognize (`bash -o pipefail -c "..."`,
# `sh -c -- "..."`, `\gh`, `eval gh ...`, `coproc`, `watch`, `script -c`,
# `env -S`, ...) would otherwise pass. So the same (group, verb) allowlist also
# runs over the argument surface (`_argument_surface`: tokens with any `git
# commit` message dropped; the whole raw text when compound): any `gh <group>
# <verb>` pair whose verb is a REAL gh subcommand of that group and is not
# allowlisted DENIES, wherever it appears. Round 2: only real subcommands count
# (the table below), so prose such as "the gh run failed on lint", "gh pr is
# green" or "fix gh run flake" passes; unknown words are left to the
# command-position allowlist, which still denies them wherever gh actually
# runs. Cost: data that names a real non-allowlisted subcommand outside a commit
# message (`ticket.py file --title "gh release create lacks..."`, `rg 'gh
# release create'`, a heredoc line `gh workflow run x`) is denied, as the old raw
# `_PUSH` did, and so is `gh <group> <write-verb> --help`.
#
# gh 2.45.0's command tree (subcommands plus their built-in aliases), captured
# from local `gh <group> [<sub>] --help` output only (no auth, no network; D11).
# Keep it in step with the installed gh when it is upgraded.
_GH_SUBCOMMANDS_245: dict[str, frozenset[str]] = {
    "auth": frozenset("login logout refresh setup-git status switch token".split()),
    "codespace": frozenset("code cp create delete edit jupyter list ls logs ports rebuild ssh stop view".split()),
    "gist": frozenset("clone create new delete edit list ls rename view".split()),
    "issue": frozenset(
        "create new list ls status close comment delete develop edit lock pin reopen "
        "transfer unlock unpin view".split()),
    "org": frozenset("list ls".split()),
    "pr": frozenset(
        "create new list ls status checkout checks close comment diff edit lock merge "
        "ready reopen review unlock view".split()),
    "project": frozenset(
        "close copy create delete edit field-create field-delete field-list item-add "
        "item-archive item-create item-delete item-edit item-list link list ls "
        "mark-template unlink view".split()),
    "release": frozenset("create new list ls delete delete-asset download edit upload view".split()),
    "repo": frozenset(
        "create new list ls archive clone delete deploy-key edit fork rename set-default "
        "sync unarchive view".split()),
    "cache": frozenset("delete list ls".split()),
    "run": frozenset("cancel delete download list ls rerun view watch".split()),
    "workflow": frozenset("disable enable list ls run view".split()),
    "alias": frozenset("delete import list ls set".split()),
    "config": frozenset("clear-cache get list ls set".split()),
    "extension": frozenset("browse create exec install list ls remove search upgrade".split()),
    "gpg-key": frozenset("add delete list ls".split()),
    "label": frozenset("clone create delete edit list ls".split()),
    "ruleset": frozenset("check list ls view".split()),
    "secret": frozenset("delete remove list ls set".split()),
    "ssh-key": frozenset("add delete list ls".split()),
    "variable": frozenset("delete remove list ls set".split()),
}
# The floor's deny table: every real subcommand not on the read allowlist.
_GH_FLOOR_DENY: dict[str, frozenset[str]] = {
    group: subs - (_GH_READ_ALLOWLIST.get(group) or frozenset())
    for group, subs in _GH_SUBCOMMANDS_245.items()
}
_GH_FLOOR_GH = re.compile(r"(?<![\w.-])gh$")
_FLOOR_WORD = re.compile(r"[A-Za-z][\w-]*+(?![\w.-])")


def _floor_word(tok: str) -> str | None:
    """The leading word of a token, as long as it ends at a non-word character
    (`create"` -> create; `create.py` -> None)."""
    m = _FLOOR_WORD.match(tok)
    return m.group(0) if m else None


def _gh_raw_floor(command: str) -> str | None:
    # dequote (round 3, N1): inside a compound the surface is raw text, where a
    # quoted or escaped command word (`watch 'gh' pr merge 5`) still runs gh.
    for toks, i in _gh_word_positions(_argument_surface(command), _GH_FLOOR_GH, dequote=True):
        if i + 1 >= len(toks):
            continue
        group, verb = _floor_word(toks[i]), _floor_word(toks[i + 1])
        if group is None or len(group) != len(toks[i]):
            continue  # the group word must be the whole token (`gh pr` not `gh pr,`)
        if verb is not None and verb in _GH_FLOOR_DENY.get(group, ()):
            return f"`gh {group} {verb}`, which is not on the subagent read allowlist (raw-text floor)"
    return None


def _gh_cli_violation(command: str, cwd: str) -> str | None:
    """Deny reason for the first gh invocation in `command` that is not an
    allowlisted read, or None."""
    for tokens in _gh_invocations(command):
        why = _gh_judge(tokens, cwd)
        if why:
            return why
    return None


# --- Credential files: read access blocked for ANY subagent (T-4135) ---
# T-1345: dispatched workers hold no credentials of their own. The Telegram
# bot-token rule (_TOKEN_READ, above) already establishes the template — an
# unconditional path-substring match — for one credential file; this section
# generalizes it to the rest of the row-13 inventory
# (notes/2026-09-28-t2893-credential-inventory.md): the GitHub PAT
# (~/gh_token.env, ~/.config/gh/hosts.yml), the Echo metered-API-key file
# (echo-prismatic/.env), the Echo event-store keys (~/j17.keys), the sudo password
# (~/root.env), the Forgejo credential (~/.git-credentials*), the Claude OAuth
# session (~/.claude/.credentials.json — the O15 class) and the codex seat's OAuth
# session (~/.codex/auth.json) — both coordinator addition 3 — plus any
# ~/.codex/*.env (none exists on this seat today; the pattern is cheap and
# fail-closed, so a future one is covered on arrival).
#
# The generic `.claude/channels/*/.env` alternative generalizes _TOKEN_READ's
# literal telegram-only path to any channel bot's credential file (a second real
# one exists today: `.claude/channels/telegram-bridge/.env`) — a clean,
# structure-following generalization, NOT a home-wide `*/.env` sweep (which would
# also catch unrelated non-credential files like ~/staging/*.env that nothing in
# this ticket named).
#
# Unlike _TOKEN_READ, this rule carries the brief's explicit carve-out:
# metadata-only `stat`/`ls -l` access stays allowed (verifying a file exists, or
# its permissions, is not "reading" it) — see `_is_metadata_only_access` below,
# consulted only for a single unambiguous invocation (same fail-closed-on-
# ambiguity discipline the module already uses elsewhere).
_CRED_FIXED_PATHS = re.compile(
    r"\bgh_token\.env\b"
    r"|\.config/gh/hosts\.yml\b"
    r"|echo-prismatic/\.env\b"
    r"|\.claude/channels/[^/\s'\"]+/\.env\b"
    r"|\bj17\.keys\b"
    r"|\broot\.env\b"
    r"|\.git-credentials"
    r"|\.claude/\.credentials\.json\b"
    r"|\.codex/auth\.json\b"
    r"|\.codex/[^/\s'\"]*\.env\b"
)

# Split on whitespace/quote runs, then substring-test each word: linear. An
# unanchored `[^\s'"]*\.ssh/[^\s'"]*` finditer backtracks from every offset of a
# long word and went quadratic (16K chars ~14 s, 200K never finished; T-4135
# round-2 review) — a hang in a PreToolUse hook bricks the worker's Bash call.
_SSH_WORD_SPLIT = re.compile(r"[\s'\"]+")
_SSH_NONSECRET_BASENAMES = frozenset({"config", "known_hosts", "known_hosts.old", "authorized_keys", ".ssh"})


def _mentions_ssh_private_key(command: str) -> bool:
    """True if `command` mentions a path under `.ssh/` that is neither a `.pub`
    public key nor one of the well-known non-secret files (`config`,
    `known_hosts[.old]`, `authorized_keys`, or a bare `.ssh/` directory
    reference). The brief scopes this to PRIVATE keys specifically, not the whole
    directory."""
    for path in _SSH_WORD_SPLIT.split(command):
        if ".ssh/" not in path:
            continue
        if path.endswith(".pub"):
            continue
        basename = path.rstrip("/").rsplit("/", 1)[-1]
        if basename in _SSH_NONSECRET_BASENAMES:
            continue
        return True
    return False


_METADATA_ONLY_VERBS = frozenset({"stat"})


def _is_metadata_only_access(tokens: list[str]) -> bool:
    """True for a bare `stat <path>` or `ls -l...<path>` single invocation — the
    credential-file rule's named carve-out. Only consulted after
    `_tokens_or_none` already confirmed a single, unambiguous invocation."""
    if not tokens:
        return False
    verb = tokens[0]
    if verb in _METADATA_ONLY_VERBS:
        return True
    if verb == "ls":
        return any(tok.startswith("-") and "l" in tok[1:] for tok in tokens[1:])
    return False


# Characters that let a second command or a read ride along with a `stat`/`ls -l`
# that shlex would still tokenize as one invocation: newline (a second line),
# process substitution `<(...)`, redirects, `$var`/`${...}` expansion.
_METADATA_CARVEOUT_UNSAFE = re.compile(r"[\n<>()$]")


def _credential_file_read(command: str) -> bool:
    """True if `command` accesses one of the T-4135 credential-file paths in a
    way that isn't pure metadata (stat / ls -l)."""
    if not _METADATA_CARVEOUT_UNSAFE.search(command):
        tokens = _tokens_or_none(command)
        if tokens and _is_metadata_only_access(tokens):
            return False
    return bool(_CRED_FIXED_PATHS.search(command)) or _mentions_ssh_private_key(command)


# --- Credential files via the Read/Grep TOOLS: T-4139 ---
# T-4135 closed the Bash surface (`cat`/`head`/`grep <path>`/...) above; a
# subagent's Read TOOL call, or a Grep TOOL call whose `path` operand names a
# credential file directly, never touches Bash and so bypassed the guard
# entirely — the same shape T-1381 already closed for Edit/Write vs. protected
# docs. `_resolve_against_cwd` is a NEW helper, added alongside rather than
# factored out of `_protected_doc_via_worktree` (whose relative-path join is
# inline to that function): that function's behavior must stay byte-identical
# for the T-3662 worktree-shape tests, so its logic is left untouched here.
def _resolve_against_cwd(path: str, cwd: str) -> str:
    """Resolve `path` to an absolute form against `cwd` when relative — same
    join+normpath shape `_protected_doc_via_worktree` already applies inline
    for relative Edit/Write paths, factored out here so Read/Grep can reuse it
    without duplicating the logic a third time.

    An absolute `path` is merely normalized. A relative `path` with no `cwd`
    is returned UNCHANGED (not blocked-by-omission): the raw relative string
    still goes through the same credential-pattern match afterward, so a
    context-INDEPENDENT match (a bare `root.env`, matching `\\broot\\.env\\b`
    regardless of any join) is still caught with no `cwd` at all — only a
    match that DEPENDS on the join (e.g. `auth.json` landing inside a `.codex/`
    cwd) requires `cwd` to be present."""
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    if cwd:
        return os.path.normpath(os.path.join(cwd, path))
    return path


def _credential_path_match(path: str, cwd: str) -> bool:
    """True if `path` (a Read `file_path` or a Grep `path` operand), resolved
    against `cwd` when relative, names a T-4135 credential file. Reuses the
    existing `_CRED_FIXED_PATHS` / `_mentions_ssh_private_key` patterns
    verbatim — no forked regex. No metadata carve-out: unlike a Bash `stat`/
    `ls -l`, both Read and Grep always return content, never bare metadata."""
    resolved = _resolve_against_cwd(path, cwd)
    return bool(_CRED_FIXED_PATHS.search(resolved)) or _mentions_ssh_private_key(resolved)


# --- Quoted heredocs that only write a file are data, not commands ---
# T-4154's merge review replayed orchestrator's subagent history and found reports
# and commit messages written as `cat > file <<'X' ... X` denied: the gh and
# escalation walks read every body line as a command line, so prose that
# mentions sudo, or a markdown line that starts with a backtick, read as a
# command. With a QUOTED delimiter the shell expands nothing in the body, and
# when the only reader is `cat`/`tee` writing a real file, or
# `git commit -F -`, the body never reaches a shell. `_data_heredoc_text`
# removes exactly those bodies, and only the checks that ask "does this RUN
# something" read the result: the GitHub-write checks and the escalation
# check. Every other check still reads the raw text.
#
# The parser is a small, strict shell lexer. It knows quotes, escapes,
# comments, parens and heredoc bodies, and it gives up (returns the command
# unchanged, so every check sees what it saw before) on anything it does not
# model exactly: `$(`, `${`, backticks, `$'`/`$"`, process substitution,
# here-strings, `;;`, an unquoted or oddly quoted delimiter, two heredocs on
# one line, a quote or `\`-newline running past a heredoc's line, a missing
# closing delimiter, unbalanced parens, anything that can redefine the
# consumer command (a `()` function definition, `function`, `alias`, `hash`,
# `enable`, a `PATH=`/`BASH_ENV=`/... assignment), and any group or compound
# word (`{`, `}`, `if`, `do`, `done`, `case`, ...) whose combined stdout could
# be piped on.
# A heredoc that does not qualify keeps its body. One that qualifies has:
#   - a quoted delimiter: <<'X', <<"X", <<\X, or the <<- forms;
#   - a consumer segment at paren depth 0 that is not piped onward (no `|`
#     or `|&` after it), made only of plain words: no `$`, no glob, and no
#     quoting or escaping (so `\cat` or a quoted path turns it off);
#   - a consumer of exactly one of these shapes:
#       cat [-] > / >> FILE
#       tee [-a|--append] [FILE...] > / >> FILE      (stdout FILE may be /dev/null)
#       git [-C DIR] commit [safe flags / pathspecs] -F -|-F-|--file=-|--file -
#     where FILE is not under /dev (except /dev/null) or /proc, and git takes
#     no redirect, no `-c`, and no flag that opens an editor.
# Anything after a closing delimiter line is lexed and checked as usual.
# Every step is one forward pass, so the cost is linear in the command.
#
# T-4328 rebound (adversarial review): "written to a file" is NOT enough,
# because the SAME command line can still run what it wrote, or redefine the
# writer. So blanking additionally fails closed (keeps the raw body) on:
#   M1 (the writer is not what it looks like). Anywhere OUTSIDE the bodies:
#       eval, source, `.`, read, mapfile/readarray, `printf -v`,
#       declare/typeset/local/readonly/export with `-n`, trap, exec, shopt,
#       enable, builtin, command_not_found_handle, and any assignment to
#       PATH / BASH_ENV / ENV / SHELLOPTS / BASHOPTS / IFS — each can make a
#       later `cat`/`tee`/`git` run the body (confirmed in real bash).
#   M2 (the file is executed later, in this same command). For a cat/tee
#     target FILE (git's target is the message, so only mkfifo/mknod and the
#     git-config checks apply there):
#       (a) FILE (or its basename, or a glob that could match it) is named in
#           a segment whose command word is NOT a read-only inspector
#           (`_HD_NONRUN_CMDS`: cat/tee/wc/ls/head/tail/echo/...). So a report
#           read back with `wc -l f` stays data, but `bash f` / `. f` /
#           `python3 f` / `./f` / `cp f g` / an unknown command / a matching
#           glob is a run and denies. Fail closed on anything not an inspector;
#       (b) the command names mkfifo or mknod anywhere;
#       (c) FILE is under `.git/`, names `hooks`, is a shell rc/profile
#           (.bashrc/.profile/.zshrc/...), or is under /etc/;
#       (d) the command sets core.hooksPath or runs `git config` at all.
# Consequence (accepted, T-4328): a write-then-run (a script file then
# executed) goes back to DENY; the deny message already routes script content
# to the Write tool. Pure report / commit-message writes — including a write
# then a read-back — stay allowed, the point of the ticket.


class _HeredocBail(Exception):
    """The command has a shape the heredoc lexer does not model exactly."""


_HD_DELIM = re.compile(
    r"""(-?)[ \t]*(?:'([A-Za-z0-9_.-]+)'|"([A-Za-z0-9_.-]+)"|\\([A-Za-z0-9_.-]+))"""
    r"""(?=[ \t\n;&|<>)]|$)"""
)
_HD_WORD_END = frozenset(" \t\n;&|<>()")
_HD_SEPARATORS = frozenset({";", "&", "&&", "||", "|", "|&", "\n", "(", ")"})
_HD_DYNAMIC = re.compile(r"[*?\[]")
_HD_DEVICE_PATH = re.compile(r"(?:^|/)(?:dev|proc)(?:/|$)")
_HD_GIT_FLAGS = frozenset({
    "-a", "--all", "-q", "--quiet", "-s", "--signoff", "--amend", "--no-edit",
    "--allow-empty", "-n", "--no-verify", "--",
})
# Words that can redefine what `cat`/`tee`/`git` runs, run the body, or open a
# group or compound whose combined stdout could be piped on (`{ ...; } | bash`).
# Any of them anywhere (as a word, so a quoted `'eval'` counts too — the lexer
# dequotes before this test), even as a plain argument, turns the blanking off.
_HD_BAIL_WORDS = frozenset({
    # structural: groups and compounds whose stdout could be piped on
    "function", "alias", "hash", "enable", "{", "}", "if", "then", "elif", "else",
    "fi", "for", "select", "while", "until", "do", "done", "case", "esac", "coproc",
    # M1 single-word triggers: run the body or redefine the consumer
    "eval", "source", ".", "read", "mapfile", "readarray", "trap", "exec", "shopt",
    "builtin", "command_not_found_handle",
    # M2 (b): a FIFO/special file the body can be read back through
    "mkfifo", "mknod",
})
# An assignment to any variable that steers command resolution or the body's
# fate. Matched on a word start (`PATH=`, `export 'BASH_ENV=x'` dequotes to
# `BASH_ENV=x`), so it fires on the assignment word itself.
_HD_ENV_ASSIGN = re.compile(r"(?:PATH|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|IFS)\+?=")
_HD_DECLARE_FAMILY = frozenset({"declare", "typeset", "local", "readonly", "export"})
# M2 (c): a shell rc / profile file, executed on the next shell start.
_HD_RC_BASENAMES = frozenset({
    ".bashrc", ".bash_profile", ".profile", ".zshrc", ".bash_login", ".zprofile",
    ".zshenv", ".bash_logout", "bash.bashrc", "profile", "zshrc", "bashrc",
})
# M2 (a): every word outside the bodies counts as a possible run of a written
# file, EXCEPT a pure read-back: a segment whose command word is a read-only
# inspector (`_HD_NONRUN_CMDS`) AND whose output provably stays on the
# terminal (`_hd_readback_only`). That carve-out keeps the primary case
# allowed: a report written with `cat > f` and read back with `wc -l f` /
# `test -s f` (orchestrator corpus #1-#3). tee/echo/printf are NOT inspectors: they
# write or construct text. Rebound 2: an inspector whose output leaves its
# segment (`cat f | bash`, `cat f > x.sh`, `tee g < f | bash`) is a run too.
_HD_NONRUN_CMDS = frozenset({
    "cat", "wc", "ls", "head", "tail", "stat", "file",
    "du", "cksum", "md5sum", "sha1sum", "sha256sum", "sha512sum", "realpath",
    "readlink", "dirname", "basename", "test", "[", "true", "false", "cd",
    "mkdir", "touch", "sleep", ":",
})
_HD_REDIR_OPS = frozenset({">", ">>", ">|", ">&", "&>", "&>>", "<", "<&", "<>"})
# The only redirects a read-back may carry: output discarded, never re-read.
_HD_NULL_REDIRS = frozenset({(None, ">"), ("1", ">"), ("2", ">"), (None, "&>")})


def _hd_word(s: str, i: int, pending: bool) -> tuple[int, str, bool]:
    """Lex one shell word at `s[i]`: (end, dequoted value, dynamic?). Raises
    _HeredocBail on a form the lexer does not model."""
    n = len(s)
    out = []
    dynamic = False
    while i < n and s[i] not in _HD_WORD_END:
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0 or (pending and "\n" in s[i:j]):
                raise _HeredocBail
            out.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise _HeredocBail
                d = s[j]
                if d == '"':
                    break
                if d == "`" or (d == "$" and s[j + 1:j + 2] in ("(", "{")):
                    raise _HeredocBail
                if d == "$":
                    dynamic = True
                if d == "\n" and pending:
                    raise _HeredocBail
                if d == "\\" and j + 1 < n:
                    if s[j + 1] == "\n":
                        raise _HeredocBail
                    j += 2
                    continue
                out.append(d)
                j += 1
            i = j + 1
        elif c == "\\":
            if i + 1 >= n or s[i + 1] == "\n":
                raise _HeredocBail  # a continuation inside a word: not modelled
            out.append(s[i + 1])
            i += 2
        elif c == "`":
            raise _HeredocBail
        elif c == "$":
            if s[i + 1:i + 2] in ("(", "{", "'", '"'):
                raise _HeredocBail
            dynamic = True
            out.append(c)
            i += 1
        else:
            if c in "*?[":
                dynamic = True
            out.append(c)
            i += 1
    return i, "".join(out), dynamic


# T-5140 r3: the overflow walk's body classification and `_data_heredoc_text`
# lex the same command; callers only read the result.  A `_HeredocBail` is
# raised, never cached.
@functools.lru_cache(maxsize=2)
def _hd_lex(s: str):
    """Tokens of `s` and the heredocs found, or _HeredocBail. A token is
    (kind, value, dynamic, plain): kind W (word — `value` is the dequoted text,
    `plain` False when it was quoted or escaped), O (operator) or H (heredoc
    number); a heredoc is [strip_tabs, body_start, body_end, paren_depth,
    token_index]."""
    toks: list[tuple[str, object, bool]] = []
    docs: list[list] = []
    pending: list | None = None  # the heredoc whose body starts at the next newline
    depth = 0
    at_word_start = True
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            i += 1
            at_word_start = True
            continue
        if c == "\n":
            toks.append(("O", "\n", False, False))
            i += 1
            at_word_start = True
            if pending is not None:
                delim, strip = pending[0], pending[1]
                start = i
                while True:
                    if i >= n:
                        raise _HeredocBail  # unterminated heredoc
                    j = s.find("\n", i)
                    end = n if j < 0 else j
                    line = s[i:end]
                    if (line.lstrip("\t") if strip else line) == delim:
                        docs[pending[2]][1:3] = [start, i]
                        i = end  # the delimiter line's newline is lexed next
                        break
                    i = end + 1
                pending = None
            continue
        if c == "#" and at_word_start:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\\" and s[i + 1:i + 2] == "\n":
            if pending is not None:
                raise _HeredocBail
            i += 2
            continue
        at_word_start = c in ";&|()<>"
        if c in ";&|<>()":
            three, two = s[i:i + 3], s[i:i + 2]
            if three == "<<<" or two == ";;":
                raise _HeredocBail
            if two == "<<":
                if pending is not None:
                    raise _HeredocBail  # two heredocs on one line
                m = _HD_DELIM.match(s, i + 2)
                if not m:
                    raise _HeredocBail  # unquoted or oddly quoted delimiter
                delim = m.group(2) or m.group(3) or m.group(4)
                docs.append([bool(m.group(1)), None, None, depth, len(toks)])
                pending = [delim, bool(m.group(1)), len(docs) - 1]
                toks.append(("H", len(docs) - 1, False, False))
                i = m.end()
                at_word_start = False
                continue
            if c in "<>" and s[i + 1:i + 2] == "(":
                raise _HeredocBail  # process substitution
            for op in ("&>>", "&&", "||", "|&", "&>", ">>", ">&", ">|", "<>", "<&"):
                if s.startswith(op, i):
                    break
            else:
                op = c
            if op == "(":
                depth += 1
                k = i + 1
                while k < n and s[k] in " \t":
                    k += 1
                if s[k:k + 1] == ")":
                    raise _HeredocBail  # a function definition
            elif op == ")":
                depth -= 1
                if depth < 0:
                    raise _HeredocBail
            toks.append(("O", op, False, False))
            i += len(op)
            continue
        if c == "`":
            raise _HeredocBail
        start = i
        i, value, dynamic = _hd_word(s, i, pending is not None)
        if value in _HD_BAIL_WORDS or _HD_ENV_ASSIGN.match(value):
            raise _HeredocBail
        # `plain` is False for a quoted or escaped word (`\cat`, `'cat'`): the
        # consumer's command word must be plain, but M1/M2 still see the
        # dequoted `value` (a quoted `'eval'` still runs eval).
        toks.append(("W", value, dynamic, s[start:i] == value))
        at_word_start = False
    if pending is not None or depth != 0:
        raise _HeredocBail
    return toks, docs


def _hd_file_ok(value: str, allow_null: bool) -> bool:
    if not value or _HD_DYNAMIC.search(value):
        return False
    if value == "/dev/null":
        return allow_null
    return not _HD_DEVICE_PATH.search(value)


def _hd_consumer_targets(seg: list):
    """The file targets written by the one heredoc in `seg` if `seg` is a pure
    data-sink consumer, else None. `cat`/`tee` return their file operands (so
    M2 can test them); `git commit -F -` returns [] — its target is the commit
    message, not a file. A digit word right before a redirect is an fd, not an
    operand (N1), so such a segment does not qualify."""
    words: list[str] = []
    stdout: list[str] = []
    k = 0
    while k < len(seg):
        kind, value, dynamic, plain = seg[k]
        if kind == "H":
            k += 1
            continue
        if kind == "W":
            if dynamic or not plain:
                return None  # a `$`, a glob, or a quoted/escaped word
            if (value.isdigit() and k + 1 < len(seg) and seg[k + 1][0] == "O"
                    and seg[k + 1][1][:1] in "<>"):
                return None  # N1: `tee f 2>/dev/null` — the `2` is an fd
            words.append(value)
            k += 1
        elif (value in (">", ">>") and k + 1 < len(seg) and seg[k + 1][0] == "W"
              and not seg[k + 1][2] and seg[k + 1][3]):
            stdout.append(seg[k + 1][1])
            k += 2
        else:
            return None  # any other redirect (>&, &>, <, <&, <>, >|)
    if not words:
        return None
    if words[0] == "cat":
        if (words[1:] in ([], ["-"]) and len(stdout) == 1
                and _hd_file_ok(stdout[0], allow_null=True)):
            return list(stdout)
        return None
    if words[0] == "tee":
        if len(stdout) != 1 or not _hd_file_ok(stdout[0], allow_null=True):
            return None
        files = list(stdout)
        for w in words[1:]:
            if w in ("-a", "--append"):
                continue
            if w.startswith("-") or not _hd_file_ok(w, False):
                return None
            files.append(w)
        return files
    if words[0] == "git":
        if stdout:
            return None
        rest = words[1:]
        if rest[:1] == ["-C"] and len(rest) > 1:
            rest = rest[2:]
        if rest[:1] != ["commit"]:
            return None
        rest = rest[1:]
        file_forms = 0
        j = 0
        while j < len(rest):
            w = rest[j]
            if w == "-F" and rest[j + 1:j + 2] == ["-"]:
                file_forms += 1
                j += 2
            elif w == "--file" and rest[j + 1:j + 2] == ["-"]:
                file_forms += 1
                j += 2
            elif w in ("-F-", "--file=-"):
                file_forms += 1
                j += 1
            elif w in _HD_GIT_FLAGS or w.startswith("--author=") or not w.startswith("-"):
                j += 1
            else:
                return None
        return [] if file_forms == 1 else None
    return None


def _hd_global_bail(words: list[str]) -> bool:
    """M1/M2 (b),(d): a command-wide reason not to trust ANY data heredoc in
    it. `words` is the dequoted text of every word token outside the bodies.
    The single-word triggers (eval/source/./read/exec/mkfifo/... and the
    env-assignment words) already raised _HeredocBail during lexing; here are
    the ones that need two words together."""
    s = set(words)
    if "printf" in s and "-v" in s:
        return True  # printf -v PATH ...
    if s & _HD_DECLARE_FAMILY and "-n" in s:
        return True  # declare -n P=PATH; P=...
    if "git" in s and "config" in s:
        return True  # git config core.hooksPath ... (M2 d)
    if any("core.hooksPath" in w for w in words):
        return True
    return False


def _hd_readback_only(seg: list, nxt, depth: int) -> bool:
    """True if `seg` is a pure read-back whose output cannot leave it: a
    read-only inspector command word, at paren depth 0 (so no `( ... ) | sh`),
    not followed by `|`/`|&`, and with no redirect except output discarded to
    /dev/null (`>`, `1>`, `2>`, `&>`) — in particular no `<` input, no `>file`,
    no `>&` fd duplication. Anything else is a possible run."""
    if depth != 0 or nxt in ("|", "|&"):
        return False
    cmdword = None
    k = 0
    while k < len(seg):
        t = seg[k]
        fd = None
        if (t[0] == "W" and t[1].isdigit() and k + 1 < len(seg)
                and seg[k + 1][0] == "O" and seg[k + 1][1] in _HD_REDIR_OPS):
            fd = t[1]
            k += 1
            t = seg[k]
        if t[0] == "O":
            if t[1] not in _HD_REDIR_OPS or (fd, t[1]) not in _HD_NULL_REDIRS:
                return False
            if not (k + 1 < len(seg) and seg[k + 1][0] == "W"
                    and seg[k + 1][1] == "/dev/null"):
                return False
            k += 2
            continue
        if t[0] == "W" and cmdword is None:
            cmdword = t[1]
        k += 1
    return cmdword in _HD_NONRUN_CMDS


def _hd_run_references(toks: list):
    """Every word outside the bodies that could name a file being RUN,
    gathered once for the whole command (M2 a). The command is walked
    segment by segment (runs of tokens between separators), tracking paren
    depth. A segment holding a heredoc (a qualifying data sink — the caller
    only gets here when every heredoc qualifies) writes, it does not run, and
    a `_hd_readback_only` segment is a read-back; every other segment
    contributes all its words. A word is recorded whole, by basename, and by
    what follows a `=`, `:` or `,` (`F=f`, `--file=f`). Returns (plain set,
    basename set, glob words, any-`$`): a `$` word in a run segment could
    expand to any path, so it counts as naming every target."""
    run_plain: set[str] = set()
    run_base: set[str] = set()
    run_globs: list[str] = []
    dollar = False
    n = len(toks)
    i = 0
    depth = 0
    while i <= n:
        j = i
        while j < n and not (toks[j][0] == "O" and toks[j][1] in _HD_SEPARATORS):
            j += 1
        seg = toks[i:j]
        nxt = toks[j][1] if j < n else None
        if seg and not any(t[0] == "H" for t in seg) and not _hd_readback_only(seg, nxt, depth):
            for t in seg:
                if t[0] != "W":
                    continue
                w = t[1]
                if "$" in w:
                    dollar = True
                parts = [w] + [p for p in re.split(r"[=:,]", w)[1:] if p]
                for p in parts:
                    run_plain.add(p)
                    run_base.add(p.rsplit("/", 1)[-1])
                    if _HD_DYNAMIC.search(p):
                        run_globs.append(p)
        if j < n:
            if toks[j][1] == "(":
                depth += 1
            elif toks[j][1] == ")":
                depth -= 1
        i = j + 1
    return run_plain, run_base, run_globs, dollar


def _hd_target_unsafe(targets: list[str], run_plain: set, run_base: set,
                      run_globs: list, dollar: bool) -> bool:
    """M2 (a),(c): a cat/tee target that may be run later in the same command
    (named, globbed, or `$`-reachable from a non-read-back segment), or is a
    hook / rc / .git / /etc file."""
    for t in targets:
        base = t.rsplit("/", 1)[-1]
        # T-4963: the prefix test reads the normalised target too, so
        # `//etc/x` and `/./etc/x` classify the same as `/etc/x`
        norm = _py_norm(t) if t.startswith("/") else t
        if ".git/" in t or "hooks" in t.lower() or t.startswith("/etc/") or norm.startswith("/etc/"):
            return True  # (c)
        if base in _HD_RC_BASENAMES:
            return True  # (c)
        if dollar or t in run_plain or base in run_base:
            return True  # (a) the file is (or may be) named in a run segment
        for w in run_globs:  # (a) a glob in a run segment that could name it
            wb = w.rsplit("/", 1)[-1]
            if (fnmatch.fnmatch(t, w) or fnmatch.fnmatch(base, w)
                    or fnmatch.fnmatch(base, wb)):
                return True
    return False


# T-5109: readers, beyond `_hd_consumer_targets`' data sinks, that read a
# quoted body as data or as a non-shell program and pass nothing on. Used only
# by the eval join (`_eval_joined_text`), never by `_data_heredoc_text`.
_HD_PY_FLAGS = frozenset({"-I", "-u", "-B", "-E", "-s", "-S"})


def _hd_join_reader_targets(seg: list):
    """`_hd_consumer_targets(seg)`, or [] for exactly two more shapes, plain
    words only and no redirect: bare `cat [-]` (its output goes to the
    terminal) and `python3|python [-I -u -B -E -s -S]... [-]` (the body is a
    Python program). None for anything else: an unknown reader counts as a
    shell (T-5109)."""
    got = _hd_consumer_targets(seg)
    if got is not None:
        return got
    words: list[str] = []
    for kind, value, dynamic, plain in seg:
        if kind == "H":
            continue
        if kind != "W" or dynamic or not plain:
            return None  # a redirect, a `$`/glob, or a quoted/escaped word
        words.append(value)
    if words[:1] == ["cat"]:
        return [] if words[1:] in ([], ["-"]) else None
    if words[:1] in (["python3"], ["python"]):
        rest = words[1:]
        while rest and rest[0] in _HD_PY_FLAGS:
            rest = rest[1:]
        return [] if rest in ([], ["-"]) else None
    return None


@functools.lru_cache(maxsize=4)
def _classified_data_heredoc_spans(command: str, reader):
    """`_classify_heredoc_spans`, as tuples, cached (T-5140 r3): decide()'s two
    readings, and the overflow walk with them, classify the same command; a
    `_HeredocBail` is a returned outcome here, so a bailing command is not
    re-lexed either."""
    bodies, spans, complete = _classify_heredoc_spans(command, reader)
    return tuple(bodies), tuple(spans), complete


def _classify_heredoc_spans(command: str, reader):
    """(all body spans, proven-data spans, every body proven data).

    The last value preserves `_data_heredoc_spans`' all-or-nothing removal
    rule.  The per-body distinction is also needed by T-5140: a mixed command
    can contain one proven data body and one shell-read body, and only the
    latter may be inspected as executable shell text.
    """
    if "<<" not in command:
        return [], [], True
    try:
        toks, docs = _hd_lex(command)
    except _HeredocBail:
        return [], [], False
    bodies = [(start, end) for _strip, start, end, _depth, _pos in docs
              if start is not None]
    all_words = [t[1] for t in toks if t[0] == "W"]
    if _hd_global_bail(all_words):
        return bodies, [], False  # M1 (multi-word) / M2 (b),(d)
    spans: list[tuple[int, int]] = []
    all_targets: list[str] = []
    complete = True
    for _strip, start, end, depth, pos in docs:
        if depth != 0 or start is None:
            complete = False
            continue
        lo = pos
        while lo > 0 and not (toks[lo - 1][0] == "O" and toks[lo - 1][1] in _HD_SEPARATORS):
            lo -= 1
        hi = pos
        while hi < len(toks) and not (toks[hi][0] == "O" and toks[hi][1] in _HD_SEPARATORS):
            hi += 1
        if hi < len(toks) and toks[hi][1] in ("|", "|&"):
            continue  # piped onward: the body can reach a shell
        seg = toks[lo:hi]
        if sum(1 for t in seg if t[0] == "H") != 1:
            complete = False
            continue
        targets = reader(seg)
        if targets is None:
            continue
        all_targets += targets
        spans.append((start, end))
    run_plain, run_base, run_globs, dollar = _hd_run_references(toks)
    if _hd_target_unsafe(all_targets, run_plain, run_base, run_globs, dollar):
        return bodies, [], False  # M2 (a),(c): a body may run later
    return bodies, spans, complete and len(spans) == len(bodies)


def _data_heredoc_spans(command: str, reader=_hd_consumer_targets) -> list[tuple[int, int]]:
    """The raw [start, end) body spans `_data_heredoc_text` removes, with
    `reader` judging each heredoc's consumer segment ([] when no body
    qualifies, the shape is outside what the lexer models, or an M1/M2 reason
    means a body could still reach a shell)."""
    _bodies, spans, complete = _classified_data_heredoc_spans(command, reader)
    # All or nothing. A body left raw is never tokenized, so a command it runs
    # could name another heredoc's file unseen.  Preserve the original removal
    # rule even though the classifier records per-body provenance for T-5140.
    return list(spans) if complete and spans else []


def _data_heredoc_text(command: str) -> str:
    """`command` with the body of each file-writing quoted heredoc removed, or
    `command` itself when there is none or its shape is outside what the lexer
    models, or an M1/M2 reason means the body could still reach a shell. The
    body is deleted outright (not replaced with its newlines): the returned
    text is only fed to the run-something checks, which do not need the body's
    line offsets, and a long body replaced by a long run of blank lines is a
    pathological input for the gh/escalation walkers (a superlinear cost on
    consecutive command anchors) — deleting it keeps the whole pass linear."""
    spans = _data_heredoc_spans(command)
    if not spans:
        return command
    out, last = [], 0
    for start, end in spans:
        out.append(command[last:start])  # body [start,end) dropped entirely
        last = end
    out.append(command[last:])
    return "".join(out)


def _shell_formed_push_in_cmd(c: _PyCmd) -> bool:
    """Whether simple command `c` executes a literal `git ... push` argv.

    `_PyLexer` has already applied shell quote removal, so adjacent fragments
    such as `"pu""sh"` are one `push` word here.  Wrapper option parsing is
    shared with the escalation walk; arguments that merely mention `git push`
    are not mistaken for the command being run.
    """
    start = 0
    while start < len(c.words) and (c.words[start] in _PY_RESERVED
                                    or _PY_ASSIGN.match(c.words[start])):
        start += 1
        if c.words[start - 1] == "time":
            # r7 (rv2 F4): bash's `time` keyword takes `-p`, then `--` (a reserved word, so
            # never `_WRAPPER_GRAMMAR`'s `time`)
            while start < len(c.words) and c.words[start] in ("-p", "--"):
                start += 1
    toks = [("word", word, i in c.dyn) for i, word in enumerate(c.words[start:], start)]
    if _shell_formed_push_wrapped(toks):
        return True
    # r7 (rv2 F4): a prefix this walk does not model (`time -p`, `tmux new -d`, `unshare -r`,
    # `chronic`, `chroot /`, ...) or an option it does not know ends the walk above. As main
    # reads `X git <subcommand>` at top level, each later static `git` word is judged from there.
    return any(t[1].rsplit("/", 1)[-1] == "git" and not t[2] and _shell_formed_git_push_argv(toks[k:])
               for k, t in enumerate(toks) if k)


def _shell_formed_push_wrapped(toks: list) -> bool:
    """The wrapper walk of `_shell_formed_push_in_cmd`: through `_WRAPPER_GRAMMAR` wrappers
    and their options to the command word, and a `git` argv there."""
    pending: list = []

    def nxt():
        return pending.pop() if pending else None

    for _ in range(len(toks) + 1):
        if not toks:
            return False
        tok = toks[0]
        if tok[2]:
            return False
        base = tok[1].rsplit("/", 1)[-1]
        if base == "git":
            return _shell_formed_git_push_argv(toks)
        if base not in _WRAPPER_GRAMMAR or base == "eval":
            return False
        pending[:] = reversed(toks[1:])
        next_tok = _skip_wrapper_options(base, nxt, pending)
        toks = ([] if next_tok is None else [next_tok]) + list(reversed(pending))
    return False


# T-5140 r6: a `--name=VALUE` git option whose name is spelt out (its value may be built at
# run time: `--git-dir=$D`).
_GIT_LONG_OPT_EQ = re.compile(r"--[A-Za-z][\w-]*=")


def _shell_formed_git_push_argv(toks: list) -> bool:
    """Whether the `git ...` argv `toks` ((kind, word, dynamic) from `_shell_formed_push_in_cmd`,
    `toks[0]` the git word) runs `git push`.  r6 (rv1 F1): only the words up to the subcommand
    must be static: an option's value (`-C "$D"`, `-c $kv`, `--git-dir=$D`) and every word
    after the subcommand (`origin "$b"`, `"$@"`, `HEAD:$BR`) may be built at run time.  A word
    built at run time where the subcommand or an option stands may expand to anything (`""`
    drops it, and the next word is the subcommand): that fails closed when a later word is
    `push`, or it or a later run-time word could still spell it (`_push_formable`)."""
    i, n = 1, len(toks)
    while i < n:
        _kind, word, dyn = toks[i]
        if dyn:
            if _GIT_LONG_OPT_EQ.match(word) and not _push_formable(word):
                i += 1  # `--git-dir=$D`: a spelt option, its value built at run time
                continue
            # (a static later word that is `push` itself; a run-time one that could form it)
            return any(_push_formable(t[1]) if t[2] else _PUSH_WORD.fullmatch(t[1]) is not None
                       for t in toks[i:])
        if word in _GIT_VALUE_OPTS:
            i += 2
        elif _GIT_FLAG_OPT.fullmatch(word):
            i += 1
        else:
            break
    return i < n and _PUSH_WORD.fullmatch(toks[i][1]) is not None


# T-5140 r6 (rv1 F2): commands that run their remaining words as one shell command line
# (`watch -n1 'CMD'`, `ssh host CMD`, `parallel CMD ::: ...`), after their own options
# (`_WRAPPER_GRAMMAR`).
_PUSH_LINE_RUNNERS = frozenset({"watch", "ssh", "parallel", "sem"})
# ... and commands one of whose words is a shell command line (`tmux new 'CMD'`, `screen -dm
# 'CMD'`, `su -c 'CMD'`, `trap 'CMD' EXIT`): each word holding a blank is read as code (a
# superset: a word that is only text adds a reading, never removes one).
_PUSH_WORD_RUNNERS = frozenset({"tmux", "screen", "su", "runuser", "trap", "machinectl",
                                "systemd-run", "sudo", "doas", "run0", "pkexec"})


def _push_runner_codes(c: _PyCmd) -> list[str]:
    """T-5140 r6: the shell command lines `c` runs through a command-line runner (not a shell
    `-c`, eval or env -S, which `_py_code_strings` reads), for the push reader only.  r9 (rv4
    F5b): a runner word counts wherever it stands, also after echo/grep: a function or alias
    from text this reader cannot see (`source FILE`, `BASH_ENV`) may make those run their
    arguments.  `echo watch 'SPLIT-PUSH'` denies: an accepted LOW over-block."""
    words = c.words
    out: list[str] = []
    for j, w in enumerate(words):
        base = w.rsplit("/", 1)[-1]
        if base in _PUSH_LINE_RUNNERS:
            pending = [("word", words[k], k in c.dyn) for k in range(len(words) - 1, j, -1)]

            def nxt(pending=pending):
                return pending.pop() if pending else None

            tok = _skip_wrapper_options(base, nxt, pending)
            rest = ([] if tok is None else [tok]) + list(reversed(pending))
            if rest:
                out.append(" ".join(t[1] for t in rest))
            break
        if base in _PUSH_WORD_RUNNERS:
            rest = words[j + 1:]
            if base == "trap":
                # `trap CMD SIG...`; a lone operand is a signal to reset, not code (rv1 LOW)
                ops = [x for x in rest if x not in ("--", "-p", "-l", "-P")]
                rest = ops[:1] if len(ops) > 1 else []
            out += [x for x in rest if any(ch in x for ch in " \t\n")]
            break
    return out


def _push_formable(text: str) -> bool:
    """T-5140 r4: whether shell quote removal could spell `push` in `text`
    (a necessary condition).  `_PyLexer` forms a word only by dropping quote
    characters, escaping backslashes, backslash-newlines and the `$` of a
    `$"..."`/`$'...'` prefix, so any quote or escape split of `push` still
    reads `push` once those are dropped.  (ANSI-C escape decoding and a
    run-time `$(...)` join are outside this reader: T-5159.)"""
    plain = _PUSH_FORM_PREFIX.sub("", text.replace("\\\n", "")).translate(_QUOTE_STRIP)
    return "push" in plain


# (T-5140 r5: a `$` before an escaped quote too: a code string's `\$\"` is a child's `$"`)
_PUSH_FORM_PREFIX = re.compile(r"""\$(?=\\*["'])""")


def _shell_formed_push_in_text(text: str) -> bool:
    """Whether `text`, or a code string it runs, executes a shell-formed
    `git push` (`_shell_formed_push_in_cmd`).  r4: text this reader cannot
    read -- past `_PyLexer`'s frame limit, or a code string past
    `_PY_CODE_DEPTH` or the re-read budget -- is unknown; it fails closed
    when it could still spell `push` (`_push_formable`).  Each text read
    (`text`, and every code string) is read in its `_push_lex_texts` forms."""
    budget = _PY_CODE_BUDGET * len(text) + 4096
    work = []
    for form in _push_lex_texts(text):
        lex = _py_lex(form)
        if lex.frame_limited and _push_formable(form[lex.tail_start:]):
            return True
        work += [(c, 0) for c in lex.cmds]
    while work:
        command, depth = work.pop()
        if _shell_formed_push_in_cmd(command):
            return True
        # r6 (rv1 F3): an eval's joined words are read as code too (`eval '! git ...'`,
        # `eval '(git ...)'` inside a code string, where no eval-joined copy of the command
        # is judged); at top level that is a second reading of T-4475's, adding denials only.
        codes = (_py_code_strings(command) or []) + _push_runner_codes(command)
        for code in codes:
            budget -= len(code)
            if depth >= _PY_CODE_DEPTH or budget < 0:
                if _push_formable(code):
                    return True  # an unread code string that could push
                continue
            for form in _push_lex_texts(code):
                sub = _py_lex(form)
                if sub.frame_limited and _push_formable(form[sub.tail_start:]):
                    return True
                work.extend((c, depth + 1) for c in sub.cmds)
    return False


def _push_lex_texts(text: str) -> tuple[str, ...]:
    """T-5140: `text`, and when it holds a locale string `$"..."`, also `text` with the `$` of
    each `$"` dropped.  bash reads `$"..."` as its text (no message catalog): `pu$"sh"` is
    `push`, where `_PyLexer` keeps the `$` and marks a run-time word.  The second form only
    drops `$` characters, so it is read in addition (OR'ed): it can only add the push deny.
    r6: an escaped `\\$"` (a literal `$`, then a quote) is left as it is (rv1 p2 over-block)."""
    if '$"' not in text:
        return (text,)
    alt = _LOCALE_DOLLAR.sub(r"\1", text)
    return (text, alt) if alt != text else (text,)


# A `$` that starts a locale string `$"...` (not escaped: an even run of backslashes before it).
_LOCALE_DOLLAR = re.compile(r'(?<!\\)((?:\\\\)*)\$(?=")')


def _shell_formed_push_match(command: str) -> bool:
    """T-5140: push spelt by shell quote removal in executable text only.

    Judge the command itself and every statically known interpreter code
    string, then quoted heredoc bodies except those whose reader is proven to
    treat them as data by T-5109's stricter classifier.  Unknown readers stay
    executable (fail closed); mixed data/shell heredocs retain the per-body
    distinction.
    """
    # (a backslash-newline or a `$'...'` prefix inside the split, `pu\<newline>sh`, `pu$'sh'`,
    # is dropped by `_PyLexer` as well: `_push_formable`; an ANSI-C escape, which `_PyLexer`
    # decodes, can spell any word: `$'pu\x73h'`)
    if not any(ch in command for ch in "'\"\\") or not (
            _push_formable(command) or _ANSI_C_ESCAPE.search(command)):
        return False  # raw `_push_match` already covers an unquoted spelling
    if _shell_formed_push_in_text(command):
        return True
    if any(_shell_formed_push_in_text(command[a:e])
           for a, e in _shell_read_heredoc_bodies(command)):
        return True
    return False


_NO_SCAN = object()


def _shell_read_heredoc_bodies(command: str, scanned=_NO_SCAN) -> list[tuple[int, int]]:
    """T-5140: the raw [start, end) heredoc body spans of `command` that may
    be read as shell code: every body except those T-5109's stricter
    classifier (`_hd_join_reader_targets`) proves data.  Unknown readers
    stay executable (fail closed); mixed data/shell heredocs keep the
    per-body distinction.  `scanned` is `_scan(command)` when the caller
    already has it."""
    if "<<" not in command:
        return []
    bodies, data, _complete = _classified_data_heredoc_spans(
        command, _hd_join_reader_targets)
    if not bodies:
        # The strict T-4154 lexer deliberately bails on source/eval and other
        # reader-changing words.  `_scan` can still expose literal body spans;
        # none is proven data in that case, so inspecting them is fail-closed.
        if scanned is _NO_SCAN:
            scanned = _scan(command)
        if scanned is not None:
            bodies = [(start, end) for start, end, _resume, _expanded in scanned.bodies]
    data_set = set(data)
    return [span for span in bodies if span not in data_set]


def _deny(reason: str) -> dict:
    """The one deny-payload shape every worker-guard block emits."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _reason(what: str) -> dict:
    return _deny(
        f"BLOCKED (worker-guard, T-1345): a subagent attempted {what}. This is "
        "reserved to the head orchestrator / project-orchestrator tier and is "
        "structurally denied to workers after the 2026-06-27 runaway. If you "
        "believe you need this, STOP and report the need to your dispatcher in "
        "your final message — do not route around this guard."
    )


# T-4389: security-gating hooks fail CLOSED on their own clock. The harness's hook timeout is
# 600s by default and a timed-out PreToolUse hook is a NON-BLOCKING error (the tool call
# proceeds), so any super-linear path that could be driven past it would be a fail-open. Two
# structural bounds, independent of which path is slow:
#   * BASH_COMMAND_MAX_CHARS: a subagent Bash command longer than this is denied before any
#     parsing (no legitimate command approaches it; file content belongs in the Write tool).
#     It caps the Bash command only: Edit/Write payloads legitimately exceed 64 KiB.
#   * DECIDE_DEADLINE_S: the TOTAL wall-clock budget of a subagent hook call, counted from
#     PROCESS START (the harness's timeout is counted from spawn, so interpreter start, the shim
#     and the import all spend it); main() denies when it runs out. Not configurable from the
#     environment (a worker controls its own env); tests pass a SHORTER value to
#     main(deadline=...), which is clamped so it can never be lengthened.
BASH_COMMAND_MAX_CHARS = 65_536
# 5s total from process start: the smallest hook timeout this guard is wired under is Codex's
# `timeout = 10` (tools/harness_adapter/codex.py _WORKER_GUARD_BLOCK, counted from spawn), and a
# Codex hook timeout also fails OPEN (T-4321). The worst LEGIT call measured ~2.0s including
# process start (a 1.7 MB apply_patch over 20k files), so 5s is ~2.5x headroom; an invariant
# test (test_worker_guard_shim.TestDeadlineUnderHarnessTimeouts) pins DECIDE_DEADLINE_S + 4.5 < it.
DECIDE_DEADLINE_S = 5.0
# If the budget left when the timer is armed is positive but tiny, arm at least this much.
_DEADLINE_FLOOR_S = 0.05


# T-5224: a SUBAGENT decision also runs under an address-space cap, so a path that grows memory
# without bound (the 2026-10-19 OOMs came from review probes against this guard, not from an
# attack) ends in a MemoryError, which main() turns into the T-4505 fail-closed deny instead of
# taking the host down. The cap is on virtual size (RLIMIT_AS), not RSS: measured on the real
# worst cases (a 64 KiB nested command) the hook process peaks at ~40 MiB VSZ against a ~19 MiB
# baseline, so 1 GiB leaves more than 25x headroom. The main thread never runs under it.
# T-5329: the cap is HEADROOM over the process's current virtual size, not an absolute limit.
# An absolute 1 GiB starved callers that run main() in an already-large process (the test
# suite's long-lived xdist workers): Thread.start() then hung or raised "can't start new
# thread", which hung CI at 99% three times on 2026-10-21. The fresh hook process is ~20 MiB,
# so its effective cap is unchanged.
DECIDE_MEMORY_CAP_BYTES = 1 << 30


def _current_vsz_bytes() -> int:
    """This process's virtual size, from /proc/self/statm; 0 when unreadable (then the cap is
    plain DECIDE_MEMORY_CAP_BYTES, the T-5224 behaviour)."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


@contextlib.contextmanager
def _memory_cap(cap: int = DECIDE_MEMORY_CAP_BYTES):
    """Lower the soft RLIMIT_AS to current VSZ + `cap` (never raise a lower one, never touch the hard limit)
    for the body, then put the old soft limit back BEFORE any handler runs, so the deny can still
    be built. Where `resource` or the limit is unavailable the body runs uncapped (the deadline
    still applies)."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = _current_vsz_bytes() + cap  # T-5329: headroom over what the process already maps
        new = cap if soft == resource.RLIM_INFINITY else min(soft, cap)
        if hard != resource.RLIM_INFINITY:
            new = min(new, hard)
        resource.setrlimit(resource.RLIMIT_AS, (new, hard))
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
        except Exception:
            pass

_TOO_LONG = _deny(
    f"BLOCKED (worker-guard, T-4389): this Bash command is longer than {BASH_COMMAND_MAX_CHARS} "
    "characters, the most this guard will read; refused (fail-closed). Split the command, or "
    "write long content to a file with the Write tool and run or pass that file."
)
_SHELL_NESTING_TOO_DEEP = _deny(
    f"BLOCKED (worker-guard, T-5140): this Bash command exceeds the structural "
    f"nesting limit of {_SCAN_NEST_MAX} executable shell frames; refused "
    "(fail-closed). Split or simplify the command."
)
_TIMED_OUT = _deny(
    "BLOCKED (worker-guard, T-4389): worker-guard timed out deciding this command; refused "
    "(fail-closed). Split or simplify the command."
)


_WORD_START_BEFORE = frozenset(" \t\n;&|()<>`")


def _comment_cut_text(command: str) -> str | None:
    """T-4393: `command` as bash reads its comments, or None when that is
    `command` itself. bash ends a comment at the first newline in it, even
    one after a `\\` (`ls # c \\<newline>sudo id` runs sudo), but every rule
    here reads `\\`+newline as a line continuation (`_scan` joins it, and
    the raw-text rules skip it), so the next line's command is hidden inside
    the comment. This copy turns each such `\\` into a space; `decide()`
    judges both texts and denies if either does, so it only ever adds
    denials, for every rule at once.

    A comment starts at an unquoted `#` that begins a word. The `#` is found
    on `_scan`'s masked text, where data quotes are blanked (`echo "a #b" \\`
    continues the line in bash), mapped back to the raw text; when `_scan`
    gives up, on the raw text (fail-closed). Each `#` is visited once and
    each comment's line is searched once: linear."""
    if "#" not in command or "\\\n" not in command:
        return None
    scanned = _scan(command)
    view, rawmap = (command, None) if scanned is None else (scanned[1], scanned[2])
    cuts: list[int] = []
    line_end = -1  # raw index of the newline ending the last comment seen
    i = view.find("#")
    while i >= 0:
        r = i if rawmap is None else rawmap[i]
        if r > line_end and (i == 0 or view[i - 1] in _WORD_START_BEFORE) and command[r] == "#":
            line_end = command.find("\n", r)
            if line_end < 0:
                break
            if command[line_end - 1] == "\\":
                cuts.append(line_end - 1)
        i = view.find("#", i + 1)
    if not cuts:
        return None
    parts, pos = [], 0
    for c in cuts:
        parts.append(command[pos:c])
        parts.append(" ")
        pos = c + 1
    parts.append(command[pos:])
    return "".join(parts)


# --- T-4426 r3 / T-4481: a command word must be a plain literal ---
# orchestrator's ratified rule: in subagent context, a word at command position is
# NON-LITERAL if it holds a quote character, a backslash, a `$'...'` or `$"..."`
# span, or runs into a backslash-newline; such a word is refused, and nothing
# is decoded. Real expansions (`$NAME`, `${...}`, `$(...)`, backticks, also
# inside double quotes) are not classified here: a command word whose only
# non-literal content is expansions stays with T-4135's dynamic-word rule.
# Command positions are the ones `_escalation_walk` already reads as command
# words (after assignments and redirections, after the wrappers it parses,
# after `-exec`, inside the bash -c / eval / ssh strings it re-reads), so this
# rule follows that walk (`_NonLiteralLevel`) instead of walking again.
_NONLITERAL_COMMAND_WORD = _deny(
    "BLOCKED (worker-guard, T-4426/T-4481): a command word here is not a plain literal (it "
    "holds a quote, a backslash, $'...' or $\"...\", or runs into a backslash-newline); "
    "refused (fail-closed). Write the command name plainly, e.g. `ls -l`, not `\"ls\" -l` or "
    "`l\\s -l`. (A command word built only from $VAR, ${...}, $(...) or backticks is judged "
    "by the dynamic-word rule instead.)"
)
# T-4426 r4: the rule above reads a level only through `_scan`, which gives up
# on some valid bash (a `case` inside `"$(...)"`, a backslash in backticks, an
# unterminated quote on a later line). T-4506: `_RawLevel` reads such a level;
# this reason is kept for a command word there spelt `$'...'` / `$"..."`, and
# for such quoting where that lexer cannot follow bash.
_UNREADABLE_QUOTING = _deny(
    "BLOCKED (worker-guard, T-4426): this command has a part the guard cannot parse, "
    "together with ANSI-C ($'...') or locale ($\"...\") quoting; refused (fail-closed). "
    "Simplify the command, or write the command word plainly."
)
_NL_PARAM = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?!$-])")
_NL_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_NL_HEREDOC = re.compile(r"<<(-?)[ \t]*")
_NL_WORD_RUN = re.compile(r"[^ \t\r\f\v\n;&|()<>]*")
_NL_EMPTY_WORD = re.compile(r"(?:''|\"\")+")


def _nl_lex(raw: str, i: int, dq: str | None = None, brace: int = 0):
    """Read the shell word at raw[i] (or its rest after a substitution) and
    classify it. True: non-literal. False: it ended, literal apart from real
    expansions. (p, dq, brace): it reached a `$(` or backtick at raw[p], whose
    body the walk reads; the word goes on after it in that state. `dq`: None
    outside double quotes, "" inside ones that hold no expansion yet, "x"
    inside ones that do. A double-quoted span that holds an expansion is that
    expansion's quoting (`"$VENV/bin/python"`, `"$(printf ls)"`): T-4135's
    dynamic-word rule judges it. One that holds none (`"ls"`, `""`) is a
    quoted literal. `brace`: the open `${` depth."""
    n = len(raw)
    while i < n:
        c = raw[i]
        if brace:  # inside `${...}`: an expansion, whatever it holds
            if c == "`" or raw.startswith("$(", i):
                return (i, dq, brace)
            if c == "\\":
                i += 2
                continue
            if raw.startswith("${", i):
                brace += 1
                i += 2
                continue
            if c == "}":
                brace -= 1
            i += 1
            continue
        if dq is not None:
            if c == '"':
                if not dq:
                    return True  # `"ls"`, `""`: a quote pair around no expansion
                dq = None
                i += 1
                continue
            if c == "\\":
                return True
            if c == "`" or raw.startswith("$(", i):
                return (i, "x", 0)
            if raw.startswith("${", i):
                dq, brace = "x", 1
                i += 2
                continue
            m = _NL_PARAM.match(raw, i) if c == "$" else None
            if m is None:
                i += 1  # a literal character inside the quotes
                continue
            dq = "x"
            i = m.end()
            continue
        if c in _SHELL_WORD_END:
            return False
        if raw.startswith("\\\n", i):
            # bash drops a `\`+newline; the word runs into what follows it
            # (`l\<nl>s` is `ls`: non-literal) unless that ends it
            # (`while\<nl> read`: the word is `while`).
            j = _nl_start(raw, i)
            return not (j >= n or raw[j] in _SHELL_WORD_END)
        if c in "'\\" or raw.startswith(("$'", '$"'), i):
            return True
        if c == '"':
            dq = ""
            i += 1
            continue
        if c == "`" or raw.startswith("$(", i):
            return (i, None, 0)
        if raw.startswith("${", i):
            brace = 1
            i += 2
            continue
        m = _NL_PARAM.match(raw, i) if c == "$" else None
        i = m.end() if m else i + 1
    return dq is not None  # an unterminated double quote


def _nl_start(raw: str, s: int) -> int:
    """`s` moved past a `\\`+newline continuation it may sit in or before."""
    while True:
        if raw.startswith("\\\n", s):
            s += 2
        elif raw.startswith("\n", s) and s > 0 and raw[s - 1] == "\\":
            s += 1
        else:
            return s


_NL_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_NL_PLAIN_WORD = re.compile(r"[A-Za-z0-9_./+-]+")
# Command names the walk reads further words of as commands or code.
_NL_ENV_WALKED = frozenset(_SHELLS | _WRAPPER_GRAMMAR.keys() | _ESCALATOR_WORDS
                           | _KEYWORD_WORDS | _COMPOUND_OPEN.keys())


def _nl_env_operand_end(raw: str, s: int) -> tuple[int, bool]:
    """T-4426 r4: (end, assign) for the shell word at raw[s], read as an env
    operand: `end` is its raw end, or -1 when it holds anything but literal
    text and quotes (a substitution, a backtick, `$'`, `$"`, a `\\`+newline,
    an unterminated quote); `assign`: it is NAME=value once dequoted."""
    n, i = len(raw), s
    while i < n:
        c = raw[i]
        if c in _SHELL_WORD_END:
            break
        if c == "`" or raw.startswith(("$(", "$'", '$"', "\\\n"), i):
            return -1, False
        if c == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                return -1, False
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and raw[i] != '"':
                if raw[i] == "`" or raw.startswith(("$(", "\\\n"), i):
                    return -1, False
                i += 2 if raw[i] == "\\" else 1
            if i >= n:
                return -1, False
            i += 1
        else:
            i += 2 if c == "\\" else 1
    i = min(i, n)
    return i, bool(_NL_ENV_NAME.match(_shell_word(raw[s:i], 0)))


def _in_ranges(ranges: list[tuple[int, int]], starts: list[int], pos: int) -> bool:
    k = bisect.bisect_right(starts, pos) - 1
    return k >= 0 and pos < ranges[k][1]


def _heredoc_body_ranges(masked: str, raw: str, rawmap: tuple[int, ...],
                         skip) -> list[tuple[int, int, bool]]:
    """T-4389 B(1): used ONLY by the base-reading pass (`_SCAN_HEREDOCS` off,
    MF3), so that pass is origin/main's reading exactly; `_scan`'s own
    `.bodies` serve the normal reading (unify under T-4438).
    Raw [start, end, expands) of each heredoc body, in one forward pass. An
    operator (`<<W`, `<<-W`, not `<<<`) is found on `_scan`'s masked text, so
    one inside a data string is none; `skip(k)` drops one in a comment or an
    interpreter string. Its delimiter is the shell word after it with quotes
    removed (`_shell_word`); `expands` is False when any of it is quoted, as
    bash then expands nothing in the body. The bodies of a line's operators
    follow that line in order, each to the line equal to its delimiter (tabs
    stripped for `<<-`), or to the end of the text. Every masked and raw
    character is read a bounded number of times: linear."""
    out: list[tuple[int, int, bool]] = []
    rn = len(raw)
    pos = 0
    while True:
        i = masked.find("<<", pos)
        if i < 0:
            return out
        eol = masked.find("\n", i)
        if eol < 0:
            return out
        delims = []
        k = i
        while 0 <= k < eol:
            if masked.startswith("<<<", k):
                k = masked.find("<<", k + 3)
                continue
            if not skip(k):
                m = _NL_HEREDOC.match(raw, rawmap[k])
                # The delimiter word, read only up to a blank or metacharacter
                # (each operator's run is its own text: linear). A quote left
                # open in it (`<<"E F"`) is not modelled: no body is assumed.
                run = _NL_WORD_RUN.match(raw, m.end()).group()
                word = _shell_word(run, 0)
                if word and run.count("'") % 2 == 0 and run.count('"') % 2 == 0:
                    expands = not any(ch in "'\"\\" for ch in run)
                    delims.append((bool(m.group(1)), word, expands))
            k = masked.find("<<", k + 2)
        if not delims:
            pos = eol + 1
            continue
        b = rawmap[eol] + 1
        for strip, word, expands in delims:
            start = b
            while True:
                if b >= rn:
                    out.append((start, rn, expands))
                    return out
                e = raw.find("\n", b)
                e = rn if e < 0 else e
                line = raw[b:e]
                if (line.lstrip("\t") if strip else line) == word:
                    out.append((start, b, expands))
                    b = e + 1
                    break
                b = e + 1
        pos = bisect.bisect_left(rawmap, b)


class _NonLiteralSink:
    """One decision's result: `hit` once any walk of it finds a non-literal
    command word outside a comment or a heredoc body; `unreadable` (T-4426 r4,
    narrowed by T-4506) once a level `_scan` cannot read has a command word
    spelt with `$'` or `$"`, or holds them where `_RawLevel` cannot follow
    bash; `undecided` (T-4506) once such a level holds other quoting or a
    backslash there."""

    def __init__(self):
        self.hit = False
        self.unreadable = False
        self.undecided = False


class _NonLiteralLevel:
    """The non-literal rule over one `_escalation_walk` (one code-string level:
    `text` is `_scan`'s masked text of `raw`). The walk calls `sep` on each
    separator token and `word` on each word token, with its own `cmdpos`.

    The walk reads every word after a separator as a command word; frames
    (`$(`, backtick, `(`, array) correct that where bash does not:
      - an array's elements are never command words (`a=("x")`), and after an
        array, a substitution, or a glued `(...)` (`<(...)`) that was not
        inside an assignment prefix, the next words are still arguments
        (`local a=(x) "y"`, `echo $(date) "x"`) until a separator (`quiet`);
      - a word glued to a `)` or closing backtick, or starting at a data
        string's closing quote, is the rest of an earlier word;
      - a word inside a multi-token assignment word (`a["k k"]=x`) is not one
        (`_AssignRuns`, as the walk's own `note_assign` reads it);
      - after `env`, a quoted word holding a `=` is env's NAME=value operand;
      - a command word holding a substitution (`"$(printf ls)"`, `$(x)"ls"`)
        is read on across it, from `_nl_lex`'s pause to the text after it."""

    def __init__(self, sink: _NonLiteralSink, text: str, spans: dict[int, int] | None,
                 raw: str, rawmap: tuple[int, ...], xspans: dict[int, int] | None):
        self.sink, self.text, self.raw, self.rawmap = sink, text, raw, rawmap
        self.spans, self.xspans = spans or {}, xspans or {}
        # [kind, continuation state or None, quiet, what the close leaves]:
        # the last is, for an array, whether it sat at command position, and
        # for any other frame whether the words after its close are arguments.
        self.frames: list[list] = [[None, None, False, False]]
        self.pause = None  # (p, dq, brace) from `_nl_lex`: a word stopped at raw[p]
        self.tail = None  # (at, (dq, brace)): a word goes on at raw[at]
        self.close_at = -1  # raw index right after the last `)` / closing backtick
        self.subs: list[int] = []  # raw index of each open `$(` / backtick
        self.assign_end = -1  # raw end of the last assignment word at command position
        self.assigns: _AssignRuns | None = None
        self.last_cmd = -1  # raw start of the last literal command word since a separator
        self.closes: set[int] | None = None  # masked indices of data strings' closing quotes
        self.last_pos = 0  # raw index of the last token the walk handed over
        self.prev_start = -1  # raw start of the last word read at command position
        self.has_empty = '""' in raw or "''" in raw  # an empty quoted word is possible
        self.skips = None  # lazily: (comments, their starts, bodies, their starts)
        self.env_scan: list | None = None  # `_in_env_operand`'s cursor (r4)
        self.env_plain: dict[int, bool] = {}  # `_env_command_plain`'s memo (r4)

    def _hit(self, pos: int) -> None:
        """A non-literal command word at raw[pos]: a hit, unless it is text."""
        if not self.is_text(pos):
            self.sink.hit = True

    def is_text(self, pos: int) -> bool:
        """raw[pos] is comment text or heredoc body text, so no command word
        (nor a code string the walk re-reads) starts there. In a body bash
        expands (unquoted delimiter), a word inside a `$(`/backtick opened in
        that body is a real command."""
        if self.skips is None:
            self.skips = self._skip_ranges()
        comments, cstarts, bodies, bstarts = self.skips
        if _in_ranges(comments, cstarts, pos):
            return True
        k = bisect.bisect_right(bstarts, pos) - 1
        if k >= 0 and pos < bodies[k][1]:
            start, _end, expands = bodies[k]
            return not (expands and self.subs and self.subs[-1] >= start)
        return False

    def _skip_ranges(self):
        scanned = _scan(self.raw)
        comments = list(scanned.comments) if scanned is not None else []
        cstarts = [a for a, _b in comments]
        if _SCAN_HEREDOCS[0]:
            # T-4389 B(1): the bodies `_scan` read (arithmetic-safe, by
            # bash's quoting), each without its delimiter line.
            bodies = [(a, e, x) for a, e, _r, x in scanned.bodies] if scanned is not None else []
            return comments, cstarts, bodies, [a for a, _b, _e in bodies]
        # The base-reading pass (MF3): origin/main's post-hoc body ranges.
        xs = sorted(self.xspans.items())
        xstarts = [a for a, _b in xs]
        xreach, top = [], -1  # the furthest close among the strings opened so far
        for _a, b in xs:
            top = max(top, b)
            xreach.append(top)

        def skip(k: int) -> bool:
            j = bisect.bisect_right(xstarts, k) - 1
            return (j >= 0 and xreach[j] >= k) or _in_ranges(comments, cstarts, self.rawmap[k])

        bodies = (_heredoc_body_ranges(self.text, self.raw, self.rawmap, skip)
                  if "<<" in self.text else [])
        return comments, cstarts, bodies, [a for a, _b, _e in bodies]

    def _lex(self, pos: int, state=(None, 0)) -> bool:
        """Lex from raw[pos]; True if it hit."""
        r = _nl_lex(self.raw, pos, *state)
        if r is True:
            self._hit(pos)
            return True
        if r is not False:
            self.pause = r
        return False

    def sep(self, tok, cmdpos: bool) -> None:
        s, glued, at = tok[1], tok[2], tok[5]
        self.last_pos = at
        pause, self.pause = self.pause, None
        tail, self.tail = self.tail, None
        self.last_cmd = -1
        top = self.frames[-1]
        if tok[3] is not None and self.raw[tok[3] - 1:tok[3]] == "$":
            # An interpreter string spelt `$'...'` / `$"..."` (`bash -c $'...'`):
            # the code the walk re-reads is not the text bash decodes and runs.
            self._hit(tok[3] - 1)
        if s == "$(" or (s == "`" and top[0] != "`"):
            cont = None
            if pause is not None and pause[0] == at:
                cont = pause[1:]
            elif tail is not None and tail[0] == at:
                cont = tail[1]  # `$(a)$(b)`: the same word goes on
            elif cmdpos and not glued and top[0] != "arr" and not top[2]:
                # A substitution that starts a command word; a quote `_scan`
                # dropped may open the word before it (`"$(x)"`, `'$(x)'`).
                j = bisect.bisect_left(self.rawmap, at)
                start = _nl_start(self.raw, self.rawmap[j - 1] + 1 if j > 0 else 0)
                r = _nl_lex(self.raw, start)
                if r is True:
                    self._hit(start)
                elif r is not False and r[0] == at:
                    cont = r[1:]
            # Inside an assignment prefix (`X=$(date) cmd`) the next word is
            # still a command word; anywhere else it is an argument.
            self.frames.append([s, cont, False, not (glued and at <= self.assign_end)])
            self.subs.append(at)
        elif s == "(":
            if glued and self.raw[at - 1:at] == "=":  # `a=(`, `a+=(`: an array
                self.frames.append(["arr", None, False, cmdpos and not top[2]])
            else:  # a glued one (`<(...)`, `f()`) leaves arguments after it
                self.frames.append(["(", None, False, glued])
        elif s in (")", "`"):
            if len(self.frames) > 1 and (top[0] == "`") == (s == "`"):
                self.frames.pop()
                if top[0] in ("$(", "`"):
                    self.subs.pop()
                if top[1] is not None:
                    self.tail = (at + 1, top[1])
                if top[3] != (top[0] == "arr"):
                    self.frames[-1][2] = True  # the next words are arguments
                self.close_at = at + 1
        elif top[0] != "arr":  # `;`, `&`, `|`, newline, `{`, `!`: a new command
            top[2] = False

    def word(self, tok, cmdpos: bool) -> None:
        self.pause = None
        tail, self.tail = self.tail, None
        top = self.frames[-1]
        if len(tok) < 5:  # a word `env -S` split out of its value
            if cmdpos and top[0] != "arr" and not top[2] and _nl_lex(tok[1], 0) is True:
                self.sink.hit = True
            return
        if tok[4]:  # inside an interpreter string: its own walk reads it
            return
        s = self.last_pos = tok[3]
        if tail is not None and s == tail[0]:
            self._lex(s, tail[1])
            return
        if (s == self.close_at or s < self.assign_end or not cmdpos or top[0] == "arr"
                or top[2]):
            return
        text, rawmap, raw = self.text, self.rawmap, self.raw
        if self.has_empty:
            # `_scan` drops a quoted command word's quotes, so an empty one
            # (`"" -l`) leaves no masked text: it is the raw text between two
            # blanks (or a separator and a blank) just before this word.
            p = bisect.bisect_left(rawmap, s) - 1
            while p >= 0 and text[p] in " \t":
                left = rawmap[p - 1] + 1 if p > 0 else 0
                if ((p == 0 or text[p - 1] in " \t\n;&|(){!`")
                        and _NL_EMPTY_WORD.fullmatch(raw, left, rawmap[p])):
                    self._hit(left)
                    return
                p -= 1
        s = _nl_start(raw, s)
        prev, self.prev_start = self.prev_start, s
        r = _nl_lex(raw, s)
        if r is not True:
            if r is not False:
                self.pause = r
            if "=" in tok[1] and self._assign_end_at(s) >= 0:
                return  # an assignment (`X=1`, `X=$(date)`): not the command word
            self.last_cmd = s
            return
        # Non-literal as read from here; it may still be no command word.
        p = bisect.bisect_left(rawmap, tok[3])
        if p < len(text) and rawmap[p] == tok[3]:
            if self.closes is None:
                self.closes = set(self.spans.values())
            if p in self.closes:
                return  # the closing quote of a data string: `echo "$(x) y"`
        if self._assign_end_at(s) >= 0 or (prev >= 0 and self._assign_end_at(prev) > s):
            return  # an assignment word, or a later piece of one (`a["k k"]=x`)
        if (self.last_cmd >= 0
                and _NL_WORD_RUN.match(raw, self.last_cmd).group().rsplit("/", 1)[-1] == "env"):
            if "=" in _dequote(tok[1]):
                return  # `env 'X=1' cmd`: env reads its own NAME=value operands
            if self._in_env_operand(s):
                return  # r4: a later piece of one quoted with blanks (`env "A=x y" make`)
        self._hit(s)

    def _in_env_operand(self, s: int) -> bool:
        """T-4426 r4: raw[s] is inside (not at the start of) a NAME=value
        operand of the env word at `last_cmd`, built from literal text and
        quotes, and env's real command word after its NAME=value operands is
        one this rule can judge here (`_env_command_plain`). The walk splits
        such an operand at its blanks (`"A=x y"` gives a piece `y"` at
        command position) and reads that piece as env's command, so the real
        command word after it reaches no walk at command position. env's
        words are read once, left to right, from a cursor kept per env word:
        linear. A word this cannot read (a substitution, `$'`, a
        continuation) or a separator stops it."""
        raw, n = self.raw, len(self.raw)
        st = self.env_scan
        if st is None or st[0] != self.last_cmd:
            # [env word start, cursor (-1: stopped), last word start, its end, NAME=value]
            st = self.env_scan = [self.last_cmd, _NL_WORD_RUN.match(raw, self.last_cmd).end(),
                                  -1, -1, False]
        while 0 <= st[1] <= s:
            i = st[1]
            while i < n and raw[i] in " \t":
                i += 1
            if i >= n or raw[i] in _SHELL_WORD_END:
                st[1] = -1
                break
            end, assign = _nl_env_operand_end(raw, i)
            st[1:] = [end, i, end, assign]
        return bool(st[4] and st[2] < s < st[3] and self._env_command_plain(st[3]))

    def _env_command_plain(self, p: int) -> bool:
        """T-4426 r4: after raw[p], past more NAME=value operands, env runs
        no command (a separator or the end), or a plain literal command word
        the walk does not read operands of as commands (no shell, wrapper,
        escalator or keyword). Anything else (a quote, an expansion, a
        redirection) is False, so the piece is refused as before. Every word
        end passed is memoized with the result: linear."""
        raw, n, memo = self.raw, len(self.raw), self.env_plain
        passed: list[int] = []
        while p not in memo:
            passed.append(p)
            i = p
            while i < n and raw[i] in " \t":
                i += 1
            if i >= n or raw[i] in ";&|\n)":
                memo[p] = True
                break
            end, assign = (-1, False) if raw[i] in _SHELL_WORD_END else _nl_env_operand_end(raw, i)
            if end >= 0 and assign:
                p = end
                continue
            word = raw[i:end] if end >= 0 else ""
            memo[p] = (bool(_NL_PLAIN_WORD.fullmatch(word))
                       and word.rsplit("/", 1)[-1] not in _NL_ENV_WALKED)
        r = memo[p]
        for q in passed:
            memo[q] = r
        return r

    def _assign_end_at(self, s: int) -> int:
        """The raw end of an assignment word starting at raw[s] (as the walk's
        `note_assign` reads it), recorded in `assign_end`; else -1."""
        if self.assigns is None:
            self.assigns = _AssignRuns(self.raw, quotes=True)
        e = self.assigns.word_end(s)
        if e is None:
            return -1
        self.assign_end = max(self.assign_end, e)
        return e


_EVAL_DEPTH = 4  # T-4475: levels of eval-in-a-quoted-eval-argument joined
_EVAL_CMD_END = frozenset(";&|\n()")
_EVAL_BLANK = frozenset(" \t")
_EVAL_WORD_END = frozenset(" \t;&|\n()<>")
_EVAL_REDIRECT = re.compile(r"\d*(?:&>>?|<<<|<<-?|<>|>>|>\||[<>]&?)")
# On the masked text at a command position (assignments and keywords already skipped by
# `_cmd_positions`): redirections, `builtin [--]`, `command [-p] [--]`, `time [-p]`, a
# simple-command `coproc` (r3: bash runs `command -- eval`, `builtin -- eval` and
# `coproc eval` as eval), then the `eval` word (group 1). Possessive throughout: each match
# reads its prefix run once.
_EVAL_AT = re.compile(
    r"(?:(?:\d*(?:&>>?|<<<|<>|>>|>\||[<>]&?)[ \t]*+(?:\"[^\"]*+\"|'[^']*+'|[^\s;&|()<>`\"'])++"
    r"|builtin(?:[ \t]++--)?|command(?:[ \t]++-p)?(?:[ \t]++--)?|time(?:[ \t]++-p)?|coproc)"
    r"[ \t]++)*+(eval)(?=[\s;&|()]|$)")


def _eval_construct_end(text: str, i: int, limit: int, memo: dict[int, int]) -> int:
    """The index just past the construct whose opening character is `text[i]` (`'`, `"`,
    the `'` of `$'`, a backtick, the `(` of `$(`/`<(`/`(`, the `{` of `${`), or -1 if it
    does not close before `limit`. Iterative (a context stack). Every construct opened on
    the way records its end (or -1) in `memo`, keyed by its opening index, and a memoized
    one is jumped over, so each character is read once however the constructs nest."""
    hit = memo.get(i)
    if hit is not None:
        return hit if 0 <= hit <= limit else -1
    stack: list[tuple[str, int]] = []
    j = i
    while j < limit:
        c = text[j]
        top = stack[-1][0] if stack else None
        opener = None
        close = False
        if top == "'":
            close = c == "'"
        elif top == "$'":
            if c == "\\":
                j += 1
            else:
                close = c == "'"
        elif c == "\\":
            j += 1
        elif top == "`":
            close = c == "`"
        elif top == '"':
            if c == '"':
                close = True
            elif c == "`":
                opener = c
            elif c == "$" and text.startswith(("$(", "${"), j):
                j += 1
                opener = text[j]
        elif c == "'":
            opener = "$'" if j > 0 and text[j - 1] == "$" else "'"
        elif c == '"' or c == "`" or c == "(" or c == "{" and j > 0 and text[j - 1] == "$":
            opener = c
        elif top == "(" and c == ")" or top == "{" and c == "}":
            close = True
        elif c == "#" and top == "(" and text[j - 1] in " \t\n;&|(":
            nl = text.find("\n", j, limit)
            j = (limit if nl < 0 else nl) - 1  # bash reads `#` to the end of the line
        if opener is not None:
            known = memo.get(j)
            if known is not None and stack:
                if not 0 <= known <= limit:
                    break
                j = known
                continue
            stack.append((opener, j))
        elif close:
            memo[stack.pop()[1]] = j + 1
            if not stack:
                return j + 1
        j += 1
    for _kind, s in stack:
        memo[s] = -1
    return -1


def _eval_word(text: str, i: int, limit: int, memo: dict[int, int], edits: list) -> int:
    """Read the word starting at `text[i]`, appending to `edits` the (start, end, "")
    removals that turn it into what eval receives: quotes and backslashes removed as bash
    removes them. Every expansion is kept as written (a run-time value is left to the
    rules that judge the original copy), and so is a `$'...'` word. Returns the word's
    end, or -1 if a construct in it does not close before `limit`."""
    j = i
    while j < limit:
        c = text[j]
        if c in _EVAL_WORD_END:
            if c in "<>" and j + 1 < limit and text[j + 1] == "(":
                e = _eval_construct_end(text, j + 1, limit, memo)
                if e < 0:
                    return -1
                j = e
                continue
            break
        if c == "\\":
            if j + 1 < limit:
                edits.append((j, j + 2 if text[j + 1] == "\n" else j + 1, ""))
            j += 2
            continue
        if c == "'":
            e = text.find("'", j + 1, limit)
            if e < 0:
                return -1
            edits.append((j, j + 1, ""))
            edits.append((e, e + 1, ""))
            j = e + 1
            continue
        if c == '"' or c == "$" and text.startswith('$"', j):
            edits.append((j, j + (2 if c == "$" else 1), ""))
            k = j + (2 if c == "$" else 1)
            while True:
                if k >= limit:
                    return -1
                d = text[k]
                if d == '"':
                    break
                if d == "\\" and k + 1 < limit:
                    if text[k + 1] in '$`"\\\n':
                        edits.append((k, k + 2 if text[k + 1] == "\n" else k + 1, ""))
                    k += 2
                elif d == "`" or d == "$" and text.startswith(("$(", "${"), k):
                    e = _eval_construct_end(text, k if d == "`" else k + 1, limit, memo)
                    if e < 0:
                        return -1
                    k = e
                else:
                    k += 1
            edits.append((k, k + 1, ""))
            j = k + 1
            continue
        if c == "`" or c == "$" and text.startswith(("$(", "${", "$'"), j):
            e = _eval_construct_end(text, j if c == "`" else j + 1, limit, memo)
            if e < 0:
                return -1
            j = e
            continue
        j += 1
    return j


def _eval_command(text: str, r: int, limit: int, memo: dict[int, int]) -> list | None | int:
    """`text[r:]` starts with the `eval` word: the edits (start, end, replacement) that turn
    that simple command into the line bash runs: the `eval` word and a leading `--` removed,
    each argument dequoted, the blanks between arguments made one space. Redirections stay
    as written. None if the word there is not a bare `eval`; -1 if a construct does not
    close before `limit` (the end of the enclosing backtick body or string)."""
    j = r + 4
    if not text.startswith("eval", r) or j < limit and text[j] not in _EVAL_WORD_END:
        return None
    edits: list = []
    gap = j  # start of the blanks after the last word
    first = True
    redirect_next = False
    while True:
        while j < limit and (text[j] in _EVAL_BLANK or text.startswith("\\\n", j)):
            j += 2 if text[j] == "\\" else 1
        if j >= limit or text[j] in _EVAL_CMD_END or text[j] == "#":
            break
        m = None if redirect_next else _EVAL_REDIRECT.match(text, j)
        is_word = not (m and not text.startswith("(", m.end()))
        if first:
            edits.append((r, j, ""))  # the `eval` word and its blanks
        elif j > gap:
            edits.append((gap, j, " "))
        if not is_word:
            j = gap = m.end()
            redirect_next = True
            first = False
            continue
        start = j
        word_edits: list = []
        j = _eval_word(text, j, limit, memo, word_edits)
        if j < 0:
            return -1
        if j == start:  # a lone `<`/`>` at the limit
            break
        if redirect_next:
            redirect_next = False  # a redirection target stays as written
        elif first and text[start:j] == "--":
            while j < limit and text[j] in _EVAL_BLANK:
                j += 1
            edits[-1] = (r, j, "")
            gap = j
            first = False
            continue
        else:
            edits.extend(word_edits)
        first = False
        gap = j
    if first:
        edits.append((r, r + 4, ""))
    return edits


def _eval_limits(masked: str, rawmap, xspans: dict[int, int], n_raw: int, command: str):
    """For each masked index, via a sorted sweep: the raw index where the innermost
    enclosing backtick body or interpreter string ends (the eval command inside can run
    no further), or None when that is a double-quoted interpreter string (its escapes are
    undone by the enclosing eval's join first, so the next level reads it). Returns a
    function of a non-decreasing masked index."""
    spans: list[tuple[int, int]] = list(xspans.items())
    opened = -1
    for m in re.finditer(r"(?<!\\)`", masked):
        if opened < 0:
            opened = m.start()
        else:
            spans.append((opened, m.start()))
            opened = -1
    spans.sort()
    stack: list[tuple[int, int]] = []
    k = 0

    def at(q: int):
        nonlocal k
        while k < len(spans) and spans[k][0] < q:
            while stack and stack[-1][1] < spans[k][0]:
                stack.pop()
            stack.append(spans[k])
            k += 1
        while stack and stack[-1][1] < q:
            stack.pop()
        if not stack:
            return n_raw
        o, c = stack[-1]
        if o in xspans and command[rawmap[o]] == '"':
            return None
        return rawmap[c] if c < len(rawmap) else n_raw
    return at


def _eval_joined_text(command: str) -> str | None:
    """T-4475: `command` with each simple command whose command word is `eval` turned into
    the line bash actually runs: eval's arguments, dequoted, joined with single spaces and
    parsed again (bash-verified with printf markers: `eval "x=1" "printf zzmark"` and
    `eval "printf a;" printf zzmark` both print the marker, as does each position below).

    The eval commands are found at the command positions `_cmd_positions` yields on
    `_scan`'s masked text (so inside `$(...)`, backticks, `<(...)`, `(...)`, case arms,
    function bodies, and after assignments and keywords; `_EVAL_AT` adds a leading
    redirection, `builtin [--]`, `command [-p] [--]`, `time [-p]` and a simple
    `coproc`), mapped back to the raw text through
    rawmap; when `_scan` gives up, on the raw text. Each eval command becomes a set of
    small edits (removals of its `eval` word, quotes and backslashes; one space per blank
    run), none inside a `$(...)`/backtick/`<(...)` argument, so the eval commands nested in
    those are edited in the same pass; an eval whose edits would touch another's is left to
    the next level. An eval inside a quoted eval argument surfaces at the next level, to
    `_EVAL_DEPTH` levels. Linear per level: constructs are memoized by `_eval_construct_end`
    and each argument character is read by its own eval command only.

    None when no eval command is found. A run-time value (`eval "$(tool -s)"`) is kept as
    written: the copy judges it as the original does. Where a construct does not close,
    the rest of that region is left as is (the original copy is still judged).
    An eval in a quoted heredoc body (T-5109) whose reader is provably no shell
    (`_hd_join_reader_targets`) is data: left as written."""
    if "eval" not in command:
        return None
    overflow = [False]
    text, changed = command, False
    for _ in range(_EVAL_DEPTH):
        scanned = _scan(text, overflow)
        if overflow[0]:
            return None
        if scanned is None:
            view, rawmap = text, None
            positions = _cmd_positions(text, _CMD_ANCHOR_CHARS_RAW)
            limit_at = lambda q: len(text)  # noqa: E731
        else:
            view, rawmap = scanned[1], scanned[2]
            positions = _cmd_positions(view, _CMD_ANCHOR_CHARS)
            limit_at = _eval_limits(view, rawmap, scanned[4], len(text), text)
        # T-5109: a quoted heredoc's body is text bash never expands, so an
        # `eval` there is no command of this text when no shell reads it.
        # Bodies as T-4389 B(1) reads them, in both of decide()'s readings, so
        # the two join alike.
        bscan = _scan_cached(text, True)
        if bscan is _SCAN_TOO_DEEP:
            _SCAN_OVERFLOWED[0] = True  # decide() denies it
            return None
        quoted = [(a, e) for a, e, _r, x in bscan.bodies if not x] if bscan is not None else []
        if quoted:
            # r2: and only a body whose reader is provably no shell (bare
            # `cat`, `python3 -`, the T-4154/T-4328 data sinks; the same
            # lexer, all or nothing). A shell-read body is joined: bash
            # runs the joined line (`bash <<'E'` + `eval "git" "push"`).
            data = _data_heredoc_spans(text, _hd_join_reader_targets)
            dstarts = [a for a, _e in data]
            quoted = [(a, e) for a, e in quoted if data and _in_ranges(data, dstarts, a)]
        qstarts = [a for a, _e in quoted]
        memo: dict[int, int] = {}
        taken: set[int] = set()  # raw indices some accepted edit covers or sits at
        edits: list = []
        skip = 0  # raw index before which an unclosed construct was already read
        for q in positions:
            m = _EVAL_AT.match(view, q) if q < len(view) else None
            if m is None:
                continue
            r = m.start(1) if rawmap is None else rawmap[m.start(1)]
            if quoted and _in_ranges(quoted, qstarts, r):
                continue  # data in a quoted heredoc body (T-5109)
            limit = limit_at(q)
            if r < skip or limit is None or r >= limit:
                continue
            got = _eval_command(text, r, limit, memo)
            if got == -1:
                skip = limit  # unclosed: that region is left as is, read once
                continue
            if got is None:
                continue
            cover = [x for a, b, _rep in got for x in range(a, max(b, a + 1))]
            if any(x in taken for x in cover):
                continue  # inside another eval's own words: the next level joins it
            taken.update(cover)
            edits.extend(got)
        if not edits:
            break
        edits.sort()
        parts, pos = [], 0
        for a, b, rep in edits:
            parts.append(text[pos:a])
            parts.append(rep)
            pos = b
        parts.append(text[pos:])
        text, changed = "".join(parts), True
        if "eval" not in text:
            break
    return text if changed else None


# --- T-5363: the decoded `env -S` reading ---
# GNU env takes its -S/--split-string value in every getopt spelling: a separate word (`-S
# 'cmd'`), glued to the option (`-S'cmd'`, shell word `-Scmd`), at the end of a short cluster
# (`-iS'cmd'`, `-vS 'cmd'`), and as any unique abbreviation of the long option (`--sp=cmd`,
# `--split-s 'cmd'`). The wrapper readers (the peel, the code reader, the escalation walk,
# `_py_env_chdirs`) read every spelling through `_option_parse`, but the raw-text gates (push,
# gh, extglob, the protected-path prefilter) see a glued value as part of its option word
# (`-Sgit`, `-Srm`: no word boundary), and every gate but the code reader saw the value's own
# quoting as written (`env -S 'git "push"'`). This reading is the command with each env -S
# value replaced by the words GNU env splits it into (`_env_s_split`), each one quoted
# plainly, and the option letters around it kept (`env -iS'rm -rf D #x'` -> `env -i rm -rf
# D`). `_decide_readings` judges it through every rule. It only adds denials.
#
# The env commands are found as `_eval_joined_text` finds eval commands: at the command
# positions of `_scan`'s masked text (in `$(...)`, backticks, subshells, after `&&`, ...),
# their words read with `_eval_word`. Every word of a command is checked for an `env` word,
# so a wrapper chain (`nice env --sp '...'`, `timeout 3 env -S...`) is found as well. A quoted
# word that holds an env -S command (`bash -c "env -S'...'"`, `ssh h '...'`, an eval argument)
# is decoded as a command of its own and put back quoted, to `_ENV_S_DECODE_DEPTH` levels.
# An env's -S chain (`env -S '-i -S "-C D" rm X'`) is decoded in one pass, as GNU env reads
# split words as its own arguments again; the result is read again (a split command may run
# another env -S: `env -S'env -S"..."'`) up to `_ENV_S_DECODE_LEVELS` times. Fail closed
# (`_ENV_S_UNREADABLE`): a value env expands at run time (a `${NAME}` word), an -S word the
# shell builds at run time (a `$` or a backtick in it), an env command whose words cannot be
# read, a reading still changing at the level bound, or past the size budget. Main's refusal
# of `\` and `$` in -S values (`_py_wrapper_codes`) stays as it is.
_ENV_S_DECODE_LEVELS = 8
_ENV_S_DECODE_DEPTH = 4
_ENV_S_UNREADABLE = object()
_ENV_S_DYN = object()  # r6: a nested -S value env expands (`${NAME}`): fails closed anywhere
_ENV_S_HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)")  # r5: a heredoc operator, not a `<<<`
# r2: commands that run a word as shell code (a shell's -c operand or stdin, eval, ssh/watch/
# parallel/sem command strings, script/flock/su -c, the escalators' -c): an env -S command
# in a quoted word of theirs that the guard cannot read fails closed.
_ENV_S_CODE_RUNNERS = _SHELL_NAMES | _ESCALATOR_WORDS | frozenset({
    "eval", "ssh", "watch", "parallel", "sem", "script", "flock", "machinectl", "systemd-run",
    "xargs", "find", "trap"})
# r3: commands whose quoted words are only text they print or match: not decoded as code
# strings (a shell their output is fed to reads it as a stdin text of its own,
# `_decide_fed_copies`, whose readings decode it).
_ENV_S_TEXT_EMITTERS = frozenset({"echo", "printf", "grep", "egrep", "fgrep", "rg"})
_ENV_S_DECODE_HINT = re.compile(r"-[A-Za-z0-9]*S|--s")
_ENV_S_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5363): this command runs `env -S`/`--split-string` with a value "
    "the guard cannot read as the words env runs (built at run time, an env `${VAR}` "
    "expansion, nested past the guard's bounds, or words it cannot parse); refused "
    "(fail-closed). Write the command out plainly, without `env -S`."
)


def _env_s_code_quote(code: str) -> str:
    """r3: a decoded code string put back as one shell word: in double quotes when nothing
    in it is special there (no `"`, `$`, backtick, `\\`, `!`), else single-quoted (`shlex`),
    so nested code strings do not grow by a `'"'"'` per quote at each level."""
    if not any(ch in code for ch in '"$`\\!'):
        return '"' + code + '"'
    return shlex.quote(code)


def _env_s_unquoted_procsub(raw: str) -> bool:
    """r3: True if the shell word `raw` holds a process substitution (`<(`/`>(` outside
    quotes), which `_eval_word` keeps as written: its commands are read at their own command
    positions, and quoting the word as a code string would make it a literal. A `<(` inside
    quotes (`watch "env -S'..' <(:)"`, a comment in a code string) is text of that string."""
    if "<(" not in raw and ">(" not in raw:
        return False
    sq = dq = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if sq:
            sq = c != "'"
        elif c == "\\":
            i += 1
        elif c == '"':
            dq = not dq
        elif c == "'" and not dq:
            sq = True
        elif not dq and c in "<>" and raw[i + 1:i + 2] == "(":
            return True
        i += 1
    return False


def _env_s_maybe(text: str) -> bool:
    """A cheap skip: `text` can hold an env -S command only if, quotes and backslashes
    dropped, it names `env` and an -S spelling."""
    view = text.translate(_QUOTE_STRIP)
    if "env" in view and _ENV_S_DECODE_HINT.search(view):
        return True
    if "$'" in text:  # r5: an ANSI-C word may spell either (`$'\x65nv -S...'`)
        view = _env_s_ansi_c(text).translate(_QUOTE_STRIP)
        return "env" in view and bool(_ENV_S_DECODE_HINT.search(view))
    return False


def _env_s_apply(text: str, start: int, end: int, edits: list) -> str:
    """`text[start:end]` with `edits` (sorted removals) applied."""
    parts, pos = [], start
    for a, b, rep in sorted(edits):
        parts.append(text[pos:a])
        parts.append(rep)
        pos = b
    parts.append(text[pos:end])
    return "".join(parts)


def _env_s_command_words(text: str, r: int, limit: int, memo: dict, seen: set,
                         herestrings: list | None = None) -> list | int:
    """The words of the simple command at `text[r]` as (start, end, dequoted text), the
    redirection targets left out; -1 if a construct in it does not close before `limit`.
    The reading stops at a word an earlier reading of this pass read (`seen`, the word
    starts): a command position in the middle of a command (after a `)` or `!` of a word)
    reads only what that one did not, so a pass reads each word once (linear). An env word
    and its options are read together by the first reading that reaches the env word.
    r5: a `<<<` here-string's word is appended to `herestrings` (when given), the same way."""
    words: list = []
    j = r
    redirect_next = False
    herestring = False
    while True:
        while j < limit and (text[j] in _EVAL_BLANK or text.startswith("\\\n", j)):
            j += 2 if text[j] == "\\" else 1
        if j >= limit or text[j] in _EVAL_CMD_END or text[j] == "#" or j in seen:
            return words
        seen.add(j)
        m = None if redirect_next else _EVAL_REDIRECT.match(text, j)
        if m and not text.startswith("(", m.end()):
            j = m.end()
            redirect_next = True
            herestring = m.group().endswith("<<<")
            continue
        start = j
        wedits: list = []
        j = _eval_word(text, j, limit, memo, wedits)
        if j < 0:
            return -1
        if j == start:
            return words
        if redirect_next:
            redirect_next = False
            if herestring and herestrings is not None:
                herestrings.append((start, j, _env_s_apply(text, start, j, wedits)))
            continue
        words.append((start, j, _env_s_apply(text, start, j, wedits)))


# r5: bash's ANSI-C quoting (`$'...'`, bash 5.2 `man bash`, QUOTING), kept as written by
# `_eval_word`: decoded here so a code string spelled with it is read (`tmux new -d
# $'env -S\'...\''`). An escape bash does not know stays as written, as bash keeps it.
_ENV_S_ANSI = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_ENV_S_ANSI_ESC = re.compile(
    r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})"
    r"|U([0-9A-Fa-f]{1,8})|c(.))", re.S)
_ENV_S_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                      "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _env_s_ansi_c(text: str) -> str:
    """`text` with each `$'...'` segment replaced by the text bash makes of it."""
    if "$'" not in text:
        return text

    def esc(m: re.Match) -> str:
        simple, octal, hx, u4, u8, ctl = m.groups()
        if simple is not None:
            return _ENV_S_ANSI_SIMPLE[simple]
        if ctl is not None:
            return chr(ord(ctl) & 0x1F)
        code = int(octal, 8) if octal else int(hx or u4 or u8, 16)
        return chr(code & 0xFF) if octal else (chr(code) if code < 0x110000 else "")

    # (a NUL ends the string, as in bash)
    return _ENV_S_ANSI.sub(lambda m: _ENV_S_ANSI_ESC.sub(esc, m.group(1)).split("\x00", 1)[0], text)


# A word left unquoted in the decoded reading: no character the shell expands, quotes or
# splits there (a `[`, `]`, `{`, `}` alone is literal to bash too, but a bracket or brace word
# could glob or brace-expand, which only adds denials). Quoting a plain word would make a
# command word non-literal (T-4426: `env -S'[ -f x ]'` -> `env '[' ...`).
_ENV_S_PLAIN = re.compile(r"[A-Za-z0-9_@%+=:,./\[\]{}^-]+")


def _env_s_quote(word: str) -> str:
    """`word` quoted plainly for the decoded reading (as is when nothing in it needs it)."""
    return word if _ENV_S_PLAIN.fullmatch(word) else shlex.quote(word)


def _env_s_env_edits(text: str, ws: list, i: int, budget: list, lenient: bool = False):
    """The edit turning the arguments of the env word `ws[i]` into the ones env reads once
    every -S value is split, [] when it has no -S option, or `_ENV_S_UNREADABLE`. Env's
    options are read as the wrapper peel reads them (`_option_parse`, both its readings: a
    superset), and as GNU env reads them: the split words come next, read as env's own
    arguments again (`env -S '-i -S "-C D" rm X'`), so a chain of -S options is decoded in one
    pass. Each split is charged to `budget` (a `-S-S-S...` chain re-splits a shrinking word:
    past the budget, fail closed). Option words of the command line are kept as written;
    split words are quoted plainly (`_env_s_quote`)."""
    shorts, longs, _ops = _WRAPPER_GRAMMAR["env"]
    pending: list[str] = []  # split words not read yet, the next one last
    out: list[str] = []  # env's arguments as the decoded reading writes them
    k = i + 1  # the next word of the command line
    found = False

    def peek(off: int):
        if off < len(pending):
            return pending[-1 - off]
        j = k + off - len(pending)
        return ws[j][2] if j < len(ws) else None

    def take():
        # (the word, as written in the reading, its index in `ws` or None for a split word)
        nonlocal k
        if pending:
            w = pending.pop()
            return w, _env_s_quote(w), None
        k += 1
        return ws[k - 1][2], text[ws[k - 1][0]:ws[k - 1][1]], k - 1

    while True:
        v = peek(0)
        if v is None:
            break
        if v == "-" or _ASSIGN_WORD.match(v):
            out.append(take()[1])
            continue
        if v == "--" or len(v) < 2 or v[0] != "-":
            break
        nw = peek(1)
        parsed = _option_parse("env", v, nw, shorts, longs, False)
        if parsed[2] not in ("S", "split-string"):
            legacy = _option_parse("env", v, nw, shorts, longs, True)
            if legacy[2] in ("S", "split-string"):
                parsed = legacy
        at, take_next, key = parsed
        if key == "?":
            break
        _w, emit, src = take()
        if key not in ("S", "split-string"):
            out.append(emit)
            if take_next and peek(0) is not None:
                out.append(take()[1])
            continue
        if take_next:
            value, _vemit, src = take()
        else:
            value = v[at:] if at is not None else ""
        if src is not None and not lenient:
            raw = text[ws[src][0]:ws[src][1]]
            if "$" in raw or "`" in raw:
                return _ENV_S_UNREADABLE  # a value the shell builds at run time
        budget[0] -= len(value) + 1
        if budget[0] < 0:
            return _ENV_S_UNREADABLE
        words = _env_s_split(value)
        if words is None and lenient and "$" in value:
            # r6: a bare `$` the enclosing shell expanded first (`"env -S'gi\"t\" push $W'"`):
            # read as text, so the rest still loses its quoting (`gi"t"` -> `git`)
            words = _env_s_split(re.sub(r"\$(?!\{)", r"\\$", value))
        if words is None:
            words = value.split()  # env rejects it and runs nothing: `_env_s_words`' superset
        if any(isinstance(w, _HoDynWord) for w in words):
            # a `${NAME}` env itself expands at run time, at every depth (r6: `export P=push;
            # watch "env -S'git \${P}'"`); `lenient` covers only what a shell expanded first.
            # A nested one fails closed in any command (`_ENV_S_DYN`), not only a runner's.
            return _ENV_S_DYN if lenient else _ENV_S_UNREADABLE
        prefix = "" if key == "split-string" or at is None else v[:at - 1]
        if prefix not in ("", "-"):
            out.append(_env_s_quote(prefix))  # the cluster's flags (`-iS...` -> `-i`)
        pending.extend(reversed([str(w) for w in words]))
        found = True
    if not found:
        return []
    out.extend(_env_s_quote(w) for w in reversed(pending))
    # From the first argument through the last command-line word env read (a redirection
    # among them is left out of this reading; the original is judged with it).
    return [(ws[i + 1][0], ws[k - 1][1], " ".join(out))]


# r6: `env`, up to four short-option words, then a short cluster ending in `S` with its value
# glued on, the words parted by blanks, quotes or `:` (an IFS split): bounded, so linear.
_ENV_S_GLUED = re.compile(
    r"(?<![\w./])(?<!\w-)(env(?:[\s:'\"]{1,8}-[A-Za-z0-9]{1,8}){0,4}[\s:'\"]{1,8}-[A-Za-z0-9]{0,8}S)"
    r"(?=[^\s:'\"])")


# r6: an `env` word heading an -S option (glued or not), in text with its quotes dropped.
_ENV_S_EXPANSION_BLANK = str.maketrans("$`", "  ")
_ENV_S_ENV_AT = re.compile(r"(?<![\w./])(?<!\w-)env(?=[\s:'\"\\])")
_ENV_S_ANY_S = re.compile(
    r"(?<![\w./])(?<!\w-)env(?:[\s:]{1,8}-[A-Za-z0-9]{1,8}){0,4}[\s:]{1,8}-[A-Za-z0-9]{0,8}S")

# T-5379: a text that names env anywhere, as a word or an assigned value (`env`, `x=env`,
# `'env'`, `/usr/bin/env`, `x=$(echo env)`, `read x <<< env`, `x=ENV; ${x,,}`, `$'\x65nv'`; not
# `printenv`, `myenv`, `.env`, `env.py`, `foo-env`), may run env with words it builds at run
# time: from a variable on any later line, through an IFS split on any separator, after
# options with arguments or expansions that may be empty, or with a value set by `read`.
# The reading does not follow how the env word arrives: every glued -S cluster in the text
# (`-Sgh`, `-iSgh`) is split off its value (`-S gh`), whatever comes before it (a blank, any
# separator, `=`, a quote, the start of a value, or an ANSI-C escape that decodes to one:
# `$'env\x0a-Sgh'`), and main's raw-text rules judge the words as they judge the same words
# without env. The cluster's `-` must not follow a word character or a `-` (`a-Sgh`, `--S`),
# so `git log -Sgh` is read as `git log -S gh` (a pickaxe of `gh`: allowed). A cost, failing
# closed: in a text that names env, a glued -S whose value is a gh/sudo/push command word
# reads as that command (`PICK=-Ssudo; git log $PICK`). One regex pass, bounded per match
# (linear).
_ENV_S_ENV_WORD = re.compile(r"(?<![\w.])(?<!\w-)(?i:env)(?![\w.-])")
_ENV_S_CLUSTER = re.compile(
    r"(?=[-\\])(\\(?:[abeEfnrtv]|[0-7]{1,3}|x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}"
    r"|U[0-9A-Fa-f]{1,8}|c.)|(?<![\w-]))(-[A-Za-z0-9]{0,8}S)(?=[^\s:])", re.S)
# (r6: a value glued on in quotes, `-S"sudo" true`, `-S'gh pr'`, is a glued value too)
_ENV_S_CLUSTER_BARE = re.compile(r"(?<![\w-])-[A-Za-z0-9]{0,8}S(?=[^\s:'\"])")


def _env_s_names_env(text: str) -> bool:
    """T-5379: True if `text` names env as a word (`_ENV_S_ENV_WORD`), as written, with its
    quotes and backslashes dropped (`e''nv`, `\\env`), or with its ANSI-C words decoded."""
    if _ENV_S_ENV_WORD.search(text) or _ENV_S_ENV_WORD.search(text.translate(_QUOTE_STRIP)):
        return True
    return "$'" in text and bool(
        _ENV_S_ENV_WORD.search(_env_s_ansi_c(text).translate(_QUOTE_STRIP)))


# r5: a function or an alias defined anywhere in the text, quoted or not (`eval "f() {...}"`),
# or an `eval` (which may define one): then no cluster is exempt.
_ENV_S_DEFINES = re.compile(
    r"(?:^|[\s;&|(){}`'\"\\])(?:function[ \t]+[^\s;&|()<>]+|[^\s;&|()<>=$`'\"\\]+[ \t]*\([ \t]*\))"
    r"|(?<![\w-])(?:alias|eval)(?![\w-])|BASH_ALIASES")
# r5: the tools whose -S is an option of their own, not a split string or a command to run:
# git (pickaxe), sort (buffer size), ssh (control socket), rg (smart case), grep (GNU:
# --no-... -S is not one; a cluster such as -Srn), tar (sparse), du (separate dirs), ccache
# (-s/-S stats), make (-S: --no-keep-going), cc/gcc/clang (-S: compile to assembly),
# python/python3 (-S: no site).
_ENV_S_PICKAXE_TOOLS = frozenset({"git", "sort", "ssh", "rg", "grep", "tar", "du", "ccache", "make",
                                  "cc", "gcc", "clang", "python", "python3",
                                  # T-5382 r3: package queries (-S: search, sync)
                                  "dpkg", "dpkg-query", "rpm", "pacman"})
# r5: a character before the cluster, in its command, that may make a word that runs env:
# an expansion (`$`, a backtick: unquoted or in double quotes), a redirection, a glob, a brace
# or a parenthesis (a `$(..;..)` part); an argument such tools pass on to a command
# (`ccache $x -Sgh`, `ssh h $x -Sgh`) is built from one of these
_ENV_S_DIRTY = frozenset("$`<>{}*?[()")
# r5: words before a command word that keep its position (`if git log -Sgh`, `time sort -S1G`)
_ENV_S_LEAD_WORDS = _KEYWORD_WORDS | frozenset({"!", "time"})
_ENV_S_PICKAXE_PATH = re.compile(r"(?:[A-Za-z0-9_.-]*/)*([A-Za-z0-9_.-]+)")


_ENV_S_LONG_OPT_EQ = re.compile(r"--[A-Za-z][\w-]*=")


def _env_s_word_start(text: str, r: int) -> bool:
    """r6: True if the cluster at `r` starts a word: after a blank or a command separator,
    or after a quote that does (`grep -e '-Ssudo'`), or as the value of a long option
    (`git log --grep='-Sgh'`). Not as an assignment's value or in a word's middle (`make
    A=-Sgh\\ pr`, `x,-Sgh`): those words may be handed on (`make E=env A=-S...`). Bounded
    (a long option's name is read back at most 64 characters)."""
    p = r - 1 if text[r - 1:r] in ("'", '"') else r
    before = text[p - 1:p]
    if before in ("", " ", "\t", "\n", ";", "&", "|"):
        return True
    if before != "=":
        return False
    k = max(p - 65, 0)
    k = max([k] + [text.rfind(c, k, p) + 1 for c in " \t\n;&|"])
    return bool(_ENV_S_LONG_OPT_EQ.fullmatch(text, k, p))


def _env_s_pickaxe_starts(text: str, starts: set) -> set:
    """T-5379 r5: the cluster `starts` left unsplit: those of a tool whose -S is its own
    option (`_ENV_S_PICKAXE_TOOLS`: `git log -Sgh`, `sort -S1G`, `git commit -m 'x -Sgh'`).
    A cluster is kept only when ALL of these hold, else it is split (fail closed):
      (1) the text defines no function or alias and has no `eval` (`_ENV_S_DEFINES`);
      (2) its simple command's word, after `NAME=val` prefixes, is a literal word (or path)
          naming such a tool;
      (3) no character of `_ENV_S_DIRTY` comes before it in that command (outside single
          quotes: a `$` in double quotes expands);
      (4) a quoted cluster is in a data string by `_scan_cached`'s reading (not a code string
          a shell runs).
    A cluster in a heredoc body or a comment is in no command (split). One forward pass
    (linear): quotes, comments and heredoc bodies are followed; only `;`, `&`, `|` and a
    newline end a command (a redirection's `&`/`|` is word text); an unreadable text keeps
    none."""
    if not starts or _ENV_S_DEFINES.search(text):
        return set()
    scanned = _scan_cached(text, True)
    if scanned is None:
        return set()
    masked, rawmap = scanned[1], scanned[2]
    blank = {r for q, r in enumerate(rawmap) if r in starts and masked[q] != "-"}
    bodies = sorted((a, e) for a, e, _r, _x in (scanned.bodies or ()))
    out: set = set()
    dirty = False
    cmdw = None  # the command word (after assignments)
    sq = dq = ansi = False  # (ansi: the single quotes are a `$'...'`, where `\'` is a quote)
    w0 = -1
    i, n, nb = 0, len(text), 0

    qenv = [0, 0, False]  # the open quoted string's start, how far it was read, env seen

    def ok_here(r: int, quoted: bool) -> bool:
        if dirty or cmdw is None or not _env_s_word_start(text, r):
            return False
        m = _ENV_S_PICKAXE_PATH.fullmatch(cmdw)
        if m is None or m.group(1) not in _ENV_S_PICKAXE_TOOLS:
            return False
        if not quoted:
            return r not in blank
        # a quoted cluster: in a data string, which names no env before it (a code string a
        # tool runs, `python3 -c '...os.system("env -S...")'`, is read as code); each part
        # of a string is searched once (linear)
        if not qenv[2] and _ENV_S_ENV_WORD.search(
                text[max(qenv[0], qenv[1] - 3):r].translate(_QUOTE_STRIP)):
            qenv[2] = True
        qenv[1] = r
        return r in blank and not qenv[2]

    def end_word(w: str) -> None:
        nonlocal cmdw
        if cmdw is None and not _ENV_S_ASSIGN.match(w) and w not in _ENV_S_LEAD_WORDS:
            cmdw = w

    def new_command() -> None:
        nonlocal dirty, cmdw
        dirty, cmdw = False, None
    # (an unquoted `(`, `$(` or backtick opens a command of its own, `x=$(git log -Sgh)`; its
    # close returns to the enclosing one, which then holds an expansion: dirty)
    nest: list = []
    while i < n:
        if nb < len(bodies) and i >= bodies[nb][0] and not (sq or dq):
            if w0 >= 0:
                end_word(text[w0:i])
                w0 = -1
            new_command()
            i = max(i, bodies[nb][1])
            nb += 1
            continue
        c = text[i]
        if i in starts and ok_here(i, sq or dq):
            out.add(i)
        if sq and ansi and c == "\\":
            i += 1
        elif sq:
            sq = c != "'"
        elif dq and c == "\\":
            i += 1
        elif dq:
            if c in "$`":
                dirty = True
            dq = c != '"'
        elif (c in " \t\n;&|" or text.startswith("\\\n", i)) and not (
                c in "&|" and (text[i - 1:i] in ("<", ">") or text[i:i + 2] == "&>")):
            # (the `&`/`|` of a redirection, `2>&1`, `<&-`, `>|f`, `&>f`, is word text)
            if w0 >= 0:
                end_word(text[w0:i])
                w0 = -1
            if c == "\\":
                i += 1  # (a line continuation: a blank)
            elif c not in " \t":
                new_command()
        elif w0 < 0 and c == "#":
            e = text.find("\n", i)  # a comment, to its line's end
            i = n if e < 0 else e
            continue
        elif c == "(" or (c == "`" and not (nest and nest[-1][2] == "`")):
            nest.append((cmdw, dirty, c))
            new_command()
            w0 = -1
        elif (c == ")" and nest and nest[-1][2] == "(") or c == "`":
            if w0 >= 0:
                end_word(text[w0:i])
            cmdw, _d, _c = nest.pop()
            dirty, w0 = True, -1
        elif (c in "{}" and w0 < 0 and cmdw is None
              and text[i + 1:i + 2] in ("", " ", "\t", "\n", ";")):
            pass  # a group's `{` (or `}`) before the command word: a keyword, not a brace
        else:
            if w0 < 0:
                w0 = i
            if c in _ENV_S_DIRTY:
                dirty = True
            if c == "\\":
                i += 1
            elif c == "'":
                sq, ansi = True, text[i - 1:i] == "$"
                qenv[:] = [i, i, False]
            elif c == '"':
                dq = True
                qenv[:] = [i, i, False]
        i += 1
    return out


def _env_s_cluster_split(text: str) -> str | object:
    """T-5379: `text` with every glued -S cluster split off its value (`_ENV_S_CLUSTER`) if
    the text names env (`_env_s_names_env`), else `text` as it is; a cluster of a tool whose
    -S is its own option (`_env_s_pickaxe_starts`) is left as it is. A reading only.
    r4/r5: the value of each split cluster, quoted or not, to the end of its command or the
    next cluster, is also appended as a line of its own when it runs an escalator, gh or git
    (`_ENV_S_VALUE_RUNS`): the command env -S runs, at a command position (`/usr/bin/e?v
    -Ssudo true`, `xargs env <<< '-Ssudo true'`: main's rules see `sudo true` as the command
    it is). The parts appended are disjoint, so the text at most doubles (linear).
    T-5382: `_ENV_S_UNREADABLE` when a split value the shell expands in an unquoted heredoc
    body holds an expansion (`_env_s_expanding_body_at`). r2: an -S or --split-string whose
    value is a word of its own (`-S sudo true`, `--split-string=sudo true`) gives the same
    value line, and is not split (nothing is glued)."""
    if "S" not in text and "--s" not in text or not _env_s_names_env(text):
        return text
    split = _env_s_option_values(text)
    # r3: a value a line continuation carries (`-S \` / `$G pr`: bash joins `\`+newline
    # outside single quotes and in an unquoted body) is read again from the joined text
    joined = []
    if "\\\n" in text:
        jt = text.replace("\\\n", "")
        jsplit = _env_s_option_values(jt)
        for k, (_a, v, _m) in enumerate(jsplit):
            stop = jsplit[k + 1][0] if k + 1 < len(jsplit) else len(jt)
            end = _ENV_S_VALUE_END.search(jt, v, max(stop, v))
            value = _env_s_value_line(jt[v:end.start() if end else max(stop, v)])
            if value:
                joined.append(value)
    if not split:
        return text + "".join("\n" + x for x in dict.fromkeys(joined)) if joined else text
    parts, pos, lines, bodies = [], 0, [], []
    for k, (a, v, m) in enumerate(split):
        if m is not None:
            parts.append(text[pos:v])
            if text[v] not in "'\"":
                # (r6: a value glued on in quotes, `-S"sudo" true`, is not split in place: a
                # blank before its quote could unbalance a code string, `at now <<< "env
                # -S'rm ..."`; its value line below is what reads it)
                parts.append(" ")
            pos = v
        stop = split[k + 1][0] if k + 1 < len(split) else len(text)
        if stop < v:
            continue  # (an -S word another option's value holds: read with it)
        end = _ENV_S_VALUE_END.search(text, v, stop)
        raw = text[v:end.start() if end else stop]
        if _ENV_S_SHELL_EXPANSION.search(raw) and _env_s_expanding_body_at(text, a, bodies):
            # T-5382: a value the shell builds in an unquoted heredoc body (`xargs env <<E` /
            # `-Sg$(true)h pr merge 1`): unreadable, refused (a value line of the unexpanded
            # text, `g$(true)h pr merge 1`, is not what runs)
            return _ENV_S_UNREADABLE
        value = _env_s_value_line(raw)
        if value:
            lines.append(value)
    parts.append(text[pos:])
    return "".join(parts) + "".join("\n" + x for x in dict.fromkeys(lines + joined))


def _env_s_option_values(text: str) -> list:
    """r2/r3: (option start, value start, cluster match or None) for each -S cluster and
    each separate-word -S / --split-string of `text`, in order, less those a tool whose -S
    is its own option holds (`_env_s_pickaxe_starts`)."""
    found = [(m.start(2), m.end(), m) for m in _ENV_S_CLUSTER.finditer(text)]
    found += [(m.start(1), m.end(), None) for m in _ENV_S_SEPARATE.finditer(text)]
    if not found:
        return []
    found.sort(key=lambda f: f[0])
    keep = _env_s_pickaxe_starts(text, {a for a, _v, m in found if m is None or not m.group(1)})
    return [f for f in found if f[0] not in keep]


_ENV_S_VALUE_ESC = re.compile(r"\\+[_nrtvf]")  # r7: env -S's blank escapes (`\_`, `\n`, ...)


def _env_s_value_line(raw: str) -> str | None:
    """r6/r7: the value line for a split value `raw`, or None. env -S's own blank escapes
    (`\\_`, `\\n`, `\\r`, `\\t`, `\\v`, `\\f`, however many backslashes a quoting level added)
    read as blanks (`nice\\_sudo\\_true` is `nice sudo true`), then quotes and backslashes are
    dropped (a value cut from a quoted word opens no quote; `su"do"` is `sudo`). A line only
    when an escalator, gh or git is among its words (`nice sudo true`, `timeout 9 sudo true`,
    `bash -c 'sudo true'`: a wrapper runs it)."""
    value = _ENV_S_VALUE_ESC.sub(" ", raw).translate(_QUOTE_STRIP).strip()
    if "${" in value and _ENV_S_VALUE_VAR.search(value):
        # T-5382: a `${NAME}` env -S expands itself (`xargs env <<< "-S'\\${P} pr merge 1'"`):
        # the value is a line as it stands, whatever its words (judged with the text around
        # it, main's rules resolve `export P=gh` there as they do for `${P} pr merge 1`),
        # and again with each such expansion empty when that runs one (`g${Z}h pr` is `gh
        # pr` with Z unset)
        bare = _ENV_S_VALUE_VAR.sub("", value).strip()
        return value + "\n" + bare if bare and _env_s_value_runs(bare) else value
    return value if _env_s_value_runs(value) else None


def _env_s_value_runs(value: str) -> bool:
    """r5/r6: an escalator, gh or git (a path's last part counts) is among `value`'s words."""
    return any(w.rsplit("/", 1)[-1] in _ENV_S_VALUE_RUNS
               for w in _ENV_S_VALUE_WORD.findall(value))


def _env_s_expanding_body_at(text: str, at: int, cache: list) -> bool:
    """T-5382: True if `at` is inside a heredoc body of `text` the shell expands (its
    delimiter unquoted: `<<E`), where `$G`, `${Z}`, `$(...)` or a backtick builds a glued
    -S value the guard reads only unexpanded. `cache` holds the bodies' spans once found
    (a failed scan: every position counts, fail closed)."""
    if not cache:
        scanned = _scan_cached(text, True) if "<<" in text else None
        if isinstance(scanned, _Scanned):
            cache.append(sorted((a, e) for a, e, _r, x in scanned.bodies if x))
        else:
            cache.append(None if "<<" in text else [])
    spans = cache[0]
    if spans is None:
        return True
    k = bisect.bisect_right(spans, (at, len(text) + 1)) - 1
    return k >= 0 and spans[k][0] <= at < spans[k][1]


def _env_s_body_value_lines(command: str, bodies: list) -> str:
    """r7: the value lines of the glued -S clusters (value glued on unquoted or in quotes)
    in the heredoc bodies `_env_s_sparse` emptied (`xargs env <<'E'` / `-S'sudo true'`), when
    `command` names env; each body is searched once and not kept (linear). T-5382 r2: an -S
    or --split-string whose value is the next word (`-S sudo true`) gives the same line."""
    lines: list = []
    named = None
    for body in bodies:
        if not body or "S" not in body and "--s" not in body:
            continue
        if "\\\n" in body:
            body = body.replace("\\\n", "")  # r3: a continuation (xargs reads `\`+newline too)
        found = [(m.start(2), m.end()) for m in _ENV_S_CLUSTER.finditer(body) if not m.group(1)]
        found += [(m.start(1), m.end()) for m in _ENV_S_SEPARATE.finditer(body)]
        if not found:
            continue
        if named is None:
            named = _env_s_names_env(command)
        if not named:
            return ""
        found.sort()
        for k, (_a, v) in enumerate(found):
            stop = found[k + 1][0] if k + 1 < len(found) else len(body)
            if stop < v:
                continue
            end = _ENV_S_VALUE_END.search(body, v, stop)
            value = _env_s_value_line(body[v:end.start() if end else stop])
            if value:
                lines.append(value)
    return "\n".join(dict.fromkeys(lines))


def _env_s_body_unreadable(text: str) -> bool:
    """T-5382 r2: True if, in a text that names env, a heredoc body the shell expands (an
    unquoted delimiter, `xargs env <<E`) holds an env option word the guard can read only
    unexpanded (each fails closed: the expanded word is what xargs hands env):
      (1) an -S cluster or --split-string (glued, `=`, or the value as the next word) whose
          value, to the end of its command, holds an expansion (`-Sg$(true)h pr`, `-S $G pr`,
          `--split-string=g${Z}h pr`);
      (2) an option word built by an expansion (`-$S gh`, `-i${X}`: a `-` before it, no `=`);
      (3) a word an expansion starts (`$O pr merge 1`, `"${O}h"`), when that expansion is a
          command substitution, an arithmetic one, a `${...}` with an operator, a special or
          positional parameter, a name bash sets itself, or a name the text itself sets in a
          way that can hold an -S cluster (`_env_s_names_set`). A name the text never sets
          (inherited, `$HOME`) is read as main reads `$HOME pr merge 1`: not refused here.
    Linear: each expanding body is read once, the text's names once."""
    if "<<" not in text or ("$" not in text and "`" not in text):
        return False
    scanned = _scan_cached(text, True)
    if not isinstance(scanned, _Scanned):
        return _ENV_S_HEREDOC_OP.search(text) is not None and _env_s_names_env(text)
    spans = [(a, e) for a, e, _r, x in scanned.bodies if x]
    if not spans or not _env_s_names_env(text):
        return False
    unsafe = None
    for a, e in spans:
        body = text[a:e]
        if "$" not in body and "`" not in body:
            continue
        if "\\\n" in body:
            body = body.replace("\\\n", "")  # r3: bash joins a continuation in the body
        found = [(m.start(2), m.end()) for m in _ENV_S_CLUSTER.finditer(body) if not m.group(1)]
        found += [(m.start(1), m.end()) for m in _ENV_S_SEPARATE.finditer(body)]
        found.sort()
        for k, (_o, v) in enumerate(found):
            stop = found[k + 1][0] if k + 1 < len(found) else len(body)
            end = _ENV_S_VALUE_END.search(body, v, max(stop, v))
            if _ENV_S_SHELL_EXPANSION.search(body, v, end.start() if end else max(stop, v)):
                return True
        for w in _ENV_S_BODY_WORD.finditer(body):
            x = _ENV_S_SHELL_EXPANSION.search(w.group())
            if x is None:
                continue
            prefix = w.group()[:x.start()].translate(_QUOTE_STRIP)
            if prefix.startswith("-") and "=" not in prefix:
                return True  # (2)
            if prefix:
                continue
            name = _env_s_word_start_name(w.group()[x.start():])
            if name is None:
                return True  # (3): not a plain name
            if unsafe is None:
                unsafe = _env_s_names_set(text)
            if unsafe is True or name in unsafe or name in _ENV_S_SHELL_SET or name.startswith("BASH"):
                return True
    return False


def _env_s_word_start_name(exp: str) -> str | None:
    """r2: the plain name of the expansion `exp` starts with (`$O`, `${O}`), else None (a
    command or arithmetic substitution, a backtick, `${...}` with an operator, a special or
    positional parameter)."""
    m = _ENV_S_PLAIN_NAME.match(exp)
    return m.group(1) or m.group(2) if m else None


def _env_s_names_set(text: str) -> set | bool:
    """r2: the names `text` may set to a value holding an -S cluster: each assigned a value
    with an `S`, an expansion, a backtick, a backslash or a quote in it (`O=-Sgh`, `O+=S`,
    `O=$P`, `O=(-Sgh)`), and each that appears as a plain word (a `read O`, `printf -v O`,
    `for O in`, `export O` target). True (every name) when the text has a nameref, an `eval`
    or an indirect `${!...}`. One pass (linear).
    r3: also each a default assignment sets (`${O:=-Sgh}`, `${O=-Sgh}`) and each glued to a
    `-v`/`-a` option (`printf -vO`, `read -aO`); True also for a case attribute (`declare -u
    O=-sgh`) and for a target built at run time (`declare "$(...)=..."`, `printf -v "$x"`,
    `read "$x"`)."""
    if _ENV_S_NAMEREF.search(text) or _ENV_S_DYN_TARGET.search(text):
        return True
    out = set()
    for m in _ENV_S_NAME_USE.finditer(text):
        if m.group(3) is None or _ENV_S_SET_RISKY.search(m.group(4)):
            out.add(m.group(1))
    if "${" in text:
        out.update(m.group(1) for m in _ENV_S_DEFAULT_SET.finditer(text))
    out.update(m.group(1) for m in _ENV_S_OPT_TARGET.finditer(text))
    return out


_ENV_S_BODY_WORD = re.compile(r"\S+")
_ENV_S_PLAIN_NAME = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
# (a name bash sets itself: `read` with no name, getopts, mapfile with none, `$_`)
_ENV_S_SHELL_SET = frozenset({"_", "REPLY", "OPTARG", "MAPFILE", "COPROC", "FUNCNAME"})
_ENV_S_NAMEREF = re.compile(
    r"(?<![\w-])(?:declare|typeset|local|readonly|export)(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*[nuc]"
    r"|(?<![\w-])eval(?![\w-])|\$\{!")
# r3: a target built at run time: an argument of a declaring command, or a `printf -v` /
# read / mapfile / getopts argument, that starts with a quote, an expansion or a backslash
_ENV_S_DYN_TARGET = re.compile(
    r"(?<![\w./-])(?:(?:declare|typeset|local|readonly|export)(?=\s)[^;&|\n]*?"
    r"|(?:read|mapfile|readarray|getopts)(?=\s)[^;&|\n<>]*?)\s[\"'$`\\]"
    r"|(?<![\w./-])printf\s+-v\s*[\"'$`\\]")
# r3: a default assignment's name; a name glued to a `-v`/`-a` option
_ENV_S_DEFAULT_SET = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
_ENV_S_OPT_TARGET = re.compile(r"(?<![\w-])-[A-Za-z]*?[va]([A-Za-z_][A-Za-z0-9_]*)")
# a name as a word or an assignment's head (not after `$` or `${`), its `=`, and its value
_ENV_S_NAME_USE = re.compile(
    r"(?<![\w$])(?<!\$\{)([A-Za-z_][A-Za-z0-9_]*)(?:(\[[^\]\n]*\])?(\+?=)([^\s;&|<>]*))?(?![\w])")
_ENV_S_SET_RISKY = re.compile(r"[S$`\\'\"(]")


_ENV_S_VALUE_END = re.compile(r"[;&|\n]")  # r4: the end of a split value's command
# r5/r6: a split value's words (a path's last part counts), and the commands a value line is
# appended for when one of them is among them (a value line for any other, `$GIT log -Srm --
# ledger`, would read a pickaxe term as a command)
_ENV_S_VALUE_WORD = re.compile(r"[^\s,:;|&(){}<>=`$]+")
_ENV_S_VALUE_RUNS = _ESCALATOR_WORDS | frozenset({"gh", "git"})
# T-5382: a `${NAME}` env -S expands itself; a shell expansion in a heredoc body (`$NAME`,
# `$1`, `$@`, `${...}`, `$(...)`, `$((...))`, a backtick)
_ENV_S_VALUE_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_ENV_S_SHELL_EXPANSION = re.compile(r"\$[\w{(\[@*#?!$-]|`")
# r2: an -S cluster or a --split-string (any prefix GNU env takes: `--s`, `--split-str`)
# whose value is the next word (after blanks, a newline in a body xargs reads, or `=`)
_ENV_S_LONG = r"--s(?:p(?:l(?:i(?:t(?:-(?:s(?:t(?:r(?:i(?:n(?:g)?)?)?)?)?)?)?)?)?)?)?"
_ENV_S_SEPARATE = re.compile(
    r"(?<![\w-])(-[A-Za-z0-9]{0,8}S(?=\s)|" + _ENV_S_LONG + r"(?=[=\s]))(?:=|\s+)(?=[^\s;&|])")


def _env_s_glued_split(text: str, alone: bool = False) -> str | tuple | object | None:
    """r6: the text reading of env -S commands the word reading cannot reach (one that does
    not start a word: `${x:-env -S'gi"t" push'}`, `printf -v x %s'env -Sgh pr'`, `IFS=:;
    x='env:-Sgh:pr'`): from each `env` word heading an -S option to the end of its line, the
    quotes and backslashes are dropped and a glued value is split off its option (`${x:-env
    -S git push}`), for main's raw-text rules, which read the same text without env (`${x:-git
    push}`), to judge. None when nothing changed. A reading only: it only adds denials; each
    line is read once (linear). T-5379: first, every glued -S cluster of a text that names
    env is split (`_env_s_cluster_split`)."""
    if "S" not in text and "--s" not in text:
        return None
    split = _env_s_cluster_split(text)
    if split is _ENV_S_UNREADABLE:
        return split  # T-5382: refused (`_decide_env_s`)
    changed = split != text
    text = split
    if "env" not in text:
        return text if changed else None
    out, pos, extra = [], 0, []
    starts = [m.start() for m in _ENV_S_ENV_AT.finditer(text)]
    for k, a in enumerate(starts):
        if a < pos:
            continue
        e = text.find("\n", a)
        e = len(text) if e < 0 else e
        if k + 1 < len(starts) and starts[k + 1] < e:
            e = starts[k + 1]  # (to the next env: each part of a line is read once)
        if _ENV_S_GLUED.match(text, a, e):  # (within its own segment: linear)
            # an unquoted value glued on: split off in place, the text around it (an
            # assignment's quotes: `IFS=:; x='env:-Sgh:pr'`) kept for the rules that read it
            seg = _ENV_S_GLUED.sub(r"\1 ", text[a:e])
            if seg != text[a:e]:
                out.append(text[pos:a])
                out.append(seg)
                pos = e
            continue
        # a quoted value: its words with the quotes dropped, as a line of their own appended
        # (dropping quotes in place would change the command around it: `git commit -m
        # "... env -S's ..."`)
        # (`$` and backticks blanked: expansions are not what this reading is for, and an
        # -S value holding one is refused by main's own rule on the original)
        seg = text[a:e].translate(_QUOTE_STRIP).translate(_ENV_S_EXPANSION_BLANK)
        if seg != text[a:e] and _ENV_S_ANY_S.match(seg):
            extra.append(_ENV_S_GLUED.sub(r"\1 ", seg).strip())
    if not out and not extra:
        return text if changed else None
    lines = "".join("\n" + x for x in dict.fromkeys(extra))
    if not out and alone and not changed:
        # (only appended lines: they are all this reading adds, judged on their own; a
        # 1-tuple says so)
        return (lines[1:],)
    out.append(text[pos:])
    # (each distinct appended line once: they are text for the raw-text rules, so a repeat
    # adds nothing, and `echo "env -S'..'" "env -S'..'" ...` stays near main's cost)
    return "".join(out) + lines


def _env_s_positions(view: str, anchors: re.Pattern) -> list[int]:
    """r5: `_cmd_positions`, plus each command position before its assignment words (`x='env
    -S...'; $x`): a reading from there reads the assignments with their command."""
    # (`_cmd_positions`' own computation, its keyword runs shared)
    runs = _AssignRuns(view)
    keywords = _KeywordRuns(view)
    starts = [0] + [m.end() for m in anchors.finditer(view)]
    pre = {keywords.end(a) for a in starts}
    out = pre | {runs.run_end(p) for p in pre}
    out.update(runs.run_end(m.end()) for m in _FIND_EXEC.finditer(view))
    if runs.arrays:
        out.update(q for m in _GLUED_CLOSE.finditer(view)
                   if (q := runs.after_glued_close(m.end())) is not None)
    return sorted(out)


# r5: a word that assigns (`NAME=VALUE`, `NAME[k]+=VALUE`), alone or as a declare/export/
# local/readonly/typeset argument: its value is read as a code string (it may be run as `$x`).
_ENV_S_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")


def _env_s_quiet(text: str) -> bool:
    """r4: True if no output in `text` can reach anything but the terminal: no pipe, no `>`
    redirection, no command or process substitution (`|`, `>`, a backtick, `$(`, `<(`). Only
    then are a text emitter's words (`_ENV_S_TEXT_EMITTERS`) left undecoded: otherwise a shell
    may run what it prints (`echo ... > f; bash f`, `| parallel`, `| at now`, `$(echo ..)`)."""
    return not any(t in text for t in ("|", ">", "`", "$(", "<("))


def _env_s_decode_level(text: str, depth: int, budget: list, quiet: bool = False):
    """One pass of `_env_s_decoded_text` over `text`: the text with each env command's
    first -S value decoded, None when nothing changed, or `_ENV_S_UNREADABLE`."""
    budget[0] -= len(text)
    if budget[0] < 0:
        return _ENV_S_UNREADABLE
    scanned = _scan(text)
    if scanned is None:
        view, rawmap = text, None
        positions = _env_s_positions(text, _CMD_ANCHOR_CHARS_RAW)
        limit_at = lambda q: len(text)  # noqa: E731
    else:
        view, rawmap = scanned[1], scanned[2]
        positions = _env_s_positions(view, _CMD_ANCHOR_CHARS)
        limit_at = _eval_limits(view, rawmap, scanned[4], len(text), text)
    memo: dict[int, int] = {}
    seen: set[int] = set()
    taken_a: list[int] = []  # the accepted edits' spans, sorted and disjoint
    taken_b: list[int] = []
    edits: list = []
    skip = 0  # raw index before which an unclosed construct was already met
    word_edits: list = []  # r5: the code-string words' decodes, applied after the env edits

    def free(a: int, b1: int) -> bool:
        k = bisect.bisect_left(taken_a, b1)
        return k == 0 or taken_b[k - 1] <= a

    def take(a: int, b: int, rep: str) -> None:
        b1 = max(b, a + 1)
        k = bisect.bisect_left(taken_a, b1)
        taken_a.insert(k, a)
        taken_b.insert(k, b1)
        edits.append((a, b, rep))
    read_heap: list = []  # r5: (start, end) of each word read, by start
    read_end = -1  # the furthest end of the words read that start at or before the position
    for q in positions:
        if q >= len(view):
            continue
        r = q if rawmap is None else rawmap[q]
        limit = limit_at(q)
        if limit is None or r >= limit or r < skip:
            # In a double-quoted interpreter string (`bash -c "env -S'...'"`): its quoting is
            # the enclosing word's, decoded with that word below (as eval's join leaves it).
            continue
        herestrings: list = []
        ws = _env_s_command_words(text, r, limit, memo, seen, herestrings)
        while read_heap and read_heap[0][0] <= r:
            read_end = max(read_end, heapq.heappop(read_heap)[1])
        if ws == -1:
            if read_end > r:
                # r5: a position inside a word an earlier reading read whole (after the `)` of
                # a `$(...)` in a quoted code string: `watch "env -S'ls' $(date)"; ...`): no
                # command starts here; that word was read (and decoded) with its command.
                continue
            # A construct that does not close: fail closed if the rest of that region can
            # hold an env -S command (each region is read once, as eval's join reads it).
            if _env_s_maybe(text[r:limit]):
                return _ENV_S_UNREADABLE
            skip = limit
            continue
        for w in ws:
            heapq.heappush(read_heap, (w[0], w[1]))
        got: list = []
        i = 0
        while i < len(ws):
            if ws[i][2].rsplit("/", 1)[-1] == "env":
                # r5: in a code string (depth > 0) an -S value's `$`/backtick is read as text:
                # the enclosing shell may already have expanded it (`git rebase --exec "env
                # -S'sudo true $W'"`); at the top the refusal stands (main's, kept)
                found = _env_s_env_edits(text, ws, i, budget, depth > 0)
                if found is _ENV_S_UNREADABLE or found is _ENV_S_DYN:
                    return found
                if found:
                    # the words after its -S value are the split command's own (`env -S'nice'
                    # env -S'ls'`): read on from there
                    got.extend(found)
                    end = found[-1][1]
                    while i < len(ws) and ws[i][0] < end:
                        i += 1
                    continue
            i += 1
        head = next((w[2] for w in ws if not _ENV_S_ASSIGN.match(w[2])), "")
        emitter = (quiet and head.rsplit("/", 1)[-1] in _ENV_S_TEXT_EMITTERS
                   and not any(w[2][:2] == "-v" for w in ws))  # r5: `printf -v x` assigns
        if not got and (ws or herestrings) and not emitter:
            # A quoted word holding an env -S command of its own (`bash -c "env -S'...'"`).
            # r2: one the guard cannot read fails closed only in a command that runs code
            # from a word (`_ENV_S_CODE_RUNNERS`); elsewhere it is data (`grep "env -S'" f`,
            # `git commit -m "... env -S's ..."`) and is skipped. (A readable one is decoded in
            # any command: perl/expect/tmux/make/git run code from words this list leaves out.)
            # r4: a word with a `$`, a backtick or an unquoted process substitution in it is
            # decoded too when a code runner reads it (`watch "env -S'..' $HOME"`, `.. #"<(:)`):
            # its expansions are kept as text and the decoded code string is put back quoted,
            # so they read as literals (a superset: only adds denials). Elsewhere it is data;
            # and a word that is only a process substitution is read at its own positions.
            # r5 (review rr15d): in any command, not only a code runner's (tmux, screen, xterm
            # -e, perl -e, git rebase -x, entr, `at`, run what a word holds as well), and with
            # ANSI-C `$'...'` read (`_env_s_ansi_c`). An assignment's value (`x='env -S...';
            # $x`, `declare`/`export` arguments, `printf -v`, a `read <<<` here-string) is
            # read the same way: the variable may be run.
            runner = None
            for s, e, dq in ws + herestrings:
                raw = text[s:e]
                if not any(ch in raw for ch in "'\"\\") or not _env_s_maybe(dq):
                    continue
                if raw.startswith(("<(", ">(")):
                    continue  # a process substitution: read at its own command positions
                code = _env_s_ansi_c(dq) if "$'" in raw else dq
                am = _ENV_S_ASSIGN.match(code)
                name, code = (code[:am.end()], code[am.end():]) if am else ("", code)
                sub = (_env_s_decoded_text(code, depth + 1, budget, quiet)
                       if depth < _ENV_S_DECODE_DEPTH else _ENV_S_UNREADABLE)
                if sub is _ENV_S_DYN:
                    return sub  # r6: env's own `${NAME}`, in any command
                if sub is _ENV_S_UNREADABLE:
                    if runner is None:
                        runner = any(w[2].rsplit("/", 1)[-1] in _ENV_S_CODE_RUNNERS for w in ws)
                    if runner:
                        return sub
                    continue
                if sub is not None:
                    word_edits.append((s, e, name + _env_s_code_quote(sub)))
        if got and all(free(a, max(b, a + 1)) for a, b, _rep in got):
            # (all of a command's edits or none; one another edit covers: the next level)
            for a, b, rep in got:
                take(a, b, rep)
    # r5: a code-string word's decode yields to an env command decoded in place inside it (a
    # `$(env -S..)` in it: quoting the word would make that substitution a literal); the next
    # level reads the word again, its own env -S commands decoded.
    for a, b, rep in word_edits:
        if free(a, max(b, a + 1)):
            take(a, b, rep)
    if not edits:
        return None
    return _env_s_apply(text, 0, len(text), edits)


def _env_s_decoded_text(command: str, depth: int = 0, budget: list | None = None,
                        quiet: bool = True):
    """T-5363: `command` with every env -S value decoded (see the comment above), None when
    it has none, or `_ENV_S_UNREADABLE` (fail closed). `quiet` (r4): the enclosing text's
    `_env_s_quiet` (a code string's output goes where its command's does)."""
    if not _env_s_maybe(command):
        return None
    if budget is None:
        budget = [16 * len(command) + 65_536]
    quiet = quiet and _env_s_quiet(command)
    text, changed = command, False
    for _ in range(_ENV_S_DECODE_LEVELS):
        got = _env_s_decode_level(text, depth, budget, quiet)
        if got is _ENV_S_UNREADABLE or got is _ENV_S_DYN:
            return got if depth else _ENV_S_UNREADABLE
        if got is None or got == text:
            return text if changed else None
        text, changed = got, True
        if not _env_s_maybe(text):
            return text
    last = _env_s_decode_level(text, depth, budget, quiet)
    if last is _ENV_S_DYN and depth:
        return last
    return _ENV_S_UNREADABLE if last is not None else text


# --- T-4506: command words on a level `_scan` cannot read ---
# `_scan` gives up on some valid bash (a `case` inside `$(...)`, a `\\` or `\$`
# in backticks, an apostrophe in a heredoc body inside `$(...)`, an
# unterminated quote on a later line), so `_NonLiteralLevel` cannot follow the
# walk there. `_RawLevel` reads such a level itself: a quote-aware lexer that
# knows heredoc bodies, comments, `$(...)`, backticks (de-escaped, as bash
# reads their bodies), arrays, `[[ ]]`, `for` heads and `case` patterns well
# enough to find each command position, and runs `_nl_lex` (the same
# predicate) on the word there. A wrapper's words are resolved with the walk's
# own `_skip_wrapper_options`, and the code strings the walk re-reads (`bash
# -c`, `eval`, `ssh`/`watch`/`parallel`, `-c` options) are read the same way,
# a level deeper. Only where the lexer cannot follow bash itself (a `)` with
# nothing open, a `case` with no `in`, a substitution left open at the end of
# a heredoc body) does the level fail closed. T-5115: so does a level with
# more than `_RL_NEST_MAX` constructs open at once. A word's text spans the
# constructs nested in it, so without a bound the words ending around deep
# nesting re-read O(n * depth) characters (O(n^2)); with it, the word text
# read per level is at most (`_RL_NEST_MAX` + 1) * n.
_UNREADABLE_COMMAND_WORD = _deny(
    "BLOCKED (worker-guard, T-4426/T-4506): this command has a part the guard cannot parse, "
    "together with quoting or a backslash that may spell a command word; refused "
    "(fail-closed). Simplify the command, or write the command word plainly."
)
_RL_BLANK = frozenset(" \t\r\f\v")
_RL_PLAIN = re.compile(r"[^ \t\r\f\v\n;&|()<>'\"\\`$]+")
_RL_DQ_STOP = re.compile(r"[\"\\`$]")
_RL_BODY_STOP = re.compile(r"[\\`$]")
_RL_BACKTICK = re.compile(r"(?:[^\\`]|\\.)*", re.S)
_RL_UNESCAPE = re.compile(r"\\([\\$`])")
_RL_UNESCAPE_DQ = re.compile(r"\\([\\$`\"])")
_RL_FD = re.compile(r"[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\}")
_RL_REDIR = re.compile(r"<<<|<<-|<<|<>|<&|>>|>&|>\||<|>")
# Reserved words after which bash reads a command word.
_RL_OPENS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "{", "!", "coproc"})
_RL_CMD_KINDS = frozenset({"top", "$(", "(", "proc", "arr"})
# T-5115: open frames per level past which `_RawLevel` stops (undecided). The
# deepest stack across ~20k test and transcript commands was 12.
_RL_NEST_MAX = _SCAN_NEST_MAX


class _RLResult:
    """What `_RawLevel` found, over all levels it read: `hit` (a non-literal
    command word), `qhit` (one spelt with `$'...'` / `$"..."`, reported with
    r4's reason), `undecided` (the lexer could not follow bash somewhere)."""

    def __init__(self):
        self.hit = self.qhit = self.undecided = False


class _RLFrame:
    """A frame of `_RawLevel`. Command frames (`_RL_CMD_KINDS`) read words:
    `mode` is normal, for (a for/select head), case_word / case_in (after
    `case`), pat (a case pattern list), func (after `function`), dbr (inside
    `[[ ]]`) or arr (array elements); `ws` the start of the word being read,
    `wnl` `_nl_lex`'s result for it (None: pending), `coll` a wrapper's or
    shell's words, collected to the end of its command. The others (q1, ansi,
    dq, brace, arith, hbody) read quoted, expansion or heredoc body text."""

    __slots__ = ("kind", "mode", "cmdpos", "case", "pat_first", "ws", "wnl", "redir", "coll",
                 "time", "resume", "depth", "dq", "queue", "after")

    def __init__(self, kind: str, mode: str = "normal", cmdpos: bool = True, dq: bool = False):
        self.kind, self.mode, self.cmdpos, self.dq = kind, mode, cmdpos, dq
        self.case = self.depth = 0
        self.pat_first = self.time = False
        self.ws = -1
        self.wnl = self.redir = self.coll = self.resume = self.queue = None
        self.after = 0


class _RawLevel:
    """T-4506: the non-literal command-word rule over one level of shell text
    that `_scan` cannot read (see above). One forward pass with an explicit
    frame stack; a backtick body and a code string are read as levels of
    their own (bounded: backticks nest only by escaping, code strings by
    `_CODE_DEPTH_MAX` and a character budget). Each character is read a
    bounded number of times per level: linear. That includes the text of
    the words around it, because a level with more than `_RL_NEST_MAX`
    frames open stops there, undecided (T-5115)."""

    def __init__(self, s: str, depth: int, budget: list, res: _RLResult):
        self.s, self.depth, self.budget, self.res = s, depth, budget, res
        self.pend = None  # (p, dq, brace, owner): `_nl_lex` paused at s[p]
        self.heredocs: list = []  # (strip tabs, delimiter, expands) for the next newline
        self.st = [_RLFrame("top")]
        self.lims = [len(s)]
        self.eof = False

    # -- results --
    def _word_hit(self, text: str) -> None:
        if "$'" in text or '$"' in text:
            self.res.qhit = True
        else:
            self.res.hit = True

    def _stop(self) -> bool:
        return self.res.hit or self.res.qhit

    # -- `_nl_lex` across substitutions --
    def _chain(self, r, owner: _RLFrame) -> None:
        if r is True or r is False:
            owner.wnl = r
        else:
            owner.wnl = None
            self.pend = (r[0], r[1], r[2], owner)

    def _push(self, fr: _RLFrame, at: int) -> None:
        if self.pend is not None and self.pend[0] == at:
            fr.resume, self.pend = self.pend[1:], None
        self.st.append(fr)

    def _pop(self, after: int) -> None:
        fr = self.st.pop()
        if fr.resume is not None:
            dq, brace, owner = fr.resume
            self._chain(_nl_lex(self.s, after, dq, brace), owner)

    def _wnl(self, f: _RLFrame) -> bool:
        if f.wnl is None:
            if not self.eof:
                self.res.undecided = True  # `_nl_lex` and this lexer fell out of step
            return False
        return f.wnl

    # -- words and commands --
    def _start_word(self, f: _RLFrame, i: int) -> None:
        f.ws = i
        f.wnl = False
        if f.mode == "normal" and f.redir is None and (f.cmdpos or f.coll is not None):
            self._chain(_nl_lex(self.s, i), f)

    def _end_word(self, f: _RLFrame, i: int) -> None:
        ws = f.ws
        if ws < 0:
            return
        f.ws = -1
        s = self.s
        text = s[ws:i]
        if f.redir is not None:  # a redirection's target: no command word
            op, f.redir = f.redir, None
            if op in ("<<", "<<-"):
                self.heredocs.append((op == "<<-", _shell_word(text, 0),
                                      not any(ch in text for ch in "'\"\\")))
            return
        if s[i:i + 1] in ("<", ">") and _RL_FD.fullmatch(text):
            return  # `2>x`, `{fd}>x`: the redirection's descriptor
        mode = f.mode
        if mode == "normal":
            if f.coll is not None:
                f.coll.append(("word", text, "$" in text or "`" in text, ws, False, self._wnl(f)))
            elif f.cmdpos:
                self._command_word(f, text)
            elif "-" in text and _dequote(text) in _FIND_EXEC_WORDS:
                f.cmdpos = True  # `find ... -exec CMD`
            return
        plain = text.replace("\\\n", "")
        if mode == "case_word":
            f.mode = "case_in"
        elif mode == "case_in":
            if plain == "in":
                f.mode, f.pat_first = "pat", True
            else:
                self.res.undecided = True
                f.mode, f.cmdpos = "normal", False
        elif mode == "pat":
            if f.pat_first and plain == "esac":
                f.case -= 1
                f.mode, f.cmdpos = "normal", False
            f.pat_first = False
        elif mode == "for":
            if plain == "do":
                f.mode, f.cmdpos = "normal", True
        elif mode == "func":
            f.mode, f.cmdpos = "normal", True
        elif mode == "dbr" and plain == "]]":
            f.mode, f.cmdpos = "normal", False

    def _command_word(self, f: _RLFrame, text: str) -> None:
        nonlit = self._wnl(f)
        if _NL_ASSIGN.match(text):
            return  # an assignment: the command word is still to come
        plain = text.replace("\\\n", "")
        if f.time:
            f.time = False
            if plain in ("-p", "--"):
                f.time = plain == "-p"
                return
        if not nonlit:
            if plain in _RL_OPENS:
                return
            if plain == "time":
                f.time = True
                return
            if plain in ("fi", "done", "}") or (plain == "esac" and f.case):
                if plain == "esac":
                    f.case -= 1
                f.cmdpos = False
                return
            if plain in ("for", "select"):
                f.mode = "for"
                return
            if plain == "case":
                f.mode = "case_word"
                f.case += 1
                return
            if plain == "function":
                f.mode = "func"
                return
            if plain == "[[":
                f.mode = "dbr"
                return
        f.cmdpos = False
        if nonlit:
            self._word_hit(text)
            return
        if "$" in text or "`" in text:
            return  # a dynamic command word: T-4135's rule
        base = plain.rsplit("/", 1)[-1]
        if base in _WRAPPER_GRAMMAR or base in _SHELLS:
            f.coll = [("word", text, False, f.ws if f.ws >= 0 else 0, False, False)]

    def _end_cmd(self, f: _RLFrame) -> None:
        toks, f.coll = f.coll, None
        f.time = False
        if toks:
            self._resolve(toks)

    def _resolve(self, toks: list) -> None:
        """A wrapper's or shell's command, as `_escalation_walk` reads it:
        the wrapped command word, the code strings, `-exec`."""
        pending: list = []
        it = iter(toks)

        def nxt():
            return pending.pop() if pending else next(it, None)

        def after_args(tok):
            while tok is not None and _dequote(tok[1]) not in _FIND_EXEC_WORDS:
                tok = nxt()
            return None if tok is None else nxt()

        tok = nxt()
        while tok is not None and not self._stop():
            text = tok[1]
            if tok[5] if len(tok) > 5 else _nl_lex(text, 0) is True:
                self._word_hit(text)
                return
            v = _dequote(text)
            if v in _FIND_EXEC_WORDS or _ASSIGN_WORD.match(v):
                tok = nxt()
                continue
            if tok[2]:
                tok = after_args(nxt())
                continue
            base = v.rsplit("/", 1)[-1]
            if base in _SHELLS:
                cmode, tok = _skip_shell_options(nxt)
                if cmode and tok is not None:
                    self._code(tok)
                    tok = nxt()
                tok = after_args(tok)
                continue
            if base not in _WRAPPER_GRAMMAR:
                tok = after_args(nxt())
                continue
            codes: list = []
            seen: list = []
            tok = _skip_wrapper_options(base, nxt, pending, codes, seen)
            for c in codes:
                self._code(c)
            if tok is None:
                return
            inner = tok[1]
            if base in ("machinectl", "script") or (
                    base == "command" and not tok[2]
                    and _dequote(inner).rsplit("/", 1)[-1] in _T4351_LOOKUP_EXEMPT
                    and any("v" in x or "V" in x for x in seen if x[:2] != "--")):
                tok = after_args(tok)
                continue
            if base == "parallel" and _dequote(inner) == ":::":
                tok = nxt()
                while tok is not None and _dequote(tok[1]) != ":::":
                    self._code(tok)
                    tok = nxt()
                tok = after_args(tok)
                continue
            if base == "eval" and inner[:1] in "'\"":
                self._code(tok)  # `eval '...'`: a code string, not a command word
                tok = after_args(nxt())
                continue
            if base in _CODE_STRING_WRAPPERS and (
                    (len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0])
                    or (not tok[2] and ("'" in inner or '"' in inner))):
                self._code(tok, ssh=base == "ssh")
                tok = after_args(nxt())
                continue
            # `tok` is the wrapped command word

    def _code(self, c, ssh: bool = False) -> None:
        """A code string the walk re-reads, read here as a level of its own."""
        if c[0] == "str":
            value = c[1]
        elif len(c) > 3 and c[3] is not None:
            if self.s.startswith(("$'", '$"'), c[3]):
                self._word_hit(c[1])  # spelt `$'...'`: not the text bash runs
                return
            value = _shell_word(self.s, c[3])
        else:
            value = _shell_word(c[1], 0)
        if ssh:
            m = _SSH_CODE_OPTION.match(value)
            if m:
                value = value[m.end():]
        self.budget[0] -= len(value)
        if self.depth + 1 >= _CODE_DEPTH_MAX or self.budget[0] < 0:
            return  # past the walk's own cap, which reads no further either
        _RawLevel(value, self.depth + 1, self.budget, self.res).run()

    # -- bodies --
    def _backtick(self, i: int, dq: bool) -> int:
        """Read the backtick substitution at s[i] (to the next unescaped
        backtick, as bash finds it) as a level of its own, de-escaped as bash
        reads it; return the index after it."""
        s, lim = self.s, self.lims[-1]
        j = _RL_BACKTICK.match(s, i + 1, lim).end()
        body = s[i + 1:j]
        if "\\" in body:
            body = (_RL_UNESCAPE_DQ if dq else _RL_UNESCAPE).sub(r"\1", body)
        _RawLevel(body, self.depth, self.budget, self.res).run()
        end = min(j + 1, lim)
        if self.pend is not None and self.pend[0] == i:
            _p, pdq, brace, owner = self.pend
            self.pend = None
            self._chain(_nl_lex(s, end, pdq, brace), owner)
        return end

    def _bodies(self, i: int) -> int:
        """At s[i], right after a newline: skip the pending heredocs' bodies.
        A body bash expands is read for its substitutions (an `hbody` frame);
        return where reading goes on."""
        s, lim = self.s, self.lims[-1]
        docs, self.heredocs = self.heredocs, []
        queue = []
        b = i
        for strip, word, expands in docs:
            start = end = b
            while True:
                if b >= lim:
                    end = lim
                    break
                e = s.find("\n", b, lim)
                e = lim if e < 0 else e
                line = s[b:e]
                if (line.lstrip("\t") if strip else line) == word:
                    end, b = b, e + 1
                    break
                b = e + 1
            if expands and end > start:
                queue.append((start, end))
        after = min(b, lim)
        if not queue:
            return after
        fr = _RLFrame("hbody")
        fr.queue, fr.after = queue[1:], after
        self.st.append(fr)
        self.lims.append(queue[0][1])
        return queue[0][0]

    def _finish(self, f: _RLFrame, at: int) -> None:
        if f.kind in _RL_CMD_KINDS:
            self._end_word(f, at)
            self._end_cmd(f)

    # -- the pass --
    def run(self) -> None:
        s, st, lims, res = self.s, self.st, self.lims, self.res
        i = 0
        while not self._stop():
            if len(st) > _RL_NEST_MAX:  # T-5115: nested deeper than the lexer reads
                res.undecided = True
                return
            f = st[-1]
            lim = lims[-1]
            k = f.kind
            if i >= lim:
                if k == "hbody":
                    if f.queue:
                        (i, lims[-1]), f.queue = f.queue[0], f.queue[1:]
                    else:
                        st.pop()
                        lims.pop()
                        i = f.after
                    continue
                if len(lims) > 1:  # a frame left open at a heredoc body's end
                    res.undecided = True
                    while st[-1].kind != "hbody":
                        self._finish(st.pop(), lim)
                    continue
                self.eof = True
                while st and not self._stop():
                    self._finish(st.pop(), lim)
                return
            c = s[i]
            if k == "q1":
                j = s.find("'", i, lim)
                if j < 0:
                    i = lim
                else:
                    st.pop()
                    i = j + 1
                continue
            if k == "ansi":
                if c == "\\":
                    i += 2
                elif c == "'":
                    st.pop()
                    i += 1
                else:
                    i += 1
                continue
            if k in ("dq", "hbody"):
                m = (_RL_DQ_STOP if k == "dq" else _RL_BODY_STOP).search(s, i, lim)
                if m is None:
                    i = lim
                    continue
                i = m.start()
                c = s[i]
            if k in ("dq", "hbody", "brace", "arith"):
                if c == "\\":
                    i += 2
                elif c == "`":
                    i = self._backtick(i, k == "dq" or f.dq)
                elif c == "$" and s.startswith("$((", i):
                    self._push(_RLFrame("arith"), i)
                    i += 3
                elif c == "$" and s.startswith("$(", i):
                    self._push(_RLFrame("$("), i)
                    i += 2
                elif c == "$" and s.startswith("${", i):
                    self._push(_RLFrame("brace", dq=k == "dq" or f.dq), i)
                    i += 2
                elif k == "dq":
                    if c == '"':
                        self._pop(i + 1)
                    i += 1
                elif k == "hbody":
                    i += 1
                elif k == "brace":
                    if c == "}":
                        self._pop(i + 1)
                    elif c == '"':
                        self._push(_RLFrame("dq", dq=True), i)
                    elif c == "'" and not f.dq:
                        self._push(_RLFrame("q1"), i)
                    elif s.startswith("$'", i) and not f.dq:
                        self._push(_RLFrame("ansi"), i)
                        i += 1
                    i += 1
                else:  # arith
                    if c == "(":
                        f.depth += 1
                    elif c == ")":
                        if f.depth:
                            f.depth -= 1
                        else:
                            if not s.startswith("))", i):
                                res.undecided = True  # `$((...)` read as a subshell: not modelled
                            self._pop(i + 2)
                            i += 1
                    elif c == '"':
                        self._push(_RLFrame("dq", dq=True), i)
                    elif c == "'":
                        self._push(_RLFrame("q1"), i)
                    i += 1
                continue
            # a command frame
            if c in _RL_BLANK or c == "\n":
                # Ending a word can end a mode (`esac`, `]]`), so the mode is
                # read after it.
                self._end_word(f, i)
                if c != "\n" or f.mode not in ("normal", "for"):
                    i += 1
                    continue
            mode = f.mode
            if c == "\\":
                if not s.startswith("\\\n", i) and f.ws < 0:
                    self._start_word(f, i)
                i += 2
                continue
            if c == "\n":
                self._end_word(f, i)
                self._end_cmd(f)
                f.mode, f.cmdpos, f.redir = "normal", True, None
                i += 1
                if self.heredocs:
                    i = self._bodies(i)
                continue
            if c == "#" and f.ws < 0:  # a comment, to the end of its line
                j = s.find("\n", i, lim)
                i = lim if j < 0 else j
                continue
            if c in "<>" and s.startswith("(", i + 1):  # `<(...)`: part of a word
                if f.ws < 0:
                    self._start_word(f, i)
                self._push(_RLFrame("proc"), i)
                i += 2
                continue
            if mode in ("dbr", "arr") and (c in ";&|(<>" or (mode == "dbr" and c == ")")):
                self._end_word(f, i)
                if f.mode == mode:  # an operator inside `[[ ]]` or an array
                    i += 1
                    continue
                mode = f.mode  # `]]` just ended: `c` is a real operator
            if c in ";&|":
                self._end_word(f, i)
                mode = f.mode
                if mode == "pat" and c == "|":
                    i += 1
                    continue
                if c == "&" and s.startswith(">", i + 1):  # `&>`, `&>>`
                    f.redir = "&>"
                    i += 3 if s.startswith("&>>", i) else 2
                    continue
                self._end_cmd(f)
                if c == ";" and f.case and mode == "normal" and s[i + 1:i + 2] in (";", "&"):
                    f.mode, f.pat_first = "pat", True  # `;;`, `;&`, `;;&`
                    i += 3 if s.startswith(";;&", i) else 2
                    continue
                i += 2 if s.startswith(("&&", "||", "|&"), i) else 1
                if mode != "pat":
                    f.mode = "normal"
                f.cmdpos, f.time, f.redir = True, False, None
                continue
            if c in "<>":
                self._end_word(f, i)
                op = _RL_REDIR.match(s, i).group()
                f.redir = op
                i += len(op)
                continue
            if c == "(":
                if mode == "pat":
                    if f.ws < 0:
                        f.pat_first = False  # the optional `(` before a pattern
                    i += 1
                    continue
                j = i + 1
                while j < lim and s[j] in " \t":
                    j += 1
                glued_eq = f.ws >= 0 and s[i - 1] == "="
                if s.startswith(")", j) and not glued_eq:  # `f()`: a function definition
                    self._end_word(f, i)
                    f.coll, f.cmdpos = None, True
                    i = j + 1
                    continue
                if glued_eq and _NL_ASSIGN.match(s, f.ws) and _NL_ASSIGN.match(s, f.ws).end() == i:
                    self._push(_RLFrame("arr", mode="arr", cmdpos=False), i)
                    i += 1
                    continue
                if f.ws < 0 and s.startswith("((", i) and (f.cmdpos or mode == "for"):
                    f.cmdpos = False  # `(( ... ))`, `for ((...))`
                    self._push(_RLFrame("arith"), i)
                    i += 2
                    continue
                # A subshell (or a glued `(`: its body read as commands too).
                f.cmdpos = False
                self._push(_RLFrame("("), i)
                i += 1
                continue
            if c == ")":
                self._end_word(f, i)
                if f.mode == "pat":
                    f.mode, f.cmdpos, f.pat_first = "normal", True, False
                    i += 1
                    continue
                self._end_cmd(f)
                i += 1
                if k == "top":
                    res.undecided = True  # a `)` with nothing open
                    f.cmdpos = True
                    continue
                self._pop(i)
                continue
            # a word character
            if f.ws < 0:
                self._start_word(f, i)
            if c == "'":
                self._push(_RLFrame("q1"), i)
                i += 1
            elif c == '"':
                self._push(_RLFrame("dq"), i)
                i += 1
            elif c == "`":
                i = self._backtick(i, False)
            elif c == "$":
                if s.startswith("$((", i):
                    self._push(_RLFrame("arith"), i)
                    i += 3
                elif s.startswith("$(", i):
                    self._push(_RLFrame("$("), i)
                    i += 2
                elif s.startswith("${", i):
                    self._push(_RLFrame("brace"), i)
                    i += 2
                elif s.startswith("$'", i):
                    self._push(_RLFrame("ansi"), i)
                    i += 2
                elif s.startswith('$"', i):
                    self._push(_RLFrame("dq"), i)
                    i += 2
                else:
                    i += 1
            else:
                m = _RL_PLAIN.match(s, i, lim)
                i = m.end() if m else i + 1


def _raw_nonliteral(command: str, depth: int, sink: "_NonLiteralSink") -> None:
    """T-4506: the non-literal rule on a level `_scan` cannot read: report a
    non-literal command word to `sink` (`hit`; `unreadable`, r4's reason, for
    one spelt `$'...'` / `$"..."`), or, where the lexer cannot follow bash
    and the level holds quoting or a backslash, fail closed."""
    res = _RLResult()
    try:
        _RawLevel(command, depth, [2 * len(command) + _TOKENIZE_MAX_CHARS], res).run()
    except Exception:  # RecursionError or a lexer fault: main() would fail open
        res.undecided = True
    if res.hit:
        sink.hit = True
    elif res.qhit or (res.undecided and ("$'" in command or '$"' in command)):
        sink.unreadable = True
    elif res.undecided and any(ch in command for ch in "'\"\\"):
        sink.undecided = True


# T-4389 B(1) MF3 (review rounds 1-4): `_scan` reads a heredoc body as
# text (quotes and backslashes inert), which is right for its own reader,
# but a SHELL reading the body parses it again with quotes live (a `;` in a
# code string there is no separator), and which command really reads a
# body is not knowable from the text (functions, aliases, PATH, a written
# file run later). Nor is where an operator appears: the texts this hook
# judges include ones it derives (eval-joined and comment-cut copies, -c
# code strings), where `<""<` or `<\<` becomes `<<`. So a decision is made
# in the B(1) reading and, unless that denies, again in the reading from
# before it (`_SCAN_HEREDOCS` off: no body modelled, and `_skip_ranges`
# uses origin/main's `_heredoc_body_ranges`, so that pass is origin/main's
# reading exactly), denying if either does.
# The second pass is skipped only when no text `_scan` read in the first
# pass (raw or masked, derived texts included: every one goes through
# `_scan`) contained `<<` (`_SCAN_SAW_LTLT`). The two readings differ only
# where `_SCAN_HEREDOCS` is read, `_scan` and `_skip_ranges`, and both read
# the same as the other reading on a text without `<<` (no operator is
# recognized, `.bodies` is empty, and `_heredoc_body_ranges` needs `<<` in
# the masked text), so the second pass would decide identically. Invariant:
# verdict = base verdict OR B(1) verdict, for every input; the DENY set
# only ever grows. The cost is a second decide() for a command holding (or
# deriving) `<<` that the first pass allows. A separately reviewed
# allowance for bodies proven to be data is T-4851.


def decide(data: dict) -> dict | None:
    """`_decide_readings` in the T-4389 B(1) reading; when that allows and
    `_scan` read a `<<`, the same in the reading from before B(1) (MF3,
    above). Deny if either denies: the verdict is the base verdict OR the
    B(1) verdict. T-5140 r2: an allow from either reading (each includes
    its comment-cut and eval-joined copies) is a deny when any scan made
    while judging overflowed (`_SCAN_OVERFLOWED`)."""
    _SCAN_OVERFLOWED[0] = False
    _SCAN_SAW_LTLT[0] = False
    # T-5505: per call (the command itself, read by `_decide_script_files`)
    _sf_reset(str((data.get("tool_input") or {}).get("command") or ""))
    try:
        denial = _overflow_checked(data, _decide_readings(data))
        if denial is not None or not _SCAN_SAW_LTLT[0]:
            return denial  # no `<<` read: the base pass would decide the same
        _SCAN_HEREDOCS[0] = False
        _SCAN_SAW_LTLT[0] = False
        try:
            return _overflow_checked(data, _decide_readings(data))
        finally:
            _SCAN_HEREDOCS[0] = True
    finally:
        _SCAN_SAW_LTLT[0] = False


# T-5332: set while `_decide_readings` judges a stdin-fed text's copies (their feeds were
# already collected, nested ones included, by `_stdin_fed_codes`).
_FED_ACTIVE = [False]
# T-5363: set while `_decide_readings` judges a command's decoded `env -S` reading (it is
# fully decoded already, or past the bounds that fail closed).
_ENV_S_ACTIVE = [False]


_HO_RECORDS_WHAT = "to write/commit orchestrator-owned protected docs (ledger / ADR / notes \u2014 and, inside an isolation worktree, the full protected-doc set: rulebook / bugs / incidents / lessons / sop / BACKLOG / goals / README)"


def _decide_py_startup(runs: str, cwd: str) -> dict | None:
    try:
        why = _py_startup_write_match(runs, cwd)
    except Exception:
        # T-4498: main() fails OPEN on an exception, so a raise here (e.g. the
        # lexer's NUL marker reaching expanduser via `~$(...)`) skipped every
        # rule; a check that can't read the command denies instead.
        why = "unreadable"
    if why:
        hint = {"dyncd": _PY_DYNCD_HINT, "dyncode": _PY_DYNCODE_HINT, "unreadable": _PY_UNREADABLE_HINT}.get(why, "")
        return _reason(_PY_STARTUP_WHAT.format(hook_dir=_HOOK_DIR) + hint)
    return None


def _gh_cli_reason(gh_what: str) -> dict:
    return _reason(
        f"{gh_what}. Subagents may run only read gh commands: pr view/list/"
        "status/diff/checks/checkout, issue view/list/status, run view/list/"
        "watch/download, workflow view/list, release view/list/download, repo "
        "view/list/clone, search, status, ruleset list/view/check, cache/secret/"
        "variable/label list, auth status (no token), and `gh api` reads. Every "
        "GitHub write is reserved to the head orchestrator (T-1345, T-1673, "
        "T-4145); route it through your dispatcher. If the gh words are only "
        "DATA: put a note's text in a file and pass it as `--note \"$(cat "
        "file)\"` (ticket.py), search with the Grep tool, and write heredoc or "
        "script content with the Write tool"
    )


# T-5332 r5: set while `_decide_fed_copies` judges a fed text at a candidate cwd other than
# the payload's; `_decide` then runs only `_decide_cwd_rules`.
_FED_CWD_ONLY = [False]


def _decide_cwd_rules(command: str, cwd: str) -> dict | None:
    """The Bash rules whose verdict depends on the cwd, in `_decide`'s order and with its
    reasons: the protected records, Python startup paths, git identity, the gh CLI."""
    if _ho_records_match(command, cwd):
        return _reason(_HO_RECORDS_WHAT)
    runs = _data_heredoc_text(command)
    denial = _decide_py_startup(runs, cwd)
    if denial is not None:
        return denial
    # (each of the two needs its tool's name in the text, quotes dropped: a cheap skip)
    view = None if "$'" in command or '$"' in command else \
        command.replace("\\\n", "").translate(_QUOTE_STRIP)
    identity_what = _git_identity_match(command, cwd) if view is None or "git" in view else None
    if identity_what:
        return _reason(identity_what)
    gh_what = _gh_cli_violation(runs, cwd) if view is None or "gh" in view else None
    if gh_what:
        return _gh_cli_reason(gh_what)
    return None


def _decide_fed_copies(data: dict, tool_input: dict, command: str,
                       judged: frozenset = frozenset()) -> dict | None:
    """T-5332: each static text a shell in `command` reads on stdin, judged as a command of
    its own at every directory the command can run it in (see `_stdin_fed_codes`).
    T-5363 r2: a text in `judged` (or a blank one) was judged already and is skipped."""
    codes, chains, refused = _stdin_fed_codes(command)
    if judged:
        codes = tuple(c for c in codes if c not in judged and c.strip())
    if not codes and not refused:
        return None
    cands = None if refused else _fed_cands(data.get("cwd") or "", chains)
    if cands is None and not refused:
        return _reason(
            "to feed a shell its commands (`printf ... | bash`, `bash <<< ...`, a heredoc) "
            f"after more than {_FED_MAX_CANDS} directory changes this hook would judge the "
            "fed text in (T-5332). Split the command, or run the fed commands with literal "
            "absolute paths"
        )
    if refused or sum(map(len, codes)) * len(cands) > 8 * len(command) + 262_144:
        return _reason(
            "to feed a shell its commands (`printf ... | bash`, `bash <<< ...`, a heredoc) "
            "from a printf this hook cannot compute (`%q`, `%f`, a `*` width, an invalid or "
            "out-of-range number, an option) or past its nesting/size/directory bounds "
            "(T-5332). Spell the commands out plainly, or write a script with the Write tool"
        )
    for code in codes:
        if not _SCAN_HEREDOCS[0] and "<<" not in code:
            continue  # `decide()`'s second (pre-B(1)) reading: same verdict without a `<<`
        for k, cwd in enumerate(cands):
            _FED_CWD_ONLY[0] = k > 0
            try:
                denial = _decide_readings({**data, "cwd": cwd, "tool_input": {**tool_input, "command": code}})
            finally:
                _FED_CWD_ONLY[0] = False
            if denial is not None:
                return denial
    return None


# --- T-5505: the commands a script FILE runs, judged as commands of their own ---
# `bash s.sh`, `sh -x s.sh`, `source ./s.sh`, `. ./s.sh`, `./s.sh`, `bash < s.sh` run the text
# of a file the worker can write, so a deny the guard gives the text as a command (a sudo, a
# push, a gh write) must hold when the text sits in a file. The FILE-fed sibling of T-5332:
# each script a command runs is read and its text judged through `_decide_readings` as a
# command of its own, at every directory the command can run it in (`_fed_cands` over the
# literal chdir chains of the command and its code strings), deny-only and recursively (a
# script that runs a script, to `_FED_DEPTH`).
#   Forms (`_sf_cmd`), wherever the lexer finds a command (top level, any list or pipe,
# subshells, `$(...)`, static `eval`/`-c` strings, stdin-fed texts) behind the shared wrapper
# peel (env/nice/timeout/nohup/time/command/builtin/exec/chronic/unbuffer/...): a shell
# (`_SHELL_NAMES`, also behind busybox) with a script operand after its options (`-c` is a
# code string, read by `_py_code_strings`; `-n`/`-o noexec` runs nothing); a shell's
# `--rcfile`/`--init-file FILE`; a shell or `source /dev/stdin` reading a `<`/`0<` FILE; `cat
# FILE | bash` (one literal file, no options: its text is judged as the fed text); `source
# ./FILE`/`. ./FILE`; a command word holding a `/` (`./x`, `d/x`, `/abs/x`, `~/x`); a
# `BASH_ENV=FILE`/`ENV=FILE` word; a bare command word after a `PATH=` that puts a static
# absolute dir first (the file found there is read as `/dir/NAME`).
#   Reading (`_sf_text`): a shell form or source reads the file as shell, whatever its first
# line; a directly run file is shell when it has a shebang naming a shell (directly or via
# `env`) or none at all (execve's ENOEXEC fallback); another interpreter's shebang (python,
# node, ...) and a binary (ELF, or a NUL in the first line) are left as the rest of this hook
# leaves them. A file under the system dirs (`_SF_SYSTEM`, after realpath) is not the worker's
# to write and is left alone too (ORG-105: no foothold). A path is opened as typed from the
# candidate cwd (never normalized first: `lnk/../s.sh` is the file the kernel reaches).
#   A script's own self-locating spellings (`$0`, `${BASH_SOURCE[0]}`, `$(cd "$(dirname
# "$0")" && pwd)`, one unconditional `HERE=...`) are read as the paths they print
# (`_sf_located_view`) only in the narrow case where nothing can make them print something
# else (`_sf_gate`): no cd/pushd/popd, function, PATH/IFS/HOME/BASH_ARGV0/BASH_ENV change,
# shopt/alias/enable/hash/trap, heredoc, or non-live `$` in the script, and none of those
# (bar the cds) in the command or any script around it; `$0` is the path as typed, from the
# cwd at invocation (one holding `..` is not read). Any other self-location is a run-time
# path.
#   Fails closed, each with a reason a worker can act on (`_sf_reason`): a path built at run
# time; a file missing at every candidate, unreadable or not a regular file; a shell-read
# file over 64 KiB, binary or not UTF-8; a bare `source NAME`/`. NAME` (bash searches PATH
# first); a process substitution as a script or rcfile; `xargs`/`find -exec` running a shell
# or source; `make -f FILE`; a shebang naming an interpreter outside the system dirs that is
# not a binary; a `PATH=` holding `.`, an empty, relative or run-time entry, or a dir the
# command writes to, before a command; a `HOME=`/`ZDOTDIR=`/`XDG_CONFIG_HOME=`/`INPUTRC=`
# change with an interactive or login shell (`-i`, `-l`, `--login`, `-s`, any zsh); more than
# `_SF_MAX_FILES` scripts, the judged-size budget, `_FED_DEPTH` nesting, `_FED_MAX_CANDS`
# directories.
#   Same-command rewrite (`_sf_final`, over every text judged in one decide(): the command and
# every script it runs, at any depth): a script in the run set (the script itself or one run
# later) whose file name is a write-redirect target, a substring of any word of a command not
# known to leave file contents alone (`_SF_READONLY`, a read-only git subcommand, sed/perl
# without -i, a copier's source operands), or whose (st_dev, st_ino) is that of such a word
# or redirect target (a symlink or hardlink alias); a script with more than one link in a
# command that writes anything; a directory above it named by a known writer; a known writer
# or redirect to a run-time path; and a bulk writer that changes files it does not name
# (`tar x`, `unzip`, `git checkout/apply/pull/...`, `patch`, `make`, `rsync`, `cp -r`, `curl
# -o/-O`, `wget`) anywhere in a command that runs a script.
# OUT of scope: a file rewritten by an earlier call's background job (cross-call TOCTOU); a
# renamed interpreter binary (`cp /bin/bash ./b; ./b s.sh`); `#!/usr/bin/env NAME` found
# through a worker-writable PATH entry set before this call; make/npm recipes and git hooks
# run without naming a file; a value set in the shell's profile by an earlier call.
_SF_MAX_BYTES = 65_536
_SF_MAX_FILES = 32
_SF_BUDGET = 262_144  # characters judged per decide() (each candidate counts; as T-5332)
_SF_MAX_STATS = 20_000  # stat() calls the rewrite check may make per decide()
_SF_SYSTEM = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib32/", "/lib64/", "/libx32/", "/etc/",
              "/snap/", "/nix/store/")
_SF_LEVEL = [0]  # how many script files deep the text being judged is
_SF_STATE: dict = {"cache": {}, "collect": {}, "spent": 0, "top": "", "runs": [], "writers": [],
                   "bulk": [], "inter": False, "home": False, "pathdirs": [], "stats": 0}
# (reset by decide(), `_sf_reset`)
_SF_GLOB = re.compile(r"[*?\[]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
_SF_READONLY = frozenset({
    "chmod", "chown", "chgrp", "cat", "ls", "test", "[", "[[", "stat", "file", "head", "tail",
    "wc", "grep", "egrep", "fgrep", "rg", "shellcheck", "echo", "printf", "realpath",
    "readlink", "basename", "dirname", "cd", "pushd", "popd", "diff", "cmp", "md5sum",
    "sha1sum", "sha256sum", "sha512sum", "od", "xxd", "hexdump", "less", "more", "true",
    "false", ":", "mkdir", "touch", "which", "type", "du", "nl", "sort", "uniq", "cut"})
# git subcommands that leave worktree files alone; any other (an alias too: `git co`) may
# rewrite them, and one not in `_SF_GIT_WRITERS` (whose files are named) is a bulk writer
_SF_GIT_READONLY = frozenset({
    "status", "log", "show", "diff", "add", "commit", "branch", "rev-parse", "ls-files",
    "grep", "blame", "describe", "fetch", "remote", "tag", "shortlog", "cat-file", "ls-tree",
    "rev-list", "show-ref", "for-each-ref", "symbolic-ref", "merge-base", "name-rev", "reflog",
    "var", "version", "help", "count-objects", "check-ignore", "check-attr", "config",
    "whatchanged", "range-diff", "cherry", "diff-tree", "diff-index", "diff-files", "fsck",
    "verify-commit", "init", "ls-remote", "show-branch", "check-ref-format", "hash-object"})
_SF_GIT_WRITERS = frozenset({"mv", "rm", "clean", "update-index", "checkout-index"})
_SF_GIT_OPT_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                               "--super-prefix", "--config-env", "--exec-path"})
_SF_COPIERS = frozenset({"cp", "mv", "ln", "install", "rsync", "scp"})
_SF_WRITERS = frozenset({"cp", "mv", "ln", "install", "tee", "dd", "rsync", "sed", "perl",
                         "truncate", "curl", "wget", "patch", "unzip", "tar", "git"})
_SF_BULK_ALWAYS = frozenset({"unzip", "patch", "make", "gmake", "rsync", "wget", "cpio",
                             "7z", "7za", "unrar", "ditto"})
_SF_ASSIGN_ENV = re.compile(r"(?:BASH_)?ENV=(.*)", re.S)
_SF_DOT = re.compile(r"(?:^|[\s;&|(){}!`])\.(?=\s)")
_SF_ARRAY_OPEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_SF_ENV_SETTERS = frozenset({"export", "declare", "typeset", "local", "env", "readonly"})
_SF_RELAYS = frozenset({"xargs", "parallel", "sem"})
_SF_SH = r"(?:static-sh|rbash|bash|dash|zsh|ksh|mksh|hush|ash|sh)"
# H: the prefilter. A text that matches none of these cannot spell a script run (on the
# quote-dropped copy): a shell word followed by a non-option word or a `<` (not a heredoc,
# not after a `-c` cluster), a shell word with a pipe in the text, source/xargs/make words, a
# `.` word, a `/`, or a name the startup/PATH rules read.
_SF_PRE_RUN = re.compile(r"(?<![\w.-])" + _SF_SH + r"(?![\w.-])(?:[ \t]+(?:--\S*|[-+](?!-)[^\sc]*))*"
                         r"(?:[ \t]+(?![-+]|<<|[|;&)\n]|$)|[ \t]*<(?!<))", re.M)
_SF_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_SF_PRE_SHELL = re.compile(r"(?<![\w.-])" + _SF_SH + r"(?![\w.-])")
# T-5571 r1: a stdin `<`/`<>` anywhere in the command (`< s.sh bash`, `X=1 < s.sh bash`,
# `{ < s.sh bash; }`, `bash <> s.sh`) with a shell word: not a heredoc, here-string or `<(`
_SF_PRE_LT = re.compile(r"(?<!<)<(?![<(&])")
_SF_PRE_NUM = re.compile(r"\d+[smhd]?\Z")
_SF_PRE_SEG = re.compile(r"[;&|\n(){}!]")  # T-5662
_SF_PRE_LEAD = frozenset({"do", "then", "else", "elif", "if", "while", "until", "time", "!", "{"})
_SF_PRE_WRAP = frozenset({"env", "exec", "command", "builtin", "nohup", "nice", "timeout", "stdbuf",
                          "setsid", "ionice", "chrt", "taskset", "flock", "watch", "busybox",
                          "xargs", "strace", "doas", "chronic", "unbuffer", "parallel", "sem"})
_SF_PRE_WORDS = re.compile(r"(?<![\w.-])(?:source|xargs|make|gmake)(?![\w.-])")
_SF_PRE_KEYS = ("/", "PATH", "HOME", "ZDOTDIR", "INPUTRC", "ENV", "rcfile", "init-file",
                "-exec", "-ok")
# (each simple-command segment holding a `<(` is read once: linear)
_SF_SEGMENT = re.compile(r"[\n;&|)]|(?<!<)\(")
_SF_PROCSUB_HEAD = re.compile(r"(?<![\w.-])(?:" + _SF_SH + r"|source|\.)(?![\w./-])|"
                              r"(?:rcfile|init-file|ENV)[ \t=]*$")
_SF_STARTUP_VAR = re.compile(r"(?<![\w$])(?<!\$\{)(?:HOME|ZDOTDIR|XDG_CONFIG_HOME|INPUTRC)(?!\w)")
_SF_PATH_ASSIGN = re.compile(r"PATH\+?=(.*)", re.S)
_SF_PATH_SETTER = re.compile(r"(?<![\w$])(?:read|mapfile|readarray|getopts|unset|for|select|"
                             r"printf|local|declare|typeset)\b")
_SF_PATH_NAME = re.compile(r"(?<![\w${])PATH(?!\w)(?!\+?=)")
_SF_TAIL = (". To run the repo's shell test suites (tools/test_*.sh), use `python3 "
            "tools/test_shell_suites.py` (the CI path). Otherwise run the script's commands "
            "directly in this call")
_SF_REASONS = {
    "dyn": ("to run a file whose path is built at run time (T-5505): the path `{}` is built at "
            "run time (a variable, `$(...)`, a glob or a brace), so the hook cannot tell which "
            "file runs. Spell the literal "
            "absolute path (`/home/me/repo/.venv/bin/python`, not `\"$VENV/bin/python\"`; "
            "`/abs/repo/tools/x.sh`, not `\"$REPO/tools/x.sh\"`), one literal path per command, "
            "not a glob or loop"),
    "missing": ("to run a file that is not there yet (T-5505): `{}` was not found in any cwd "
                "this command can reach (the payload cwd or a literal `cd` target). If a step "
                "generates it, create it in a separate call first, then run it by a literal "
                "path"),
    "bare": ("to `source`/`.` the bare name `{}` (T-5505): bash searches PATH before the cwd "
             "for a name without a `/`, so the hook cannot tell which file runs. Use `./{}` or "
             "an absolute path"),
    "rewrite": ("to run a script file that the same command (or a script it runs) also writes, "
                "appends to, replaces, aliases (a symlink or hardlink to it) or passes to a "
                "command this hook does not know as read-only (`printf ... > s.sh; bash s.sh`, "
                "`python3 -c \"open('s.sh','w')...\"; bash s.sh`, a script that appends to "
                "itself) (T-5505): the hook judges the file as it is before the command runs. "
                "Write the script first (the Write tool, or a separate call), then run it in a "
                "call of its own"),
    "bulk": ("to run a script file in the same command as `{}`, which rewrites files it does "
             "not name (`tar x`, `unzip`, `git checkout/apply/pull/...`, `patch`, `make`, "
             "`rsync`, `cp -r`, `curl -o/-O`, `wget`) (T-5505): the hook judges the script as it "
             "is before the command runs. Run that step in a separate call first, then run the "
             "script in a call of its own"),
    "startup": ("to start a shell that reads a startup file this hook cannot judge (T-5505): "
                "{}. Drop the `HOME=`/`ZDOTDIR=` change or the `-i`/`-l`/`--login`/`-s` flag, "
                "and give `--rcfile`/`--init-file`/`ENV=`/`BASH_ENV=` a literal path to an "
                "existing file"),
    "path": ("to run a command after a `PATH=` that {} (T-5505): the hook cannot tell which "
             "file a bare command name runs. Use only absolute PATH entries that exist before "
             "this call (`PATH=/abs/bin:$PATH cargo build`), or run the program by its literal "
             "absolute path"),
    "relay": ("to hand a shell its script through {} (T-5505): the hook cannot see which file "
              "runs. Run each script by a literal path (`bash /abs/x.sh`), one per command"),
    "dynhead": ("to run a command whose name is built at run time (T-5662): `{}` is a "
                "variable the hook cannot resolve to a literal (its value comes from a command's "
                "output, `read`, a positional parameter, a pattern or a brace), a `$(...)` or a "
                "backtick, so the hook cannot tell whether it runs a script it should read. "
                "Spell the command literally (`/usr/bin/python3`, not `$(command -v python3)`), "
                "or assign the variable a literal (`x=bash; $x /abs/x.sh`)"),
    "envhead": ("to run a command named by a variable the hook cannot see, together with a "
                "file (T-5662): the variable (`$PY`, `$X`) may hold a shell, which would read "
                "that file as a script, and the same command also names the file in a way "
                "that may rewrite it. Spell the command literally (`/usr/bin/python3 FILE`, "
                "`bash FILE`), or assign the variable a literal first (`x=bash; $x FILE`)"),
    "pathhead": ("to run a command whose bare name `{}` is built at run time, in a text that "
                 "also sets PATH or renames a command (`PATH=`, `export PATH`, the hash "
                 "builtin's `-p`) (T-5662): bash searches PATH for a bare name, so the hook "
                 "cannot tell which file runs. Spell the command's path literally "
                 "(`/usr/bin/ls`, `/abs/dir/x.sh`), or set PATH in a call of its own"),
    "generic": ("to run a script file (`bash FILE`, `sh FILE`, `source ./FILE`, `./FILE`, "
                "`bash < FILE`) whose contents this hook cannot read and judge (T-5505): {}. "
                "Fix the path so it names an existing, readable text script (at most 64 KiB) "
                "written before this call"),
}


class _SfRefused(Exception):
    """A script this hook cannot read or judge: (reason class, detail)."""


def _sf_reset(top: str) -> None:
    _SF_STATE.clear()
    _SF_STATE.update(cache={}, collect={}, spent=0, top=top, runs=[], writers=[], bulk=[],
                     inter=False, home=False, pathdirs=[], stats=0)


def _sf_show(word) -> str:
    if word is None:
        return "(a redirect built at run time)"
    w = word.replace(_PY_SUBST, "$(...)").replace(_PY_PWD_MARK, "$PWD").replace("`", "'")
    return w if len(w) <= 80 else w[:77] + "..."


def _sf_reason(cls: str, arg=None) -> dict:
    text = _SF_REASONS[cls]
    if cls in ("dyn", "missing", "dynhead", "pathhead"):
        text = text.format(_sf_show(arg))
    elif cls == "bare":
        text = text.format(_sf_show(arg), _sf_show(arg))
    elif "{}" in text:
        text = text.format(arg or "")
    return _reason(text + _SF_TAIL)


def _sf_dynamic(word, dyn: bool) -> bool:
    return (word is None or dyn or _PY_SUBST in word or _py_dynamic(word)
            or bool(_SF_GLOB.search(word)))


def _sf_shell_operand(words: list[str]):
    """For a shell's words (`words[0]` the shell): ("file", index) of its script operand,
    ("stdin", None) when it reads commands from stdin, or None (a `-c` code string, or
    `-n`/`-o noexec`/`-D`: nothing runs from a file)."""
    k, smode, noexec = 1, False, False
    while k < len(words):
        w = words[k]
        if w == "-":
            return ("stdin", None)
        if w == "--":
            k += 1
            break
        if len(w) < 2 or w[0] not in "-+":
            break
        if w[:2] == "--":
            if w in ("--dump-strings", "--dump-po-strings"):
                noexec = True
            k += 2 if w in ("--rcfile", "--init-file") else 1
            continue
        on = w[0] == "-"
        letters = w[1:]
        if "c" in letters:
            return None  # a code string (`_py_code_strings`)
        smode = smode or (on and "s" in letters)
        if "n" in letters:
            noexec = on
        elif "D" in letters and on:
            noexec = True
        nvals = letters.count("o") + letters.count("O")
        vals = words[k + 1:k + 1 + nvals]
        for letter, val in zip([x for x in letters if x in "oO"], vals):
            if letter == "o" and val == "noexec":
                noexec = on
        k += 1 + nvals
    if noexec:
        return None
    if smode or k >= len(words) or words[k] in _FED_STDIN_PATHS:
        return ("stdin", None)
    return ("file", k)


def _sf_shell_startup(words: list[str]) -> tuple[list[int], bool]:
    """(the indexes of a shell's `--rcfile`/`--init-file` values, whether it is interactive,
    a login shell or reads stdin: `-i`/`-l`/`-s` in an option cluster, `--login`)."""
    rc: list = []
    inter = False
    k = 1
    while k < len(words):
        w = words[k]
        if w == "--" or len(w) < 2 or w[0] not in "-+":
            break
        if w[:2] == "--":
            if w in ("--rcfile", "--init-file"):
                rc.append(k + 1)
                k += 2
                continue
            if w.startswith(("--rcfile=", "--init-file=")):
                rc.append(-k - 1)  # the value after `=` (decoded by the caller)
            inter = inter or w in ("--login", "--interactive")
            k += 1
            continue
        letters = w[1:]
        if w[0] == "-" and set(letters) & set("ils"):
            inter = True
        if "c" in letters:
            break
        k += 1 + letters.count("o") + letters.count("O")
    return rc, inter


def _sf_git_sub(words: list[str]) -> str | None:
    """git's subcommand (past its global options), or None when it cannot be read."""
    k = 1
    while k < len(words):
        w = words[k]
        if w in _SF_GIT_OPT_VALUE:
            k += 2
            continue
        if w[:1] == "-":
            k += 1
            continue
        return w
    return None


def _sf_bulk(head: str, words: list[str]) -> bool:
    """True if this command may rewrite files it does not name (B(iv))."""
    if head in _SF_BULK_ALWAYS:
        return not (head == "unzip" and any(w in ("-l", "-t", "-v", "-Z", "-p") for w in words[1:]))
    if head in ("tar", "bsdtar"):
        first = words[1] if len(words) > 1 else ""
        if first[:1] != "-" and "x" in first:
            return True  # `tar xf a.tar`
        return any(w in ("--extract", "--get") or (w[:1] == "-" and w[1:2] != "-" and "x" in w)
                   for w in words[1:])
    if head == "cp":
        return any(w in ("--recursive", "--archive") or (w[:1] == "-" and w[1:2] != "-"
                                                         and set(w[1:]) & set("raR"))
                   for w in words[1:])
    if head == "curl":
        return any(w in ("-O", "--remote-name", "--remote-name-all", "-J", "--remote-header-name",
                         "-o", "--output", "--output-dir", "--create-dirs")
                   or w.startswith(("--output=", "--output-dir="))
                   or (w[:1] == "-" and w[1:2] != "-" and set(w[1:]) & set("oOJ"))
                   for w in words[1:])
    return False


def _sf_cmd(c: _PyCmd, runs: list, writers: list, info: dict) -> None:
    """Add `c`'s script runs to `runs` ((word, kind, dynamic, consumer)), the words that may
    write a file to `writers` ((word, dynamic, known writer, redirect target)), and to `info`
    what the other rules read (refusals, bulk writers, interactive shells)."""
    for t in c.targets:
        writers.append((t, "$" in t or _PY_SUBST in t or "`" in t, True, True))
    for k, w in enumerate(c.words):
        m = _SF_ASSIGN_ENV.match(w)
        if m and m.group(1) and (k == 0 or _PY_ASSIGN.match(c.words[k - 1])
                                 or os.path.basename(c.words[k - 1]) in _SF_ENV_SETTERS):
            # (sh/bash read ENV only when interactive: held until the walk knows, `_sf_collect_walk`)
            (runs if w.startswith("BASH_ENV=") else info["envfiles"]).append(
                (m.group(1), "env", k in c.dyn, None))
    i = _py_head_index(c.words)
    if i >= len(c.words) or _ASSIGN_WORD.match(c.words[i]):
        return  # (`A[k]=v`: an assignment the head index does not skip)
    full = c.words[i:]
    words, dyn = list(full), [k + i in c.dyn for k in range(len(full))]
    qw = c.qwords or ()
    qd = [k + i in qw for k in range(len(full))]
    prefix: list = []
    if os.path.basename(full[0]) in _WRAPPER_GRAMMAR:
        try:
            words, base, n, src = _fed_peeled(full)
            dyn = [src[n + k] + i in c.dyn for k in range(len(words))]
            qd = [src[n + k] + i in qw for k in range(len(words))]
            prefix = list(base[:n])
        except _FedUnknown:
            pass
    pnames = [os.path.basename(w) for w in prefix]
    relay = any(p in _FED_STDIN_TAKERS or p == "xargs" for p in pnames)
    if words and os.path.basename(words[0]) == "busybox":
        words, dyn, qd = words[1:], dyn[1:], qd[1:]
    if not words or words[0][:1] == "-":
        return  # (an option word as the command: `time --output=F cmd` read past `time`)
    head = os.path.basename(words[0])
    if head in _SHELL_NAMES or head in (".", "source"):
        hit = [p for p in pnames if p in _SF_RELAYS]
        if hit and not _sf_noexec(words):
            info["refuse"].append(("relay", f"`{hit[0]}` (`xargs bash`, `xargs sh -c`, "
                                            "`parallel bash ::: FILE`)"))
            return
    if head in _SF_RUNNERS:
        _sf_runner_words(head, words, dyn, qd, runs, writers, info)  # T-5662 r1
    if head in ("find",):
        for k in range(1, len(words) - 1):
            if words[k] in _FIND_EXEC_WORDS:
                nxt = os.path.basename(words[k + 1])
                if (nxt in _SHELL_NAMES and not _sf_noexec(words[k + 1:])) or nxt in (".", "source"):
                    info["refuse"].append(("relay", "`find -exec` (`find ... -exec bash {} \\;`)"))
                elif "/" in words[k + 1]:
                    runs.append((words[k + 1], "exec", dyn[k + 1], None))
    if head in ("make", "gmake") and any(
            w in ("-f", "--file", "--makefile") or w.startswith(("--file=", "--makefile="))
            or (w[:2] == "-f" and len(w) > 2) for w in words[1:]):
        info["refuse"].append(("generic", "`make -f FILE` runs the recipes in FILE, which this "
                                          "hook does not judge; run the recipe's commands "
                                          "directly"))
    if c.infiles and not relay and (head in _SHELL_NAMES or head in (".", "source")):
        try:
            run = _fed_consumer(c)
        except _FedUnknown:
            run = _sf_same  # judged as a shell's: a superset
        if run is not None:
            runs.extend((f, "stdin", f is None, run) for f in c.infiles)
    if head in (".", "source"):
        k = 2 if words[1:2] == ["--"] else 1
        used = None
        if k < len(words) and words[k] not in _FED_STDIN_PATHS:
            runs.append((words[k], "src", dyn[k], None))
            used = k
        _sf_writer_words(words, dyn, used, False, writers)
        return
    if head in _SHELL_NAMES:
        rc, inter = _sf_shell_startup(words)
        if head == "zsh" or inter or (
                "exec" in pnames and any(w[:1] == "-" and w[1:2] != "-" and "l" in w
                                         for w in prefix)):
            info["inter"] = True  # (zsh reads $ZDOTDIR/.zshenv on every start)
        for k in rc:
            if k >= 0:
                if k < len(words):
                    runs.append((words[k], "env", dyn[k], None))
            else:
                runs.append((words[-k - 1].partition("=")[2], "env", dyn[-k - 1], None))
        got = _sf_shell_operand(words)
        if got is None:
            return  # nothing runs from a file; a `-c` string's commands are walked
        used = None
        if got[0] == "file":
            used = got[1]
            runs.append((words[used], "sh", dyn[used], None))
        _sf_writer_words(words, dyn, used, False, writers)
        return
    if "/" in words[0]:
        runs.append((words[0], "exec", dyn[0], None))
        _sf_writer_words(words, dyn, 0, False, writers)
        return
    if dyn[0]:
        # T-5662: what it may run; a head read as literal values is those commands (each
        # read below with its own writer words), any other is an unknown command
        if not _sf_dynhead(c, words, dyn, qd, runs, writers, info):
            _sf_writer_words(words, dyn, 0, False, writers)
        return
    if head in _SF_READONLY or head == "eval":
        return  # (an eval's words are code, walked as a code string: its writes are read there)
    if head == "git":
        sub = _sf_git_sub(words)
        if sub in _SF_GIT_READONLY:
            return  # (a subcommand that leaves worktree files alone)
        if sub not in _SF_GIT_WRITERS:
            info["bulk"].append(f"git {sub or ''}".strip())
        _sf_writer_words(words, dyn, 0, True, writers)
        return
    if _sf_bulk(head, words):
        info["bulk"].append(head)
    if head in ("sed", "perl") and not any(
            w[:2] == "-i" or w.startswith("--in-place") or (w[:1] == "-" and w[1:2] != "-"
                                                            and "i" in w) for w in words[1:]):
        return  # writes only its output (redirects are read above)
    if head in _SF_COPIERS:
        # only the destination is written: the last operand, or a -t/--target-directory dir
        ops = [k for k in range(1, len(words)) if words[k][:1] != "-" or dyn[k]]
        dest = [k for k in range(1, len(words)) if words[k - 1] in ("-t", "--target-directory")
                or words[k].startswith("--target-directory=") or (
                    words[k][:2] == "-t" and len(words[k]) > 2)]
        for k in set(dest) | set(ops[-1:]):
            writers.append((words[k].partition("=")[2] if words[k].startswith("--") else
                            words[k][2:] if words[k][:2] == "-t" and len(words[k]) > 2 else
                            words[k], dyn[k], True, False))
        return
    _sf_writer_words(words, dyn, 0, head in _SF_WRITERS, writers)


# --- T-5662: a command word built at run time, read as the script it may run ---
# `x=/abs/s.sh; $x`, `x=bash; $x s.sh`, `$(echo /abs/s.sh)`, `a=(/abs/s.sh); "${a[@]}"` ran a
# script the T-5505 reading never saw (the head was not a literal, so no run was recorded).
# Per walked text (`_sf_assigns`) the literal values each NAME is assigned are collected (`NAME=v`,
# `NAME=(a b)`, `for NAME in a b`; at most `_SF_ASSIGN_MAX` values, text order ignored: the union
# of every assignment); a head that is a NAME-expansion (`$x`, `${x}`, `"$x"`, `${a[@]}`,
# `${x:-d}`, pieces of them) is read as each value it can take, as if spelled (`_sf_dynhead`
# re-reads the command with the value as its head: a path is a script run, a shell takes the
# operand as its script, a wrapper is peeled). Otherwise:
#   * a head built from output or input (`$(..)`, a backtick, a NAME assigned from either, `read`,
#     `+=`, a positional `$1`/`"$@"`, a pattern or brace, an unreadable expansion) fails closed
#     (`dynhead`);
#   * a NAME the text never assigns is the environment's value: it passes (OUT of scope with the
#     rest of the profile environment, T-5647). Its first operand is read as the command when
#     the expansion is empty and the word is unquoted (`$UNSET ./x`), and each operand that is
#     an existing file is read as shell (`opd`: a shell shebang always; otherwise unless its
#     extension names data or another language, `_SF_OPD_SKIP`) because the value could be a shell.
_SF_ASSIGN_MAX = 32
_SF_WHOLE_ARRAY = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[[@*]\]\}")
_SF_ASSIGN_W = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?(\+?)=(.*)\Z", re.S)
_SF_ASSIGN_EXP = re.compile(r"\$\{!?([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?:?[=?]")
# (`PATH=`, `PATH+=`, `PATH[0]=` in any position; the hash builtin's `-p`; `BASH_CMDS`)
_SF_PATH_ANY = re.compile(r"(?<![\w$])PATH(?:\[[^\]]*\])?\+?=|(?<![\w-])hash\s+-\w*p|BASH_CMDS")
_SF_DECLARERS = frozenset({"declare", "typeset", "local", "export", "readonly", "let"})
_SF_READERS = frozenset({"read", "mapfile", "readarray", "getopts", "select"})
_SF_VARREF = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)"
                        r"(?:\[[^\]]*\])?(?:(:?-)([^}$`*?\[{\\]*))?\})\Z")
_SF_DYN_TOKEN = re.compile(r"(\$\{[^}]*\}|\$[A-Za-z0-9_@*#?!$-]+|[\0\1`]|\$\()")
_SF_OPD_SKIP = frozenset({
    ".py", ".pyi", ".pyc", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".jsonl", ".md",
    ".markdown", ".rst", ".txt", ".toml", ".yaml", ".yml", ".rs", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".go", ".java", ".kt", ".rb", ".pl", ".php", ".lua", ".lock", ".cfg", ".ini",
    ".conf", ".csv", ".tsv", ".log", ".xml", ".html", ".htm", ".css", ".sql", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webm", ".mp4", ".mp3", ".wav", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".o", ".a", ".so", ".tex", ".diff", ".patch", ".proto"})
_SF_SHELLISH_EXT = (".sh", ".bash", ".zsh", ".ksh", ".dash")


def _sf_note(table: dict, name: str, value) -> None:
    """Record `value` (a str, or None: built at run time) among `name`'s values."""
    got = table.setdefault(name, set())
    if got is None or (value is not None and value in got):
        return
    if value is None or len(got) >= _SF_ASSIGN_MAX:
        table[name] = None
    else:
        got.add(value)


def _sf_assigns(text: str, lex, table: dict) -> None:
    """Add to `table` (NAME -> its literal values, or None) what `text`'s commands assign."""
    for m in _SF_ASSIGN_EXP.finditer(text):
        table[m.group(1)] = None  # `${x:=v}` assigns, whatever v is
    # T-5662 r3: PATH set anywhere in the walk (any position, function body, subshell, or a
    # command renamed with the hash builtin's -p)
    pview = text.replace("\\\n", "").translate(_QUOTE_STRIP)
    if "PATH" in pview or "BASH_CMDS" in pview or "hash" in pview:
        if _SF_PATH_ANY.search(pview) or _sf_path_set_otherwise(pview):
            table["\0path"] = True
    events = lex.events
    arrays: set = set()
    open_name, elems = None, []  # the array `a=(x y)` being read, one pass (an inner `b=(` is data)
    for idx, e in enumerate(events):
        if open_name is not None:
            if isinstance(e, _PyCmd):
                for j, w in enumerate(e.words):
                    elems.append(None if _gc_dynamic(e, j, w) else w)
                    _sf_note(table, open_name, elems[-1])
            elif e == ")":
                # (the elements in order, for the whole array as a command: None if set twice)
                key = "\0arr:" + open_name
                table[key] = None if key in table or None in elems else elems
                open_name, elems = None, []
            continue
        if (isinstance(e, _PyCmd) and e.words and idx + 1 < len(events) and events[idx + 1] == "("
                and _SF_ARRAY_OPEN.fullmatch(e.words[-1])):
            open_name = _SF_ASSIGN_W.match(e.words[-1] + "x").group(1)  # `a=(x y)`
            arrays.add(open_name)
            table.setdefault(open_name, set())
            elems = []
    if open_name is not None:
        table[open_name] = None  # (an array that does not close)
        table["\0arr:" + open_name] = None
    for c in lex.cmds:
        words = c.words
        if not words:
            continue
        if words[0] in ("for", "select") and words[2:3] == ["in"]:
            for j in range(3, len(words)):
                _sf_note(table, words[1], None if _gc_dynamic(c, j, words[j]) else words[j])
            continue
        if words[0] in ("for", "select") and len(words) > 1:
            _sf_note(table, words[1], None)  # (`for x; do`: the positional parameters)
            continue
        i = _py_head_index(words)
        while i < len(words) and _ASSIGN_WORD.match(words[i]):
            i += 1  # (a subscripted `a[0]=v` is an assignment `_py_head_index` stops at)
        head = os.path.basename(words[i]) if i < len(words) else ""
        if head in _SF_READERS:
            for w in words[i + 1:]:
                if w[:1] != "-" and _DC_NAME.fullmatch(w):
                    _sf_note(table, w, None)
        elif head in ("declare", "typeset", "local", "export", "readonly") and any(
                w[:1] == "-" and w[1:2] != "-" and "n" in w for w in words[i + 1:]):
            table["\0nameref"] = None  # a nameref: every NAME may stand for another
        elif head == "printf" and "-v" in words[i + 1:]:
            nxt = words[words.index("-v", i) + 1:words.index("-v", i) + 2]
            if nxt:
                _sf_note(table, nxt[0], None)
        # (only a word in assignment position: before the command word, or a declarer's/env's
        # argument; the code string `x=ls; $x` given to `bash -c` is no assignment)
        for j, w in enumerate(words):
            if j >= i and head not in _SF_ENV_SETTERS and head not in _SF_DECLARERS:
                break
            m = _SF_ASSIGN_W.match(w)
            if m is None:
                continue
            name, plus, value = m.groups()
            if "[" in w[:w.find("=")]:
                table["\0arr:" + name] = None  # (an element set on its own: the order is unknown)
            if not value and name in arrays and j == len(words) - 1 and plus == "":
                continue  # (`a=` of `a=(..)`, read above)
            _sf_note(table, name, None if plus or _gc_dynamic(c, j, w) else value)


_SF_RUNNERS = frozenset({"find", "fd", "fdfind", "entr"})
_SF_FD_EXEC = frozenset({"-x", "-X", "--exec", "--exec-batch"})


def _sf_runner_words(head: str, words: list, dyn: list, qd: list, runs: list, writers: list,
                     info: dict) -> None:
    """T-5662 r1: a runner's command slot (`find -exec/-execdir/-ok/-okdir`, `fd -x/-X/--exec/
    --exec-batch`, `entr`) holding a word built at run time is a command word like any head:
    the command from there (to the `;`/`+` of a find) is read as a command of its own
    (`_sf_dynhead`). A literal word there keeps the rules of `_sf_cmd` (xargs, parallel, watch
    and the other wrappers are peeled to their command before the head is read). One pass: a
    command ends at the next `-exec` word."""
    slots = []
    if head == "find":
        slots = [k + 1 for k in range(1, len(words) - 1) if words[k] in _FIND_EXEC_WORDS]
    elif head in ("fd", "fdfind"):
        slots = [k + 1 for k in range(1, len(words) - 1) if words[k] in _SF_FD_EXEC]
    elif head == "entr":
        k = 1
        while k < len(words) and words[k][:1] == "-":
            k += 1
        slots = [k] if k < len(words) else []
    for k in slots:
        # (a run-time word, or a wrapper before one: `-exec nice $x \;` is peeled like a head)
        if not dyn[k] and not (os.path.basename(words[k]) in _WRAPPER_GRAMMAR and any(dyn[k:])):
            continue
        end = k + 1
        while end < len(words):
            if head == "find" and (words[end] == ";" or (words[end] == "+" and words[end - 1] == "{}")
                                   or words[end] in _FIND_EXEC_WORDS):
                break
            end += 1
        sub = _PyCmd()
        sub.words = words[k:end]
        sub.dyn = {j for j in range(end - k) if dyn[k + j]}
        sub.qwords = {j for j in range(end - k) if qd[k + j]}
        _sf_cmd(sub, runs, writers, info)


def _sf_head_values(head: str, table: dict):
    """(the literal values the run-time command word `head` can take, whether it uses a NAME
    the text never assigns), or None when it cannot be read."""
    if "\0nameref" in table or _PY_SUBST in head or _PY_PWD_MARK in head or "`" in head:
        return None
    pieces = _SF_DYN_TOKEN.split(head)
    values, unset = [""], False
    for k, piece in enumerate(pieces):
        if k % 2 == 0:
            options = [piece]
        else:
            m = _SF_VARREF.match(piece)
            if m is None:
                return None
            name, braced, op, dflt = m.group(1), m.group(2), m.group(3), m.group(4)
            name = name or braced
            if name in table:
                got = table[name]
                if got is None:
                    return None
                options = sorted(got)
                if op == ":-" and dflt is not None and "" in got:
                    options.append(dflt)
            else:
                unset = True
                options = ([""] + ([dflt] if op and dflt is not None else []))
        values = [v + o for v in values for o in options]
        if len(values) > _SF_ASSIGN_MAX:
            return None
    values = list(dict.fromkeys(values))
    if any(_SF_GLOB.search(v) or _py_dynamic(v) for v in values):
        return None
    return values, unset


def _sf_dynhead(c: _PyCmd, words: list, dyn: list, qd: list, runs: list, writers: list,
                info: dict) -> bool:
    """T-5662: `words[0]` is a command word built at run time: read each value it can take as
    the command (see the block comment above), or fail closed. True if the head was read as
    values of names the text assigns (no name unset)."""
    got = _sf_head_values(words[0], info["assigns"])
    if got is None:
        # (a word with blanks or quotes is a code string a wrapper runs, `watch "env x $(d)"`,
        # read as code; a lone substitution a shell reads on stdin is T-5571's)
        if not (re.search(r"[\s'\"]", words[0]) or (
                (info.get("fed") or _FED_ACTIVE[0]) and len(words) == 1 and words[0] == _PY_SUBST)):
            info["refuse"].append(("dynhead", words[0]))
        return False
    values, unset = got
    whole = _SF_WHOLE_ARRAY.fullmatch(words[0])
    elems = info["assigns"].get("\0arr:" + whole.group(1)) if whole else None
    if whole and elems is None and len(values) > 1:
        # (elements set one by one: the command is their joined words, in an order not read)
        info["refuse"].append(("dynhead", words[0]))
        return False
    if info["assigns"].get("\0path") and any(
            v.split() and "/" not in v.split()[0] for v in values):
        # a bare name searched along a PATH the text sets (or renames with the hash builtin)
        info["refuse"].append(("pathhead", words[0]))
        return False
    for v in values + ([None] if elems else []):
        if v is None:  # the whole array: its elements are the command and its arguments
            sub = _PyCmd()
            sub.infiles = c.infiles
            sub.words = list(elems) + list(words[1:])
            sub.dyn = {len(elems) + k - 1 for k in range(1, len(words)) if dyn[k]}
            sub.qwords = {len(elems) + k - 1 for k in range(1, len(words)) if qd[k]}
            _sf_cmd(sub, runs, writers, info)
            continue
        if v and not qd[0]:
            head_words = v.split() or [""]  # (an unquoted expansion splits at blanks)
        else:
            head_words = [v]
        sub = _PyCmd()
        sub.infiles = c.infiles
        sub.words = [h for h in head_words if h] + list(words[1:])
        if not v and qd[0]:
            continue  # (a quoted empty word: "command not found", nothing runs)
        keep = len([h for h in head_words if h])
        sub.dyn = {keep + k - 1 for k in range(1, len(words)) if dyn[k]}
        sub.qwords = {keep + k - 1 for k in range(1, len(words)) if qd[k]}
        if sub.words:
            _sf_cmd(sub, runs, writers, info)
    if unset:
        for k in range(1, len(words)):
            w = words[k]
            if (w[:1] != "-" and not dyn[k] and w and _PY_SUBST not in w and "$" not in w
                    and "`" not in w and not _SF_GLOB.search(w)):
                runs.append((w, "opd", False, None))
    return not unset


def _sf_opd_kind(path: str) -> str | None:
    """How the operand file at `path` of a command word that may be a shell is read: "sh" for a
    file that begins with a shell shebang, or whose extension is not data or another language;
    None for a directory, a non-file, a binary, a system file, or such a file."""
    try:
        real = os.path.realpath(path)
        if real.startswith(_SF_SYSTEM) or not os.path.isfile(real):
            return None
        with open(real, "rb") as fh:
            first = fh.read(256).split(b"\n", 1)[0]
    except (OSError, ValueError):
        return None
    if first[:4] == b"\x7fELF" or b"\0" in first:
        return None
    if _sf_shebang_shell(first):
        return "sh"
    return None if os.path.splitext(path)[1].lower() in _SF_OPD_SKIP else "sh"


def _sf_noexec(words: list[str]) -> bool:
    """A shell that runs nothing (`bash -n`, `-o noexec`): no `-c` and no file it executes."""
    return (os.path.basename(words[0]) in _SHELL_NAMES and _sf_shell_operand(words) is None
            and not any(w[:1] == "-" and w[1:2] != "-" and "c" in w for w in words[1:]))


def _sf_same(text: str) -> str:
    return text


def _sf_writer_words(words: list[str], dyn: list, skip, known: bool, writers: list) -> None:
    for k in range(1, len(words)):
        if k != skip:
            writers.append((words[k], dyn[k], known, False))


def _sf_dynword_head(view: str) -> bool:
    """T-5662: True if a simple command of `view` (quote-dropped) starts with a word that begins
    with an expansion (`$x`, `${x}`, `$(..)`, a backtick), or a wrapper word has one after it.
    Linear: each segment's words are looked at once."""
    for seg in _SF_PRE_SEG.split(view):
        if "$" not in seg and "`" not in seg:
            continue  # (the cheap reject: most segments hold no expansion)
        words = seg.split()
        k = 0
        while k < len(words) and (words[k] in _SF_PRE_LEAD or (
                "=" in words[k] and _PY_ASSIGN.match(words[k]))):
            k += 1
        if k >= len(words):
            continue
        w0 = words[k]
        if w0[0] in "$`":
            return True
        base = w0.rsplit("/", 1)[-1] if "/" in w0 else w0
        if base in _SF_RUNNERS:  # (the command slot is anywhere after its option)
            if any(w[0] in "$`" for w in words[k + 1:]):
                return True
        elif base in _SF_PRE_WRAP:
            j = k + 1  # (past the wrapper's options, their values and a duration or count)
            while j < len(words) and (words[j][:1] == "-" or words[j - 1][:1] == "-"
                                      or _SF_PRE_NUM.match(words[j])
                                      or ("=" in words[j] and _PY_ASSIGN.match(words[j]))):
                j += 1
            if j < len(words) and words[j][0] in "$`":
                return True
    return False


def _sf_wanted(text: str) -> bool:
    """False when `text` cannot spell a script run or a form the T-5505 rules read (H: the
    cheap prefilter run before any lexing; on the copy with quotes and backslashes dropped,
    each `$'...'` decoded first, as it makes new text; one this cannot decode is read)."""
    if "$'" in text:
        try:
            text = _SF_ANSI_C.sub(lambda m: _fed_escapes(m.group(1), "fmt")[0], text)
        except Exception:
            return True
        if "$'" in text:
            return True  # (unterminated, or made by a decoded string)
    view = text.replace("\\\n", "").translate(_QUOTE_STRIP)
    if "/" in view or any(k in view for k in _SF_PRE_KEYS):
        return True
    if ("$" in view or "`" in view) and _sf_dynword_head(view):
        return True  # T-5662: a command word that starts with an expansion
    return bool(_SF_PRE_RUN.search(view) or _SF_PRE_WORDS.search(view) or _SF_DOT.search(view)
                or (("|" in view or _SF_PRE_LT.search(view)) and _SF_PRE_SHELL.search(view)))


def _sf_collect(command: str) -> tuple:
    """(runs, writers, chdir chains, refused, info): the script runs of `command`, of its
    static code strings and of its stdin-fed texts (to `_FED_DEPTH`), the words that may
    write a file, the literal chdir chains of every text read, whether the walk passed its
    bounds, and what the other rules read (`info`). Read once per text in a decide() call."""
    memo = _SF_STATE.setdefault("collect", {})
    got = memo.get(command)
    if got is None:
        got = memo[command] = _sf_collect_walk(command)
    return got


_SF_LEX_LAST: list = [None]  # T-5571: (text, view) of the last text lexed here (read-only views)


def _sf_lexed(text: str):
    cached = _FED_LEX_CACHE[0]
    if cached is not None and cached[0] == text:
        return _FedLexView(cached[1].cmds[:cached[2]], cached[1].events)
    last = _SF_LEX_LAST[0]
    if last is not None and last[0] == text:
        return last[1]  # (T-5571: the stdin-feed reading lexes the text T-5505 just did)
    lex = _py_lex(text)
    view = _FedLexView(list(lex.cmds), lex.events)
    _SF_LEX_LAST[0] = (text, view)
    return view


def _sf_collect_walk(command: str) -> tuple:
    runs: list = []
    writers: list = []
    chains: list = []
    info: dict = {"refuse": [], "bulk": [], "inter": False, "home": False, "pathdirs": [],
                  "envfiles": [], "assigns": {}}
    work = [(command, 0)]
    queued = {command}
    fed: set = set()  # T-5662: the texts a shell reads from stdin (T-5571 reads those)
    budget = 4 * len(command) + 4096
    while work:
        text, depth = work.pop()
        info["fed"] = text in fed
        lex = _sf_lexed(text)
        view = text.replace("\\\n", "").translate(_QUOTE_STRIP)
        if "<(" in view and _sf_procsub(view):
            info["refuse"].append(("relay", "a process substitution (`bash <(...)`, `source "
                                            "<(...)`, `--rcfile <(...)`, `ENV=<(...)`)"))
        if _SF_STARTUP_VAR.search(view):
            info["home"] = True
        # (a `source`/`.` changes directory only through its file's own commands: the
        # sourced file's chain is added when it is read, `_decide_script_files`)
        chain = _fed_chain([c for c in lex.cmds if not _sf_is_source(c)], 0)
        if chain:
            chains.append(tuple(chain))
        more: list = []
        skip = _sf_not_commands(lex.events)
        if "$" in view:
            _sf_assigns(text, lex, info["assigns"])  # T-5662 (before the commands read it)
        for c in lex.cmds:
            if id(c) not in skip:
                _sf_cmd(c, runs, writers, info)
            if _fed_may_run_code(c):
                more.extend(_py_code_strings(c) or ())
        if "|" in text:
            _sf_cat_feeds(lex.events, runs, info)
        if "PATH" in view:
            _sf_path_rule(lex, view, runs, info)
        if _fed_wanted(text):
            codes = _stdin_fed_codes(text)[0]
            fed.update(codes)
            more.extend(codes)
        for code in more:
            if code in queued:
                continue
            queued.add(code)
            budget -= len(code)
            if depth >= _FED_DEPTH or budget < 0:
                # past the bounds: refused only when an unread text could spell a T-5505 form
                return runs, writers, tuple(chains), budget < 0 or any(_sf_wanted(t) for t in more) or any(
                    _sf_wanted(t) for t, _d in work), info
            work.append((code, depth + 1))
    if info["inter"]:
        runs.extend(info["envfiles"])  # an interactive shell in the text reads `ENV=FILE`
        info["envfiles"] = []
    runs = [r for r in runs if r[0] is None or r[1] == "stdin" or not (
        r[0][:1] == "/" and (_py_norm(r[0]) + "/").startswith(_SF_SYSTEM))]
    return runs, writers, tuple(dict.fromkeys(chains)), False, info


def _sf_cat_feeds(events: list, runs: list, info: dict) -> None:
    """E: `cat FILE | bash` (a shell or `source /dev/stdin` reading the pipe): FILE's text is
    judged as the text the shell reads. More than one file, or an option, fails closed."""
    for idx in range(len(events) - 2):
        a, bar, b = events[idx], events[idx + 1], events[idx + 2]
        if bar != "|" or not isinstance(a, _PyCmd) or not isinstance(b, _PyCmd):
            continue
        i = _py_head_index(a.words)
        if i >= len(a.words) or os.path.basename(a.words[i]) != "cat":
            continue
        try:
            run = _fed_consumer(b)
        except _FedUnknown:
            run = _sf_same
        if run is None:
            continue
        ops = a.words[i + 1:]
        files = [(w, k + i + 1 in a.dyn) for k, w in enumerate(ops) if w != "-"]
        if not files and a.infiles:
            files = [(f, f is None) for f in a.infiles]
        if any(w[:1] == "-" and w != "-" for w in ops) or len(files) > 1:
            info["refuse"].append(("relay", "`cat` with options or several files piped to a "
                                            "shell (`cat a.sh b.sh | bash`)"))
            continue
        runs.extend((w, "stdin", d, run) for w, d in files)


def _sf_procsub(view: str) -> bool:
    """D: a `<(...)` given to a shell, source or `.` (its script, or after --rcfile/ENV=)."""
    for seg in _SF_SEGMENT.split(view):
        at = seg.rfind("<(")
        if at >= 0 and _SF_PROCSUB_HEAD.search(seg[:at]):
            return True
    return False


def _sf_path_set_otherwise(view: str) -> bool:
    """E: PATH named after a setter word (`read`, `printf -v`, `unset`, `declare`, `for`) in
    the same simple command: its value is not in the text."""
    for seg in re.split(r"[\n;|&]", view):
        m = _SF_PATH_SETTER.search(seg) if "PATH" in seg else None
        if m and _SF_PATH_NAME.search(seg, m.end()):
            return True
    return False


def _sf_path_seg(seg: str) -> str | None:
    """A static absolute PATH entry, "" for the inherited `$PATH`, None for any other (`.`,
    empty, relative, run-time)."""
    if seg in ("$PATH", "${PATH}"):
        return ""
    if not seg or "`" in seg or _PY_SUBST in seg or _PY_PWD_MARK in seg:
        return None
    pre = _py_static_prefix(seg)
    if pre in ("$PWD", "${PWD}"):
        return None
    if seg[:1] == "/" or seg[:1] == "~" or pre in ("$HOME", "${HOME}"):
        if "$" in seg[len(pre):]:
            return None
        return _py_resolve(seg, None)
    return None


def _sf_path_rule(lex, view: str, runs: list, info: dict) -> None:
    """E: a `PATH=` (prefix, statement, export/env word) holding `.`, an empty, relative or
    run-time entry, or a PATH set another way (`read`, `printf -v`, `unset`), before a
    command, fails closed; a static absolute entry put before `$PATH` is searched for each
    later bare command word, and a file found there is read as a run of `/dir/NAME`."""
    if _sf_path_set_otherwise(view):
        info["refuse"].append(("path", "is set by `read`, `printf -v`, `unset`, `declare` or "
                                       "a loop (its value is not in the text)"))
        return
    dirs: list = []
    alldirs: list = []
    for c in lex.cmds:
        for k, w in enumerate(c.words):
            m = _SF_PATH_ASSIGN.match(w)
            if not m or (k and not (_PY_ASSIGN.match(c.words[k - 1])
                                    or os.path.basename(c.words[k - 1]) in _SF_ENV_SETTERS)):
                continue
            new: list = []
            inherited = False
            for seg in m.group(1).split(":"):
                got = _sf_path_seg(seg)
                if got is None:
                    info["refuse"].append(("path", f"holds `{_sf_show(seg) or '(empty)'}` (a "
                                                   "`.`, an empty, relative or run-time entry)"))
                    return
                inherited = inherited or got == ""
                if got:
                    alldirs.append(got)
                if not inherited:
                    new.append(got)  # (entries after the inherited PATH are searched after it)
            if w.startswith("PATH+="):
                new = []  # (appended after the inherited entries)
            dirs = new + [d for d in dirs if d not in new]
        if not dirs:
            continue
        i = _py_head_index(c.words)
        if i < len(c.words) and "/" not in c.words[i] and i not in c.dyn:
            names = [c.words[i]]
        else:  # (a bare name built at run time: `_sf_dynhead` fails it closed, T-5662 r3)
            names = []
        for name in names:
            for d in dirs:
                cand = os.path.join(d, name)
                if os.path.isfile(cand):
                    runs.append((cand, "exec", False, None))
                    break
    info["pathdirs"].extend(dict.fromkeys(alldirs))


def _sf_not_commands(events: list) -> set:
    """The ids of the top-level lexed `commands` that are no commands: the elements of an
    array assignment (`a=(x y)`) and the patterns of a `case` (`./*)`, `a|b)`)."""
    out: set = set()
    if "(" not in events and ")" not in events:
        return out
    mode: list = []  # "array", "pat" (case patterns expected), "body" (in a case arm)
    prev_semi = False
    for idx, e in enumerate(events):
        top = mode[-1] if mode else None
        if not isinstance(e, _PyCmd):
            if e == "(":
                if top in ("array", "pat"):
                    pass  # `( a | b )` opens a pattern; nested parens in an array: data
                else:
                    j = idx - 1
                    while j >= 0 and events[j] == "\n":
                        j -= 1
                    prev = events[j] if j >= 0 else None
                    if isinstance(prev, _PyCmd) and prev.words and _SF_ARRAY_OPEN.fullmatch(
                            prev.words[-1]) and not prev.targets:
                        mode.append("array")
            elif e == ")":
                if top == "array":
                    mode.pop()
                elif top == "pat":
                    mode[-1] = "body"
            elif e == ";" and top == "body" and prev_semi:
                mode[-1] = "pat"  # `;;`
            elif e == "&" and top == "body" and prev_semi:
                mode[-1] = "pat"  # `;&`
            prev_semi = e == ";"
            continue
        prev_semi = False
        words = e.words
        if top in ("array", "pat"):
            if top == "pat" and words[:1] == ["esac"]:
                mode.pop()
                continue
            out.add(id(e))
            continue
        if top == "body" and "esac" in words:
            mode.pop()
        k = _py_head_index(words)
        if k + 2 < len(words) and words[k] == "case" and words[k + 2] == "in":
            mode.append("pat")  # (a first pattern on the `case` line is a word of it)
    return out


def _sf_shebang_shell(first: bytes) -> bool | None:
    """For a directly run file's first line: True if it runs as shell (a shebang naming a
    shell directly or through env, or a bare `#!`), False for another interpreter, None when
    there is no shebang."""
    if not first.startswith(b"#!"):
        return None
    name = os.path.basename(_sf_shebang_interp(first) or "")
    return not name or name in _SHELL_NAMES


def _sf_shebang_interp(first: bytes) -> str | None:
    """The interpreter a shebang line names (through `env`, the program env runs), "" for a
    bare `#!`, None for no shebang."""
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").split()
    if not parts:
        return ""
    if os.path.basename(parts[0]) != "env":
        return parts[0]
    rest = parts[1:]
    k = 0
    while k < len(rest):
        w = rest[k]
        if w in ("-u", "--unset", "-C", "--chdir", "-a", "--argv0"):
            k += 2
            continue
        if w.startswith("-S") and len(w) > 2:
            rest[k] = w[2:]  # `-Sbash`: the rest of the word is the first -S word
            continue
        if w[:1] == "-" or "=" in w:
            k += 1
            continue
        break
    return rest[k] if k < len(rest) else ""


def _sf_text(path: str, kind: str, cwd: str = "") -> str | None:
    """The text of the script at `path` that runs as shell, or None when it does not run as
    shell (a binary or another interpreter's script, run directly) or lies in a system dir.
    Raises `_SfRefused` when it cannot be read or judged."""
    try:
        real = os.path.realpath(path)
        if real.startswith(_SF_SYSTEM) or real == "/dev/null" or os.path.isdir(real):
            return None  # not the worker's to write, or nothing runs (a directory)
        if not os.path.isfile(real):
            raise _SfRefused("generic", f"`{path}` is not a regular file")
        with open(real, "rb") as fh:
            data = fh.read(_SF_MAX_BYTES + 1)
    except _SfRefused:
        raise
    except (OSError, ValueError):
        raise _SfRefused("generic", f"`{path}` cannot be read") from None
    first = data.split(b"\n", 1)[0]
    binary = data[:4] == b"\x7fELF" or b"\0" in first[:80]
    if kind == "exec":
        if binary:
            return None
        interp = _sf_shebang_interp(first)
        if interp and "/" in interp:
            _sf_interp_check(interp, cwd)
        if _sf_shebang_shell(first) is False:
            return None
    elif binary:
        raise _SfRefused("generic", f"`{path}` is a binary file read as shell")
    if len(data) > _SF_MAX_BYTES:
        raise _SfRefused("generic", f"`{path}` is larger than 64 KiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _SfRefused("generic", f"`{path}` is not UTF-8 text") from None


def _sf_interp_check(interp: str, cwd: str) -> None:
    """E: a shebang interpreter outside the system dirs that is not a binary (another script:
    the kernel runs ITS interpreter on it) fails closed."""
    p = interp if interp[:1] == "/" else os.path.join(cwd or "/", interp)
    try:
        real = os.path.realpath(p)
        if real.startswith(_SF_SYSTEM) or not os.path.exists(real):
            return  # a system interpreter, or none (the exec fails)
        with open(real, "rb") as fh:
            head = fh.read(4)
    except OSError:
        raise _SfRefused("generic", f"its shebang interpreter `{interp}` cannot be read") from None
    if head != b"\x7fELF":
        raise _SfRefused("generic", f"its shebang names `{interp}`, a script outside the system "
                                    "dirs (run the interpreter by its literal path instead)")


def _sf_is_source(c: _PyCmd) -> bool:
    i = _py_head_index(c.words)
    return i < len(c.words) and c.words[i] in (".", "source")


# A script's own text locates the scripts it runs through its own path (`"$0"`,
# `"${BASH_SOURCE[0]}"`, `"$(cd "$(dirname "$0")" && pwd)/x.sh"`, `HERE=...; "$HERE/x.sh"`).
# `_sf_located_view` reads those spellings as the literal paths they print, in a copy of the
# script's text used only to find the scripts it runs and the files it writes (the text
# judged is the script's own): `$0` (not in a sourced file) and `${BASH_SOURCE[0]}` are the
# script's path as typed (from the cwd at invocation); a `$(...)` body of only static
# absolute paths that is `dirname P`, `realpath P`, `readlink -f P` or `cd P && pwd` is what
# it prints; and `$NAME`/`${NAME}` after NAME's one assignment is its static value, when that
# assignment is a top-level, unconditional, persistent command, NAME appears nowhere else in
# the quote-dropped text but as `$NAME`/`${NAME}` (A: `HE"RE"=`, `declare`, `read`, `printf
# -v`, arrays, continuations all trip it), and no file the script sources assigns it.
# Nothing is read this way unless `_sf_gate` passes for the script, the command and every
# script around it.
_SF_STACK: list = []  # (path, real, kind, cwd, typed, text) per script being judged
_SF_SELF = re.compile(r"\$\{BASH_SOURCE(?:\[0\])?\}|\$BASH_SOURCE(?![\w\[])")
_SF_ZERO = re.compile(r"\$\{0\}|\$0(?!\w)")
_SF_INNER = re.compile(r"\$\(([^()`$]*)\)")
_SF_TAINT = re.compile(r"CDPATH|BASH_FUNC|(?<![\w-])(export|declare|typeset)((?:\s+[-+]\w+)+)")
# A: what can make `$0`, `dirname`, `cd`, `pwd` or a variable print something other than the
# text says. The cd words only in the script's own text (an outer cd is a candidate cwd).
_SF_GATE_CD = re.compile(r"(?<![\w.-])(?:cd|pushd|popd)(?![\w.-])")
# (the `$(cd DIR && pwd)` idiom changes only its own subshell's directory)
_SF_CD_IDIOM = re.compile(r"\$\([ \t]*cd(?:[ \t]+-[-PLe]*)*[ \t]+(?:\$\([^()]*\)|[^\s()&;|<>])+"
                          r"(?:[ \t]+\d*>[ \t]*/dev/null|[ \t]+2>&1)*[ \t]*&&[ \t]*pwd"
                          r"(?:[ \t]+-[PL])?[ \t]*\)")
_SF_GATE = re.compile(
    r"(?<![\w.-])(?:shopt|alias|enable|hash|trap|function|BASH_ARGV0)(?![\w.-])"
    r"|(?<![\w$])(?<!\$\{)(?:PATH|BASH_SOURCE|HOME|BASH_ENV|ENV|BASH_ARGV0)(?!\w)"
    r"|(?<![\w$])(?<!\$\{)IFS(?!\w)(?!=\S*[ \t]+(?:read|mapfile|readarray)\b)"
    r"|[\w.:-][ \t]*\([ \t]*\)|<<(?!<)")


def _sf_tainted(text: str) -> bool:
    """True if `text` names CDPATH or exports a function (`export -f`, `declare -fx`)."""
    for m in _SF_TAINT.finditer(text):
        if m.group(1) is None:
            return True
        flags = "".join(f[1:] for f in m.group(2).split() if f[:1] == "-")
        if "f" in flags and (m.group(1) == "export" or "x" in flags):
            return True
    return False


def _sf_gate(text: str, own: bool) -> bool:
    """A: True if nothing in `text` can change what a self-locating spelling prints: no
    function definition, PATH/IFS/HOME/BASH_ARGV0/BASH_ENV/ENV change, shopt/alias/enable/
    hash/trap, heredoc, CDPATH or exported function, and (`own`: the script's own text) no
    cd/pushd/popd."""
    view = text.replace("\\\n", "").translate(_QUOTE_STRIP)
    if _SF_GATE.search(view) or _sf_tainted(text):
        return False
    return not (own and _SF_GATE_CD.search(_SF_CD_IDIOM.sub("", view)))


def _sf_live(text: str) -> set:
    """The offsets of the `$` characters of `text` that expand: not in single quotes or
    `$'...'`, not escaped, not in a comment."""
    live: set = set()
    i, n, state = 0, len(text), ""
    while i < n:
        ch = text[i]
        if state == "'":
            if ch == "'":
                state = ""
        elif state == "a":  # $'...'
            if ch == "\\":
                i += 1
            elif ch == "'":
                state = ""
        elif ch == "\\":
            i += 1
        elif state == '"':
            if ch == '"':
                state = ""
            elif ch == "$":
                live.add(i)
        elif ch == "'":
            state = "'"
        elif ch == '"':
            state = '"'
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif ch == "$":
            if text[i + 1:i + 2] == "'":
                state = "a"
                i += 2
                continue
            live.add(i)
        i += 1
    return live


def _sf_name_clean(view: str, name: str) -> bool:
    """A: NAME appears in the quote-dropped `view` only as `$NAME`/`${NAME}` and in exactly one
    `NAME=` assignment."""
    esc = re.escape(name)
    total = len(re.findall(r"(?<![\w])" + esc + r"(?!\w)", view))
    expands = len(re.findall(r"\$" + esc + r"(?!\w)|\$\{" + esc + r"\}", view))
    assigns = len(re.findall(r"(?<![\w$])(?<!\$\{)" + esc + r"=", view))
    return assigns == 1 and total == expands + 1


def _sf_idiom(body: str) -> str | None:
    """What a script-locating `$(...)` body of static absolute paths prints, or None."""
    try:
        w = shlex.split(body)
    except ValueError:
        return None
    if not w:
        return None
    opts = [x for x in w[1:] if x[:1] == "-"]
    ops = [x for x in w[1:] if x[:1] != "-"]
    if w[0] == "dirname" and len(ops) == 1 and set(opts) <= {"--"} and ops[0][:1] == "/":
        # textual, as dirname is: a `..` stays (the kernel resolves it through any link)
        return os.path.dirname(ops[0].rstrip("/") or "/") or "/"
    if w[0] in ("realpath", "readlink") and len(ops) == 1 and ops[0][:1] == "/" and (
            w[0] == "realpath" or any(set(o[1:]) & set("fem") for o in opts if o[1:2] != "-")):
        return os.path.realpath(ops[0])
    if w[0] == "cd" and "&&" in w:
        k = w.index("&&")
        head = [x for x in w[1:k] if not _SF_REDIR_TOKEN.fullmatch(x)]
        tail = w[k + 1:]
        dirs = [x for x in head if x[:1] != "-"]
        if (tail[:1] == ["pwd"] and set(tail[1:]) <= {"-P", "-L"} and len(dirs) == 1
                and dirs[0][:1] == "/" and {x for x in head if x[:1] == "-"} <= {"-P", "-L", "--"}):
            return os.path.realpath(dirs[0]) if "-P" in w[1:] else _py_norm(dirs[0])
    return None


# an assignment's value: "..." (no `"`, backtick or `\\` inside), '...', a `$(...)` with no
# parentheses inside, or a plain word; then blanks and a comment
_SF_VALUE = re.compile(r"""(?:"([^"`\\]*)"|'([^']*)'|(\$\([^()`]*\))|([^\s"'`;&|<>()\\#]+))"""
                       r"[ \t]*(?:#[^\n]*)?")
_SF_PATHLIKE = re.compile(r"(?:[\w./+@%:,=-]|\$\{?\w+\}?|\$\([^()'\"`]*\))*")
_SF_REDIR_TOKEN = re.compile(r"\d*>>?(?:/dev/null|&\d)?|&>/dev/null|/dev/null")
_SF_OPEN = frozenset({"if", "while", "until", "for", "select", "{", "case"})
_SF_CLOSE = frozenset({"fi", "done", "}", "esac"})
_SF_DECL = frozenset({"export", "readonly", "declare", "typeset"})
# a command that may set NAME other than by `NAME=` (a `.format` template: `{}` is NAME); the
# strict reading also refuses a local/declare/typeset of it
_SF_NAME_USE_LOOSE = (r"(?<![\w$])(?:read|mapfile|readarray|getopts|unset|for|select|printf)\b"
                      r"[^\n;|&]*?(?<![\w${{]){}(?!\w)")
_SF_NAME_USE = (r"(?<![\w$])(?:read|mapfile|readarray|getopts|unset|for|select|printf|local|"
                r"declare|typeset)\b[^\n;|&]*?(?<![\w${{]){}(?!\w)")


def _sf_top_assigns(text: str) -> set:
    """The names `text` assigns in a top-level, unconditional, persistent command."""
    lex = _py_lex(text)
    if lex.tail_start is not None or lex.frame_limited:
        return set()
    out: set = set()
    stack: list = []
    ev = lex.events
    clean = True  # only `;`/newlines since the previous command
    for idx, e in enumerate(ev):
        if not isinstance(e, _PyCmd):
            if e == "(":
                stack.append("(")
            elif e == ")":
                if stack and stack[-1] == "(":
                    stack.pop()
                elif not (stack and stack[-1] == "case"):
                    return set()
            if e not in ("\n", ";"):
                clean = False
            continue
        nxt = ev[idx + 1] if idx + 1 < len(ev) else None
        words = e.words
        k = 0
        if words and words[0] in _SF_DECL:
            k = 1
            while k < len(words) and words[k][:1] == "-":
                if set(words[k]) & set("naAf"):
                    k = len(words) + 1  # a nameref, an array, a function: not read
                k += 1
        if (clean and not stack and nxt in (None, "\n", ";") and k < len(words)
                and not e.targets and all(_PY_ASSIGN.match(w) for w in words[k:])):
            for w in words[k:]:
                name = _PY_ASSIGN.match(w).group(0)
                if not name.endswith("+="):
                    out.add(name[:-1])
        for w in words:
            if w in _SF_OPEN:
                stack.append(w)
            elif w in _SF_CLOSE:
                if not stack:
                    return set()
                stack.pop()
        clean = True
    return out


def _sf_located_view(text: str, path: str, kind: str, loose: bool = False) -> tuple[str, set]:
    """(the copy of a script's `text` with its self-locating spellings read as literal paths,
    the names read so: see the block comment above). `loose` (for the words that may write a
    file, where a wrong value can only add a hit) also reads a name assigned once anywhere.
    The caller checks `_sf_gate` first; `path` is the path as typed (None: not read)."""
    if path is None:
        return text, set()
    live = _sf_live(text)
    qview = text.replace("\\\n", "").translate(_QUOTE_STRIP)

    def _all_live(rx) -> bool:
        return all(m.start() in live for m in rx.finditer(text))

    view = text
    if kind != "stdin" and _all_live(_SF_SELF):  # (a shell reading stdin has no script path)
        view = _SF_SELF.sub(lambda m: path, view)
    if kind in ("sh", "exec") and _all_live(_SF_ZERO):  # (in a sourced file `$0` is the caller's)
        view = _SF_ZERO.sub(lambda m: path, view)
    names: set = set()
    if "eval" in text.split():
        return view, names
    assigns = None
    for _round in range(8):
        before = view
        for _k in range(16):
            new = _SF_INNER.sub(lambda m: _sf_idiom(m.group(1)) or m.group(0), view)
            if new == view:
                break
            view = new
        if "$" not in view:
            break
        if assigns is None:
            pool = set(re.findall(r"(?<![\w$])([A-Za-z_]\w*)=", text)) if loose else \
                _sf_top_assigns(text)
            assigns = sorted(n for n in pool if n in view)
        for name in assigns:
            esc = re.escape(name)
            if not loose and not (_sf_name_clean(qview, name) and _all_live(
                    re.compile(r"\$" + esc + r"(?!\w)|\$\{" + esc + r"\}"))):
                continue
            sets = list(re.finditer(r"(?<![\w$])" + esc + r"\s*(?:\[[^\]]*\])?\s*\+?=(?!=)", view))
            if len(sets) != 1 or re.search(r"\$\{" + esc + r":?=", view) or re.search(
                    (_SF_NAME_USE_LOOSE if loose else _SF_NAME_USE).format(esc), view):
                continue
            m = re.compile(r"(?<![\w$])" + esc + r"=([^\n]*)$", re.M).search(view)
            if m is None or m.start() != sets[0].start():
                continue
            val = _SF_VALUE.fullmatch(m.group(1))
            if val is None:
                continue
            value = next(g for g in val.groups() if g is not None)
            if not _SF_PATHLIKE.fullmatch(value):
                continue  # (inserted as is, quoted or not: path characters only)
            if re.search(r"\$\{?" + esc + r"(?!\w)", value):
                continue  # `X="$X/y"`: the inherited value
            rest = re.sub(r"\$\{" + esc + r"\}|\$" + esc + r"(?![\w\[])", lambda _m: value,
                          view[m.end():])
            if rest != view[m.end():]:
                names.add(name)
                view = view[:m.end()] + rest
        if view == before:
            break
    return view, names


def _sf_judge(data: dict, tool_input: dict, text: str, cwd: str, cwd_only: bool,
              frame: tuple) -> dict | None:
    """`text`, the script `frame` names, judged as a command of its own at `cwd` (only the
    cwd rules when `cwd_only`), one script deeper, with every derived-reading flag cleared
    (it is a fresh text)."""
    st = _SF_STATE
    key = (text, frame[0], frame[4], cwd, cwd_only, _SCAN_HEREDOCS[0], _SF_LEVEL[0])
    if key in st["cache"]:
        return st["cache"][key]
    st["spent"] += len(text)
    if st["spent"] > _SF_BUDGET:
        raise _SfRefused("generic", "the scripts it runs pass the judged-size budget")
    saved = (_FED_ACTIVE[0], _FED_CWD_ONLY[0], _ENV_S_ACTIVE[0], _RN_ACTIVE[0], _DC_ACTIVE[0],
             _GC_ACTIVE[0])
    _FED_ACTIVE[0], _FED_CWD_ONLY[0], _ENV_S_ACTIVE[0] = False, cwd_only, False
    _RN_ACTIVE[0], _DC_ACTIVE[0], _GC_ACTIVE[0] = False, False, 0
    _SF_LEVEL[0] += 1
    _SF_STACK.append(frame + (text,))
    try:
        denial = _decide_readings({**data, "cwd": cwd,
                                   "tool_input": {**tool_input, "command": text}})
    finally:
        _SF_STACK.pop()
        _SF_LEVEL[0] -= 1
        (_FED_ACTIVE[0], _FED_CWD_ONLY[0], _ENV_S_ACTIVE[0], _RN_ACTIVE[0], _DC_ACTIVE[0],
         _GC_ACTIVE[0]) = saved
    st["cache"][key] = denial
    return denial


def _sf_path(word: str, kind: str, cwd: str) -> str | None:
    """`word` from `cwd` as the kernel opens it (joined, not normalized: `lnk/../x` passes
    through the link). C: a bare `source NAME`/`. NAME` is refused (bash searches PATH)."""
    if kind == "src" and "/" not in word:
        raise _SfRefused("bare", word)
    base = cwd if cwd and os.path.isabs(cwd) else None
    if word[:1] == "~" or _py_static_prefix(word):
        if "/../" in word + "/":
            return None
        return _py_resolve(word, base)
    if word[:1] == "/":
        return word
    return os.path.join(base, word) if base else None


def _sf_typed(word: str, path: str) -> str | None:
    """A: the script path bash puts in `$0`/`${BASH_SOURCE[0]}`: the word as typed, joined
    to the invocation cwd and never normalized (a `..` stays, so the paths built from it
    reach what the kernel reaches; `~` words are expanded by bash before the script runs)."""
    if word[:1] == "~" or _py_static_prefix(word):
        return None if path is None or "/../" in word + "/" else path
    return path


def _sf_isdir(word: str, cwd: str) -> bool:
    path = _py_resolve(word.rstrip("/") or "/", cwd if cwd and os.path.isabs(cwd) else None)
    return bool(path) and os.path.isdir(path)


def _sf_relative(word: str) -> bool:
    return not (word[:1] in ("/", "~") or _py_static_prefix(word) in ("$HOME", "${HOME}"))


def _decide_script_files(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5505: each script file `command` runs, its text judged as a command of its own at
    every directory the command can run it in (see the block comment above). An error while
    reading is a deny (main() fails open on an exception)."""
    try:
        denial = _decide_script_files_impl(data, tool_input, command)
        if denial is None and not _SF_LEVEL[0] and "runs" in _SF_STATE:
            denial = _sf_final()
        return denial
    except RecursionError:
        return _SHELL_NESTING_TOO_DEEP
    except _SfRefused as why:
        if _SF_LEVEL[0]:
            raise
        return _sf_reason(*why.args)
    except Exception:
        if _SF_LEVEL[0]:
            raise  # the outermost call answers
        return _sf_reason("generic", "reading it raised an error")


def _decide_script_files_impl(data: dict, tool_input: dict, command: str) -> dict | None:
    if _DC_ACTIVE[0]:
        return None  # a dynamic command-word copy: its words are the guard's, not the text's
    level = _SF_LEVEL[0]
    if not level and not _sf_wanted(command):
        return None  # H: no T-5505 form in the text (every script text is read in full)
    view, names, located = command, set(), False
    frame = _SF_STACK[-1] if _SF_STACK else None
    if frame is not None and frame[5] == command and frame[4] is not None and _sf_gate(
            command, True) and all(_sf_gate(t, False) for t in
                                   [_SF_STATE.get("top", "")] + [f[5] for f in _SF_STACK[:-1]]):
        view, names = _sf_located_view(command, frame[4], frame[2])
        located = view != command
    runs, writers, chains, refused, info = _sf_collect(view)
    if located:
        wview = _sf_located_view(command, frame[4], frame[2], loose=True)[0]
        if wview != view:
            writers = _sf_collect(wview)[1]
    st = _SF_STATE
    cwd0 = data.get("cwd") or ""
    if refused:
        raise _SfRefused("generic", "its code strings nest past the bounds this hook reads")
    if info["refuse"]:
        raise _SfRefused(*info["refuse"][0])
    if not runs and not level:
        st["bulk"].extend(info["bulk"])  # (a later text of this decide() may run a script)
        st["home"] = st["home"] or info["home"]
        st["inter"] = st["inter"] or info["inter"]
        st["envset"] = st.get("envset") or bool(info["envfiles"])
        if writers or info["pathdirs"]:
            cands = _fed_cands(cwd0, chains) or [cwd0]
            st["writers"].extend((w, tuple(cands)) for w in writers)
            st["pathdirs"].extend((d, tuple(cands)) for d in info["pathdirs"])
        return None
    if _SF_LEVEL[0] >= _FED_DEPTH and runs:
        raise _SfRefused("generic", f"scripts nest more than {_FED_DEPTH} deep")
    if len({r[0] for r in runs}) > _SF_MAX_FILES:
        raise _SfRefused("generic", f"it runs more than {_SF_MAX_FILES} script files")
    for word, _kind, dyn, _run in runs:
        if _sf_dynamic(word, dyn):
            raise _SfRefused("dyn", word)
    runs = list(dict.fromkeys(runs))
    cands = _fed_cands(cwd0, chains)
    # a sourced file's own directory changes count for what runs after it
    extra = []
    for word, kind, _dyn, _run in runs:
        if kind != "src" or cands is None:
            continue
        for cwd in cands:
            path = _sf_path(word, kind, cwd)
            if path and os.path.lexists(path):
                text = _sf_text(path, kind, cwd)
                if text and _FED_CHDIR_HINT.search(text):
                    chain = _fed_chain(_py_lex(text).cmds, 0)
                    if chain:
                        extra.append(tuple(chain))
    if extra:
        chains = tuple(dict.fromkeys(chains + tuple(extra)))
        cands = _fed_cands(cwd0, chains)
    if cands is None:
        raise _SfRefused("generic", f"it changes directory more than {_FED_MAX_CANDS} times")
    st["bulk"].extend(info["bulk"])
    st["home"] = st["home"] or info["home"]
    st["inter"] = st["inter"] or info["inter"]
    st["envset"] = st.get("envset") or bool(info["envfiles"])
    st["writers"].extend((w, tuple(cands)) for w in writers)
    st["pathdirs"].extend((d, tuple(cands)) for d in info["pathdirs"])
    if any(None in ch for ch in chains) and any(_sf_relative(r[0]) for r in runs if r[1] != "opd"):
        raise _SfRefused("generic", "it runs a relative script path after a change to a "
                                    "directory built at run time (`cd \"$d\"`, `cd -`, `popd`)")
    seen_real: set = set()
    for word, kind, _dyn, run in runs:
        found = False
        for cwd in cands:
            path = _sf_path(word, kind, cwd)
            if path is None or not os.path.lexists(path):
                continue
            found = True
            eff = kind
            if kind == "opd":  # T-5662: an operand of a command word that may be a shell
                eff = _sf_opd_kind(path)
                if eff is None:
                    continue
            if not os.path.isdir(path):
                st["runs"].append((word, path))
                if kind == "opd":
                    st.setdefault("opd", set()).add(path)
            text = _sf_text(path, eff, cwd)
            if text is None:
                continue
            if eff == "src" and located and (not _sf_gate(text, True) or (names and any(
                    re.search(r"(?<![\w$])" + re.escape(n) + r"\s*\+?=|"
                              + _SF_NAME_USE.format(re.escape(n)), text) for n in names))):
                raise _SfRefused("generic", f"the sourced `{word}` may change a path this hook "
                                            "read the script paths from (a cd, a function, a "
                                            "PATH change, or a variable it reassigns)")
            if run is not None:
                try:
                    text = run(text)
                except _FedUnknown:
                    raise _SfRefused("generic", f"`{word}` is fed to a command this hook cannot "
                                                "read it through") from None
            real = os.path.realpath(path)
            if any(f[1] == real and f[3] == cwd for f in _SF_STACK):
                continue  # the script runs itself: it is being judged at this cwd now
            cwd_only = real in seen_real
            seen_real.add(real)
            if not text.strip():
                continue
            denial = _sf_judge(data, tool_input, text, cwd, cwd_only,
                               (os.path.abspath(path), real, eff, cwd, _sf_typed(word, path)))
            if denial is not None:
                return denial
        if not found and not (kind == "opd" and not word.lower().endswith(_SF_SHELLISH_EXT)):
            raise _SfRefused("missing", word)
    return None


def _sf_stat(path: str):
    st = _SF_STATE
    st["stats"] += 1
    if st["stats"] > _SF_MAX_STATS:
        raise _SfRefused("generic", "the command names more files than the rewrite check reads")
    try:
        s = os.stat(path)
    except (OSError, ValueError):
        return None
    return s


def _sf_final() -> dict | None:
    """B, D: over every text judged in this decide() (the command and every script it ran),
    a write to any script in the run set (name, substring, inode, a directory above it, a
    run-time target), a multiply linked script in a command that writes, a bulk writer, a
    startup-dir change with an interactive shell, or a PATH entry the command writes. Read
    again only when a text added to what it reads."""
    st = _SF_STATE
    key = (len(st["runs"]), len(st["writers"]), len(st["bulk"]), st["home"], st["inter"],
           len(st["pathdirs"]), st.get("envset"))
    if st.get("final", (None,))[0] != key:
        st["final"] = (key, _sf_final_impl())
    return st["final"][1]


def _sf_final_impl() -> dict | None:
    got = _sf_final_core()
    # T-5662 r1: every script of the run set is an operand of a command word that may be a shell
    # (`$PY ok.sh`): the rewrite check fired on the operand the command names, not on a write
    st = _SF_STATE
    if got is not None and st.get("opd") and got == _sf_reason("rewrite") and all(
            path in st["opd"] for _w, path in st["runs"]):
        return _sf_reason("envhead")
    return got


def _sf_final_core() -> dict | None:
    st = _SF_STATE
    if st.get("envset") and st["inter"]:
        return _sf_reason("startup", "an `ENV=` file is set in one text and an interactive "
                                     "shell (`-i`, `-l`, `-s`, any zsh) starts in another; set "
                                     "`ENV=` on the interactive shell's own command")
    if st["home"] and st["inter"]:
        return _sf_reason("startup", "a `HOME=`/`ZDOTDIR=`/`XDG_CONFIG_HOME=`/`INPUTRC=` change "
                                     "with an interactive or login shell (`-i`, `-l`, "
                                     "`--login`, `-s`, any zsh) runs startup files from a "
                                     "directory the hook does not read")
    for d, _cands in st["pathdirs"]:
        for (word, dyn, _known, _redirect), cands in st["writers"]:
            if word is None or dyn:
                continue
            for cwd in cands:
                p = _py_resolve(word.rstrip("/") or "/", cwd if cwd and os.path.isabs(cwd) else None)
                if p and (p == d or p.startswith(d + "/") or d.startswith(p + "/")):
                    return _sf_reason("path", f"names `{d}`, a directory the same command "
                                              "writes to")
    if not st["runs"]:
        return None
    if st["bulk"]:
        return _sf_reason("bulk", f"`{st['bulk'][0]}`")
    names: set = set()
    paths: set = set()
    inodes: set = set()
    multi = False
    for word, path in st["runs"]:
        for p in (word.rstrip("/"), path):
            b = os.path.basename(p.rstrip("/"))
            if b:
                names.add(b)
        real = os.path.realpath(path)
        names.add(os.path.basename(real))
        paths.update({_py_norm(path), real})
        s = _sf_stat(path)
        if s is not None:
            inodes.add((s.st_dev, s.st_ino))
            multi = multi or (s.st_nlink > 1 and not os.path.isdir(path))
    names.discard("")
    anywrite = False
    for (word, dyn, known, redirect), cands in st["writers"]:
        if word is None:
            return _sf_reason("rewrite")
        if not (redirect and (word.startswith("/dev/") or word.startswith("&"))):
            anywrite = True
        if dyn or _SF_GLOB.search(word):
            last = word.rstrip("/").rsplit("/", 1)[-1]
            if known and (not last or last in names or "$" in last or _PY_SUBST in last
                          or "`" in last or _SF_GLOB.search(last)):
                return _sf_reason("rewrite")
            if not redirect and any(n in word for n in names):
                return _sf_reason("rewrite")
            continue
        for part in {word, word.partition("=")[2]}:
            w = part.rstrip("/")
            if not w:
                continue
            if os.path.basename(w) in names or (not redirect and any(n in part for n in names)):
                return _sf_reason("rewrite")
            for cwd in cands:
                base = cwd if cwd and os.path.isabs(cwd) else None
                p = w if w[:1] == "/" else (os.path.join(base, w) if base else None)
                if w[:1] == "~" or _py_static_prefix(w):
                    p = _py_resolve(w, base)
                if p is None:
                    continue
                if inodes:
                    s = _sf_stat(p)
                    if s is not None and (s.st_dev, s.st_ino) in inodes:
                        return _sf_reason("rewrite")
                if known:
                    for cand in {_py_norm(p), os.path.realpath(p)}:
                        if cand and cand not in ("/", ".") and any(
                                q.startswith(cand + "/") for q in paths):
                            return _sf_reason("rewrite")
    if multi and anywrite:
        return _sf_reason("rewrite")
    return None


def _overflow_checked(data: dict, denial: dict | None) -> dict | None:
    """`denial`, or for a subagent Bash call that would be allowed after
    some scan overflowed (T-5140 r2), the structural nesting deny."""
    if (denial is None and _SCAN_OVERFLOWED[0] and data.get("agent_type")
            and data.get("tool_name") == "Bash"):
        return _SHELL_NESTING_TOO_DEEP
    return denial


# --- T-5386: command-word renames (`hash -p`, BASH_CMDS, BASH_ALIASES, `alias`, `enable -f`) ---
# `hash -p PATH NAME` (and `BASH_CMDS[NAME]=PATH`) makes a later command word NAME run PATH;
# `BASH_ALIASES[NAME]=VALUE` (and `alias NAME=VALUE`) makes it read as VALUE; `enable -f LIB
# NAME` makes it a builtin loaded from LIB. Every rule here reads the command word as written,
# so `hash -p /usr/bin/git x; x push origin main` was a plain push the guard never saw. The
# renamed reading (`_decide_renamed`) judges, through every reading `_decide_readings` makes,
# a copy of the text with each word that is NAME (quotes and backslashes dropped: `'x'`, `\x`)
# replaced by PATH's basename (or VALUE), and a second copy with PATH itself when it lies
# outside the system bin dirs (a copy past twice the length cap fails closed). It only adds
# denials. Words are read without regard to quoting (a `-c` code string, an eval argument and
# a function body are read like the rest), and every use of NAME is replaced, wherever it is:
# a cost (an argument that spells NAME reads as the command) only in a text that renames.
# Fails closed (`_RN_UNREADABLE_DENY`), for a verb where it runs (r2, `_rn_find`):
#   * a NAME (or `BASH_*[KEY]`) built at run time or holding a blank (`' x '`), (T-5491 r1)
#     any run-time hash/enable word where bash reads options, an unparsed write of
#     BASH_CMDS/BASH_ALIASES (quote-split, `declare -n`, `printf -v`, `read`), a verb a brace,
#     glob or expansion builds (`{hash,}`): any later command word may be anything;
#   * a PATH or VALUE built at run time, a NAME given two values, an `enable -f` builtin:
#     the text denies if it uses NAME anywhere outside the words that define it.
# `hash` without `-p` (`hash git`, `hash -r`, `hash -t git`) only refreshes the path cache and
# is left alone. One forward pass per reading (linear).
# T-5598: read flatly, a word in a backtick body or a double-quoted eval / `-c` / ssh string is
# at the wrong quoting level (`eval "hash -p P \$(echo git)"` runs `$(echo git)`). Rule A
# (`_RN_ESC_EXPANSION`, everywhere): a word holding an escaped expansion start is built at run
# time, and an escaped quote or `$` is no literal first character. Rule B (`_rn_in_region`,
# inside a code region `_rn_find` tracks: a backtick body, a double-quoted code string, the
# arguments of eval/ssh/watch): any `$` or backtick makes a word run time and turns the T-5600
# narrowing off (printf only at its `-v` NAME: it writes nothing else). Deny-only, linear.
# r1: one level down quotes, backslashes, braces and globs are read again too, so a hash/enable
# operand or an alias NAME holding one is run time there (`_RN_REGION_RENAME`; the quote that
# closes a code string is trimmed, `_rn_trim_closer`); `eval --`, `builtin --`, `command -p`
# keep command position (`_RN_LEAD_OPTS`); region ends only grow, under a scan budget.
# r2: a declare-family word's NAME side (all of a nameref word) holding one of them in a
# region is run time; an alias word in an eval/ssh/watch argument outside one double-quoted
# string (`_rn_join_plain`) that does not stay one word once the join removes its quotes is
# unreadable (`eval alias x='1 git=/usr/bin/sudo'`); a BASH_CMDS/BASH_ALIASES key holding a
# brace or glob character is unreadable (`declare BASH_CMDS[{git,x}]=...` expands it).
_RN_ACTIVE = [False]
_RN_GATE = ("hash", "alias", "enable", "BASH_")
_RN_RUN = re.compile(r"[^\s;&|()<>`]+")
_RN_STOP = frozenset(" \t\n;&|()<>")
_RN_GLOB = frozenset("*?[{")
_RN_ARR = re.compile(r"(\$\{?[!#]?)?(?<!\w)BASH_(CMDS|ALIASES)(?!\w)")
# r2: the command-position reading (`_rn_find`)
_RN_TOK = re.compile(r"[;&|()`\n]|[^\s;&|()<>`]+")
_RN_SEP_TOKENS = frozenset(";&|()`\n")
_RN_LEAD_WORDS = _KEYWORD_WORDS | frozenset({"!", "time", "{", "}", "fi", "done", "esac",
                                             "builtin", "command", "eval"})
_RN_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_RN_TEXT_HEADS = frozenset({"echo", "printf", "grep", "egrep", "fgrep", "rg", "git"})
_RN_ARR_WRITER_HEADS = frozenset({"declare", "typeset", "local", "readonly", "export", "printf",
                                  "read", "mapfile", "readarray", "eval", "let"})
_RN_ARR_NAME = re.compile(r"BASH_(?:CMDS|ALIASES)(?!\w)")
_RN_ARR_READ = re.compile(r"\$\{?[!#]?BASH_(?:CMDS|ALIASES)(?!\w)")
_RN_VERBS = ("hash", "alias", "enable")
_RN_CODE_HEADS = frozenset({"eval", "watch", "ssh", "su", "sh", "bash", "dash", "zsh", "ksh"})
_RN_JOIN_HEADS = frozenset({"eval", "watch", "ssh"})  # T-5598: join their arguments as code
# T-5598 r1: options after which a lead word still runs the next word as its command (`eval
# -- hash ...`, `builtin -- hash`, `command -p -- hash`, `time -p hash`)
_RN_LEAD_OPTS = {"eval": frozenset({"--"}), "builtin": frozenset({"--"}),
                 "command": frozenset({"--", "-p"}), "time": frozenset({"--", "-p"})}
_RN_C_OPT = re.compile(r"-[A-Za-z]*c")
# T-5417: mapfile/readarray's `-C CALLBACK` (separate, or glued to its cluster: `-tCcb`, `-C=cb`)
_RN_CB_HEADS = frozenset({"mapfile", "readarray"})
_RN_CB_OPT = re.compile(r"-[A-BD-Za-z]*C")
_RN_CB_GLUED = re.compile(r"-[A-BD-Za-z]*C=?(.+)")
_RN_PARAM_OP = re.compile(r"\$\{?[!#]?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])(?::?[-=+?])?")
_RN_LETTER = re.compile(r"[A-Za-z]")
_RN_ALIAS_NAME = re.compile(r"[^\s$`'\"\\=]+")
_RN_UNSAFE_VALUE = frozenset("'\"\\$`")
_RN_OPTS = {"hash": "p", "enable": "f"}
_RN_CHAIN_MAX = 8
_RN_SYSTEM_BIN = frozenset({"/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin"})
_RN_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5386): this command renames a command word (`hash -p PATH "
    "NAME`, `BASH_CMDS[NAME]=`, `BASH_ALIASES[NAME]=`, `alias NAME=`, `enable -f LIB NAME`) "
    "with a name, path, value or option the guard cannot read (built at run time, or given "
    "two values), or a library-loaded builtin, and then uses a word it may rename; refused "
    "(fail-closed). Run the command by its own name or path instead. If the words are only "
    "text, search with the Grep tool or put a message in a file (`git commit -F <file>`)."
)
_RN_RUNTIME_ARG_DENY = _deny(
    "BLOCKED (worker-guard, T-5491): this command gives `hash` or `enable` an argument built "
    "at run time (`$c`, `\"$@\"`, `\"${a[@]}\"`, `$(...)`, a glob or a brace) where bash may "
    "read it as an option (`-p PATH NAME`, `-f LIB NAME`) that renames a later command word; "
    "refused (fail-closed). Put run-time names after `--`, where they cannot be options: "
    "`hash -- \"$name\"` (`for c in git jq; do hash -- \"$c\"; done`), or spell the arguments "
    "literally. `enable` takes literal arguments only."
)
_RN_RUNTIME_NAME_DENY = _deny(
    "BLOCKED (worker-guard, T-5491): this command writes a variable whose name is built at run "
    "time (`declare \"$n=v\"`, `local \"$@\"`, `export \"${p}_X=v\"`, `read \"$n\"`, "
    "`printf -v \"$n\"`, `mapfile \"$n\"`, or a nameref `declare -n r=\"$x\"`): it may be "
    "BASH_CMDS or BASH_ALIASES, which rename a command word; refused (fail-closed). Spell the "
    "variable name literally (`local x=\"$1\"` is fine; a run-time printf format goes after "
    "`--`: `printf -- \"$fmt\" \"$x\"`)."
)


class _RnUnknown(Exception):
    """A rename the guard cannot read: any later command word may be anything."""


class _RnRuntimeArg(_RnUnknown):
    """T-5491 r1: a hash/enable argument built at run time where bash may read it as an option
    (`_rn_hash_like`), or a variable name built at run time (`_rn_writer_args`): `args[1]` is
    the deny (`_RN_RUNTIME_ARG_DENY` / `_RN_RUNTIME_NAME_DENY`)."""


class _RnText:
    """The text renames are read from, with per-quote memos that keep unterminated-quote
    scans linear (a quote found missing from `i` on is missing from every later `i`)."""

    __slots__ = ("text", "n", "nofrom", "inner_until", "bt_open", "region_budget", "plain",
                 "plain_from")

    def __init__(self, text: str):
        self.text, self.n = text, len(text)
        self.nofrom = {"'": self.n + 1, '"': self.n + 1, "`": self.n + 1}
        # T-5598: the end of the code region (`_rn_find`) the reader is in: a word starting
        # before it is one quoting level down (`_rn_in_region`); `bt_open` memoizes, as
        # `nofrom` does, the first backtick-body start no bare backtick closes. r1: region
        # ends only grow, and `region_budget` bounds the characters their scans read (past
        # it the rest of the text is one region: deny-only, and linear)
        self.inner_until, self.bt_open = 0, self.n + 1
        self.region_budget = 4 * self.n + 4096
        # r2: the characters of eval/ssh/watch arguments that are not one double-quoted
        # string (`plain`, marked lazily), where the join removes the outer quotes before the
        # text is read again; every character from `plain_from` on counts as one
        self.plain, self.plain_from = None, self.n + 1


def _rn_mark_join(t: _RnText, words: list) -> None:
    """T-5598 r2: mark the join arguments `words` (as `_rn_args` reads them) that are not one
    double-quoted string as plain (`_rn_join_plain`). Marking costs no more than the scan that
    read them (bounded by `region_budget`)."""
    for a, e, *_rest in words:
        if t.text[a] == '"' and _rn_dq_end(t, a) == e:
            continue
        if t.plain is None:
            t.plain = bytearray(t.n + 1)
        t.plain[a:e] = b"\x01" * (e - a)


def _rn_join_plain(t: _RnText, a: int) -> bool:
    """T-5598 r2: True if position `a` lies in a plain eval/ssh/watch argument (above)."""
    return a >= t.plain_from or (t.plain is not None and a < t.n and t.plain[a] == 1)


def _rn_grow_region(t: _RnText, start: int, end: int) -> None:
    """T-5598 r1: a code region from `start` to `end` (the scan that found it read about
    `end - start` characters, charged to the budget; spent, the region runs to the end).
    Callers scan only while `inner_until < n`: a region reaching the end cannot grow, so the
    budget bounds the scanned characters to about `4 n` (linear)."""
    t.region_budget -= max(0, end - start)
    t.inner_until = max(t.inner_until, end if t.region_budget >= 0 else t.n)


# T-5598 (Rule A, escape level): a backslash before an expansion start protects it from ONE
# level of evaluation, and the reader is flat (it cannot tell the level a word is at): in a
# backtick body or a double-quoted eval / `-c` / ssh string, `\$(echo git)` and `` \`echo
# git\` `` are substitutions when the string runs. A word holding one (`` \` ``, or `\$` before
# a character that starts an expansion: `\$%`, `\$ ` start none) is built at run time.
_RN_ESC_EXPANSION = re.compile(r"\\(?:`|\$(?=[A-Za-z0-9_({@*#?$!'\"-]))")


# T-5598 r1: one level down, a quote, backslash, brace or glob character is read again too
# (`eval hash "'-p'"` runs `hash '-p'`; `bash -c "hash \"-p\" ..."`; `eval hash ... '{git,x}'`).
# The rename words (hash/enable operands, an alias NAME) are run time if they hold any of
# them in a code region; a variable writer's words keep `$`/backtick (its NAME side is read
# by the BASH_* word rule and the glob rule: `BASH_CMDS[git]` holds `[`).
_RN_REGION_RUNTIME = "$`"
_RN_REGION_RENAME = "$`'\"\\{}*?["


def _rn_in_region(t: _RnText, a: int, e: int, chars: str = _RN_REGION_RUNTIME) -> bool:
    """T-5598 (Rule B, quote level): True if the word `text[a:e]` starts inside a code region
    and holds one of `chars` anywhere: one level down the reader's quotes need not be the
    shell's (`eval hash '"-$q"'` runs `hash "-$q"`), so its value is built at run time."""
    if a >= t.inner_until:
        return False
    raw = _rn_trim_closer(t.text[a:e])
    return any(c in raw for c in chars)


def _rn_trim_closer(raw: str) -> str:
    """T-5598 r1: `raw` less a last quote character no other quote of its kind in it pairs
    with (unescaped, outside the other kind of quote): read flatly, the quote that closes a
    code string sticks to its last word (`eval "hash -r"` reads `-r"`), and one level down it
    is gone."""
    if not raw or raw[-1] not in "'\"":
        return raw
    q, k, n, quote, odd = raw[-1], 0, len(raw), "", False
    while k < n:
        c = raw[k]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            k += 1
        elif quote == '"':
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
            if k == n - 1:
                odd = c == q
        k += 1
    return raw[:-1] if odd else raw


def _rn_lit(t: _RnText, a: int, e: int) -> bool:
    """`_rn_literal_head` of the word `text[a:e]`, off (False) inside a code region (T-5598
    Rule B: there a quote may be no quote, so no first character is known to be literal)."""
    return a >= t.inner_until and _rn_literal_head(t.text[a:e])


def _rn_dq_end(t: _RnText, j: int) -> int:
    """T-5598: the end of the double-quoted string opening at `j` (`\\X` skipped; the end of
    the text when no quote closes it, memoized as `_rn_word` does)."""
    text, n = t.text, t.n
    k = j + 1 if j < t.nofrom['"'] else n
    while k < n and text[k] != '"':
        k += 2 if text[k] == "\\" else 1
    if k >= n:
        t.nofrom['"'] = min(t.nofrom['"'], j)
        return n
    return k + 1


def _rn_bt_end(t: _RnText, j: int) -> int:
    """T-5598: the end of the backtick body opening at `j`: past the next backtick no
    backslash escapes (a nested one is `` \\` ``); the end of the text when none closes it."""
    text, n = t.text, t.n
    if j >= t.bt_open:
        return n
    k = j + 1
    while k < n and text[k] != "`":
        k += 2 if text[k] == "\\" else 1
    if k >= n:
        t.bt_open = min(t.bt_open, j)
        return n
    return k + 1


# T-5491: a quoted expansion bash still splits into one word per element: `"$@"`, `"${@:2}"`,
# `"${@/x/y}"`, `"${a[@]}"`, `"${a[@]:1}"`, `"${!a[@]}"` (the keys), `"${!pre@}"` (the names).
# Not `"${#a[@]}"` (a count) or `"${a@Q}"` (one value transformed); the quoted `*` forms join
# into one word on IFS.
_RN_QUOTED_AT = re.compile(r"\$(?:@|\{(?:@|![A-Za-z_][A-Za-z0-9_]*@|!?[A-Za-z_][A-Za-z0-9_]*\[@\]))")


def _rn_word(t: _RnText, i: int) -> tuple:
    """(end, value, dyn, splits) of the shell word starting at `i`: its value with quotes
    and escapes removed; `dyn` if an expansion, substitution, glob or brace builds it; `splits`
    if one of those is unquoted (the word may split, or glob, into several), or (T-5491) is a
    quoted `@` expansion (`_RN_QUOTED_AT`: one word per element). An unterminated
    quote (a word at the end of a code string, `bash -c 'hash -p P x'`) ends the word at its
    run, quote characters dropped."""
    text, n = t.text, t.n
    j, out, dyn, splits = i, [], False, False
    if text.startswith("~", i):
        dyn = True  # T-5491 narrow fix: a leading unquoted `~` expands ($OLDPWD, $PWD, a home)
    while j < n:
        c = text[j]
        if c in _RN_STOP:
            break
        if c == "'":
            k = text.find("'", j + 1) if j < t.nofrom["'"] else -1
            if k < 0:
                t.nofrom["'"] = min(t.nofrom["'"], j)
                return _rn_word_run(t, i)
            out.append(text[j + 1:k])
            j = k + 1
        elif c == '"':
            k = j + 1 if j < t.nofrom['"'] else n
            while k < n and text[k] != '"':
                if text[k] == "\\":
                    k += 1
                elif text[k] in "$`":
                    dyn = True
                    if text[k] == "$" and _RN_QUOTED_AT.match(text, k):
                        splits = True  # T-5491: `"$@"`, `"${a[@]}"` expand to one word each
                k += 1
            if k >= n:
                t.nofrom['"'] = min(t.nofrom['"'], j)
                return _rn_word_run(t, i)
            out.append(text[j + 1:k].replace("\\", ""))
            j = k + 1
        elif c == "\\":
            if j + 1 < n and text[j + 1] != "\n":
                out.append(text[j + 1])
            j += 2
        elif c == "`" and (j >= t.nofrom["`"] or text.find("`", j + 1) < 0):
            # T-5491 r1: a backtick no later one closes ends the word: it closes a substitution
            # the word was read inside (`` `printf ls` -l ``; bash refuses an unclosed one)
            t.nofrom["`"] = min(t.nofrom["`"], j)
            break
        elif c == "$" and j + 1 < n and text[j + 1] in "\"'":
            dyn = True  # T-5491 r1: `$"..."` (`$'...'`) is one quoted word: it does not split
            j += 1
        elif (c == "$" and text.startswith(("$(", "${"), j)) or c == "`":
            # a substitution or expansion: its text is part of the word (built at run time)
            j = _rn_subst_end(text, j)
            dyn = splits = True
        else:
            if c in "$`" or c in _RN_GLOB:
                dyn = splits = True
            out.append(c)
            j += 1
    return min(j, n), "".join(out), dyn, splits


def _rn_subst_end(text: str, j: int) -> int:
    """The end of the `$(...)`, `${...}` or backtick substitution at `j` (brackets counted,
    quotes not read; the end of the text when it does not close)."""
    if text[j] == "`":
        k = text.find("`", j + 1)
        return len(text) if k < 0 else k + 1
    open_, close = text[j + 1], ")" if text[j + 1] == "(" else "}"
    depth, k, n = 0, j + 1, len(text)
    while k < n:
        ch = text[k]
        if ch == "\\":
            k += 2
            continue
        if ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return n


def _rn_word_run(t: _RnText, i: int) -> tuple:
    m = _RN_RUN.match(t.text, i)
    run = m.group() if m else ""
    dyn = any(c in run for c in "$`") or any(c in run for c in _RN_GLOB)
    return i + len(run), run.translate(_QUOTE_STRIP), dyn, dyn


def _rn_args(t: _RnText, i: int, region: str | None = _RN_REGION_RUNTIME) -> tuple:
    """(end, words) of the arguments from `i` to the end of the simple command: each word
    as `_rn_word` reads it, with its raw start; redirections (`2>/dev/null`) are skipped.
    T-5598: a word holding an escaped expansion (Rule A, `_RN_ESC_EXPANSION`) or, with
    `region` (a character set; None: off), one of those characters inside a code region
    (Rule B, `_rn_in_region`) is built at run time and may split (one level down it may be
    unquoted)."""
    text, n = t.text, t.n
    words = []
    while True:
        while i < n and (text[i] in " \t" or text.startswith("\\\n", i)):
            i += 2 if text[i] == "\\" else 1
        if i >= n or text[i] in ";&|()\n":
            return i, words
        if text[i] in "<>":
            while i < n and text[i] in "<>&-":
                i += 1
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] not in _RN_STOP:
                i = _rn_word(t, i)[0]
            continue
        end, value, dyn, splits = _rn_word(t, i)
        if end == i:
            return i, words
        if end < n and text[end] in "<>" and value.isdigit() and not dyn:
            i = end  # an fd number: the redirection is skipped next
            continue
        # (the lookahead may read the character after the word: `\$(` ends a word at `(`)
        if not splits and (_RN_ESC_EXPANSION.search(text, i, min(end + 1, n))
                           or (region and _rn_in_region(t, i, end, region))):
            dyn = splits = True  # T-5598: run time one quoting level down
        words.append((i, end, value, dyn, splits))
        i = end


class _RnFound:
    """What a text renames: `maps` NAME -> set of (kind, value) (kind "cmd": value is a path,
    "alias": value is text); `dyn` the names whose value cannot be read; `defs` how many
    words that are only NAME define it (`hash -p P NAME`), not counted as uses."""

    __slots__ = ("maps", "dyn", "defs")

    def __init__(self):
        self.maps: dict = {}
        self.dyn: set = set()
        self.defs: dict = {}

    def add(self, name: str, kind: str, value: str | None) -> None:
        if value is None:
            self.dyn.add(name)
        else:
            self.maps.setdefault(name, set()).add((kind, value))


# T-5600 (narrows T-5491 r1): a word whose FIRST character is a literal one that cannot open
# an option or a pattern is not an option however the rest expands (`"x$c"`, `"Total $n\n"`).
# Not one: a run-time first character (`$`, a backtick, `$'..'`, `$"..."`, `${x}`), `-`, `+`
# (an option prefix for declare/set), a glob or brace start (`*`, `?`, `[`, `{`) or an extglob
# start (`@`, `!`); nor an unquoted `~` (tilde expansion), nor (narrow fix) a word holding an
# unquoted glob, extglob or expansion anywhere (`_rn_unquoted_pattern`: nullglob may delete
# the word, so only a quoted run-time part qualifies: `"x$c"`, not `x$c`). It
# narrows only the OPTION-position rules (hash/enable words, the printf format); a run-time
# variable NAME stays closed whatever its first character (`"B$x[git]="`).
_RN_UNSAFE_HEAD = frozenset("-+*?[{@!")


def _rn_literal_head(raw: str) -> bool:
    """T-5600: True if the shell word `raw` starts, after quote removal, with a literal
    character outside `_RN_UNSAFE_HEAD` (empty quote pairs skipped; unsure means False)."""
    k, n = 0, len(raw)
    while k < n:
        c = raw[k]
        nxt = raw[k + 1] if k + 1 < n else ""
        if c in "'\"" and nxt == c:
            k += 2  # (an empty quote pair: the next character leads)
            continue
        if c == "'":
            ch = nxt
        elif c == '"':
            if nxt in ("$", "`", ""):
                return False
            ch = nxt
            if nxt == "\\":
                nn = raw[k + 2] if k + 2 < n else ""
                if nn == "\n" or not nn:
                    return False
                ch = nn if nn in '$`"\\' else "\\"
        elif c == "\\":
            if nxt in ("\n", "") or nxt in "$`\"'\\":
                return False  # (T-5598: one level down `\"`, `\$` are a quote, an expansion)
            ch = nxt
        elif c in "$`~":
            return False  # (an unquoted `~` expands: `~-` is $OLDPWD, `~+` $PWD, `~u` a home)
        else:
            ch = c
        return bool(ch) and ch not in _RN_UNSAFE_HEAD and not _rn_unquoted_pattern(raw)
    return False


def _rn_unquoted_pattern(raw: str) -> bool:
    """T-5491 narrow fix: True if word `raw` holds an unquoted glob character (`*`, `?`, `[`),
    extglob opener (`@(`, `!(`, `+(`) or expansion (`$`, a backtick: its value may bring the
    pattern, `c='*'; hash x$c`) anywhere: under `shopt -s nullglob` a pattern that matches
    nothing deletes the whole word, so the word after it leads (`hash zzq*$c -p ...`)."""
    k, n, quote = 0, len(raw), ""
    while k < n:
        c = raw[k]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            k += 1
        elif quote == '"':
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in "*?[$`" or (c in "@!+" and raw.startswith("(", k + 1)):
            return True
        k += 1
    return False


def _rn_hash_like(t: _RnText, cmd: str, i: int, found: _RnFound) -> int:
    """`hash`/`enable` arguments from `i`: a `-p PATH` (`-f LIB`) option makes each NAME a
    rename. Returns the end of the arguments.

    T-5491 r1 (structural): a word built at run time (an expansion, a substitution, a glob or
    a brace, quoted or not, splitting or not) where bash still reads options fails closed
    (`_RnRuntimeArg`): it may be, or split into, `-p PATH NAME`. Bash stops reading options at
    a literal `--`, at the first word that is not an option, and at `-` (a name), so a word
    after those is a name only: `hash -- "$c"` and `hash git "$c"` rename nothing. A `-p`
    path word that may split fails closed too (it may supply the NAME); one that cannot
    (`-p "$g" x`) makes NAME a rename the guard cannot read (denied where NAME is used).
    `enable` is held to literal words throughout (`enable -- "$b"` turns builtins on and off).

    T-5600: a run-time word whose first character is literal and safe (`_rn_literal_head`:
    `hash "x$c"`) cannot be an option: it is the first name (options end there), and for
    `enable` a builtin name. `hash "$c"` still fails closed (use `hash -- "$c"`).
    T-5598 r1: in a code region a word holding a quote, backslash, brace or glob is run time
    too (`_RN_REGION_RENAME`: `eval hash "'-p'" ...` runs `hash '-p' ...`)."""
    end, words = _rn_args(t, i, _RN_REGION_RENAME)
    if cmd == "enable" and any(w[3] and not _rn_lit(t, w[0], w[1]) for w in words):
        raise _RnRuntimeArg(end, _RN_RUNTIME_ARG_DENY)
    opt = _RN_OPTS[cmd]
    path, has_opt, k = None, False, 0
    while k < len(words):
        _a, _e, value, dyn, _s = words[k]
        if dyn:
            if _rn_lit(t, _a, _e):
                break  # T-5600: the first name (the NAME rule below reads it under `-p`)
            raise _RnRuntimeArg(end, _RN_RUNTIME_ARG_DENY)
        if value == "--":
            k += 1
            break
        if value.startswith("--"):
            return end  # (r2) a long option: the builtin refuses it and renames nothing
        if not value.startswith("-") or value == "-":
            break
        letters = value[1:]
        at = letters.find(opt)
        if at >= 0:
            has_opt = True
            rest = letters[at + 1:]
            if rest:
                path = (rest, False)
            elif k + 1 < len(words):
                if words[k + 1][4]:
                    raise _RnRuntimeArg(end, _RN_RUNTIME_ARG_DENY)  # (it may supply the NAME)
                path = (words[k + 1][2], words[k + 1][3])
                k += 1
        k += 1
    if not has_opt:
        return end
    for _a, _e, value, dyn, _s in words[k:]:
        if dyn or not value or any(c in _RN_STOP for c in value):
            raise _RnUnknown(end)  # (r2: `' x '`, a name no command word matches as read here)
        found.defs[value] = found.defs.get(value, 0) + 1
        if cmd == "enable" or path is None or path[1] or not os.path.basename(path[0]):
            found.add(value, "cmd", None)
        else:
            found.add(value, "cmd", path[0])
    return end


# T-5491 r1: commands that write a variable NAMEd by an argument. A name built at run time may
# be BASH_CMDS / BASH_ALIASES (`n=CMDS; declare "BASH_$n[git]=/usr/bin/sudo"`), which the array
# reading (`_rn_arrays`) only sees spelled. Per head, the letters of an option that takes a
# value (read: `-p PROMPT`), and those whose value is a NAME (read: `-a ARRAY`, printf: `-v`).
_RN_DECL_HEADS = frozenset({"declare", "typeset", "local", "readonly", "export"})
_RN_NAME_HEADS = {"read": ("dinNptu", "a"), "mapfile": ("dnOsuCc", ""),
                  "readarray": ("dnOsuCc", ""), "printf": ("", "v")}
_RN_WRITER_WORD = re.compile(r"(?<![\w-])(?:declare|typeset|local|readonly|export|read|mapfile"
                             r"|readarray|printf)(?![\w-])")
_RN_RUNTIME_CHARS = frozenset("$`*?[{")


def _rn_writer_args(t: _RnText, head: str, i: int) -> None:
    """T-5491 r1: raise `_RnRuntimeArg` if the `head` command whose arguments start at `i`
    writes a variable whose name is built at run time: a declare/typeset/local/readonly/export
    word whose name (the text before `=`, `+=` or `[`) is run time, or any run-time word with
    a nameref option (`-n`); a read/mapfile/readarray NAME (or `read -a` ARRAY), or a printf
    `-v` NAME, built at run time; or a run-time word where one of those may read options (it
    may be `-v NAME`, `-a NAME`), unless (printf) the words after it are literal.

    Rules per site: a declare-family word and a read/mapfile/readarray NAME (or `-a`/`-v`
    value) use the NAME rule (strict: `"B$x[git]="` may build BASH_CMDS); only the printf
    FORMAT word, an option position, is narrowed by T-5600 (`printf "Total $n\n"`).

    T-5598: printf writes only through `-v`, so in a code region (Rule B) only its `-v` NAME
    is read one quoting level down; its format and arguments are read as at top level (Rule
    A still applies: `printf \\"\\$f\\"` in `eval "..."` is `printf "$f"`)."""
    end, words = _rn_args(t, i, region=None if head == "printf" else _RN_REGION_RUNTIME)
    text = t.text
    if head in _RN_DECL_HEADS:
        nameref = False
        for a, e, value, dyn, _s in words:
            if a < t.inner_until:
                # T-5598 r2: one level down a quote, brace or glob in the NAME side (before
                # `=`; all of a nameref's word, whose value is a name) is read again:
                # `eval declare 'BASH_CMDS[{git,x}]=...'`, `eval declare -n r=BASH_'{,C}MDS'`.
                # The VALUE side keeps `$`/backtick (`export PATH=\"\$HOME/bin\"` is fine)
                # (a word with no `=` assigns nothing unless an expansion builds one, which
                # the `dyn` rule reads: `export MSG=\"hello world\"` splits flatly)
                raw = text[a:e]
                eq = raw.find("=")
                side = raw if nameref else raw[:eq]
                if eq >= 0 and any(c in _rn_trim_closer(side) for c in _RN_REGION_RENAME):
                    raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
            if not dyn:
                if value[:1] in "-+" and "=" not in value and "n" in value[1:]:
                    nameref = True
                continue
            raw = text[a:e]
            stop = min((x for x in (raw.find("="), raw.find("[")) if x >= 0), default=len(raw))
            if nameref or any(c in _RN_RUNTIME_CHARS for c in raw[:stop]):
                raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
        return
    vals, names = _RN_NAME_HEADS[head]
    k, n = 0, len(words)
    while k < n:
        a, e, value, dyn, splits = words[k]
        raw = text[a:e]
        if not dyn and value == "--":
            k += 1
            break
        if not raw.startswith("-") or raw == "-":
            if not dyn:
                break  # the first name (printf: the format; options end)
            if head == "printf" and _rn_literal_head(raw):
                return  # T-5600: a format with a literal safe first character is no `-v`
            # (read/mapfile: a NAME, held to the NAME rule whatever its first character)
            if head != "printf" or splits or (k + 1 < n and words[k + 1][3]):
                # it may be (or split into) an option and its NAME (`-v "$n"`, `"$@"`)
                raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
            return  # (printf: one run-time word, then literal ones: a `-v` names a literal)
        # an option cluster (a run-time character may stand only inside a glued value)
        x, take = 1, None
        while x < len(raw):
            ch = raw[x]
            if not ch.isalpha():
                raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
            if ch in vals or ch in names:
                take = ch
                break
            x += 1
        k += 1
        if take is None:
            continue
        glued = raw[x + 1:]
        if not glued:
            if k < n:
                if take in names and (words[k][3] or _rn_in_region(t, words[k][0], words[k][1])):
                    raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
                k += 1
        elif take in names and any(c in _RN_RUNTIME_CHARS for c in glued):
            raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)
        if take in names and head == "printf":
            return  # (`-v NAME`; the format follows)
    if head == "printf":
        return
    for _a, _e, _v, dyn, _s in words[k:]:
        if dyn:
            raise _RnRuntimeArg(end, _RN_RUNTIME_NAME_DENY)


def _rn_alias(t: _RnText, i: int, found: _RnFound) -> int:
    """`alias` arguments from `i`: each `NAME=VALUE` word is a rename."""
    end, words = _rn_args(t, i)
    for a, e, value, dyn, splits in words:
        if _rn_join_plain(t, a) and any(c in t.text[a:e] for c in "'\"\\"):
            # T-5598 r2: eval/ssh/watch remove these quotes before the text is read again: the
            # word is read again as the value they leave, and one that does not stay one word
            # there splits into a second NAME=VALUE (`eval alias x='1 git=/usr/bin/sudo'`);
            # `eval alias ll="'ls -l'"` stays one word (`ll='ls -l'`)
            again = _RnText(value)
            if _rn_word(again, 0)[0] < again.n:
                raise _RnUnknown(end)
        if a < t.inner_until:
            # T-5598 r1: in a code region the NAME side (before `=`) is read again one level
            # down (`bash -c "alias \"git\"=/usr/bin/sudo"`); its VALUE keeps the rules below
            # (a word with no `=` can become NAME=VALUE one level down only by an expansion,
            # which the `dyn` rule below reads: `alias g=\"git status\"` splits flatly)
            raw = t.text[a:e]
            eq = raw.find("=")
            if eq >= 0 and any(c in raw[:eq] for c in _RN_REGION_RENAME):
                raise _RnUnknown(end)
        if not dyn:
            name, eq, val = value.partition("=")
            if eq and any(c in _RN_STOP for c in name):
                raise _RnUnknown(end)  # (r2) a name no command word matches as read here
            if eq and name:
                found.add(name, "alias", None if any(c in val for c in _RN_UNSAFE_VALUE) else val)
            continue
        raw = t.text[a:e]
        eq = raw.find("=")
        m = _RN_ALIAS_NAME.fullmatch(raw[:eq]) if eq > 0 else None
        if m is None:
            if raw.startswith("-") and "=" not in raw and not splits:
                continue
            raise _RnUnknown(end)  # the word may be `NAME=VALUE` for any NAME
        found.add(m.group(), "alias", None)
    return end


def _rn_arrays(t: _RnText, found: _RnFound) -> list:
    """Writes of BASH_CMDS / BASH_ALIASES spelled plainly: `ARR[KEY]=VALUE`, `ARR+=([KEY]=VALUE
    ...)`. Returns where each parsed write starts (sorted). A `$ARR` / `${ARR[..]}` is a read;
    any other word naming an array is `_rn_find`'s to judge (r2)."""
    text, n = t.text, t.n
    skip, parsed = 0, []
    for m in _RN_ARR.finditer(text):
        if m.group(1) or m.start() < skip:
            continue
        kind = "cmd" if m.group(2) == "CMDS" else "alias"
        pos = m.end()
        if text.startswith("[", pos):
            close = text.find("]", pos, min(n, pos + 256))
            at = close + 1
            if close < 0 or not (text.startswith("=", at) or text.startswith("+=", at)):
                continue
            _rn_arr_item(t, kind, text[pos + 1:close], at + (1 if text[at] == "=" else 2), found)
            parsed.append(m.start())
            continue
        if text.startswith("=(", pos) or text.startswith("+=(", pos):
            j = pos + (2 if text[pos] == "=" else 3)
            while True:
                while j < n and text[j] in " \t\n":
                    j += 1
                if j >= n or text[j] == ")":
                    break
                close = text.find("]", j, min(n, j + 256)) if text[j] == "[" else -1
                if close < 0 or not text.startswith("=", close + 1):
                    raise _RnUnknown  # an item the guard does not read
                j = _rn_arr_item(t, kind, text[j + 1:close], close + 2, found)
            skip = j
            parsed.append(m.start())
    return parsed


def _rn_arr_item(t: _RnText, kind: str, key: str, at: int, found: _RnFound) -> int:
    """One `[KEY]=VALUE` of BASH_CMDS / BASH_ALIASES, the value read from `at`.
    T-5598 r2: a key holding a brace or glob character, quoted or not, is unreadable: as a
    declare/typeset/local argument (or one level down) the word is brace- and glob-expanded
    (`declare BASH_CMDS[{git,x}]=/usr/bin/sudo` writes BASH_CMDS[git]). A plain assignment
    word is not expanded, but the reader does not tell the two apart: it fails closed."""
    if any(c in key for c in "$`{}*?["):
        raise _RnUnknown
    key = key.translate(_QUOTE_STRIP)
    if not key or any(c in _RN_STOP for c in key):
        raise _RnUnknown  # r2: a key no command word can match as read here (`[ x ]`)
    if at < t.n and t.text[at] not in _RN_STOP:
        end, value, dyn, _s = _rn_word(t, at)
        # T-5598: a `$` or backtick, quoted or escaped, may be live one level down
        # (`eval "BASH_CMDS[x]=\$(echo /usr/bin/git)"`)
        dyn = dyn or "$" in t.text[at:end] or "`" in t.text[at:end]
    else:
        end, value, dyn = at, "", False
    if kind == "cmd" and (dyn or not os.path.basename(value)):
        found.add(key, kind, None)
    elif kind == "alias" and (dyn or any(c in value for c in _RN_UNSAFE_VALUE)):
        found.add(key, kind, None)
    else:
        found.add(key, kind, value)
    return end


def _rn_find(text: str) -> _RnFound:
    """Every rename `text` makes (see the comment above); raises `_RnUnknown`.

    r2: a rename verb (`hash`, `alias`, `enable`) is read where it runs: at command position
    (after a separator, a keyword, a prefix assignment or `builtin`/`command`/`eval`), or as
    the first word of a quoted string that is not the argument of a text command (`echo`,
    `grep`, `git commit -m`: `_RN_TEXT_HEADS`), which may be a code string (`bash -c 'hash
    ...'`). Elsewhere (`echo commit hash $sha`, `systemctl --user enable $u`, `grep alias $f`)
    its readable renames are still read, but nothing there fails closed. At command position a
    word an expansion, a brace or a glob builds whose letters name a rename verb (`{hash,}`,
    `ha{sh,}`) fails closed. A BASH_CMDS/BASH_ALIASES word the array reading did not parse
    (quote-split, `declare BA''SH_CMDS[x]=...`; `printf -v`, `read`, `mapfile`, a nameref)
    fails closed when its command is one that can write an array (`_RN_ARR_WRITER_HEADS`)."""
    t = _RnText(text)
    found = _RnFound()
    parsed = _rn_arrays(t, found) if "BASH_" in text else []
    comments = _rn_comments(text)
    skip, at_cmd, head, prev, lead = 0, True, None, "", None
    for m in _RN_TOK.finditer(text):
        raw = m.group()
        if raw in _RN_SEP_TOKENS:
            if raw == "`" and m.start() >= t.inner_until:
                # T-5598: a backtick body (one opening inside a region is its closer or a
                # nested, escaped one)
                _rn_grow_region(t, m.start(), _rn_bt_end(t, m.start()))
            at_cmd, head, prev, lead = True, None, "", None
            continue
        if m.start() < skip:
            continue  # an argument of the rename read last
        word = raw.translate(_QUOTE_STRIP)
        live = not (comments and _rn_parsed_in(comments, m.start(), m.start() + 1, True))
        if not at_cmd and raw[0] in "'\"" and (
                head in _RN_CODE_HEADS or _RN_C_OPT.fullmatch(prev)
                or (head not in _RN_TEXT_HEADS and head not in _RN_ARR_WRITER_HEADS)):
            # r3 (G1): a quoted string that may be code (`bash -c "{ hash ...; }"`) starts a
            # command: its lead words (`{`, `time`, `!`, `builtin`, `FOO=1`, `if`) keep that
            # position exactly as at top level
            at_cmd, lead = True, None
            if raw[0] == '"' and t.inner_until < t.n:
                # T-5598: a double-quoted code string (a single-quoted one is read verbatim);
                # r1: also inside an open region, whose end may be a false one (a backtick
                # between single quotes opens a "body" the tokenizer cannot tell from one)
                _rn_grow_region(t, m.start(), max(_rn_dq_end(t, m.start()), _rn_word(t, m.start())[0]))
        if at_cmd and word in _RN_JOIN_HEADS:
            # T-5598: eval / ssh / watch join their arguments and run them as code: the rest
            # of the simple command is one quoting level down (`eval hash '"-$q"'`); r1:
            # also inside an open region (ends only grow; `region_budget` keeps it linear).
            # r2: its arguments outside one double-quoted string are marked plain; once the
            # scans stop, everything after it counts as plain (fail closed)
            if t.inner_until < t.n:
                j_end, j_words = _rn_args(t, m.end(), region=None)
                _rn_grow_region(t, m.end(), j_end)
                if t.region_budget >= 0:
                    _rn_mark_join(t, j_words)
                else:
                    t.plain_from = min(t.plain_from, m.end())
            else:
                t.plain_from = min(t.plain_from, m.end())
        if not at_cmd and head in _RN_CB_HEADS:
            # T-5417: a mapfile/readarray `-C CALLBACK` runs as code in this shell: its first
            # word is at command position (`-C 'hash -p ...'`, `-tC hash`); a callback glued
            # to its cluster (`-C'hash ...'`, `-tC{hash,}`, `-C=hash`) that may start with a
            # rename verb fails closed
            if _RN_CB_OPT.fullmatch(prev):
                at_cmd = True
            elif live:
                glued = _RN_CB_GLUED.match(word)
                if glued and (glued.group(1) in _RN_VERBS or (
                        any(c in raw for c in "${}*?[") and _rn_spells_verb(glued.group(1)))):
                    raise _RnUnknown
        runs = heads = live and at_cmd
        assign = bool(_RN_ASSIGN.match(word))
        prev = word
        if at_cmd:
            if word in _RN_LEAD_WORDS or assign:
                if word == "eval":
                    head = word  # (its words are code: an assignment there runs)
                lead = word
            elif lead in _RN_LEAD_OPTS and word in _RN_LEAD_OPTS[lead]:
                pass  # T-5598 r1: `eval --`, `command -p`, `builtin --` keep command position
            else:
                head, at_cmd, lead = word, False, None
        if heads and not assign and any(c in raw for c in "${}*?[") and _rn_spells_verb(word):
            raise _RnUnknown  # a rename verb an expansion, brace or glob builds
        if runs and (word in _RN_DECL_HEADS or word in _RN_NAME_HEADS):
            _rn_writer_args(t, word, m.end())  # T-5491 r1 (its words are still read below)
        if word in _RN_OPTS or word == "alias":
            sub = _RnFound() if not runs else found
            try:
                if word == "alias":
                    skip = _rn_alias(t, m.end(), sub)
                else:
                    skip = _rn_hash_like(t, word, m.end(), sub)
            except _RnUnknown as e:
                if runs:
                    raise
                skip = e.args[0] if e.args else m.end()  # (past its arguments: linear)
            if sub is not found:  # not where it runs: only its readable renames
                for name, vals in sub.maps.items():
                    found.maps.setdefault(name, set()).update(vals)
                for name, k in sub.defs.items():
                    found.defs[name] = found.defs.get(name, 0) + k
            at_cmd = False
            continue
        if live and "BASH_" in word and _RN_ARR_NAME.search(word) and not _RN_ARR_READ.search(word) \
                and (head in _RN_ARR_WRITER_HEADS or (runs and assign)) \
                and not _rn_parsed_in(parsed, m.start(), m.end()):
            raise _RnUnknown  # an array write the guard does not read
    return found


def _rn_spells_verb(word: str) -> bool:
    """r2: True if `word` (quotes dropped), built by an expansion, a brace or a glob, may be a
    rename verb: with each parameter expansion's name and operator left out (`${v:-hash}` reads
    `hash}`; `$hashfile` reads empty), one of its brace expansions (`{hash,}`, `ha{sh,}`) is
    one, or, holding letters and no `/`, matches one as a glob (`h?sh`, `[h]ash`; not `*`)."""
    rest = _RN_PARAM_OP.sub("", word)
    if "{" not in rest and ("/" in rest or not any(c in rest for c in "*?[")):
        return rest.replace("}", "") in _RN_VERBS  # (a path is no builtin; `${v:-hash}`)
    if len(rest) > 256:
        return True  # (a brace or glob word this long at command position: fail closed)
    for cand in _rn_brace_expand(rest):
        cand = cand.replace("{", "").replace("}", "")
        if cand in _RN_VERBS:
            return True
        if any(c in cand for c in "*?[") and "/" not in cand and _RN_LETTER.search(cand) \
                and any(fnmatch.fnmatchcase(v, cand) for v in _RN_VERBS):
            return True
    return False


def _rn_brace_expand(word: str, limit: int = 64) -> list:
    """The brace expansions of `word` (`a{b,c}` -> `ab`, `ac`), at most `limit` of them; a
    brace with no top-level comma is kept as text."""
    out, todo = [], [word]
    while todo and len(out) + len(todo) <= limit:
        w = todo.pop()
        start = 0
        while True:
            i = w.find("{", start)
            if i < 0:
                out.append(w)
                break
            depth, j, commas = 0, i, []
            while j < len(w):
                if w[j] == "{":
                    depth += 1
                elif w[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif w[j] == "," and depth == 1:
                    commas.append(j)
                j += 1
            if j >= len(w) or not commas:
                start = i + 1
                continue
            bounds = [i] + commas + [j]
            todo.extend(w[:i] + w[a + 1:b] + w[j + 1:] for a, b in zip(bounds, bounds[1:]))
            break
    return out + todo


def _rn_parsed_in(parsed: list, a: int, e: int, ranges: bool = False) -> bool:
    """True if the array reading parsed a write starting inside [a, e); with `ranges`,
    `parsed` holds sorted (start, end) ranges and the test is whether `a` lies in one."""
    if ranges:
        k = bisect.bisect_right(parsed, (a, float("inf"))) - 1
        return k >= 0 and parsed[k][0] <= a < parsed[k][1]
    k = bisect.bisect_left(parsed, a)
    return k < len(parsed) and parsed[k] < e


def _rn_comments(text: str) -> list:
    """r2: the sorted [start, end) comment ranges `_scan` reads in `text` (a verb in one does
    not run), [] when it has none or cannot be scanned (then every word may run)."""
    if "#" not in text:
        return []
    scanned = _scan_cached(text, True)
    if not isinstance(scanned, _Scanned):
        return []
    return sorted(scanned.comments)


def _rn_resolve(found: _RnFound) -> tuple[dict, dict]:
    """(basename copy, path copy) replacements for each readable NAME; a NAME given two
    values joins `found.dyn`. An alias whose first word is another NAME is resolved through
    it, as bash expands it (at most `_RN_CHAIN_MAX` steps)."""
    one = {}
    for name, vals in found.maps.items():
        if name in found.dyn or len(vals) != 1:
            found.dyn.add(name)
        else:
            one[name] = next(iter(vals))
    base, full = {}, {}

    def expand(name: str, path: bool, depth: int = 0) -> str | None:
        kind, value = one[name]
        if kind == "cmd":
            return value if path else os.path.basename(value)
        head, sep, rest = value.lstrip(" \t").partition(" ")
        if head in found.dyn:
            return None
        if head in one and head != name:
            if depth >= _RN_CHAIN_MAX:
                return None
            sub = expand(head, path, depth + 1)
            return None if sub is None else sub + sep + rest
        return value

    for name in one:
        b, f = expand(name, False), expand(name, True)
        if b is None or f is None:
            found.dyn.add(name)
        else:
            base[name], full[name] = b, f
    return base, full


def _rn_rewrite(text: str, repl: dict) -> str:
    """`text` with each run that is a NAME of `repl` (quotes and backslashes dropped) replaced
    by its value; the run's quote characters are kept, in order, around it."""
    out, pos = [], 0
    for m in _RN_RUN.finditer(text):
        word = m.group()
        bare = word.translate(_QUOTE_STRIP)
        if bare not in repl:
            continue
        lead = len(word) - len(word.lstrip("'\""))
        rest = "".join(c for c in word[lead:] if c in "'\"")
        out.append(text[pos:m.start()])
        out.append(word[:lead] + repl[bare] + rest)
        pos = m.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _rn_uses(text: str, names: set, defs: dict) -> bool:
    """True if a run of `text` that is one of `names` is more than the words defining it."""
    seen: dict = {}
    for m in _RN_RUN.finditer(text):
        word = m.group().translate(_QUOTE_STRIP)
        if word in names:
            seen[word] = seen.get(word, 0) + 1
            if seen[word] > defs.get(word, 0):
                return True
    return False


def _decide_renamed(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5386: the renamed readings of `command` (see the comment above), or None."""
    view = command.replace("\\\n", "")
    flat = view.translate(_QUOTE_STRIP)
    if "$'" in view:
        view = _env_s_ansi_c(view)
        flat = view.translate(_QUOTE_STRIP)
    if not any(g in flat for g in _RN_GATE) and not any(c in flat for c in "{*?[") and not (
            ("$" in flat or "`" in flat) and _RN_WRITER_WORD.search(flat)):
        return None  # (r2: a brace or glob may build a verb, `ha{sh,}`, `h?sh`; T-5491 r1:
        # a variable writer whose name may be built at run time)
    try:
        found = _rn_find(view)
    except _RnUnknown as e:
        return e.args[1] if len(e.args) > 1 else _RN_UNREADABLE_DENY
    if not found.maps and not found.dyn:
        return None
    base, full = _rn_resolve(found)
    if found.dyn and _rn_uses(view, found.dyn, found.defs):
        return _RN_UNREADABLE_DENY
    copies = []
    # (the path copy only when a path lies outside the system bin dirs: the rules read
    # `/usr/bin/git` as `git`, which the basename copy already judges)
    if all(os.path.dirname(v) in _RN_SYSTEM_BIN or v == base.get(k) for k, v in full.items()):
        full = {}
    for repl in (base, full):
        if repl:
            copy = _rn_rewrite(view, repl)
            if copy != view and copy not in copies:
                copies.append(copy)
    for copy in copies:
        if len(copy) > 2 * BASH_COMMAND_MAX_CHARS:
            return _RN_UNREADABLE_DENY
        _RN_ACTIVE[0] = True
        try:
            denial = _decide_readings({**data, "tool_input": {**tool_input, "command": copy}})
        finally:
            _RN_ACTIVE[0] = False
        if denial is not None:
            return denial
    return None


# --- T-5417: a mapfile/readarray `-C CALLBACK` read as code ---
# `mapfile -C CODE -c N ARR` (readarray likewise) runs CODE in this shell every N lines, but
# every rule read CODE as an argument: `mapfile -C 'sudo ls #' -c 1 a <<< z` ran sudo. Bash
# runs it as `CODE INDEX 'LINE'` with mapfile's input still readable, so the input data is
# code too (`-C 'f(){ $2; }; f'`, `-C 'source /dev/stdin #'`, `-C 'hash -p /usr/bin/sudo'`).
# r1 rule (structural, `_decide_mapfile_cb`): a callback is ALLOWED only when it is a static,
# fully literal simple command whose command word does nothing with its arguments (`:`,
# `true`, `false`, `echo`: `_CB_SAFE`), with no expansion, quote, operator, redirection,
# assignment or comment, in a text that defines no function and holds no alias, enable, eval
# or source that could rename that word; every other callback fails closed (`_CB_DENY`).
# Each spelling is read: `-C CODE`, `-CCODE`, `-C=CODE` (bash runs `=CODE`; read as CODE, a
# superset), a cluster (`-tC CODE`), after the value of an option that takes one (`-c 1`,
# `-d x`). A mapfile/readarray word is matched anywhere (quotes dropped: `builtin`, `command`,
# `eval '...'`, `bash -c '...'` reach it), a superset, as is a brace or glob word that may build
# one (`{mapfile,}`, `m?pfile`: `_cb_spells_name`). Fails closed on a callback built at run time
# (`-C "$cb"`) and (r1) on ANY word built at run time, quoted or not, where an option may still
# stand (before the array name: `o=-C; mapfile "$o" CODE`, `"${q:-"-C"}"`), unless it is glued
# after a letter that takes a value (`-d"$x"`). One forward pass; each callback read once.
_CB_NAMES = frozenset({"mapfile", "readarray"})
_CB_TAKES = frozenset("dnOsuc")
_CB_SAFE = re.compile(r"[ \t]*(?::|true|false|echo)(?:[ \t]+[A-Za-z0-9_.,:/+%@=-]+)*[ \t]*")
_CB_RENAMERS = re.compile(r"\(\s*\)|(?<![\w-])(?:function|alias|enable|eval|source)(?![\w-])"
                          r"|(?:^|[\s;&|(){}])\.\s")
_CB_STATIC_PRE = re.compile(r"-[A-Za-z]*")
_CB_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5417): this command gives mapfile/readarray a `-C` callback (code "
    "the shell runs, with each input line as an argument) that the guard cannot read (built at "
    "run time, or an option word built at run time that may be `-C`); refused (fail-closed). "
    "Run the code as its own command instead."
)
_CB_DENY = _deny(
    "BLOCKED (worker-guard, T-5417): this command gives mapfile/readarray a `-C` callback (code "
    "the shell runs as `CALLBACK INDEX 'LINE'`, so the input becomes code) other than a plain "
    "`:`, `true`, `false` or `echo` with literal arguments; refused (fail-closed). Read the "
    "lines first (`mapfile -t arr < file`), then run each command on its own."
)


def _cb_callbacks(view: str) -> list | None:
    """T-5417: the static `-C` callbacks of each mapfile/readarray word in `view`, or None when
    one may be built at run time (r1: or a run-time word stands where an option may)."""
    t = _RnText(view)
    out = []
    nxt: dict = {}
    seen = set()  # (r1) (word start, state) pairs read once: every scan stays linear
    for m in _RN_TOK.finditer(view):
        if not _cb_spells_name(m.group()):
            continue
        i, state = m.end(), "opt"
        while True:
            word = _cb_next_word(t, i, nxt)
            if word is None or (word[0], state) in seen:
                break
            seen.add((word[0], state))
            a, i, value, dyn = word
            if state == "val":
                state = "opt"
                continue
            if state == "cb":
                if dyn:
                    return None
                out.append(value)
                state = "opt"
                continue
            if dyn:
                # r1: a word built at run time, quoted or not, may itself be `-C` (or a cluster
                # ending in it); only a static cluster whose value-taking letter comes before
                # any `C` makes the rest its value (`-d"$x"`)
                pre = _CB_STATIC_PRE.match(view, a)
                hit = next((ch for ch in (pre.group()[1:] if pre else "")
                            if ch == "C" or ch in _CB_TAKES), None)
                if hit is None or hit == "C":
                    return None
                continue
            if not value.startswith("-") or value in ("-", "--"):
                break  # the array name: no option follows it
            letters = value[1:]
            for x, ch in enumerate(letters):
                if ch == "C":
                    rest = letters[x + 1:]
                    if rest:
                        out.append(rest[1:] if rest[0] == "=" else rest)
                    else:
                        state = "cb"  # the callback is the next word
                    break
                if ch in _CB_TAKES:
                    if x + 1 == len(letters):
                        state = "val"  # its value is the next word
                    break
    return out


def _cb_next_word(t: _RnText, i: int, memo: dict) -> tuple | None:
    """T-5417 r1: (start, end, value, dyn) of the next argument word from `i` to the end of the
    simple command, as `_rn_args` reads it (redirections and fd numbers skipped), or None; each
    position read once (`memo`), so scans that meet share the rest."""
    text, n = t.text, t.n
    path, got = [], None
    while True:
        if i in memo:
            got = memo[i]
            break
        path.append(i)
        while i < n and (text[i] in " \t" or text.startswith("\\\n", i)):
            i += 2 if text[i] == "\\" else 1
        if i >= n or text[i] in ";&|()\n":
            break
        if text[i] in "<>":
            while i < n and text[i] in "<>&-":
                i += 1
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] not in _RN_STOP:
                i = _rn_word(t, i)[0]
            continue
        end, value, dyn, _splits = _rn_word(t, i)
        if end == i:
            break
        if end < n and text[end] in "<>" and value.isdigit() and not dyn:
            i = end
            continue
        got = (i, end, value, dyn)
        break
    for p in path:
        memo[p] = got
    return got


def _cb_spells_name(raw: str) -> bool:
    """T-5417: True if token `raw` (quotes dropped) is mapfile/readarray, or a brace or glob
    word that may build one (`{mapfile,}`, `ma{p,}file`, `m?pfile`; with letters, no `/`)."""
    word = raw.translate(_QUOTE_STRIP)
    if word in _CB_NAMES:
        return True
    if not any(c in word for c in "{*?[") or "/" in word or "$" in word or len(word) > 256:
        return False
    for cand in _rn_brace_expand(word):
        cand = cand.replace("{", "").replace("}", "")
        if cand in _CB_NAMES or (any(c in cand for c in "*?[") and _RN_LETTER.search(cand)
                                 and any(fnmatch.fnmatchcase(v, cand) for v in _CB_NAMES)):
            return True
    return False


def _decide_mapfile_cb(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5417: the mapfile/readarray `-C` callbacks of `command` held to the r1 allowlist (see
    the comment above), or None."""
    del data, tool_input  # (r1: the allowlist needs no reading of its own)
    view = command.replace("\\\n", "")
    flat = view.translate(_QUOTE_STRIP)
    if "mapfile" not in flat and "readarray" not in flat and not any(c in flat for c in "{*?["):
        return None  # (a brace or glob may build the name)
    if "$'" in view:
        # each `$'...'` as the single-quoted word bash makes of it (r1: still one word, so a
        # decoded `;` or space cannot end the callback early)
        view = _ENV_S_ANSI.sub(lambda m: shlex.quote(_env_s_ansi_c(m.group())), view)
    cbs = _cb_callbacks(view)
    if cbs is None:
        return _CB_UNREADABLE_DENY
    if not cbs:
        return None
    if not all(_CB_SAFE.fullmatch(cb) and cb.strip() for cb in cbs) or _CB_RENAMERS.search(flat):
        return _CB_DENY
    return None


# --- T-5407: a command word that is not a literal, read as git (gh) ---
# The push rules read `git` only where it is spelt: `n=git; $n push origin main`, `{git,} push`,
# `$(echo git) push`, `${G:-git} push`, `x() { git "$@"; }; x push` ran a push the guard
# allowed (`{gh,} pr merge 1` likewise). In a text that can spell `push` (`_push_formable`, or
# an ANSI-C escape), the dynamic reading (`_decide_dyncmd`) judges, through every reading
# `_decide_readings` makes (T-5386's renamed reading included), whether a copy pushes (`_decide`
# runs only the push rules on it: `"$PY" -m pytest -k push` is no quoted `"git"` command word)
# with each word that is not a literal replaced by `git`:
#   * a word an expansion or substitution builds (`$n`, `"$G"`, `${G:-git}`, `${x}t`, `$(..)`,
#     a backtick, a positional `$1`/`"$@"`), a brace word (`{git,}`, `g{i,}t`) or a glob word
#     (`g?t`, `[g]it`; not `[`/`[[`, not an assignment);
#   * a NAME the text defines as a function whose body forwards its arguments (`"$@"`, `$*`,
#     `$1`...) to git (a `git` word, a word that is not a literal, or another such NAME); a
#     body that does not close, or more than `_DC_MAX_DEFS` definitions, is not readable: the
#     NAME counts. (An alias is T-5386's: its renamed copy reads `alias x=git; x push` as
#     `git push`, and a function there that forwards to it is read here, on that copy.)
# The same with `gh` for a text that spells `pr merge`/`pr create`. Each such word is replaced
# wherever it stands, as the raw push rule reads `git push` wherever it stands, so a literal
# `git` that main denies is denied here in every spelling; words inside quotes are replaced
# too (a `-c` code string runs them). Literal command words keep today's judgement, and a text
# with no push word (or no `pr merge`) is not read again. Over-block (LOW, fail-closed): a
# word that is not a literal followed by `push` (`$TOOL push img`, `docker $FLAGS push`,
# `echo $x push`, `f() { echo "$@"; }; f push`). Deny-only; one linear pass per copy.
_DC_ACTIVE = [False]
_DC_STOP = frozenset(" \t\n;&|()<>")
_DC_GH_GATE = re.compile(r"(?<![\w-])pr\s+(?:merge|create)(?![\w-])")
_DC_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# (an assignment word; its subscript read at most 256 chars, so a run of `a[` stays linear)
_DC_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\s]{0,256}\])?\+?=")
_DC_BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_DC_GLOB_SKIP = frozenset({"[", "[[", "]]", "]"})
_DC_POSITIONAL = re.compile(r"\$(?:[@*1-9]|\{[#!]?(?:[@*]|[1-9]))")
_DC_PRE = re.compile(r"[$`{*?\[]|\(\s*\)|(?<![\w-])function(?![\w-])")
# (r2: a definition may open a quoted code string, `bash -c 'x() { git "$@"; }; x push'`)
_DC_FUNC = re.compile(r"(?:^|(?<=[\s;&|(){}'\"`]))(?:function\s+([^\s;&|()<>'\"\\$`={}]+)(?:\s*\(\s*\))?"
                      r"|([^\s;&|()<>'\"\\$`={}]+)\s*\(\s*\))")
_DC_MAX_DEFS = 64
# (r2: a forwarding NAME called with one of these argument words is read as that subcommand,
# wherever it stands: `x() { shift; git "$@"; }; x a push`, `git "$2"`, `git "${@:2}"`)
_DC_SEG_STOP = frozenset(";&|()\n")


def _dc_runs(text: str):
    """(start, end, literal, positional, plain) for each word-like run of `text` (runs end at a
    blank or a shell operator; a `$(..)`, `${..}` or backtick inside one is part of it,
    `_rn_subst_end`). `literal`: no expansion, substitution, brace or glob builds the run, or it
    is an assignment (`NAME=...`: no command word); `positional`: its only expansions are
    positional (`"$@"`, `$1`); `plain`: the run without its substitutions and escaped
    characters (its own quotes). Linear."""
    n, i = len(text), 0
    while i < n:
        if text[i] in _DC_STOP:
            i += 1
            continue
        start, dyn, other, plain = i, False, False, []
        while i < n:
            c = text[i]
            if c in _DC_STOP:
                break
            if c == "\\":
                if text[i + 1:i + 2] not in ("'", '"'):
                    plain.append(text[i + 1:i + 2])
                i += 2
                continue
            if c == "$" and i + 1 < n:
                d = text[i + 1]
                if d in "({":
                    other = other or d == "(" or not _DC_POSITIONAL.match(text, i)
                    i, dyn = _rn_subst_end(text, i), True
                    continue
                if d in "@*123456789":
                    i, dyn = i + 2, True
                    continue
                if d == "_" or d.isalpha():
                    i, dyn, other = _DC_NAME.match(text, i + 1).end(), True, True
                    continue
                if d == "0":  # (r2: `bash -c '$0 push' git`: `$0` is a word the caller names)
                    i, dyn, other = i + 2, True, True
                    continue
                if d in "?#$!-":
                    i += 2
                    continue
            if c == "`":
                i, dyn, other = _rn_subst_end(text, i), True, True
                continue
            plain.append(c)
            i += 1
        p = "".join(plain)
        if _DC_ASSIGN.match(text, start):
            dyn = False  # (an assignment, `x=$(..)`, `a[1]=*`: its value is no command word)
        elif not dyn and (_DC_BRACE.search(p) or (any(ch in p for ch in "*?[")
                                                  and p not in _DC_GLOB_SKIP and "=" not in p)):
            dyn = other = True
        yield start, i, not dyn, dyn and not other, p


def _dc_body(text: str, j: int) -> str:
    """The body of the function definition whose name and parens end at `j`: its `{..}` or `(..)`
    group, or the rest of the text when it has none or it does not close (not readable)."""
    n = len(text)
    while j < n and text[j] in " \t\n":
        j += 1
    if j < n and text[j] in "{(":
        # (`_rn_subst_end` reads the group whose opener follows its `j`; `text[j - 1]` is the
        # blank or `)` before it, never a backtick)
        return text[j:_rn_subst_end(text, j - 1)]
    return text[j:]


def _dc_names(text: str, target: str) -> set:
    """The NAMEs `text` defines as functions that may run `target` with the arguments they are
    given (every defined NAME past `_DC_MAX_DEFS` definitions)."""
    defs = []
    for m in _DC_FUNC.finditer(text):
        defs.append((m.group(1) or m.group(2), m.end()))
        if len(defs) > _DC_MAX_DEFS:
            return {name for name, _end in defs} | {
                f.group(1) or f.group(2) for f in _DC_FUNC.finditer(text, m.end())}
    # (each body read once: its literal words, whether one is not a literal)
    info = []
    for name, end in defs:
        body = _dc_body(text, end)
        words, nonlit = set(), False
        for s, e, literal, positional, _p in _dc_runs(body):
            if literal:
                words.add(body[s:e].translate(_QUOTE_STRIP).rsplit("/", 1)[-1])
            elif not positional:
                nonlit = True
        if _DC_POSITIONAL.search(body):
            info.append((name, words, nonlit))
    names: set = set()
    # (r2: to a fixpoint: a pass that adds nothing ends it, so at most len(info) + 1 passes,
    # `_DC_MAX_DEFS` or fewer; a chain whose callers are defined before their callees converges)
    for _ in range(len(info) + 1):
        grew = False
        for name, words, nonlit in info:
            if name not in names and (target in words or nonlit or words & names):
                names.add(name)
                grew = True
        if not grew:
            break
    return names


def _dc_copy(text: str, target: str) -> str:
    """`text` with each run that is not a literal, or is a NAME that may run `target`
    (`_dc_names`), replaced by `target`; as in `_rn_rewrite`, the run's quote characters are
    kept, in order, around it (only its own: not those inside a substitution, `$(cat <<'E')`,
    nor an escaped one)."""
    names = _dc_names(text, target) if "(" in text or "function" in text else set()
    runs = list(_dc_runs(text))
    sub: dict = {}
    segs: list = []
    if names:
        # (r2: a NAME that may run `target`, called with a `push` (`pr merge`, `pr create`)
        # argument word anywhere in its simple command, is read as `target push`: a wrapper
        # may shift or pick its arguments. One pass: each run's segment, each segment's words)
        seg, prev = 0, 0
        words: dict = {}
        for s, e, _literal, _positional, plain in runs:
            if any(c in _DC_SEG_STOP for c in text[prev:s]):
                seg += 1
            segs.append(seg)
            words.setdefault(seg, set()).add(plain.translate(_QUOTE_STRIP))
            prev = e
        for k, ws in words.items():
            if target == "git" and "push" in ws:
                sub[k] = " push"
            elif target == "gh" and "pr" in ws and ws & {"merge", "create"}:
                sub[k] = " pr " + ("merge" if "merge" in ws else "create")
    out, pos = [], 0
    for idx, (s, e, literal, _positional, plain) in enumerate(runs):
        run = text[s:e]
        if literal and (not names or run.translate(_QUOTE_STRIP) not in names):
            continue
        lead = len(run) - len(run.lstrip("'\""))
        out.append(text[pos:s])
        tail = sub.get(segs[idx], "") if literal and sub else ""
        out.append(run[:lead] + target + tail + "".join(c for c in plain[lead:] if c in "'\""))
        pos = e
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _decide_dyncmd(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5407: the dynamic command-word readings of `command` (see the comment above), or None."""
    if not _DC_PRE.search(command):
        return None
    targets = []
    if _push_formable(command) or _ANSI_C_ESCAPE.search(command):
        targets.append("git")
    if "pr" in command and _DC_GH_GATE.search(command.replace("\\\n", "").translate(_QUOTE_STRIP)):
        targets.append("gh")
    for target in targets:
        copy = _dc_copy(command, target)
        if copy == command:
            continue
        if len(copy) > 2 * BASH_COMMAND_MAX_CHARS:
            return _TOO_LONG
        _DC_ACTIVE[0] = True
        try:
            denial = _decide_readings({**data, "tool_input": {**tool_input, "command": copy}})
        finally:
            _DC_ACTIVE[0] = False
        if denial is not None:
            return denial
    return None


# --- T-5414: command-valued git config values, read as code ---
# git runs some config values through /bin/sh: an alias whose value starts with `!`, core.pager,
# core.editor, sequence.editor, a filter or diff driver, ... So `git -c alias.y='!sudo ls' y`,
# `git -c core.pager='sudo ls' log` and `git rebase --exec 'sudo ls' HEAD~1` ran a command the
# guard never read: to every rule here the value is an argument of git. The git config reading
# (`_decide_gitcfg`) finds, after each `git` word in every simple command (lexed, `_py_lex`, so
# quotes are already removed; code strings included, `_py_expand_code`), the values of
#   * `-c KEY=VALUE` / `-cKEY=VALUE`, and `git config [opts] KEY VALUE` (any positional pair),
#     for a KEY in `_gc_code_of` (case-insensitive): `alias.*` when VALUE starts with `!` (the
#     code is the rest), `credential.helper` likewise, and the keys whose whole value is a
#     command (`_GC_KEYS`, `_GC_KEY_RE`);
#   * `git rebase` `--exec CMD`, `--exec=CMD` (an abbreviation from `--ex`), `-x CMD`, `-xCMD`,
#     an `x` in a short cluster (`-ix CMD`);
# and judges the codes through every reading `_decide_readings` makes, twice: alone (each a line
# of its own), and appended to the command (a variable it exports, a cd before it). Deny-only:
# a code that would be allowed changes nothing. Fails closed (`_GC_UNREADABLE_DENY`): a
# `--config-env` for a command-valued KEY (the value is an environment variable's), a
# command-valued KEY or VALUE built at run time, a `-c` KEY built at run time, a core.hooksPath
# other than the default (it names hook scripts; `/dev/null`, no hooks, stays allowed), a code
# nested past `_FED_DEPTH` levels (a code may hold another `git -c`: each level reads only the
# codes no outer level judged, so a chain ends). Bounded to these keys inside a git invocation.
# r2: git passes the words after an alias name to the alias (`git -c alias.y='!git' y push`,
# `git -c alias.y=push y origin`), so each alias is also judged joined with the words of every
# git invocation in the command whose subcommand is its name (`_gc_alias_uses`).
_GC_ACTIVE = [0]  # the nesting level of the code reading being judged (0: none)
_GC_JUDGED: list = [set()]  # codes an outer level already judged
_GC_KEYS = frozenset({
    "core.pager", "core.editor", "sequence.editor", "core.sshcommand", "core.askpass",
    "core.fsmonitor", "diff.external", "gpg.program", "uploadpack.packobjectshook"})
_GC_KEY_RE = re.compile(r"pager\..+|diff\..+\.(?:textconv|command)|merge\..+\.driver"
                        r"|filter\..+\.(?:clean|smudge|process)|gpg\..+\.program"
                        r"|sendemail\..*cmd", re.S)
_GC_BANG_KEY_RE = re.compile(r"alias\..+|credential\.(?:.+\.)?helper", re.S)
_GC_HOOKS_DEFAULT = frozenset({"", ".git/hooks", "/dev/null"})
# git's global options that take the next word as their value (when not glued with `=`)
_GC_GIT_VALUE_OPTS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace",
                                "--super-prefix", "--attr-source"})
# `git config` options that take the next word as their value
_GC_CONFIG_VALUE_OPTS = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment",
                                   "--value"})
# (a short option whose value ends the cluster; all but `-S[KEYID]` take the next word when
# nothing is glued on)
_GC_REBASE_VALUE_SHORT = frozenset("sXCS")
_GC_GATE = re.compile(r"-c|config|rebase")
_GC_UNREADABLE = object()
_GC_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5414): this git command sets a config value git runs as a "
    "command (an `alias.*` starting with `!`, core.pager, core.editor, sequence.editor, "
    "core.sshCommand, a diff/merge/filter driver, credential.helper, ...) or core.hooksPath, "
    "and the guard cannot read it: it comes from `--config-env`, is built at run time, sets "
    "a non-default hooks path, or nests past the guard's depth; refused (fail-closed). Spell "
    "the value out literally, or run the command it names directly."
)


def _gc_code_of(key: str, value: str):
    """The code git runs for config KEY=VALUE, None when it runs none, or `_GC_UNREADABLE`
    (core.hooksPath set to a non-default path)."""
    k = key.lower()
    if k in _GC_KEYS or _GC_KEY_RE.fullmatch(k):
        return value
    if _GC_BANG_KEY_RE.fullmatch(k):
        return value[1:] if value.startswith("!") else None
    if k == "core.hookspath":
        return None if value in _GC_HOOKS_DEFAULT else _GC_UNREADABLE
    return None


def _gc_runs_code(key: str) -> bool:
    """True if a value of config KEY may be code git runs (for a value the guard cannot read)."""
    k = key.lower()
    return bool(k in _GC_KEYS or k == "core.hookspath" or _GC_KEY_RE.fullmatch(k)
                or _GC_BANG_KEY_RE.fullmatch(k))


def _gc_dynamic(c: _PyCmd, j: int, text: str) -> bool:
    """True if `text` (all or part of word `j`) may be built at run time."""
    return (j in c.dyn and any(ch in text for ch in "$`")) or _PY_SUBST in text or _PY_PWD_MARK in text


class _GcOut(list):
    """The codes `_gc_codes` collects, plus (T-5414 r2) each alias defined (`aliases`: (name
    lowercased, value)) and each git invocation's subcommand (`invs`: (command, word index))."""

    __slots__ = ("aliases", "invs")

    def __init__(self):
        super().__init__()
        self.aliases: list = []
        self.invs: list = []


def _gc_pair(c: _PyCmd, j: int, key: str, value: str, vj: int, out: list) -> bool:
    """Add the code of KEY=VALUE (`j`, `vj`: the words holding them) to `out`; False to fail
    closed."""
    if _gc_dynamic(c, j, key):
        return False
    if not _gc_runs_code(key):
        return True
    if _gc_dynamic(c, vj, value):
        return False
    code = _gc_code_of(key, value)
    if code is _GC_UNREADABLE:
        return False
    if code is not None:
        out.append(code)
    k = key.lower()
    if k.startswith("alias.") and value and isinstance(out, _GcOut):
        out.aliases.append((k[6:], value))  # r2: expanded with each use's own words
    return True


def _gc_config_codes(c: _PyCmd, k: int, out: list) -> bool:
    """`git config [opts] KEY VALUE` from word `k` on: each positional pair whose first word is
    a command-valued KEY."""
    words, pos = c.words, []
    n = len(words)
    while k < n:
        w = words[k]
        if w in _GC_CONFIG_VALUE_OPTS:
            k += 2
            continue
        if w.startswith("-") and w != "-":
            k += 1
            continue
        pos.append(k)
        k += 1
    for a, b in zip(pos, pos[1:]):
        if _gc_runs_code(words[a]) or _gc_dynamic(c, a, words[a]):
            if not _gc_pair(c, a, words[a], words[b], b, out):
                return False
    return True


def _gc_rebase_codes(c: _PyCmd, k: int, out: list) -> bool:
    """`git rebase` from word `k` on: each `--exec`/`-x` command."""
    words = c.words
    n = len(words)
    while k < n:
        w = words[k]
        if w == "--":
            break
        name, eq, val = w.partition("=")
        if len(name) >= 4 and name.startswith("--") and "--exec".startswith(name):
            at, code = (k, val) if eq else (k + 1, words[k + 1] if k + 1 < n else None)
            if code is not None:
                if _gc_dynamic(c, at, code):
                    return False
                out.append(code)
            k = at + 1
            continue
        if len(w) >= 2 and w[0] == "-" and w[1] != "-":
            for x in range(1, len(w)):
                ch = w[x]
                if ch == "x":
                    at, code = (k, w[x + 1:]) if x + 1 < len(w) else (k + 1, words[k + 1] if k + 1 < n else None)
                    if code is not None:
                        if _gc_dynamic(c, at, code):
                            return False
                        out.append(code)
                    k = at
                    break
                if ch in _GC_REBASE_VALUE_SHORT:
                    if x + 1 == len(w) and ch != "S":
                        k += 1  # its value is the next word
                    break
        k += 1
    return True


def _gc_git_codes(c: _PyCmd, j: int, out: list) -> bool:
    """The codes the git invocation at word `j` of `c` runs from its config values (into `out`);
    False to fail closed."""
    words = c.words
    n = len(words)
    k = j + 1
    while k < n:
        w = words[k]
        if w == "-c" or (w.startswith("-c") and len(w) > 2):
            at = k + 1 if w == "-c" else k
            if at >= n:
                return True
            kv = words[at] if w == "-c" else w[2:]
            key, _eq, value = kv.partition("=")  # (no `=`: a boolean true, no code)
            if not _gc_pair(c, at, key, value, at, out):
                return False
            k = at + 1
            continue
        if w == "--config-env" or w.startswith("--config-env="):
            at = k + 1 if w == "--config-env" else k
            if at >= n:
                return True
            kv = words[at] if w == "--config-env" else w[len("--config-env="):]
            key = kv.partition("=")[0]
            if _gc_runs_code(key) or _gc_dynamic(c, at, key):
                return False  # the value is an environment variable's: not readable
            k = at + 1
            continue
        if w in _GC_GIT_VALUE_OPTS:
            k += 2
            continue
        if w.startswith("-"):
            k += 1
            continue
        break
    if k < n:
        if isinstance(out, _GcOut):
            out.invs.append((c, k))
        if words[k] == "config":
            return _gc_config_codes(c, k + 1, out)
        if words[k] == "rebase":
            return _gc_rebase_codes(c, k + 1, out)
    return True


def _gc_codes(command: str):
    """The command-valued git config codes of `command` (see the comment above), in order and
    without repeats, or `_GC_UNREADABLE`."""
    flat = command.replace("\\\n", "").translate(_QUOTE_STRIP)
    env_on = _ge_gate(flat)  # T-5637: environment variables git or a pager runs
    git_on = "git" in flat and (env_on or bool(_GC_GATE.search(flat)))
    if not (git_on or env_on):
        return []
    if env_on and _GE_EXPANSION.search(flat):
        return _GE_UNREADABLE  # `${PAGER:=...}` sets it from text this reading cannot see
    lex = _py_lex(command)
    _py_expand_code(lex, skip_unknown=True)
    out = _GcOut()
    kv: dict = {}
    lits = _ge_literals(lex.cmds) if env_on else {}
    # `named`: a tracked name is spelled somewhere (r2: in the lexed words too, where `$'GIT_\x45DITOR'`
    # is decoded, or a `$'`/`$"` quoting is there for the decode to hide a name behind)
    named = env_on and (bool(_GE_NAMES_RE.search(flat)) or "$'" in flat or '$"' in flat
                        or any(_GE_NAMES_RE.search(w) for c in lex.cmds for w in c.words))
    for c in lex.cmds:
        if git_on:
            for j, w in enumerate(c.words):
                if os.path.basename(w) == "git" and not _gc_git_codes(c, j, out):
                    return _GC_UNREADABLE
        if env_on and not _ge_cmd(c, out, kv, lits, named):
            return _GE_UNREADABLE
    if kv and not _ge_pairs(kv, out):
        return _GE_UNREADABLE
    if out.aliases and _gc_alias_uses(out) is _GC_UNREADABLE:
        return _GC_UNREADABLE
    return [x for x in dict.fromkeys(out) if x.strip()]


def _gc_alias_uses(out: _GcOut):
    """T-5414 r2: git passes the words after an alias name to the alias: a `!` alias runs as
    `sh -c '<code> "$@"' NAME WORDS...`, any other expands to `git <value> WORDS...` (and git
    honours a `-c` inside it). For each alias defined (`-c`, `git config`, anywhere in the
    command) and each git invocation in the command whose subcommand is its name (git compares
    alias names case-insensitively), add that expansion to `out`. A use whose words are built at
    run time, or expansions past twice the command cap, fail closed (`_GC_UNREADABLE`)."""
    budget = 2 * BASH_COMMAND_MAX_CHARS
    by_name: dict = {}
    for name, value in dict.fromkeys(out.aliases):
        by_name.setdefault(name, []).append(value)
    for c, k in out.invs:
        values = by_name.get(c.words[k].lower())
        if not values:
            continue
        parts = []
        for t in range(k + 1, len(c.words)):  # (charged as it goes: no use is read twice for free)
            if _gc_dynamic(c, t, c.words[t]):
                return _GC_UNREADABLE
            parts.append(shlex.quote(c.words[t]))
            budget -= len(parts[-1]) + 1
            if budget < 0:
                return _GC_UNREADABLE
        args = " ".join(parts)
        for value in values:
            code = value[1:] if value.startswith("!") else "git " + value
            use = code + " " + args if args else code
            budget -= len(use)
            if budget < 0:
                return _GC_UNREADABLE
            out.append(use)
    return None


def _decide_gitcfg(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5414: the command-valued git config values of `command`, judged as code (see the
    comment above), or None."""
    codes = _gc_codes(command)
    if codes is _GC_UNREADABLE:
        return _GC_UNREADABLE_DENY
    if codes is _GE_UNREADABLE:
        return _GE_UNREADABLE_DENY
    outer = _GC_ACTIVE[0]
    if outer == 0:
        _GC_JUDGED[0] = set()
    judged = _GC_JUDGED[0]
    codes = [x for x in codes if x not in judged]
    if not codes:
        return None
    if outer >= _FED_DEPTH:
        return _GC_UNREADABLE_DENY
    judged.update(codes)
    alone = "\n".join(codes)
    copies = [alone, command + "\n" + alone]
    dc = _DC_ACTIVE[0]
    _GC_ACTIVE[0], _DC_ACTIVE[0] = outer + 1, False  # (a code is judged by every rule)
    try:
        for copy in copies:
            if len(copy) > 2 * BASH_COMMAND_MAX_CHARS:
                return _TOO_LONG
            denial = _decide_readings({**data, "tool_input": {**tool_input, "command": copy}})
            if denial is not None:
                return denial
    finally:
        _GC_ACTIVE[0], _DC_ACTIVE[0] = outer, dc
    return None


# --- T-5637: command-valued environment variables, read as code ---
# The environment sibling of T-5414. git (and the pagers and helpers it starts) run the value of
# some environment variables as a command: `GIT_SSH_COMMAND='sudo ls' git fetch`,
# `GIT_EDITOR='sudo ls' git commit`, `GIT_PAGER=... git log`, `PAGER`, `EDITOR`, `VISUAL`,
# `GIT_EXTERNAL_DIFF`, `GIT_ASKPASS`/`SSH_ASKPASS`, `GIT_PROXY_COMMAND`, `GIT_SSH`, `BROWSER`,
# `MANPAGER`, and `LESSOPEN`/`LESSCLOSE` (less runs them through a shell when a pager starts).
# To every other rule the value was an argument. `_gc_codes` (T-5414) now also reads, in every
# lexed simple command (code strings included), the assignments in these positions: the
# leading `NAME=value` words; the arguments of export/declare/typeset/readonly/local; and the
# words after `env` (past its options and nested wrappers: env, command, exec, nohup, nice,
# time, ...; an `env -S STRING` is judged as a code string). An assignment is read wherever the
# variable's name appears in the text (the gate does not need a `git` word: `PAGER=... man x`).
# A value is judged as code by every rule as a T-5414 code is (`_decide_gitcfg`), deny-only;
# `LESSOPEN`/`LESSCLOSE` lose their leading `|`/`||`/`-`. GIT_CONFIG_PARAMETERS (`'k'='v'`
# words) and GIT_CONFIG_COUNT with GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n (every KEY and VALUE of
# an index, anywhere in the command, are paired) go through `_gc_pair`, the T-5414 key check
# itself. Fails closed (`_GE_UNREADABLE_DENY`): a value built at run time; a variable name
# built at run time in an export-style or env argument; the variable set where the guard
# cannot read the value (`read`, `mapfile`, `printf -v`, `getopts`, `for`/`select`,
# `declare -n`, `${NAME:=...}`); and the path-valued variables (GIT_EXEC_PATH: helpers git
# execs; GIT_TEMPLATE_DIR: hooks `git init` copies; GIT_CONFIG_GLOBAL/SYSTEM: a config file)
# off their default values, as core.hooksPath is.
_GE_CODE_VARS = frozenset({
    "GIT_SSH_COMMAND", "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EDITOR",
    "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_EXTERNAL_DIFF", "PAGER", "EDITOR", "VISUAL", "BROWSER",
    "MANPAGER", "LESSOPEN", "LESSCLOSE"})
_GE_PATH_VARS = {
    "GIT_EXEC_PATH": frozenset({"/usr/lib/git-core", "/usr/libexec/git-core"}),
    "GIT_TEMPLATE_DIR": frozenset({"", "/dev/null", "/usr/share/git-core/templates"}),
    "GIT_CONFIG_GLOBAL": frozenset({"/dev/null", "~/.gitconfig", "$HOME/.gitconfig",
                                    "${HOME}/.gitconfig", os.path.expanduser("~/.gitconfig")}),
    "GIT_CONFIG_SYSTEM": frozenset({"/dev/null", "/etc/gitconfig"}),
}
_GE_NAMES = sorted(_GE_CODE_VARS | set(_GE_PATH_VARS) | {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"})
_GE_ALT = "|".join(_GE_NAMES) + r"|GIT_CONFIG_(?:KEY|VALUE)_\d+"
_GE_TRACKED = re.compile(r"(?:" + _GE_ALT + r")\Z")
# the gate: a tracked name, or an `=` with a word that can set a variable whose name the text
# does not spell (`env "$k=$v" git log`)
_GE_NAMES_RE = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(_GE_NAMES) + r"|GIT_CONFIG_(?:KEY|VALUE)_)")
_GE_SETTER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:export|declare|typeset|readonly|local|env)(?![A-Za-z0-9_])")
_GE_DYN_TOKEN = re.compile(r"(\$\{[^}]*\}|\$[A-Za-z0-9_@*#?!$-]+|[\0\1`]|\$\()")
_GE_VAR_TOKEN = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\Z")
_GE_LIT_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)\Z", re.S)
_GE_LIT_MAX = 8  # literal values kept per variable name


_GE_ENV_RE = re.compile(r"(?<![A-Za-z0-9_])env(?![A-Za-z0-9_])")


# r2: a runner that substitutes or appends arguments to the command it runs: with an `env` among
# its words, the words it supplies (`xargs -a f -I{} env {} git commit`) may be a NAME=VALUE
_GE_RUNNERS = frozenset({"xargs", "parallel", "find", "fd", "entr", "watch"})
_GE_RUNNER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:xargs|parallel|find|fd|entr|watch)(?![A-Za-z0-9_])")
# every tracked name holds one of these (a substring test before the regexes: `_GE_NAMES_RE`
# tries ~25 alternatives at each position of a large text)
_GE_KEYS = ("GIT_", "PAGER", "EDITOR", "VISUAL", "BROWSER", "ASKPASS", "LESS")


def _ge_gate(flat: str) -> bool:
    # (r1: an `env` with a run-time word, which may supply a NAME=VALUE: `env $(cat f) git`;
    # r2: an `env` and a runner)
    if any(k in flat for k in _GE_KEYS) and _GE_NAMES_RE.search(flat):
        return True
    if "=" in flat and _GE_SETTER_RE.search(flat):
        return True
    return "env" in flat and bool(_GE_ENV_RE.search(flat) and (
        ("$" in flat or "`" in flat) or _GE_RUNNER_RE.search(flat)))


def _ge_literals(cmds) -> dict:
    """NAME -> the literal values the command text assigns it (None: a value built at run time,
    or past `_GE_LIT_MAX` of them): what `$NAME` in a variable name may stand for."""
    lits: dict = {}
    for c in cmds:
        for j, w in enumerate(c.words):
            m = _GE_LIT_ASSIGN.match(w)
            if m is None:
                continue
            seen = lits.setdefault(m.group(1), set())
            if seen is None:
                continue
            if _gc_dynamic(c, j, w) or len(seen) >= _GE_LIT_MAX:
                lits[m.group(1)] = None
            else:
                seen.add(m.group(2))
    return lits


def _ge_may_name(text: str, lits: dict) -> bool:
    """True if `text` (the part of a word before its `=`, run-time pieces left as written) may
    spell a tracked variable's name: a `$x` whose literal values the command assigns stands for
    them (`x=BASHOPT; env ${x}S=...`), any other run-time piece for anything."""
    pieces = _GE_DYN_TOKEN.split(text)
    if len(pieces) == 1:
        return False
    parts = []
    for k, piece in enumerate(pieces):
        if k % 2 == 0:
            parts.append(re.escape(piece))
            continue
        m = _GE_VAR_TOKEN.match(piece)
        vals = lits.get(m.group(1)) if m else None
        parts.append("(?:" + "|".join(re.escape(v) for v in sorted(vals)) + ")" if vals else ".*")
    pat = re.compile("".join(parts), re.S)
    return any(pat.fullmatch(name) for name in _GE_SAMPLES)
_GE_SAMPLES = _GE_NAMES + ["GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]
_GE_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?(\+?)=", re.S)  # (`NAME[0]=` is NAME's value)
_GE_KV = re.compile(r"GIT_CONFIG_(KEY|VALUE)_(\d+)\Z")
_GE_EXPANSION = re.compile(r"\$\{(?:" + _GE_ALT + r")(?:\[[^\]]*\])?:?=")
_GE_DECLARERS = frozenset({"export", "declare", "typeset", "readonly", "local"})
_GE_SETTERS = frozenset({"read", "mapfile", "readarray", "getopts"})
_GE_WRAPPERS = frozenset({"env", "command", "builtin", "exec", "nohup", "nice", "time", "setsid",
                          "timeout", "stdbuf", "ionice"})
_GE_ENV_ARG_OPTS = frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0"})
_GE_PAIR_MAX = 256  # KEY x VALUE combinations judged per index
_GE_DYN = object()  # a GIT_CONFIG_KEY_n/VALUE_n built at run time
_GE_UNREADABLE = object()
_GE_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5637): this command sets an environment variable that git or a "
    "pager runs as a command (GIT_SSH_COMMAND, GIT_EDITOR, GIT_PAGER, PAGER, EDITOR, "
    "GIT_EXTERNAL_DIFF, *ASKPASS, LESSOPEN, ...), injects git config through GIT_CONFIG_*, or "
    "points GIT_EXEC_PATH / GIT_TEMPLATE_DIR / GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM off their "
    "defaults, and the guard cannot read it: the value (or the variable name) is built at run "
    "time, is set by read/mapfile/printf -v/a ${NAME:=...} expansion, or is a path the guard "
    "cannot vouch for; refused (fail-closed). Spell the value out literally, or run the "
    "command it names directly."
)


def _ge_tracked_name(word: str) -> bool:
    return bool(_GE_TRACKED.match(word.partition("[")[0]))


def _ge_value(c: _PyCmd, j: int, name: str, value: str, out: list, kv: dict,
              append: bool = False) -> bool:
    """Judge one `NAME=value` (word `j` of `c`): add its code to `out` (or the config pair to
    `kv`); False to fail closed. r1: `NAME+=value` on a tracked name fails closed: the shell
    assembles the value from pieces this reading judges one at a time (`E=s E+=udo`)."""
    if append and _ge_tracked_name(name):
        return False
    dyn = _gc_dynamic(c, j, c.words[j])
    k = _GE_KV.match(name)
    if k is not None:
        kv.setdefault((k.group(1), k.group(2)), {}).setdefault(_GE_DYN if dyn else value, (c, j))
        return True
    if not (name in _GE_CODE_VARS or name in _GE_PATH_VARS or name == "GIT_CONFIG_PARAMETERS"):
        return True
    if dyn:
        return False
    if name in _GE_PATH_VARS:
        return value in _GE_PATH_VARS[name]
    if name == "GIT_CONFIG_PARAMETERS":
        try:
            toks = shlex.split(value)
        except ValueError:
            return False
        return all(_gc_pair(c, j, *tok.partition("=")[::2], j, out) for tok in toks)
    if name in ("LESSOPEN", "LESSCLOSE"):
        value = value.lstrip("|-")
    if value.strip():
        out.append(value)
    return True


def _ge_declare(c: _PyCmd, k: int, out: list, kv: dict, lits: dict, named: bool = True,
                nameref_ok: bool = False) -> bool:
    """The arguments of export/declare/... from word `k` on. `named`: the command text spells a
    tracked variable's name somewhere. `nameref_ok`: `-n` is not a nameref flag here (export,
    readonly: `export -n` only drops the export attribute)."""
    words, nameref = c.words, False
    for j in range(k, len(words)):
        w = words[j]
        if w.startswith("-") and w != "-":
            nameref = nameref or (not nameref_ok and not w.startswith("--") and "n" in w)
            continue
        m = _GE_ASSIGN.match(w)
        base = m.group(1) if m else w.partition("=")[0]
        if nameref and (_ge_tracked_name(base) or named or m is None or _gc_dynamic(c, j, w)):
            # r1: the name refers to another variable, so a later `ref=value` is a write to the
            # target, and this reading cannot follow it: fails closed when the command spells a
            # tracked name anywhere (the target may be it: `declare -n r=GIT_EDITOR`, `r=GIT_EDITOR`
            # later, `x=GIT_EDITOR; declare -n x`), when the target is built at run time or when
            # none is given (`declare -n r` takes the variable's own value as target).
            return False
        if m:
            if not _ge_value(c, j, m.group(1), w[m.end():], out, kv, bool(m.group(2))):
                return False
        elif _gc_dynamic(c, j, base) and _ge_may_name(base, lits):
            return False  # (a name built at run time: `export "$k=v"`, `export $(echo PAGER=x)`)
    return True


def _ge_cmd(c: _PyCmd, out: list, kv: dict, lits: dict, named: bool = True) -> bool:
    """T-5637: the environment assignments of one simple command; False to fail closed."""
    words, n = c.words, len(c.words)
    if n >= 2 and words[0] in ("for", "select") and _ge_tracked_name(words[1]):
        return False
    i = 0
    while i < n and words[i] in _PY_RESERVED:
        i += 1
    while i < n and (m := _GE_ASSIGN.match(words[i])):
        if not _ge_value(c, i, m.group(1), words[i][m.end():], out, kv, bool(m.group(2))):
            return False
        i += 1
    while i < n:
        b = os.path.basename(words[i])
        if b in _GE_DECLARERS:
            return _ge_declare(c, i + 1, out, kv, lits, named, b in ("export", "readonly"))
        if b in _GE_SETTERS:
            return not any(_ge_tracked_name(w) for w in words[i + 1:])
        if b == "printf":  # `printf -v NAME ...`
            return not any(w == "-v" and t + 1 < n and _ge_tracked_name(words[t + 1])
                           or w.startswith("-v") and _ge_tracked_name(w[2:])
                           for t, w in enumerate(words) if t > i)
        if b in _GE_RUNNERS and any(os.path.basename(w) == "env" for w in words[i + 1:]):
            return False  # (r2: the words the runner supplies may be env's NAME=VALUE)
        if b not in _GE_WRAPPERS:
            # (an `env` further on: `xargs -I{} env A=1 cmd`, `find -exec env A=1 cmd \;`)
            nxt = next((t for t in range(i + 1, n) if os.path.basename(words[t]) == "env"), None)
            if nxt is None:
                break
            i = nxt
            continue
        is_env = b == "env"
        i += 1
        while i < n:
            w = words[i]
            if (m := _GE_ASSIGN.match(w)):
                if not _ge_value(c, i, m.group(1), w[m.end():], out, kv, bool(m.group(2))):
                    return False
                i += 1
            elif is_env and "=" not in w and not w.startswith("-") and i in c.dyn:
                # r1: a run-time word in env's argument position can split into (or be) a
                # NAME=VALUE the shell supplies (`env $(cat envfile) git commit`); the text of
                # its value is not visible, so it cannot be judged
                return False
            elif is_env and "=" in w and _gc_dynamic(c, i, w.partition("=")[0]) \
                    and _ge_may_name(w.partition("=")[0], lits):
                return False
            elif w == "--":
                i += 1
            elif is_env and (w in ("-S", "--split-string") or w.startswith("--split-string=")
                             or (w.startswith("-S") and not w.startswith("--"))):
                s = words[i + 1] if w in ("-S", "--split-string") and i + 1 < n else (
                    w.partition("=")[2] if w.startswith("--") else w[2:])
                at = i + 1 if w in ("-S", "--split-string") else i
                if s and _gc_dynamic(c, at, s):
                    return False
                if _GE_NAMES_RE.search(s):
                    out.append(s)  # (the other rules read an env -S string's other words)
                i = at + 1
            elif w.startswith("-") and w != "-":
                i += 2 if is_env and w in _GE_ENV_ARG_OPTS else 1
            elif not is_env and w.rstrip("smhd").isdigit():
                i += 1  # `nice -n 5`, `timeout 5`
            else:
                break
    return True


def _ge_pairs(kv: dict, out: list) -> bool:
    """Pair each GIT_CONFIG_KEY_n with each GIT_CONFIG_VALUE_n and judge them as `-c KEY=VALUE`."""
    for (kind, idx), keys in kv.items():
        if kind != "KEY":
            continue
        values = kv.get(("VALUE", idx), {})
        if len(keys) * len(values) > _GE_PAIR_MAX:
            return False
        for key, (c, j) in keys.items():
            if key is _GE_DYN:
                return False
            if not _gc_runs_code(key):
                continue
            for value in values:
                if value is _GE_DYN or not _gc_pair(c, j, key, value, j, out):
                    return False
    return True


# --- T-5441: a trap ACTION read as code ---
# `trap ACTION SIGSPEC...` runs ACTION as code in this shell when a signal or pseudo-signal
# fires (EXIT, ERR, DEBUG, RETURN, INT, ...), but every rule read ACTION as an argument:
# `trap 'sudo ls' EXIT` ran sudo at exit. The trap reading (`_decide_trap`) judges each action,
# as T-5414 judges a git config code, through every reading `_decide_readings` makes: alone,
# and after the command (so a function, alias or rename the command makes applies to it). The
# action is the first word after trap's options (or after `--`) when at least one word (a
# signal spec) follows it; `trap -p`, `trap -l`, `trap - SIG` (reset) and `trap '' SIG`
# (ignore) have none. Actions come from two readings, deny-only:
# (B) the lexed commands, code strings included (`eval`, `bash -c`, the wrappers:
#     `_py_expand_code`), whose command word is trap past `builtin`/`command`
#     (`_py_head_index`), each `$'...'` decoded by the lexer;
# (A) a superset over the raw text: every token that is `trap` once quotes are dropped, or a
#     brace or glob word that may build it (`{trap,}`, `t?ap`: `_trap_spells_name`).
# The action is judged as eval judges its string: `trap 'rm -f "$tmp"' EXIT` reads as
# `eval 'rm -f "$tmp"'` does (allowed). Fails closed (`_TRAP_UNREADABLE_DENY`) on a word built
# at run time up to and including the action's place (`trap "$a" EXIT`, `trap "rm $f" EXIT`
# as `eval "rm $f"` is, `o=--; trap $o CODE EXIT`; (A) adds an unquoted glob or brace, which
# may expand to file names), on a lexer tail or frame limit in a text that names trap, and
# past `_TRAP_MAX` distinct actions, `_FED_DEPTH` levels or the size budget.
# r1: (A) leaves out an action that still holds a quote or backslash (the token may have been
# read out of phase, inside a quoted string); such an action that (B) did not read too (`time
# -p trap "sudo 'ls'" EXIT` before (B) skipped `time -p`; `watch -n1 "trap \"sudo 'ls'\" EXIT"`,
# `ssh h "trap ..."`, whose strings (B) does not expand) fails closed. It counts as read when
# (B) read it as is, in the command or in a heredoc body (whose trap actions are then judged
# too, as a fed shell would run them), or with one double-quote level of `\\` undone
# (`_TRAP_DQ_ESC`: (A) reads `bash -c "trap 'rm -f \\"\\$t\\"' EXIT"` out of phase, (B)
# reads `rm -f "$t"` in the code string); then the action as (A) read it is judged too, so
# whichever of the two the shell runs is judged. (B)'s command word is also past the `time`
# keyword's `-p`/`--` (`_trap_head_index`).
_TRAP_NAME = "trap"
_TRAP_DQ_ESC = re.compile(r'\\([$`"\\\n])')
_TRAP_MAX = 32
_TRAP_OPT_MAX = 64  # (A) option words read per trap token, then the token is left to (B)
_TRAP_ACTIVE = [0]  # the nesting level of the trap reading being judged (0: none)
_TRAP_JUDGED: list = [set()]  # actions an outer level already judged
_TRAP_BUDGET = [0]  # characters left to judge in this decision (set at level 0)
_TRAP_RUNTIME = re.compile(r"[$`]")  # (A) a `$` or backtick: (B) reads those words
_TRAP_UNREADABLE = object()
_TRAP_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5441): this command sets a `trap` action (code the shell runs "
    "on a signal or on EXIT/ERR/DEBUG/RETURN) that the guard cannot read: built at run time "
    "(`trap \"$a\" EXIT`, `trap \"rm $f\" EXIT`), an option or glob word built at run time, "
    "or past the guard's bounds; refused (fail-closed). Spell the action as a literal "
    "single-quoted string (`trap 'rm -f \"$tmp\"' EXIT`), or run the code as its own command."
)


def _trap_spells_name(raw: str) -> bool:
    """T-5441: True if token `raw` (quotes dropped) is trap, or a brace or glob word that may
    build it (`{trap,}`, `tr{a,}p`, `t?ap`; with letters, no `/` or `$`)."""
    word = raw.translate(_QUOTE_STRIP)
    if word == _TRAP_NAME:
        return True
    if not any(c in word for c in "{*?[") or "/" in word or "$" in word or len(word) > 256:
        return False
    for cand in _rn_brace_expand(word):
        cand = cand.replace("{", "").replace("}", "")
        if cand == _TRAP_NAME or (any(c in cand for c in "*?[") and _RN_LETTER.search(cand)
                                  and fnmatch.fnmatchcase(_TRAP_NAME, cand)):
            return True
    return False


def _trap_raw_actions(view: str, out: list, finds: list) -> bool | None:
    """T-5441 (A): add to `out` the static action of each trap-named token of `view`, read
    with `_rn_word` (a superset: the token may stand anywhere), unless a quote or backslash
    is left in it; and to `finds` each action as (value, token start, action start, action
    end) (r1: the caller checks the quoted ones against (B)). None when a token that may
    stand at command position (`_trap_at_cmd`) has, up to its action's place, a word with an
    unquoted glob or brace and no `$` or backtick (those (B) reads); else whether any
    trap-named token was seen."""
    t = _RnText(view)
    text, n = view, len(view)
    seen = False
    prev = None
    for m in _RN_TOK.finditer(view):
        tok = m.group()
        before, prev = prev, tok
        if not _trap_spells_name(tok):
            continue
        seen = True
        at_cmd = _trap_at_cmd(tok, before)
        i, opts, after_dd = m.end(), 0, False
        action = span = None
        while True:
            while i < n and (text[i] in " \t" or text.startswith("\\\n", i)):
                i += 2 if text[i] == "\\" else 1
            if i >= n or text[i] in _RN_STOP:
                break
            end, value, dyn, _splits = _rn_word(t, i)
            if end == i:
                break
            if dyn and at_cmd and not _TRAP_RUNTIME.search(text, i, end):
                return None  # an unquoted glob or brace: it may expand to file names
            if action is not None:
                # (a quote or backslash left in the value: the token may have been read out of
                # phase, inside a quoted string (`echo "trap \\"x\\" EXIT"`); r1: the caller
                # fails closed unless (B) read it)
                if not any(c in action for c in "'\"\\"):
                    out.append(action)
                finds.append((action, m.start()) + span)
                break
            if dyn:
                break  # (a run-time word: (B) reads it)
            if not after_dd and value == "--":
                after_dd = True
            elif not after_dd and value.startswith("-") and value != "-":
                opts += 1
                if opts > _TRAP_OPT_MAX:
                    break
            else:
                action, span = value, (i, end)
            i = end
    return seen


def _trap_at_cmd(tok: str, before: str | None) -> bool:
    """T-5441 (A): True if trap-named token `tok` may stand at command position: first, after a
    separator or a lead word (`builtin`, `command`, `eval`, a keyword), or opening a quoted
    code string after a `-c` cluster (`bash -c 'trap ...'`; not `grep 'trap ' *.py`)."""
    if before is None or before in _RN_SEP_TOKENS:
        return True
    before = before.translate(_QUOTE_STRIP)
    return before in _RN_LEAD_WORDS or (tok[0] in "'\"" and _RN_C_OPT.fullmatch(before) is not None)


def _trap_head_index(words: list[str]) -> int:
    """T-5441 r1 (B): `_py_head_index`, also past the `time` keyword's `-p` and `--` (`time -p
    trap ...`, `time -p command trap ...`, `time -- time -p trap ...`). Local to the trap
    reading (r1: the other rules keep `_py_head_index`)."""
    i, n = 0, len(words)
    while True:
        j = i + _py_head_index(words[i:])
        k = j
        while j > i and words[j - 1] == "time" and k < n and words[k] in ("-p", "--"):
            k += 1
            if words[k - 1] == "--":
                break
        if k == j:
            return j
        i = k


def _trap_lexed_actions(command: str, out: list, bodies: list | None = None) -> bool:
    """T-5441 (B): add to `out` the action of each lexed trap command of `command` (code
    strings included), and to `bodies` (if given) each heredoc body's text; False when one is
    built at run time (or a word in option place is), or the lexer left text unread in a text
    that names trap."""
    lex = _py_lex(command)
    _py_expand_code(lex, skip_unknown=True)
    if lex.frame_limited or any(
            _TRAP_NAME in x.translate(_QUOTE_STRIP)
            for x in (lex.tails if lex.code_tails is None else lex.code_tails)):
        return False
    for c in lex.cmds:
        if bodies is not None:
            bodies.extend(body for body, _expands, _strip in c.heredocs if _TRAP_NAME in body)
        words = c.words
        n = len(words)
        h = _trap_head_index(words)
        if h >= n or not _trap_spells_name(os.path.basename(words[h])):
            continue

        def dyn(k: int, words=words, c=c) -> bool:
            return k in c.dyn or _PY_SUBST in words[k] or _PY_PWD_MARK in words[k]

        k = h + 1
        while k < n:
            if dyn(k):
                return False  # a run-time word where an option may stand (`trap $o CODE SIG`)
            w = words[k]
            if w == "--":
                k += 1
                break
            if w.startswith("-") and w != "-":
                k += 1
                continue
            break
        if k < n:
            if dyn(k):
                return False  # a run-time action (`trap "$a" EXIT`; alone, it may split)
            if k + 1 < n:
                out.append(words[k])
    return True


def _trap_actions(command: str):
    """T-5441: the trap actions of `command` worth judging (see the comment above), in order
    and without repeats, or `_TRAP_UNREADABLE`."""
    view = command.replace("\\\n", "")
    flat = view.translate(_QUOTE_STRIP)
    if _TRAP_NAME not in flat and "$'" not in view and not any(c in flat for c in "{*?["):
        return []  # (a brace or glob may build the name; a `$'...'` may spell it)
    out: list = []
    finds: list = []
    # (A) reads each `$'...'` as the single-quoted word bash makes of it (still one word)
    raw = view
    if "$'" in view:
        raw = _ENV_S_ANSI.sub(lambda m: shlex.quote(_env_s_ansi_c(m.group())), view)
    seen = _trap_raw_actions(raw, out, finds)
    if seen is None:
        return _TRAP_UNREADABLE
    lexed: list = []
    bodies: list = []
    if (seen or _TRAP_NAME in flat or "$'" in view) and not _trap_lexed_actions(command, lexed, bodies):
        return _TRAP_UNREADABLE
    if any(any(c in x for c in "'\"\\") for x, *_pos in finds):
        # r1: a quoted action (A) found and (B) did not read fails closed
        for body in bodies:
            read_body: list = []
            if _trap_lexed_actions(body, read_body):
                lexed += read_body
        read = set(lexed)
        spans: list = []  # the raw spans of actions (B) read too (each is judged as code)
        for x, tok_at, start, end in finds:
            spans = [sp for sp in spans if sp[1] > tok_at]  # (tokens come in order)
            if any(a <= tok_at for a, _b in spans):
                continue  # (inside an action judged as code, whose traps that reading reads)
            if x in read:
                spans.append((start, end))
                if len(spans) > _TRAP_MAX:
                    return _TRAP_UNREADABLE  # (overlapping spans: read out of phase)
            elif not any(c in x for c in "'\"\\"):
                continue
            elif _TRAP_DQ_ESC.sub(r"\1", x) in read:
                out.append(x)
            else:
                return _TRAP_UNREADABLE
    out += lexed
    return [x for x in dict.fromkeys(out) if x.strip() and x != "-"]


def _decide_trap(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5441: the trap actions of `command`, judged as code by every reading (see the comment
    above), or None."""
    outer = _TRAP_ACTIVE[0]
    if outer == 0:
        _TRAP_JUDGED[0] = set()
        _TRAP_BUDGET[0] = 8 * len(command) + 4096
    actions = _trap_actions(command)
    if actions is _TRAP_UNREADABLE:
        return _TRAP_UNREADABLE_DENY
    judged = _TRAP_JUDGED[0]
    actions = [x for x in actions if x not in judged]
    if not actions:
        return None
    if outer >= _FED_DEPTH or len(actions) > _TRAP_MAX:
        return _TRAP_UNREADABLE_DENY
    judged.update(actions)
    alone = "\n".join(actions)
    copies = [alone, command + "\n" + alone]
    _TRAP_BUDGET[0] -= sum(len(x) for x in copies)
    if _TRAP_BUDGET[0] < 0:
        return _TRAP_UNREADABLE_DENY
    dc = _DC_ACTIVE[0]
    _TRAP_ACTIVE[0], _DC_ACTIVE[0] = outer + 1, False  # (an action is judged by every rule)
    try:
        for copy in copies:
            if len(copy) > 2 * BASH_COMMAND_MAX_CHARS:
                return _TOO_LONG
            denial = _decide_readings({**data, "tool_input": {**tool_input, "command": copy}})
            if denial is not None:
                return denial
    finally:
        _TRAP_ACTIVE[0], _DC_ACTIVE[0] = outer, dc
    return None


# --- T-5571: sg/newgrp/firejail, and shells fed a stdin this hook cannot compute ---
# (1) Wrappers that run a command the shared peel does not read. shadow's `sg [-] GROUP [-c]
# CODE` runs CODE with `/bin/sh -c`: only the first word after GROUP and an optional `-c`,
# later words are ignored (checked against /usr/bin/sg on this box: `sg g echo a b` runs
# `echo`). `firejail [--opt[=v] ...] [--] CMD ...` runs CMD (older releases through a shell,
# its words joined). Each such code is judged as a command of its own by every reading
# (`_decide_wrapcode`, as T-5441 judges a trap action), alone and after the command (its cwd
# context): sg's CODE; firejail's words as argv (quoted), as one shell line (joined), and from
# each of its next `_WC_SUFFIXES` words on (a superset: an option value this reading does not
# know cannot hide the command). The command word is read past the shared wrapper peel
# (`env`/`nice`/`timeout`/...), and may be a brace or glob word that builds the name. With no
# command, `sg GROUP`, `newgrp [-] [GROUP]` and `firejail [opts]` start a shell that reads
# its commands on stdin: they are stdin consumers to the T-5332 fed reading (`_fed_consumer`)
# and to (2). Fails closed (`_WC_UNREADABLE_DENY`) on a word built at run time up to and
# including the command (`sg grp -c "$c"`, `sg "$g" -c CODE`, `firejail "$X"`), on an
# `xargs`/`parallel`/`find -exec` that hands one its words, and past `_FED_DEPTH`/the budget.
# (2) A shell reading its commands on stdin (`_fed_consumer`'s direct consumers: a shell with
# no `-c` and no script operand, `-s`, `-`, `source`/`.` of /dev/stdin, `eval bash`, the (1)
# wrappers; also a command word built at run time with no operand, `| $X`, `| "$SH" -s`) must
# be fed text this hook computes (`_decide_noncomputable`): its own here-string or heredoc
# with no expansion; a stdin `< FILE` the T-5505 reading reads (a shell or source only); a
# pipe from computable producers (a printf/echo of literal words, `xargs printf|echo`,
# `cat`/`tee` passing a computable stdin on, a `{ }`/`( )` group of them, `true`/`false`/`:`);
# or `cat FILE` piped straight into it (T-5505 judges FILE). Anything else is refused:
# another producer (`tac`, `head`, `sed`, `grep`, `curl`, `python3 -c ...`, `cat FILE | cat`,
# `env`, `cat - FILE`), a printf/echo/here-string/heredoc holding an expansion (r1:
# `$HOME`/`$PWD`/`~` too), a producer whose stderr reaches the pipe (r1: its own `|&`/`2>&1`/
# `2>/dev/stdout`, recorded by the lexer), the consumer's own `<&N`, a `<`/`<>` the T-5505
# reading does not read, `script` without `-c` fed anything (r1), and a consumer that
# inherits its stdin (`exec 3<f; bash <&3`, `exec <f; bash`, a `while`/`{ }`/function body,
# a process substitution, a `-c` string or `$(...)` body of a piped or redirected command,
# a script file's own text) when the text, or the text around it, may pipe or redirect it
# (`_nc_has_feed`: a superset), and a call of a function the text defines when a body may
# read stdin. A consumer is also a brace or glob command word that may build one (`{bash,}`,
# `b?sh`; with a letter, as T-5441 reads trap), and an operand or `<` under /dev/ or /proc/
# (`/dev//stdin`); a `case` pattern (`a|b)`) is no command. A command's compound depth comes
# from the lexer's `(`/`)` events and its leading reserved words. The worker's alternative is
# always there: write the script with the Write tool and run it by a literal path, or run
# the commands directly.
# Children (each static or run-time `-c`/eval/sg/firejail code string; each `$(...)`/backtick
# body, when the lexer found a substitution command that may read stdin or run code) are
# walked to `_PY_CODE_DEPTH` and the fed-walk budget; past them, refused when the unread text
# may hold a consumer.
_WC_NAMES = ("sg", "firejail", "newgrp")
_WC_SUFFIXES = 3
_WC_SUFFIX_HEADS = _ESCALATOR_WORDS | _SHELL_NAMES | frozenset(_WRAPPER_GRAMMAR) | {"git", "gh"}
_WC_MAX = 256  # (r1: the joinable codes are judged in one text)
_WC_ACTIVE = [0]  # the nesting level of the wrapper-code reading being judged (0: none)
_WC_JUDGED: list = [set()]  # codes an outer level already judged
_WC_BUDGET = [0]  # characters left to judge in this decision (set at level 0)
_WC_UNREADABLE = object()
_WC_UNREADABLE_DENY = _deny(
    "BLOCKED (worker-guard, T-5571): this command runs `sg`, `newgrp` or `firejail` with a "
    "group, option or command word built at run time (`sg grp -c \"$c\"`, `sg \"$g\" -c CODE`, "
    "`firejail \"$X\"`), hands one its words through `xargs`/`parallel`/`find -exec`, or passes "
    "the guard's bounds; the guard cannot tell what it runs, so it is refused (fail-closed). "
    "Spell the command as literal words (`sg grp -c 'make test'`), or run it directly."
)


def _wc_name(word: str) -> str | None:
    """T-5571: which of sg/firejail/newgrp command word `word` runs: its basename, or a brace
    or glob word that may build one (`{sg,}`, `s?`, `fire*`); None otherwise."""
    base = os.path.basename(word)
    if base in _WC_NAMES:
        return base
    if not any(ch in base for ch in "{*?[") or "$" in base or len(base) > 256:
        return None
    for cand in _rn_brace_expand(base):
        cand = cand.replace("{", "").replace("}", "")
        for name in _WC_NAMES:
            if cand == name or (any(ch in cand for ch in "*?[") and _RN_LETTER.search(cand)
                                and fnmatch.fnmatchcase(name, cand)):
                return name
    return None


def _wc_parse(name: str, words: list[str], dyn) -> tuple:
    """T-5571: what wrapper `name` (`words[0]`) runs: ("code", CODE) for `sg`'s code string,
    ("argv", WORDS) for `firejail`'s command, ("stdin",) for a shell reading stdin, or
    ("dyn",) when a word up to the command's place (`dyn(k)`) is built at run time."""
    n = len(words)
    if name == "newgrp":
        return ("stdin",)
    k = 1
    if name == "sg":
        if k < n and not dyn(k) and words[k] == "-":
            k += 1
        if k < n and dyn(k):
            return ("dyn",)
        k += 1  # GROUP
        if k < n and dyn(k):
            return ("dyn",)
        if k < n and words[k] == "-c":
            k += 1
        if k >= n:
            return ("stdin",)
        return ("dyn",) if dyn(k) else ("code", words[k])
    while k < n:  # firejail: `--opt`, `--opt=value`, `-c`; `--` ends them
        if dyn(k):
            return ("dyn",)
        w = words[k]
        k += 1
        if w == "--":
            break
        if w[:1] != "-" or w == "-":
            k -= 1
            break
    if k >= n:
        return ("stdin",)
    if any(dyn(j) for j in range(k, n)):
        return ("dyn",)
    return ("argv", list(words[k:]))


def _wc_cmd(c: _PyCmd):
    """T-5571: the code strings simple command `c` runs through sg/firejail (see (1) above),
    or `_WC_UNREADABLE`."""
    i = _py_head_index(c.words)
    full = c.words[i:]
    if not full or not any(_wc_name(w) for w in full):
        return []
    words = list(full)
    dyn = [k + i in c.dyn or _PY_SUBST in w or _PY_PWD_MARK in w for k, w in enumerate(full)]
    pnames: list = []
    if os.path.basename(full[0]) in _WRAPPER_GRAMMAR:
        try:
            words, base, n, src = _fed_peeled(full)
        except _FedUnknown:
            return _WC_UNREADABLE
        dyn = [src[n + k] + i in c.dyn or _PY_SUBST in w or _PY_PWD_MARK in w
               for k, w in enumerate(words)]
        pnames = [os.path.basename(w) for w in base[:n]]
    if words and os.path.basename(words[0]) == "busybox":
        words, dyn = words[1:], dyn[1:]
    if words and os.path.basename(words[0]) == "find":
        for k in range(1, len(words) - 1):
            if words[k] in _FIND_EXEC_WORDS and _wc_name(words[k + 1]):
                return _WC_UNREADABLE  # (find appends its own words)
    if not words or dyn[0]:
        return []
    name = _wc_name(words[0])
    if name is None:
        return []
    if any(p in _SF_RELAYS for p in pnames):
        return _WC_UNREADABLE  # (`xargs sg grp -c`: the code comes from the input)
    got = _wc_parse(name, words, lambda k: dyn[k])
    if got[0] == "dyn":
        return _WC_UNREADABLE
    if got[0] == "code":
        return [got[1]]
    if got[0] == "argv":
        rest = got[1]
        codes = [shlex.join(rest), " ".join(rest)]
        # (r1: a suffix only from a word that can start what the rules deny: an escalator,
        # git/gh, a shell or a wrapper; a plain argument judged as a command over-blocks)
        codes += [shlex.join(rest[j:]) for j in range(1, min(len(rest), _WC_SUFFIXES + 1))
                  if os.path.basename(rest[j]) in _WC_SUFFIX_HEADS or _wc_name(rest[j])]
        return codes
    return []


_WC_SPECIAL = re.compile(r"[{*?\[]")
_WC_SEP = re.compile(r"[\s;&|()<>`]")


def _wc_hint(text: str) -> bool:
    """A superset: `text` may name sg/firejail/newgrp (each `$'...'` decoded first; a brace
    or glob token that may build a name counts)."""
    if "$'" in text:
        try:
            text = _SF_ANSI_C.sub(lambda m: _fed_escapes(m.group(1), "fmt")[0], text)
        except Exception:
            return True
        if "$'" in text:
            return True
    flat = text.replace("\\\n", "").translate(_QUOTE_STRIP)
    if any(x in flat for x in _WC_NAMES):
        return True
    end = 0  # (each token holding a brace or glob character is read once: linear)
    for m in _WC_SPECIAL.finditer(flat):
        if m.start() < end:
            continue
        start = m.start()
        while start > end and not _WC_SEP.match(flat, start - 1):
            start -= 1
        sep = _WC_SEP.search(flat, m.end())
        end = sep.start() if sep else len(flat)
        if _wc_name(flat[start:end]):
            return True
    return False


def _wc_codes(command: str):
    """T-5571: the sg/firejail codes of `command` (code strings included), in order and
    without repeats, or `_WC_UNREADABLE`."""
    if not _wc_hint(command):
        return []
    lex = _py_lex(command)
    _py_expand_code(lex, skip_unknown=True)
    if lex.frame_limited:
        return _WC_UNREADABLE
    out: list = []
    for c in lex.cmds:
        got = _wc_cmd(c)
        if got is _WC_UNREADABLE:
            return got
        out.extend(got)
    return [x for x in dict.fromkeys(out) if x.strip()]


def _wc_joinable(code: str) -> bool:
    """r1: `code` is a whole text on its own line: the lexer reads it to the end with nothing
    unfinished, it opens no heredoc and does not end in a `\\` continuation."""
    if "<<" in code or code.rstrip(" \t\n").endswith("\\"):
        return False
    lex = _py_lex(code)
    return not lex.frame_limited and not lex.tails


def _decide_wrapcode(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5571 (1): the codes `command` runs through sg/firejail, judged as commands of their
    own by every reading (see the comment above), or None."""
    outer = _WC_ACTIVE[0]
    if outer == 0:
        _WC_JUDGED[0] = set()
        _WC_BUDGET[0] = 8 * len(command) + 4096
    codes = _wc_codes(command)
    if codes is _WC_UNREADABLE:
        return _WC_UNREADABLE_DENY
    judged = _WC_JUDGED[0]
    codes = [x for x in codes if x not in judged]
    if not codes:
        return None
    if outer >= _FED_DEPTH or len(codes) > _WC_MAX:
        return _WC_UNREADABLE_DENY
    judged.update(codes)
    # r1: every code that can stand on a line of its own (no unfinished quote or construct,
    # no heredoc, no trailing `\`: it cannot swallow the line after it) is judged in ONE text,
    # one code per line (alone, and after the command for its cwd context); any other alone
    joined = [x for x in codes if _wc_joinable(x)]
    copies = []
    if joined:
        alone = "\n".join(joined)
        copies += [alone, command + "\n" + alone]
    for code in codes:
        if code not in joined:
            copies += [code, command + "\n" + code]
    _WC_BUDGET[0] -= sum(len(x) for x in copies)
    if _WC_BUDGET[0] < 0:
        return _WC_UNREADABLE_DENY
    dc = _DC_ACTIVE[0]
    _WC_ACTIVE[0], _DC_ACTIVE[0] = outer + 1, False  # (a code is judged by every rule)
    try:
        for copy in copies:
            if len(copy) > 2 * BASH_COMMAND_MAX_CHARS:
                return _TOO_LONG
            denial = _decide_readings({**data, "tool_input": {**tool_input, "command": copy}})
            if denial is not None:
                return denial
    finally:
        _WC_ACTIVE[0], _DC_ACTIVE[0] = outer, dc
    return None


_NC_REASON = (
    "to feed a shell its commands on stdin from {} (T-5571): the hook cannot compute that "
    "text, so it cannot judge the commands the shell runs (only a literal printf/echo, "
    "here-string or heredoc, or `cat FILE`/`< FILE` straight into the shell, is read). Write "
    "the commands to a script with the Write tool and run it by a literal path (`bash "
    "/abs/x.sh`), or run the commands directly in this call"
)
_NC_HINT = re.compile(r"\||<|>\(|(?<![\w-])coproc(?![\w-])")
_NC_PRE = re.compile(r"(?<![\w.-])(?:" + _SF_SH + r"|source|eval|sg|newgrp|firejail|busybox|script)"
                     r"(?![\w.-])|[$`*?\[{]|(?:^|[\s;&|(){}!])\.(?=\s)")  # (a brace/glob word may build one)
_NC_EXPANSION = re.compile(r"\\.|(\$[A-Za-z_{(0-9@*#?!$-]|`)", re.S)
# r1: a word-bounded `$HOME`/`$PWD` (static to the lexer) and a leading `~` expand at run time
# too, and the command can set them (`HOME=$(...)`, `read HOME`, `cd 'a;b'`): an expansion.
_NC_HOME_WORD = re.compile(r"\$\{?(?:HOME|PWD|OLDPWD)(?!\w)|^~")
# r1: a `( ... )` group's stderr into its pipe (the redirect after `)` makes no command)
_NC_PAREN_ERR = re.compile(r"\)[ \t]*(?:\d*>&|&>|\|&)")
_NC_SILENT = frozenset({"true", "false", ":"})
_NC_INHERIT = ("a stdin the shell inherits (`bash <&3`, `exec < f; bash`, a `while`/`{ }`/"
               "function body, a process substitution, or a `-c` string, `$(...)` body or "
               "script file run with a piped or redirected stdin)")
_NC_DYN_TEXT = ("a printf/echo/here-string/heredoc holding an expansion (`<<< \"$(cat f)\"`, "
                "`echo \"$X\" |`, an unquoted heredoc with `$X` or `$HOME`)")
_NC_ERR_TEXT = "a producer whose stderr reaches the pipe (`|&`, `2>&1`, `2>/dev/stdout`)"
_NC_DUP_TEXT = "a `<&N` file-descriptor duplication (`bash <&3`, `exec 3<f; bash 0<&3`)"
# r1: util-linux `script` without `-c`/`--command` runs an interactive `$SHELL` on its stdin
_NC_SCRIPT_C = re.compile(r"-[A-Za-z]*c|--c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?(?:=|$)")


def _nc_has_feed(text: str) -> bool:
    """A superset: `text` may pipe or redirect a command's stdin."""
    return bool(_NC_HINT.search(text.replace("||", "  ")))


def _nc_own(c: _PyCmd) -> str | None:
    """Why `c`'s own here-strings/heredocs are not computable text, or None."""
    for w in c.herestrs:
        if w is None or _NC_HOME_WORD.search(w):
            return _NC_DYN_TEXT
    for body, expands, _strip in c.heredocs:
        if expands and any(m.group(1) for m in _NC_EXPANSION.finditer(body)):
            return _NC_DYN_TEXT  # (r1: `$HOME`/`$PWD` too: no exemption)
    return None


def _nc_words_dyn(c: _PyCmd) -> bool:
    """`c` (a printf/echo producer) has a word built at run time (r1: `$HOME`/`$PWD`/`~` too)."""
    return _fed_dynamic(c) or any(_NC_HOME_WORD.search(w) for w in c.words)


_NC_NAMES = tuple(sorted(_SHELL_NAMES | {".", "source", "eval", "busybox"} | set(_WC_NAMES)))


def _nc_spelled(word: str) -> list[str]:
    """The stdin consumers' names brace or glob word `word` may build (as `_wc_name` reads it)."""
    out: list = []
    for cand in _rn_brace_expand(word):
        cand = cand.replace("{", "").replace("}", "")
        for name in _NC_NAMES:
            if name not in out and (cand == name or (
                    any(ch in cand for ch in "*?[") and _RN_LETTER.search(cand)
                    and fnmatch.fnmatchcase(name, cand))):  # (a lone `*` builds no name: T-5441)
                out.append(name)
    return out


def _nc_kind(c: _PyCmd) -> str | None:
    """"fed": `_fed_consumer` reads `c`'s stdin as code (a direct consumer, not xargs);
    "dyn": a command word built at run time with a shell's stdin shape; else None."""
    try:
        run = _fed_consumer(c)
    except _FedUnknown:
        return "dyn"
    if run is not None:
        return None if getattr(run, "nc_xargs", False) else "fed"
    i = _py_head_index(c.words)
    if i >= len(c.words):
        return None
    if "script" in c.words[i] or (c.words[i:] and os.path.basename(c.words[i]) in _WRAPPER_GRAMMAR):
        words = c.words[i:]
        if os.path.basename(words[0]) in _WRAPPER_GRAMMAR and os.path.basename(words[0]) != "script":
            try:
                words = _fed_peeled(words)[0]
            except _FedUnknown:
                words = []
        if words and os.path.basename(words[0]) == "script" and not any(
                _NC_SCRIPT_C.fullmatch(x) for x in words[1:]):
            return "opaque"  # (r1: `| script -q /dev/null`: an interactive shell on stdin)
    w = c.words[i]
    if not (i in c.dyn or _PY_SUBST in w or _PY_PWD_MARK in w):
        # a brace or glob command word that may build a consumer's name (`{bash,}`, `b?sh`,
        # `/bin/b*sh`, `{source,} /dev/stdin`): read as each name it may build
        base = os.path.basename(w)
        if not any(ch in base for ch in "{*?[") or len(base) > 256:
            return None
        for name in _nc_spelled(base):
            sub = _PyCmd()
            sub.words = [name] + c.words[i + 1:]
            sub.dyn = {k - i for k in c.dyn if k > i}
            try:
                if _fed_consumer(sub) is not None:
                    return "dyn"
            except _FedUnknown:
                return "dyn"
        return None
    tail = w.rsplit("/", 1)[-1]
    if tail and "$" not in tail and _PY_SUBST not in tail and "`" not in tail and (
            tail not in _SHELL_NAMES and _wc_name(tail) is None):
        return None  # (`"$VENV/bin/python"`: a static name that is no shell)
    args = c.words[i + 1:]
    if any(a[:2] == "--" for a in args):
        return None  # (`"$f" --version`: read as a program, not a shell)
    sub = _PyCmd()  # (the word read as a shell: `| $X`, `| "$SH" -s`, `| $X -`)
    sub.words = ["bash"] + list(args)
    sub.dyn = {k - i for k in c.dyn if k > i}
    try:
        return "dyn" if _fed_consumer(sub) is not None else None
    except _FedUnknown:
        return "dyn"


def _nc_sf_reads(c: _PyCmd) -> bool:
    """`c`'s `< FILE` is read by the T-5505 script-file reading (`_sf_cmd`'s head: a shell or
    source past the wrapper peel, not behind a relay)."""
    i = _py_head_index(c.words)
    full = c.words[i:]
    if not full:
        return False
    words, pnames = full, []
    if os.path.basename(full[0]) in _WRAPPER_GRAMMAR:
        try:
            words, base, n, _src = _fed_peeled(full)
            pnames = [os.path.basename(w) for w in base[:n]]
        except _FedUnknown:
            return False
    if any(p in _FED_STDIN_TAKERS or p == "xargs" for p in pnames):
        return False
    if words and os.path.basename(words[0]) == "busybox":
        words = words[1:]
    return bool(words) and (os.path.basename(words[0]) in _SHELL_NAMES
                            or os.path.basename(words[0]) in (".", "source"))


def _nc_upstream(p: _PyCmd, src: dict, gin, depth: int) -> str | None:
    """Why the stdin `cat`/`tee` producer `p` passes on is not computable, or None."""
    if p.stdindup:
        return _NC_DUP_TEXT
    if p.herestrs or p.heredocs:
        return _nc_own(p)
    if p.infiles:
        return "`cat < FILE` that is not piped straight into the shell"
    if id(p) in src:
        return _nc_unit(src[id(p)], src, False, depth + 1)
    if gin is not None and gin is not p:
        return _nc_own(gin)
    return "`cat`/`tee` passing on a stdin this hook cannot see"


def _nc_producer(p: _PyCmd, src: dict, cat_ok: bool, gin, depth: int) -> str | None:
    """Why producer `p`'s output is not computable text (see (2) above), or None."""
    if p.errout:
        return _NC_ERR_TEXT  # (r1: stderr into the pipe: `x |& bash`, `x 2>&1 | bash`)
    full = _fed_words(p)
    if not full:
        return None  # (`}`, or the group's own here-string holder)
    xs = _fed_xargs_split(full)
    words = xs[1] if xs else _ho_static_command_words(full)
    if xs:
        cmd = words or ["echo"]
        if os.path.basename(cmd[0]) not in ("printf", "echo"):
            return f"`xargs {_sf_show(cmd[0])}`"
        return _NC_DYN_TEXT if _nc_words_dyn(p) else _nc_upstream(p, src, gin, depth)
    if not words:
        return f"`{_sf_show(full[0])}`"  # (a wrapper alone: `env` prints the environment)
    head = os.path.basename(words[0])
    if head in ("printf", "echo"):
        return _NC_DYN_TEXT if _nc_words_dyn(p) else None
    if head == "tee" or (head == "cat" and all(w[:1] == "-" or w in _FED_STDIN_PATHS for w in words[1:])):
        if p.infiles and head == "cat" and cat_ok and not (p.herestrs or p.heredocs) and not (
                words[1:] or p.stdindup):
            return None  # (`cat < FILE | bash`: T-5505 judges FILE)
        return _nc_upstream(p, src, gin, depth)
    if head == "cat":
        # r1: computable only as T-5505 reads it: every operand a literal file (no `-`,
        # /dev/stdin or /dev/fd/N beside it, no option), and none built at run time
        raw = p.words[_py_head_index(p.words)] if _py_head_index(p.words) < len(p.words) else ""
        ops = words[1:]
        if any(w[:1] == "-" or _fed_stdin_operand(w) for w in ops) or _fed_dynamic(p) or (
                p.infiles or p.herestrs or p.heredocs or p.stdindup):
            return "a `cat` that reads its stdin beside or instead of a file (`cat - FILE`)"
        if cat_ok and os.path.basename(raw) == "cat":
            return None  # (T-5505 judges FILE, or refuses several files)
        return "`cat FILE` that is not piped straight into the shell (`cat FILE | cat | bash`)"
    if head in _NC_SILENT:
        return None
    return f"`{_sf_show(head)}`"


def _nc_unit(unit: list, src: dict, cat_ok: bool, depth: int) -> str | None:
    """Why the producer unit `unit` (one command, or a group's) writes text this hook cannot
    compute into the pipe after it, or None."""
    if depth > 16:
        return "a relay chain past 16 hops"
    last = unit[-1]
    gin = None
    if not last.words[_py_head_index(last.words):] and (last.herestrs or last.heredocs):
        gin = last
    for p in unit:
        what = _nc_producer(p, src, cat_ok and len(unit) == 1, gin, depth)
        if what:
            return what
    return None


_NC_OPEN = frozenset({"{", "if", "while", "until", "for", "select", "case"})
_NC_CLOSE = frozenset({"}", "fi", "done", "esac"})
_NC_PAREN_REDIR = re.compile(r"\)[ \t]*\d*<")
_NC_PRE_NAMES = re.compile(r"(?<![\w.-])(?:" + _SF_SH + r"|source|eval|sg|newgrp|firejail|busybox|script)"
                           r"(?![\w.-])|(?:^|[\s;&|(){}!])\.(?=\s)")


def _nc_after_pipe(ev: list, idx: int) -> bool:
    j = idx - 1
    while j >= 0 and ev[j] == "\n":
        j -= 1
    return j >= 0 and ev[j] == "|" and not (j >= 1 and ev[j - 1] == "|")


def _nc_pattern_start(ev: list, idx: int) -> bool:
    """A `(` at event `idx` opens a `case` pattern: right after `case WORD in` or a `;;`/`;&`."""
    j = idx - 1
    while j >= 0 and ev[j] == "\n":
        j -= 1
    if j < 0:
        return False
    if isinstance(ev[j], _PyCmd):
        return bool(ev[j].words) and ev[j].words[-1] == "in"
    return ev[j] in (";", "&") and j >= 1 and ev[j - 1] == ";"


def _nc_text(text: str, ctx: bool, children: list) -> str | None:
    """Why a consumer in `text` (whose own stdin may be fed from around it when `ctx`) reads
    a stdin this hook cannot compute, or None; adds each code string and `$(...)` body of
    `text` to `children` as (text, whether its stdin may be fed)."""
    lex = _sf_lexed(text)
    ev = lex.events
    src: dict = {}
    adjacent: set = set()
    where: dict = {}  # id(cmd) -> (inside a compound, inside one whose stdin may be fed)
    stack: list = []  # open compounds: (kind, fed)
    funcs: set = set()
    defs: set = set()  # ids of the `NAME ( )` / `function NAME` words: a definition, no call
    kw_def = False
    procsub = ">(" in text
    exec_in = False
    patterns: set = set()  # ids of a `case` pattern's words, which the lexer reads as commands
    paren_fed: set = set()
    for idx, e in enumerate(ev):
        if e == "(":
            if stack and stack[-1][0] == "case" and _nc_pattern_start(ev, idx):
                stack.append(("pat", stack[-1][1]))  # (`case x in (a|b) ...`)
                continue
            stack.append(("(", _nc_after_pipe(ev, idx) or procsub or bool(stack and stack[-1][1])))
            continue
        if e == ")":
            if stack and stack[-1][0] in ("case", "pat"):
                if stack[-1][0] == "pat":
                    stack.pop()
                k = idx - 1  # (`a|b)`: the run of words joined by `|` before it is a pattern)
                while k >= 0 and isinstance(ev[k], _PyCmd):
                    patterns.add(id(ev[k]))
                    if k >= 1 and ev[k - 1] == "|" and not (k >= 2 and ev[k - 2] == "|"):
                        k -= 2
                        continue
                    break
            elif stack and stack[-1][0] == "(":
                stack.pop()
            continue
        if not isinstance(e, _PyCmd):
            continue
        j = idx - 1
        while j >= 0 and ev[j] in ("\n", "("):
            j -= 1
        if j >= 0 and ev[j] == "|" and not (j >= 1 and ev[j - 1] == "|"):
            unit = _fed_group(ev, j - 1) if j >= 1 else []
            if unit:
                src[id(e)] = unit
            if j == idx - 1 and j >= 1 and isinstance(ev[j - 1], _PyCmd):
                adjacent.add(id(e))
            if j >= 1 and ev[j - 1] == ")":
                paren_fed.add(id(e))  # (fed by a `( ... )` group: its stderr, `_NC_PAREN_ERR`)
        first = True
        for w in e.words:
            if w in _NC_CLOSE:
                if stack and stack[-1][0] != "(":
                    stack.pop()
            elif w in _NC_OPEN:
                stack.append((w, (first and _nc_after_pipe(ev, idx)) or bool(stack and stack[-1][1])))
                first = False
            elif w not in _PY_RESERVED:
                break
        where[id(e)] = (bool(stack), bool(stack and stack[-1][1]))
        h = _py_head_index(e.words)
        if h < len(e.words):
            head = e.words[h]
            if head == "exec" and (e.herestrs or e.heredocs or e.infiles or e.stdindup):
                exec_in = True  # (`exec < f`, `exec <> f`, `exec 0<&3`: every later command's stdin)
            if len(e.words) == h + 1 and ev[idx + 1:idx + 3] == ["(", ")"]:
                funcs.add(head)  # `NAME ( )`
                defs.add(id(e))
            elif head == "function" and h + 1 < len(e.words):
                funcs.add(e.words[h + 1].partition("(")[0])
                defs.add(id(e))
                kw_def = True  # (`function f { bash; }`: the body's first words share this command)
    # (`done < f`, `} <<< x`, `( ... ) < f`: the redirect of a compound may feed any command in
    # one, a superset)
    closer_fed = bool(_NC_PAREN_REDIR.search(text)) or any(
        (e.herestrs or e.heredocs or e.infiles or e.stdindup) and e.words[:1]
        and e.words[0] in _NC_CLOSE for e in ev if isinstance(e, _PyCmd))
    feeds = _nc_has_feed(text)
    paren_err = None
    wc = _wc_hint(text)
    func_reads = None
    for idx, e in enumerate(ev):
        if not isinstance(e, _PyCmd) or id(e) in patterns:
            continue
        nested, cfed = where[id(e)]
        own = bool(e.herestrs or e.heredocs or e.infiles)
        fed_here = ctx or exec_in or own or id(e) in src or (nested and (cfed or closer_fed))
        kind = _nc_kind(e)
        h = _py_head_index(e.words)
        if kind is None and funcs and h < len(e.words) and e.words[h] in funcs and id(e) not in defs:
            if func_reads is None:  # (a function whose body may read stdin as code)
                func_reads = kw_def or any(_nc_kind(c) is not None for c in lex.cmds)
            kind = "func" if func_reads else None
        # a code string's stdin is its command's: fed when this one's may be (a consumer's own
        # feed is judged here, so its strings read only what is around it)
        sub = ctx if kind is not None else fed_here
        if _fed_may_run_code(e):
            children.extend((x, sub) for x in _py_code_strings(e, keep_dynamic=True) or ())
        got = _wc_cmd(e) if wc else ()
        if got is not _WC_UNREADABLE:
            children.extend((x, sub) for x in got)
        if kind is None:
            continue
        if e.stdindup:
            return _NC_DUP_TEXT  # (r1: the consumer's own `<&N`, not one anywhere in the text)
        if "coproc" in e.words[:h]:
            return "a `coproc` (the shell writes its stdin at run time)"
        if kind == "func" and (own or id(e) in src or fed_here):
            return "a function whose body reads stdin as code (`f() { bash; }; ... | f`)"
        if kind == "opaque" and (own or id(e) in src or fed_here):
            return "`script` without `-c`, an interactive shell that reads its stdin as commands"
        what = _nc_own(e)
        if what:
            return what
        if any(f is not None and _fed_stdin_operand(f) for f in e.infiles):
            return "a `< /dev/stdin`-style redirect (the shell reads the pipe or fd it names)"
        if e.infiles and (kind != "fed" or not _nc_sf_reads(e)):
            return "a `< FILE` redirect into a command the script-file reading does not read"
        if own:
            continue  # (its own stdin: the fed and script-file readings judge it)
        if id(e) in src:
            # r1: stderr is read on the producer side only (`_nc_producer`: each producer's own
            # `|&`/`2>&1`); a `( ... )` group's redirect makes no command: the raw text after a `)`
            if id(e) in paren_fed:
                if paren_err is None:
                    paren_err = bool(_NC_PAREN_ERR.search(text))
                if paren_err:
                    return _NC_ERR_TEXT
            # (`cat FILE` straight into a shell or source: T-5505's `_sf_cat_feeds` reads FILE)
            what = _nc_unit(src[id(e)], src, kind == "fed" and id(e) in adjacent and _nc_sf_reads(e), 0)
            if what:
                return what
            continue
        if kind != "dyn" and (ctx or exec_in or (nested and (cfed or closer_fed))):
            return _NC_INHERIT
    if ("$(" in text or "`" in text) and _nc_inner_reads(lex, ev):
        # (a `$(...)` body's stdin is that of the command it is expanded for: a superset. The
        # raw bodies are read only when the lexer found a substitution command that may read
        # stdin as code or run a code string: a backtick in a comment or quotes is no body.)
        sub = ctx or exec_in or feeds
        children.extend((x, sub) for x in _fed_subst_bodies(text))
    return None


def _nc_inner_reads(lex, ev: list) -> bool:
    """A command the lexer read inside a substitution (not a top-level event) that may read
    stdin as code, or run a code string, or be a brace/glob/run-time command word."""
    top = {id(e) for e in ev if isinstance(e, _PyCmd)}
    for c in lex.cmds:
        if id(c) in top:
            continue
        if _nc_kind(c) is not None or _fed_may_run_code(c) or _wc_cmd(c):
            return True  # (`_wc_cmd`: an sg/firejail code, or `_WC_UNREADABLE`)
    return False


def _nc_walk(command: str, ctx: bool) -> str | None:
    """T-5571 (2): why a shell in `command` (or its code strings and `$(...)` bodies, to
    `_PY_CODE_DEPTH`) reads its commands from a stdin this hook cannot compute, or None."""
    work = [(command, ctx, 0)]
    queued = {(command, ctx)}
    budget = 4 * len(command) + 4096
    while work:
        text, fed, depth = work.pop()
        if not fed and not _nc_has_feed(text) and "$'" not in text:
            continue  # (no stdin here or below it can be fed: children are substrings)
        if not _NC_PRE.search(text.replace("\\\n", "").translate(_QUOTE_STRIP)):
            continue  # (no consumer and no code string can be spelled)
        children: list = []
        what = _nc_text(text, fed, children)
        if what:
            return what
        for code, sub in children:
            if not code or (code, sub) in queued:
                continue
            queued.add((code, sub))
            budget -= len(code)
            if depth >= _PY_CODE_DEPTH or budget < 0:
                # past the bounds: refused only when the unread text may hold a consumer this
                # rule reads (a consumer's name, or a run-time command word with its own pipe
                # or redirect: one that inherits its stdin is not refused, as above)
                view = code.replace("\\\n", "").translate(_QUOTE_STRIP)
                if "$'" in code or _NC_PRE_NAMES.search(view) or (
                        ("$" in view or "`" in view) and _nc_has_feed(code)):
                    return "a nesting past the guard's depth or size bounds"
                continue
            work.append((code, sub, depth + 1))
    return None


def _decide_noncomputable(data: dict, tool_input: dict, command: str) -> dict | None:
    """T-5571 (2): a shell fed a stdin this hook cannot compute is refused (see above)."""
    del data, tool_input
    what = _nc_walk(command, _SF_LEVEL[0] > 0)  # (a script file's stdin is never the tool's)
    return None if what is None else _reason(_NC_REASON.format(what))


# --- T-5634: prompt expansion, history and editor commands run a value as code ---
# bash runs the code in a value it prompt-expands: `${var@P}` (any `@P}`; a quoted, escaped
# or run-time operator is a bad substitution, a `\`+newline inside one is not), PS4 under
# xtrace (`set -x`, `set -o xtrace`, `shopt -so xtrace`, `bash -x`, SHELLOPTS), PS0/PS1/PS2,
# PROMPT_COMMAND and the `?message` of a MAILPATH entry in an interactive shell (`... | bash
# -i`). `fc` runs its editor (`-e CMD`, FCEDIT) or a stored line (`-s`), history expansion
# (`!!`, `!-1`, `!?s?`, `^a^b`, on with `set -H`/`-o histexpand`/`-o history` or in `bash -i`)
# runs a stored line (`history -s/-r/-n`, a HISTFILE), `compgen -C CMD` runs CMD and
# `compgen -W WORDS` expands WORDS (a `$(...)` in them runs), even in `bash -c`. Every other
# rule reads a quoted value as data, so `PS4='$(sudo -n true)'; set -x; :` ran sudo (checked
# in bash 5.2 with a stand-in). Of the other transforms none runs code (@E/@A/@Q/@K/@k/@a/
# @U/@u/@L), nor do PS3 (`select` prints it as is), `read -p`, `complete -C/-F` (no completion
# runs without readline), MAIL or TIMEFORMAT; history expansion is off in `bash -c`.
# Each text (the command, its comment-cut and eval-joined copies, and every code string the
# lexer finds in them, `-c`/eval/wrapper codes, to `_PY_CODE_DEPTH` levels under a budget)
# fails closed (`_pe_walk`) on:
#   (1) an `@P}` or an undecodable `$'...'` (a `\cX`) in its normalised copy (each
#       `\`+newline dropped, each `$'...'` decoded, every quote and backslash dropped);
#   (2) a PS0-PS4/PROMPT_COMMAND/MAILPATH/HISTFILE/histchars name in that copy unless the
#       raw text has it, as written, as a read (`$PS4`, `${PS4}`, `${PS1:-}`: an operator
#       that neither writes nor evaluates), an `unset`, or a PS0-PS4 assignment (`PS4=`,
#       `PS4+=` at a word start, as an export/declare/local word too) whose value is a literal
#       that cannot run code (`_pe_value_safe`); a name the copy shows that the raw text does
#       not spell (`P"S4"`, `$'\x50S4'`) is unreadable; every other PROMPT_COMMAND/MAILPATH/
#       histchars write fails, and every HISTFILE write but an empty or `/dev/null` one;
#   (3) a command (`_py_lex`) whose command word is `fc`; `compgen` with a `-C` value, a `-W`
#       value holding `$`, a backtick, a backslash or (T-5667) a `(` (`'<(cmd)'` runs cmd),
#       glued or not, or any argument built at run time; `history` with a `-s`/`-r`/`-n`
#       option word (glued too) or a run-time one;
#   (4) history on (`_PE_HIST_ON`, a SHELLOPTS name, `_pe_hist_switch`'s word reader, or an
#       outer text's) in a text that holds a history designator (`_PE_HIST_DESIG`) or a
#       command word built at run time (it may be `fc`); a code string or a fed text
#       (`_stdin_fed_codes`) of such a text is read with history on;
#   (5) T-5667: history on in a text whose shell reads its commands from a feed the readings
#       above do not judge with history on (`_pe_fed_unread`): a `< FILE` other than
#       /dev/null, a pipe, a script file operand, or a stdin inside a `$(...)` (`bash -i <
#       f`, `cat f | bash -i`, `bash -o history -H f`, `env SHELLOPTS=history:histexpand
#       bash < f`; the script-file reading reads f with history off). Refused (fail closed).
# T-5667: `_pe_hist_switch` reads the words as bash does, in any order: for `set` and a
# shell's own options, each `o`/`O` of an option word takes the next word as its value
# (`set -euo pipefail -o history`, `set -oo a history`; a next word that is empty or starts
# with `-`/`+` is not taken), and history is on with a literal `history`/`histexpand` value
# of a `-` word, a `-` word holding `H` (a shell's: `H` or `i`, or `--interactive`), or an
# option word or value built at run time (a shell's run-time word is T-5505's); `+H`/`+o`
# turn it off and are not counted. `shopt` takes no option values (`shopt -o -s nounset
# history`): any `history`/`histexpand` or run-time argument turns it on. BASHOPTS carries no
# history option (checked in bash 5.2), so it is not read.
# It runs after every other rule (their reasons come first). Deny-only; linear (one pass
# builds the copy, the index map only when a name shows; value words parsed under a budget).
_PE_NAME = re.compile(
    r"(?<![A-Za-z0-9_])(?:PS[0-4]|PROMPT_COMMAND|MAILPATH|HISTFILE|histchars)(?![A-Za-z0-9_])")
# reads only (r2: and `unset`, and a HISTFILE of `/dev/null` or empty, which turn saving off)
_PE_NO_WRITE = frozenset({"PROMPT_COMMAND", "MAILPATH", "HISTFILE", "histchars"})
_PE_UNSET = re.compile(r"(?<![\w-])unset[ \t]+(?:-v[ \t]+)?$")
_PE_AT_P = re.compile(r"@P\}")
_PE_FC_WORD = re.compile(r"(?<![\w.$/@:%+-])fc(?![\w.=/@:%+-])")
# r2: a history store option word, its value glued or not (`-s'x'`, `-rfile`)
_PE_HISTORY_OPT = re.compile(r"-[A-Za-z]*[srn]")
# compgen's option letters that take a value (glued, `-W'x'`, or the next word)
_PE_COMPGEN_ARG = frozenset("oAGWFCXPS")
_PE_LEX_HINT = re.compile(r"fc|compgen|history")
_PE_DEEP_HINT = re.compile(
    r"fc|compgen|history|@P|PS[0-4]|PROMPT_COMMAND|MAILPATH|HISTFILE|histchars")
_PE_HIST_WORD = re.compile(r"set|sh|history|histexpand|interactive")
_PE_HIST_ON = re.compile(
    r"histexpand|-o\s*history|(?<![\w-])set\s+(?:[-+][A-Za-z]*\s+)*-[A-Za-z]*H"
    r"|(?<![\w.-])(?:ba|da|z|k)?sh\s+(?:-\S+\s+)*-[A-Za-z]*[iH]|--interactive")
# r2: bash inhibits history expansion only in `$!` and `${!` (not `{!`)
_PE_HIST_DESIG = re.compile(r"(?<!\$)(?<!\$\{)![^\s=(]|(?m:^[ \t]*\^)")
# T-5667: the words that may turn history on (`_pe_hist_switch`): a `set`/`shopt` or shell
# word; and the option values that do
_PE_SWITCH_HINT = re.compile(r"(?<![\w.-])(?:set|shopt)(?![\w.=-])|sh(?![\w.=-])")
_PE_SHELL_WORD = re.compile(r"sh(?![\w.=-])")  # (a shell may read a script file: `bash -i f`)
_PE_HIST_NAMES = frozenset({"history", "histexpand"})
_PE_SHELL_LONG_VALUE = frozenset({"--rcfile", "--init-file"})
_PE_WORD_START = frozenset(" \t\n;&|()`")
_PE_WORD_END = frozenset(" \t\n;&|)")
_PE_ANSI = re.compile(r"\$'((?:[^'\\]|\\.|\\$)*)(?:'|$)", re.S)
_PE_DROP = str.maketrans("", "", "'\"\\")
_PE_QSPACE = str.maketrans("'\"\\", "   ")  # (a decoded quote stays a word boundary)
# a read's operator right after the name in `${NAME...}`: one that neither writes nor
# evaluates (`:-` `-` `:+` `+` `:?` `?` `#` `%` `/` `^` `,` `@Q`..., not `:=`, `=`, `@P`)
_PE_READ_OP = re.compile(r"\}|:[-+?]|[-+?#%/^,]|@[^P]")
# a stored prompt value may hold `$NAME`, a special parameter, `$` before a blank or at the
# end, and `${...}` expansions (nested too) whose subscripts are digits, `@` or `*`; never a
# `$(`, `$[`, backtick, backslash (a prompt escape such as `\044` decodes to `$`), `$'`, `$"`,
# `@P`, `${!` (indirection) or a `${NAME:...}` substring (its offset is arithmetic)
_PE_DOLLAR_OK = re.compile(r"\$(?:[A-Za-z0-9_#?!@*$-]|(?=[ \t\n])|$)")
_PE_SUBSCRIPT_OK = re.compile(r"\[(?:\d+|[@*])\]")
_PE_DENY = _deny(
    "BLOCKED (worker-guard, T-5634): this command prompt-expands a value (`${var@P}`, or a "
    "PS0-PS4/PROMPT_COMMAND/MAILPATH value, which xtrace (`set -x`, `bash -x`) or an "
    "interactive shell expands), stores or expands shell history (`history -s`, HISTFILE, "
    "`!!` with history expansion on), or runs an editor or completion command (`fc`, "
    "`compgen -C`/`-W`): bash runs any `$(...)` in that value or line as code, and the guard "
    "reads it as data; refused (fail-closed). Set PS4 to a literal (`PS4='+ '`, "
    "`PS4='+ $LINENO: '`), drop `@P`, and run the code as its own command. If the words are "
    "only text, search with the Grep tool, and put a commit message that names PS4/"
    "PROMPT_COMMAND in a file: `git commit -F <file>`."
)


class _PeUnreadable(Exception):
    """A `$'...'` the decoder cannot read (`\\cX`): fail closed."""


def _pe_ansi_value(m: re.Match) -> str:
    val = _py_ansi_c(m.group(1))
    if val is None:
        raise _PeUnreadable
    return val.translate(_PE_QSPACE)


def _pe_normalised(text: str) -> tuple[str, list[int]]:
    """`text` (`\\`+newline already dropped) with each `$'...'` decoded and every quote and
    backslash dropped, and for each character of it the index in `text` it came from (every
    character a `$'...'` decodes to comes from its `$`). Misreading a quote only makes a name
    look unreadable (a deny), never readable. Raises `_PeUnreadable` on an undecodable one."""
    out: list[str] = []
    org: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            val = _py_ansi_c(text[i + 2:min(j, n)])
            if val is None:
                raise _PeUnreadable
            for c in val:
                out.append(c)
                org.append(i)
            i = j + 1
            continue
        if ch not in "'\"\\":
            out.append(ch)
            org.append(i)
        i += 1
    return "".join(out), org


def _pe_value(text: str, i: int, budget: list) -> str | None:
    """The value bash stores for the assignment word whose value starts at `text[i]`, when it
    is a literal (single-quoted, `$'...'`, double-quoted or bare text with no expansion); None
    when it is built at run time, is an array, or is unreadable here."""
    n = len(text)
    out: list[str] = []
    if i < n and text[i] in "(~":
        return None
    while i < n:
        budget[0] -= 1
        if budget[0] < 0:
            return None
        ch = text[i]
        if ch in _PE_WORD_END:
            break
        if ch in "(<>$`" and not (ch == "$" and i + 1 < n and text[i + 1] == "'"):
            return None
        if ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            budget[0] -= j - i
            out.append(text[i + 1:j])
            i = j + 1
        elif ch == "$":  # `$'...'`
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return None
            budget[0] -= j - i
            val = _py_ansi_c(text[i + 2:j])
            if val is None:
                return None
            out.append(val)
            i = j + 1
        elif ch == '"':
            i += 1
            while i < n and text[i] != '"':
                budget[0] -= 1
                c = text[i]
                if c in "$`":
                    return None
                if c == "\\" and i + 1 < n:
                    e = text[i + 1]
                    out.append(e if e in '$`"\\' else "\\" + e)
                    i += 2
                    continue
                out.append(c)
                i += 1
            if i >= n:
                return None
            i += 1
        elif ch == "\\":
            if i + 1 >= n:
                return None
            out.append(text[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return None if budget[0] < 0 else "".join(out)


def _pe_value_safe(stored: str) -> bool:
    """True when a prompt value `stored` cannot run code when prompt-expanded (see
    `_PE_DOLLAR_OK` above): one pass, tracking the `${...}` depth."""
    if "`" in stored or "\\" in stored or "@P" in stored:
        return False
    depth, k, n = 0, 0, len(stored)
    ref = False  # just past a `${NAME` or its `[N]`, where `:` starts its operator
    while k < n:
        ch = stored[k]
        if ref and ch == ":" and not stored.startswith((":-", ":+", ":?"), k):
            return False  # `${NAME:offset}` (arithmetic) or `${NAME:=v}` (a write)
        if ref and ch == "[":
            m = _PE_SUBSCRIPT_OK.match(stored, k)
            if not m:
                return False  # an arithmetic subscript
            k = m.end()
            continue
        ref = False
        if ch == "$":
            if stored.startswith("${", k):
                if stored.startswith("${!", k):
                    return False
                depth += 1
                k += 2
                while k < n and (stored[k].isalnum() or stored[k] == "_"):
                    k += 1
                ref = True
                continue
            if not _PE_DOLLAR_OK.match(stored, k):
                return False
        elif ch == "}" and depth:
            depth -= 1
        elif ch == "[" and depth:
            return False  # a subscript not right after a name: unreadable here
        k += 1
    return True


def _pe_raw_ok(text: str, p: int, name: str, budget: list) -> bool:
    """True when the name `name` at `text[p]` is a read or a literal-safe PS0-PS4 assignment
    as written."""
    e = p + len(name)
    if p >= 1 and text[p - 1] == "$" and (p < 2 or text[p - 2] != "$"):
        return True  # `$PS4` (a read; its value is never re-read)
    if (p >= 2 and text[p - 2:p] == "${") or (p >= 3 and text[p - 3:p] == "${#"):
        return bool(_PE_READ_OP.match(text, e))  # `${PS4}`, `${PS1:-}`, `${#PS4}`
    if (e >= len(text) or text[e] in _PE_WORD_END) and _PE_UNSET.search(text, max(0, p - 64), p):
        return True  # `unset HISTFILE`
    if p > 0 and text[p - 1] not in _PE_WORD_START:
        return False
    if text.startswith("=", e):
        v = e + 1
    elif text.startswith("+=", e) and name not in _PE_NO_WRITE:
        v = e + 2
    else:
        return False
    stored = _pe_value(text, v, budget)
    if name in _PE_NO_WRITE:
        # its value is code (or a history file): every write fails closed, but saving off
        return name == "HISTFILE" and stored in ("", "/dev/null")
    return stored is not None and _pe_value_safe(stored)


def _pe_names_ok(text: str, norm: str) -> bool:
    """(2) above, for `text` (`\\`+newline dropped) and its normalised copy `norm`."""
    if not _PE_NAME.search(norm) and not _PE_NAME.search(text):
        return True
    norm, org = _pe_normalised(text)
    budget = [4 * len(text) + 4096]
    raw_at = {}
    for m in _PE_NAME.finditer(text):
        if not _pe_raw_ok(text, m.start(), m.group(0), budget):
            return False
        raw_at[m.start()] = m.group(0)
    for m in _PE_NAME.finditer(norm):
        k, name = m.start(), m.group(0)
        start = org[k]
        if raw_at.get(start) != name or any(org[k + j] != start + j for j in range(len(name))):
            return False  # the copy shows a name the raw text does not spell as such
    return True


def _pe_dyn(c: "_PyCmd", j: int) -> bool:
    w = c.words[j]
    return j in c.dyn or _PY_SUBST in w or _PY_PWD_MARK in w


def _pe_compgen_runs(c: "_PyCmd", i: int) -> bool:
    """compgen's words from `i + 1`: any built at run time (it may become an option), a `-C`
    value, or a `-W` value holding `$`, a backtick, a backslash or (T-5667) a `(` (a
    `<(...)`/`>(...)` in it runs) (r2: an option's value glued to it, `-W'...'`, `-aC'...'`,
    is read too; every option-shaped word counts, `--` or not)."""
    words, n = c.words, len(c.words)
    if any(_pe_dyn(c, k) for k in range(i + 1, n)):
        return True
    j = i + 1
    while j < n:
        w = words[j]
        if w.startswith("-") and w != "--":
            for k in range(1, len(w)):
                ch = w[k]
                if ch in _PE_COMPGEN_ARG:
                    val = w[k + 1:]
                    if not val and j + 1 < n:
                        j += 1
                        val = words[j]
                    if ch == "C" or (ch == "W" and any(x in val for x in "$`\\(")):
                        return True
                    break
        j += 1
    return False


def _pe_cmd_runs(c: "_PyCmd") -> bool:
    """(3) above, for one lexed command."""
    words = c.words
    i = _py_head_index(words)
    if i >= len(words):
        return False
    head = words[i]
    if head == "fc":
        return True
    if head == "compgen":
        return _pe_compgen_runs(c, i)
    if head != "history":
        return False
    return any(_pe_dyn(c, j) or _PE_HISTORY_OPT.match(words[j]) for j in range(i + 1, len(words)))


def _pe_opts_on(c: "_PyCmd", j: int, letters: str, shell: bool) -> bool:
    """T-5667: `c`'s option words from `j` read as bash reads them (see above): True when
    they turn history (expansion) on, or (`set`) one is built at run time. Ends at `--`, `-`
    or the first word that is no option and no option's value (`set -- -o history`). One
    pass: every word is read once."""
    words, n = c.words, len(c.words)
    while j < n:
        if _pe_dyn(c, j):
            return not shell  # (a shell's run-time option or script word is T-5505's)
        w = words[j]
        if w in ("--", "-") or len(w) < 2 or w[0] not in "-+":
            return False
        j += 1
        if shell and w[1] == "-":
            if w == "--interactive":
                return True
            if w in _PE_SHELL_LONG_VALUE:
                j += 1
            continue
        on = w[0] == "-"
        for ch in w[1:]:
            if ch in "oO":
                if j >= n:
                    break
                if _pe_dyn(c, j):
                    return not shell
                v = words[j]
                if v and v[0] not in "-+":  # (else bash lists the options and takes no value)
                    if on and v in _PE_HIST_NAMES:
                        return True
                    j += 1
            elif on and ch in letters:
                return True
    return False


def _pe_hist_switch(c: "_PyCmd") -> bool:
    """r2 (M1), T-5667: True when `c` may turn history or history expansion on: a `set`
    (`set -o vi -H`, `set -euo pipefail -o $h`), a `shopt` (`shopt -so history`, `shopt -o -s
    history`, `shopt -s $x`) or a shell's own options (`bash -i`, `bash --rcfile f -i`, `bash
    -o history -H`), read by `_pe_opts_on` (see above)."""
    words = c.words
    i = _py_head_index(words)
    n = len(words)
    if i >= n:
        return False
    head = words[i]
    if head == "set":
        return _pe_opts_on(c, i + 1, "H", False)
    if head == "shopt":
        return any(_pe_dyn(c, j) or words[j] in _PE_HIST_NAMES for j in range(i + 1, n))
    for k in range(i, n):  # a shell, behind any wrapper (a superset: any shell-named word)
        if os.path.basename(words[k]) in _SHELL_NAMES and not _pe_dyn(c, k):
            return _pe_opts_on(c, k + 1, "iH", True)
    return False


def _pe_shell_script(c: "_PyCmd") -> bool:
    """T-5667: `c` runs a shell (behind any wrapper) with a script file operand and no code
    string (`bash -i f`, `bash -o history -H f`)."""
    words, n = c.words, len(c.words)
    i = _py_head_index(words)
    k = next((k for k in range(i, n) if os.path.basename(words[k]) in _SHELL_NAMES), n)
    k += 1
    while k < n:
        w = words[k]
        if w == "--":
            k += 1
            break
        if len(w) < 2 or w[0] not in "-+":
            break
        if w[1] == "-":
            k += 2 if w in _PE_SHELL_LONG_VALUE else 1
            continue
        if "c" in w[1:]:
            return False  # a code string: `_py_code_strings` reads it
        k += 1 + w.count("o", 1) + w.count("O", 1)
    return k < n and words[k] != "-"


def _pe_shell_unread(c: "_PyCmd", piped: bool, nested: bool) -> bool:
    """T-5667 (5): with history on, `c` reads its commands from a feed no reading here judges
    with history on: a stdin from a `< FILE` other than /dev/null (or one built at run time),
    a pipe, or (inside a `$(...)`) its command's stdin; or a script file operand."""
    try:
        run = _fed_consumer(c)
    except _FedUnknown:
        return True
    if run is None:
        return _pe_shell_script(c)
    if any(f != "/dev/null" for f in c.infiles) or piped:
        return True
    return nested and not (c.herestrs or c.heredocs or c.infiles)


def _pe_fed_unread(lex) -> bool:
    """T-5667 (5): a command of `lex` that `_pe_shell_unread` refuses (a pipe as the fed
    reading finds one: a `|` event before it, not `||`). Linear in the events."""
    ev = lex.events
    top = set()
    for idx, e in enumerate(ev):
        if not isinstance(e, _PyCmd):
            continue
        top.add(id(e))
        j = idx - 1
        while j >= 0 and ev[j] in ("\n", "("):
            j -= 1
        piped = j >= 0 and ev[j] == "|" and not (j >= 1 and ev[j - 1] == "|")
        if _pe_shell_unread(e, piped, False):
            return True
    return any(id(c) not in top and _pe_shell_unread(c, False, True) for c in lex.cmds)


def _pe_head_dyn(c: "_PyCmd") -> bool:
    i = _py_head_index(c.words)
    return i < len(c.words) and _pe_dyn(c, i)


def _pe_walk(text: str, depth: int, budget: list, hist: bool = False) -> bool:
    """True when `text`, or a code string in it, holds what the rule above refuses. `hist`:
    an outer text may turn history on for this one (a code string, a fed text)."""
    budget[0] -= len(text)
    if budget[0] < 0 or depth > _PY_CODE_DEPTH:
        return True
    text = text.replace("\\\n", "")
    try:
        # the same copy `_pe_normalised` makes, built at C speed without the index map
        norm = _PE_ANSI.sub(_pe_ansi_value, text) if "$'" in text else text
        norm = norm.translate(_PE_DROP)
        if _PE_AT_P.search(norm) or not _pe_names_ok(text, norm):
            return True
    except _PeUnreadable:
        return True
    desig = _PE_HIST_DESIG.search(norm) is not None
    # (T-5667: a SHELLOPTS a child shell reads may carry history: `env SHELLOPTS=... bash`)
    hist = hist or _PE_HIST_ON.search(norm) is not None or "SHELLOPTS" in norm
    if desig and hist:
        return True
    # a code string's text is this text's with quotes and backslashes removed, or `$'...'`
    # decoded: with no `$'` and none of the words the rule reads, no code string holds one
    deep = ("$'" in text or _PE_DEEP_HINT.search(norm) is not None
            or (("!" in norm or "^" in norm) and _PE_HIST_WORD.search(norm) is not None))
    codes = deep and _may_hold_code_strings(text)
    # r2 (M1/M4), T-5667: a `set`/`shopt`/shell word may turn history on (read by words),
    # which matters only to a designator, a run-time command word, a code string or a feed
    switch = not hist and _PE_SWITCH_HINT.search(norm) is not None and (
        desig or codes or "$" in norm or "`" in norm or "<" in norm or "|" in norm
        or _PE_SHELL_WORD.search(norm) is not None)
    if not codes and not hist and not switch and not _PE_LEX_HINT.search(norm):
        return False
    lex = _py_lex(text)
    if lex.frame_limited and (hist or switch or desig or _PE_FC_WORD.search(norm)
                              or "compgen" in norm or "history" in norm):
        return True  # the rest of the text was never lexed
    if switch and any(_pe_hist_switch(c) for c in lex.cmds):
        if desig:
            return True
        hist = True
    if hist and _pe_fed_unread(lex):
        return True  # T-5667 (5)
    for c in lex.cmds:
        if _pe_cmd_runs(c):
            return True
        if hist and _pe_head_dyn(c):
            return True  # r2 (M4): with history on, a run-time command word may be fc
        if codes or hist:
            for code in _py_code_strings(c, keep_dynamic=True) or ():
                if _pe_walk(code, depth + 1, budget, hist):
                    return True
    if hist:
        # (M4) a text fed to a shell here runs with this one's history on (`bash -i <<E`)
        fed, _chains, refused = _stdin_fed_codes(text)
        if refused:
            return True
        for code in fed:
            if code != text and _pe_walk(code, depth + 1, budget, True):
                return True
    return False


def _prompt_eval_match(command: str) -> bool:
    """T-5634: True when `command` prompt-expands a value the guard cannot read, stores or
    expands shell history, or runs an `fc` / `compgen -C`/`-W` string (see above)."""
    return _pe_walk(command, 0, [_PY_CODE_BUDGET * len(command) + 4096])


def _decide_readings(data: dict) -> dict | None:
    """`_decide`, and for a Bash command whose comment hides a `\\`+newline,
    `_decide` on the copy bash actually reads (`_comment_cut_text`, T-4393):
    deny if either denies. Last, the copy with each `eval ARGS` replaced by
    the line bash runs (`_eval_joined_text`, T-4475) is judged the same way;
    each copy only ever adds denials."""
    denial = _decide(data)
    if denial is not None or not data.get("agent_type") or data.get("tool_name") != "Bash":
        return denial
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not isinstance(command, str):
        return None
    denial = _decide_mapfile_cb(data, tool_input, command)  # T-5417: each -C callback as code
    if denial is not None:
        return denial
    if not _RN_ACTIVE[0]:  # T-5386: each renamed command word read as what it runs
        denial = _decide_renamed(data, tool_input, command)
        if denial is not None:
            return denial
    denial = _decide_gitcfg(data, tool_input, command)  # T-5414: git config values git runs
    if denial is not None:
        return denial
    denial = _decide_trap(data, tool_input, command)  # T-5441: each trap action as code
    if denial is not None:
        return denial
    denial = _decide_wrapcode(data, tool_input, command)  # T-5571: sg/firejail codes as code
    if denial is not None:
        return denial
    alt = _comment_cut_text(command)
    pe_texts = [command] if alt is None else [command, alt]  # T-5634: judged last, below
    if alt is not None:
        denial = _decide({**data, "tool_input": {**tool_input, "command": alt}})
        if denial is not None:
            return denial
    if not _FED_ACTIVE[0]:  # T-5332: the text a stdin-fed shell runs
        _FED_ACTIVE[0] = True
        try:
            denial = _decide_fed_copies(data, tool_input, command)
        finally:
            _FED_ACTIVE[0] = False
        if denial is not None:
            return denial
    # T-4475. A scan overflow here, as in any derived reading, sets
    # `_SCAN_OVERFLOWED`, and `decide()` denies it.
    joined = _eval_joined_text(command)
    if joined == command:
        joined = None
    if joined is not None:
        pe_texts.append(joined)
        denial = _decide({**data, "tool_input": {**tool_input, "command": joined}})
        if denial is not None:
            return denial
        alt = _comment_cut_text(joined)
        if alt is not None:
            pe_texts.append(alt)
            denial = _decide({**data, "tool_input": {**tool_input, "command": alt}})
            if denial is not None:
                return denial
    if not _DC_ACTIVE[0] and not _ENV_S_ACTIVE[0]:  # T-5407: a non-literal command word as git
        denial = _decide_dyncmd(data, tool_input, command)
        if denial is not None:
            return denial
    denial = _decide_script_files(data, tool_input, command)  # T-5505: each script file run
    if denial is not None:
        return denial
    denial = _decide_noncomputable(data, tool_input, command)  # T-5571: uncomputed stdin feeds
    if denial is not None:
        return denial
    # T-5634: after every other rule, so theirs come first; the comment-cut and eval-joined
    # copies too (`_decide` alone judges those)
    if any(_prompt_eval_match(t) for t in pe_texts):
        return _PE_DENY
    if _ENV_S_ACTIVE[0]:
        return None
    if not _SCAN_HEREDOCS[0] and not _ENV_S_HEREDOC_OP.search(command):
        # r5: `decide()`'s second (pre-B(1)) reading differs from the first only in how it
        # reads heredoc bodies; a `<<<` here-string (which also starts it) has none: the
        # decoded reading would give the same verdict again.
        return None
    # T-5363: each env -S value read as the words env runs, in the copy bash runs (the eval
    # join, then the comment cut, of the readings above)
    return _decide_env_s(data, tool_input, command, alt if alt is not None else joined or command)


def _env_s_sparse(text: str, fed: frozenset, orig: str | None = None) -> tuple[str, list]:
    """T-5363 r2/r3: (`text` with each heredoc body the decoded reading must not read again
    emptied, its delimiter line kept; per body in order, its text when emptied, else None).
    Emptied: one that cannot hold an env -S command; one the original's fed reading judged
    (`fed`, its texts: a shell reads it, and its readings decode it); a quoted one whose
    reader is provably no shell (`_data_heredoc_spans`: data). Kept, and decoded with the
    command: any other (an unquoted body's `$(...)` runs in this shell; a reader the guard
    does not model, `ssh host <<'E'`, may run it)."""
    if "<<" not in text:
        return text, []
    scanned = _scan_cached(text, True)
    if scanned is None or not scanned.bodies:
        return text, []
    data = None
    # `text` read from the original `orig` (its eval join or comment cut, which may rewrite a
    # body): a body stands for the original's body in the same place when the counts agree.
    was_fed: list = []
    if orig is not None and orig != text:
        oscan = _scan_cached(orig, True)
        if oscan is not None and len(oscan.bodies) == len(scanned.bodies):
            was_fed = [orig[a:e] in fed for a, e, _r, _x in sorted(oscan.bodies)]
    parts, pos, bodies = [], 0, []
    named = None  # T-5379: whether the text names env (asked once, when needed)
    for k, (a, e, _r, x) in enumerate(sorted(scanned.bodies)):
        if a < pos:
            return text, []  # bodies that do not line up: read the text as it is
        body = text[a:e]
        # (r6: a cluster whose value is glued on unquoted; a quoted one, `env -S'ls'`, is
        # T-5363's to read, and keeping each such body would read it again)
        if "S" in body and _ENV_S_CLUSTER_BARE.search(body):
            # T-5379: a body with a glued -S cluster, in a text that names env, is kept for
            # the cluster split (`read b <<'E'` holding `-Sgh pr` for a later `$a $b`)
            if named is None:
                named = _env_s_names_env(text if orig is None else orig)
            if named:
                bodies.append(None)
                continue
        if _env_s_maybe(body) and body not in fed and not (was_fed and was_fed[k]):
            if data is None:
                data = set(_data_heredoc_spans(text, _hd_join_reader_targets))
            if x or (a, e) not in data:
                bodies.append(None)
                continue
        bodies.append(body)
        parts.append(text[pos:a])
        pos = e
    parts.append(text[pos:])
    return "".join(parts), bodies


def _env_s_restore(text: str, bodies: list) -> str | None:
    """r3: `text` (decoded from an `_env_s_sparse` copy) with each emptied body put back, or
    None when its bodies no longer line up with `bodies`."""
    if not any(b is not None for b in bodies):
        return text
    scanned = _scan_cached(text, True)
    if scanned is None or len(scanned.bodies) != len(bodies):
        return None
    parts, pos = [], 0
    for (a, e, _r, _x), body in zip(sorted(scanned.bodies), bodies):
        if body is None:
            continue
        if a != e or a < pos:
            return None
        parts.append(text[pos:a])
        parts.append(body)
        pos = e
    parts.append(text[pos:])
    return "".join(parts)


def _decide_env_s(data: dict, tool_input: dict, command: str, runs: str) -> dict | None:
    """T-5363: the decoded env -S reading (`_env_s_decoded_text`) of `runs`, the copy of
    `command` bash runs (eval joined, comment cut). r2/r3: only what the decode can change is
    read again. `_decide` judges the decoded copy with its quoted and already-fed heredoc
    bodies emptied (`_env_s_sparse`: they are not decoded here, and not judged twice), and
    the stdin-fed reading judges the texts of the decoded copy, bodies put back, that the
    original's (`_decide_fed_copies`) did not already judge: a body is the same text in both,
    so only a shell the decode reveals reads anything new (`env -S'-i bash' <<'E'`,
    `printf CODE | env -S '-i source /dev/stdin'`). The eval and comment-cut readings are not
    repeated: `runs` is their result."""
    fed = frozenset(_stdin_fed_codes(command)[0])
    # T-5382: an env option word an unquoted heredoc body builds from an expansion
    if _env_s_body_unreadable(runs) or (runs != command and _env_s_body_unreadable(command)):
        return _ENV_S_UNREADABLE_DENY
    sparse, bodies = _env_s_sparse(runs, fed, command)
    decoded = _env_s_decoded_text(sparse)
    if decoded is None and runs != command:
        sparse, bodies = _env_s_sparse(command, fed)
        decoded = _env_s_decoded_text(sparse)
    if decoded is _ENV_S_UNREADABLE or (
            decoded is not None and len(decoded) > 2 * BASH_COMMAND_MAX_CHARS):
        return _ENV_S_UNREADABLE_DENY
    # r7: value lines of glued -S clusters in the emptied heredoc bodies (not kept: each
    # body is read once, `xargs env <<'E'` / `-S'sudo true'` / `E`)
    blines = _env_s_body_value_lines(command, bodies)
    # r6: an env -S command that does not start a word (`${x:-env -Sgit push}`, `printf -v x
    # %s'env -S...'`, `IFS=:; x='env:-Sgh:pr'`) is out of the word reading's reach: its glued
    # value is split off as text (`_env_s_glued_split`), for the raw-text rules that read the
    # same text without env (`${x:-git push}`) to see it.
    # When nothing else was decoded and only lines are appended (`alone`), those lines are the
    # whole reading: the original text was judged as it is already.
    glued = _env_s_glued_split(sparse if decoded is None else decoded, alone=decoded is None)
    if glued is _ENV_S_UNREADABLE:
        return _ENV_S_UNREADABLE_DENY  # T-5382 (`_env_s_cluster_split`)
    if glued is not None:
        if isinstance(glued, tuple):
            glued = glued[0]
            bodies = []  # appended lines only: no heredoc bodies to put back
        decoded = glued
    if blines:
        if decoded is None and "${" in blines and _ENV_S_VALUE_VAR.search(blines):
            # T-5382: a `${NAME}` line is judged after the text that may set NAME (`export
            # P=gh; xargs env <<'E'` / `-S'${P} pr merge 1'`), its bodies put back as usual
            decoded = sparse + "\n" + blines
        elif decoded is None:
            decoded, bodies = blines, []  # (the lines alone: the original was judged as is)
        else:
            decoded = decoded + "\n" + blines
    if decoded is None:
        return None
    full = _env_s_restore(decoded, bodies)
    if full is None:
        # the bodies no longer line up: the whole text decoded (slower, as complete)
        full = _env_s_decoded_text(runs)
        if full is _ENV_S_UNREADABLE:
            return _ENV_S_UNREADABLE_DENY
        full = full or runs
    _ENV_S_ACTIVE[0] = True
    try:
        denial = _decide({**data, "tool_input": {**tool_input, "command": decoded}})
        if denial is None and not _FED_ACTIVE[0]:
            # A fed text the decode rewrote (`printf 'env -S...' | bash`: the printf word is
            # decoded too) is the decoded form of one the original's fed reading judged, with
            # that form (its own readings decode it): not judged again.
            # (Decoded only when the decoded copy feeds a text the original did not.)
            codes, _chains, refused = _stdin_fed_codes(full)
            judged = set(fed)
            if refused or any(c not in judged and c.strip() for c in codes):
                for code in fed:
                    if _env_s_maybe(code):
                        got = _env_s_decoded_text(code)
                        if isinstance(got, str):
                            judged.add(got)
                _FED_ACTIVE[0] = True
                try:
                    denial = _decide_fed_copies(data, tool_input, full, frozenset(judged))
                finally:
                    _FED_ACTIVE[0] = False
    finally:
        _ENV_S_ACTIVE[0] = False
    return denial


# (T-5407: shared by `_decide` and its dynamic command-word copies)
_GIT_PUSH_WHAT = (
    "to git push (push/PR/merge is HO-exclusive; every subagent — including "
    "project-orchestrators and named instances — commits on its branch only and reports "
    "to HO, who pushes after review)"
)


def _decide(data: dict) -> dict | None:
    """Return a deny-payload to BLOCK, or None to ALLOW.

    Allows everything on the main thread (agent_type absent). For subagents, blocks
    the explicit drift-dangerous patterns only; everything else passes. Covers four
    tool surfaces: drift-dangerous Bash commands; Edit/Write/NotebookEdit of
    protected canonical docs (the audit-found gap, T-1381); Read of a credential
    file (T-4139, closing the Read-tool sibling of T-4135's Bash coverage); and a
    Grep whose `path` operand names a credential file directly (T-4139; a
    directory-scoped Grep is a named, out-of-scope gap — see the Grep branch below).
    """
    agent_type = data.get("agent_type")
    if not agent_type:
        return None  # main thread / HO — never restricted by this guard

    tool_name = data.get("tool_name")

    if tool_name == "Bash":
        command = (data.get("tool_input") or {}).get("command") or ""
        if len(command) > BASH_COMMAND_MAX_CHARS * (
                2 if _ENV_S_ACTIVE[0] or _RN_ACTIVE[0] or _DC_ACTIVE[0] or _GC_ACTIVE[0] else 1):
            # T-4389: before ANY parsing; subagents only (agent_type gated above). T-5363 r3: a
            # decoded env -S copy of a command under the cap may grow (its code strings
            # re-quoted) up to twice it; past that `_decide_env_s` fails closed first. T-5386:
            # so may a renamed copy (`_decide_renamed` fails closed past twice the cap). T-5414:
            # and a git config code copy (`_decide_gitcfg`, likewise).
            return _TOO_LONG
        if not command:
            return None
        if _DC_ACTIVE[0]:
            # T-5407: a dynamic command-word copy asks one thing, whether it pushes (the
            # other rules judged the command itself; a copy's `"git"` word is no new hit)
            runs = _data_heredoc_text(command)
            if _push_match(runs) or _shell_formed_push_match(runs):
                return _reason(_GIT_PUSH_WHAT)
            return None
        if _FED_CWD_ONLY[0]:
            # T-5332 r5: a fed text judged again at another candidate cwd: only the rules
            # that read the cwd (the rest gave the same verdict at the payload cwd).
            return _decide_cwd_rules(command, data.get("cwd") or "")
        if _scan_depth_may_exceed(command):
            if _executable_code_scan_overflow(command):
                return _SHELL_NESTING_TOO_DEEP
        if _TOKEN_READ.search(command):
            return _reason("to read the Telegram bot token file directly")
        if _BOT_API.search(command):
            return _reason("to contact the Telegram Bot API directly (use the reply tool via your dispatcher)")
        if _extglob_enable_match(command):
            return _reason(
                "to enable bash's extglob (`shopt -s extglob`, `bash -O extglob`, or a BASHOPTS "
                "value): extglob opens glob forms (`@(..)`, `+(..)`, `!(..)`) this hook cannot "
                "model, so a destroy it would deny can pass (T-5326). Use plain globs, `find`, "
                "or a script written with the Write tool. If the words are only text (a "
                "commit message, a note), put it in a file: `git commit -F <file>`"
            )
        if _ho_records_match(command, data.get("cwd") or ""):
            return _reason(_HO_RECORDS_WHAT)
        # T-4328: the checks that ask "does this run something" read `runs`, the
        # command with file-writing quoted heredoc bodies blanked; the rest
        # read the raw text.
        runs = _data_heredoc_text(command)
        denial = _decide_py_startup(runs, data.get("cwd") or "")
        if denial is not None:
            return denial
        if _push_match(runs) or _shell_formed_push_match(runs):
            return _reason(_GIT_PUSH_WHAT)
        identity_what = _git_identity_match(command, data.get("cwd") or "")
        if identity_what:
            return _reason(identity_what)
        if _gh_api_mutating_rest_write(runs) or _gh_api_graphql_mutation(runs):
            return _reason(
                "to write to GitHub via `gh api` (a non-GET REST call, or a GraphQL "
                "mutation) — every GitHub write (push-equivalents, merges, repo "
                "settings, protection, keys) is reserved to the head orchestrator "
                "(T-1673, T-4135); reads stay allowed. A read whose arguments come "
                "from `$(...)` or a standalone `$VAR` counts as a write here: assign "
                "the value first (`PR=$(...); gh api repos/o/r/pulls/$PR`). Route "
                "any real write through your dispatcher"
            )
        if _GH_REPO_ADMIN.search(runs):
            return _reason(
                "to change or remove a repository via `gh repo edit` / `delete` / "
                "`archive` / `unarchive` / `rename` — reserved to the operator/the head "
                "orchestrator (T-4135)"
            )
        gh_what = _gh_cli_violation(runs, data.get("cwd") or "") or _gh_raw_floor(runs)
        if gh_what:
            return _gh_cli_reason(gh_what)
        nonliteral = _NonLiteralSink()  # T-4426 r3: filled by the escalation walks
        escalation = _escalation_reason(runs, sink=nonliteral)
        if escalation == "dynamic":
            return _reason(
                "to run a command whose name this hook cannot read, in a command that "
                "mentions `sudo`/`su`/`doas`/`pkexec`: the command word is dynamic "
                "($VAR/$(...)); use a literal command name. Workers hold no elevated "
                "credentials of their own (T-1345). If this is file content (a heredoc "
                "report, commit message or script: a line starting with a backtick, or "
                "a `(`/`;` followed by one, reads as a command here), write it with the "
                "Write tool instead, or use `git commit -F <file>`"
            )
        if escalation == "option":
            return _reason(
                "to run a wrapper (flock, parallel, xargs, env, ...) with a long option "
                "it does not accept as spelled (unknown, or an abbreviation of more than "
                "one option), in a command that mentions `sudo`/`su`/`doas`/`pkexec`/"
                "`sudoedit`/`run0`/`runuser` or that reads a piped stdin: this hook "
                "cannot tell which word is the "
                "command (T-4361). Workers hold no elevated credentials of their own "
                "(T-1345). Spell the wrapper's long options in full"
            )
        if escalation == "stdin":
            return _reason(
                "to feed a shell its commands through a pipe or here-string "
                "(`... | bash`, `bash <<< ...`) in a command that mentions `sudo`/`su`/"
                "`doas`/`pkexec`/`sudoedit`/`run0`/`runuser`: the lines it runs are "
                "built at run time. Workers hold no elevated credentials of their own "
                "(T-1345). Run the commands directly, or write a script with the Write "
                "tool and run `bash script.sh`"
            )
        if escalation:
            return _reason(
                "to escalate privileges (`sudo`/`su`/`doas`/`pkexec`/`sudoedit`/`run0`/"
                "`runuser`, `machinectl shell`, or `systemd-run` without `--user`), "
                "directly or behind a command wrapper (env/nohup/time/exec/xargs/"
                "command/nice/ionice/timeout/stdbuf/eval/watch/setsid/ssh/taskset/"
                "chrt/flock/strace/script/parallel/sem/coproc/systemd-run --user) — workers hold no "
                "elevated credentials of their own (T-1345). If the escalator word "
                "is only an ARGUMENT after a wrapper (a test filter or search term, "
                "as in `timeout 60 pytest -k sudo`), quote it: `timeout 60 pytest "
                "-k \"sudo\"` passes. If this is file content (a heredoc script), "
                "write the file with the Write tool instead"
            )
        if _credential_file_read(command):
            return _reason(
                "to read a credential file directly (GitHub PAT, gh hosts config, "
                "Echo API keys, the sudo password, git/Forgejo credentials, an SSH "
                "private key, or a Claude/codex OAuth session) — dispatched workers "
                "hold no credentials of their own (T-1345)"
            )
        if nonliteral.hit:  # T-4426 r3 / T-4481
            return _NONLITERAL_COMMAND_WORD
        if nonliteral.unreadable:  # T-4426 r4 / T-4506
            return _UNREADABLE_QUOTING
        if nonliteral.undecided:  # T-4506
            return _UNREADABLE_COMMAND_WORD
        return None

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"):
        tool_input = data.get("tool_input") or {}
        if tool_name == "apply_patch":
            paths = _apply_patch_paths(tool_input.get("command") or "")
        else:
            paths = [tool_input.get("file_path") or tool_input.get("notebook_path") or ""]
        for path in paths:
            if path and (
                _PROTECTED_DOC.search(path)
                or _protected_doc_via_worktree(path, data.get("cwd") or "")  # T-3662 worktree-shape gap
            ):
                return _reason(
                    "to Edit/Write/patch a protected canonical doc (rulebook / ADR / ledger / "
                    "notes / bugs / incidents / lessons / sop / README / BACKLOG / goals) — "
                    "these are orchestrator-owned; propose the change to your dispatcher instead"
                )
            if path and _py_startup_path(path, data.get("cwd") or ""):
                return _reason(_PY_STARTUP_WHAT.format(hook_dir=_HOOK_DIR))
        return None

    if tool_name == "Read":
        file_path = (data.get("tool_input") or {}).get("file_path") or ""
        if file_path and _credential_path_match(file_path, data.get("cwd") or ""):
            return _reason(
                "to Read a credential file directly (GitHub PAT, gh hosts config, "
                "Echo API keys, the sudo password, git/Forgejo credentials, an SSH "
                "private key, or a Claude/codex OAuth session) — dispatched workers "
                "hold no credentials of their own (T-1345)"
            )
        return None

    # T-4139: a Grep whose `path` operand names a credential file directly
    # returns its content the same as a Read — the identical judgment applies.
    # KNOWN GAP (named in the ticket, not silently accepted): a directory-
    # scoped Grep (`path` absent, defaulting to cwd, or `path` a bare
    # directory like `~` or `~/.ssh`) is NOT judged here — matching on the
    # `path` operand alone can't tell "this directory recursively contains a
    # credential file among many others" from an ordinary directory search
    # without over-blocking routine work. Left unhandled rather than adding a
    # filesystem `isdir` probe, which would add a filesystem dependency this
    # hook doesn't otherwise carry and still couldn't safely resolve the
    # directory-recursion case.
    if tool_name == "Grep":
        path = (data.get("tool_input") or {}).get("path") or ""
        if path and _credential_path_match(path, data.get("cwd") or ""):
            return _reason(
                "to Grep a credential file directly (the search `path` itself "
                "names a T-4135 credential file) — dispatched workers hold no "
                "credentials of their own (T-1345)"
            )
        return None

    return None


# T-4144: codex's file-edit tool. On codex-cli 0.147.0 its PreToolUse payload is
# tool_name "apply_patch" with the whole patch text in tool_input.command (probed
# live), and every path the patch touches is on one of these header lines, usually
# relative to the payload's cwd. Codex's Bash payload already matches Claude's
# shape ({"command": "..."}), and it sets agent_type/agent_id only on subagent calls,
# so the presence gate above works unchanged on codex.
_APPLY_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE
)


def _apply_patch_paths(patch: str) -> list[str]:
    return [m.group(1) for m in _APPLY_PATCH_PATH.finditer(patch)]


class _DeadlineExpired(BaseException):
    """Raised by the SIGALRM handler. A BaseException so no `except Exception` inside decide()
    (or the fail-open handler in main) can swallow it."""


def _on_alarm(signum, frame):
    raise _DeadlineExpired


def _decide_by_deadline(data: dict, deadline: float) -> dict | None:
    """decide(data), or _TIMED_OUT if it has not returned within `deadline` seconds (T-4389).

    Primary: a SIGALRM interval timer (POSIX, main thread only; CPython's regex engine polls for
    signals, so a backtracking match is interrupted too). The timer is disarmed in `finally`
    BEFORE the caller prints anything, so a late alarm can neither double nor corrupt the one
    JSON line on stdout; an alarm landing between decide()'s return and the disarm just turns
    the result into the timeout deny (still exactly one output). Fallback where SIGALRM is
    unavailable: decide() runs on a daemon thread and the caller waits at most `deadline`; an
    exception there re-raises here, so main() turns it into the T-4505 fail-closed deny.
    """
    import signal
    import threading

    if hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer") \
            and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGALRM, _on_alarm)
        try:
            signal.setitimer(signal.ITIMER_REAL, deadline)
            try:
                return decide(data)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        except _DeadlineExpired:
            return _TIMED_OUT
        finally:
            if previous is not None:  # None: a handler not installed from Python; leave it
                signal.signal(signal.SIGALRM, previous)

    box: dict = {}

    def run() -> None:
        try:
            box["payload"] = decide(data)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, name="worker-guard-decide", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        return _TIMED_OUT  # checked deadline; the daemon thread dies with the process
    if "error" in box:
        raise box["error"]
    return box.get("payload")


def _elapsed_since_process_start(started_monotonic: float | None = None) -> float:
    """Seconds since THIS process started (T-4389 r2): the harness counts its hook timeout from
    spawn, so the guard's budget must too.

    Primary: /proc/self/stat field 22 (starttime, clock ticks since boot) against CLOCK_BOOTTIME,
    the clock the kernel counts it on. Tick truncation can only OVER-state the elapsed time (by
    at most one tick), the fail-closed direction. Fallback without /proc: the time.monotonic()
    the entry shim recorded as its first statement (`started_monotonic`), which misses only
    interpreter start. With neither, 0.0: the budget then runs from now.
    """
    try:
        import time

        with open("/proc/self/stat", "rb") as f:
            stat = f.read()
        fields = stat[stat.rindex(b")") + 2:].split()  # comm may hold spaces/parens
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")  # field 22 = index 19 after comm
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - started)
    except Exception:
        pass
    if started_monotonic is not None:
        import time

        return max(0.0, time.monotonic() - started_monotonic)
    return 0.0


def main(raw: str | None = None, deadline: float | None = None,
         started_monotonic: float | None = None) -> None:
    """Run the guard on one PreToolUse payload and exit.

    `raw` is the already-read stdin text, handed over by the entry shim (worker-guard.py, T-4177)
    so stdin is read exactly once. Called with no argument (the impl run as a script) it reads
    stdin itself, which is the pre-shim guard's behavior verbatim. Every failure below is a
    fail-open silent exit 0, as before, with two fail-CLOSED exceptions for a SUBAGENT
    decision (a dict payload with a truthy agent_type, the exact test decide() gates on): one
    that has not finished DECIDE_DEADLINE_S after this PROCESS started is a deny (T-4389), and
    one that raises is a deny naming the exception class, with one stderr line (T-4505). If the
    budget is already spent when main() gets here, the deny is immediate. The main thread never runs under
    the clock, so it is never restricted. `deadline` is for tests only and can only SHORTEN the
    budget: only a value in (0, DECIDE_DEADLINE_S] is honoured. Nothing reads it from the
    environment. `started_monotonic` is the shim's start stamp, used only where /proc is missing.
    """
    try:
        if raw is None:
            raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # fail-open
    try:
        if isinstance(data, dict) and data.get("agent_type"):
            # Only a finite value in (0, DECIDE_DEADLINE_S] is honoured: 0 would DISARM
            # setitimer and NaN compares false, so both fall back to the constant.
            limit = deadline if deadline is not None and 0 < deadline <= DECIDE_DEADLINE_S else DECIDE_DEADLINE_S
            remaining = limit - _elapsed_since_process_start(started_monotonic)
            if not remaining > 0:  # budget spent before we got here (NaN too): fail closed
                payload = _TIMED_OUT
            else:
                with _memory_cap():
                    payload = _decide_by_deadline(data, min(limit, max(_DEADLINE_FLOOR_S, remaining)))
        else:
            payload = decide(data)
    except Exception as exc:
        if not (isinstance(data, dict) and data.get("agent_type")):
            sys.exit(0)  # main thread: fail-open, as before
        # T-4505: a subagent decision that raises is a deny (a raise used to skip every rule,
        # T-4498). Only the class is named: the message may carry the command's text.
        name = type(exc).__name__
        try:
            sys.stderr.write(f"worker-guard: fail-closed: decide raised {name} (T-4505)\n")
        except Exception:
            pass
        payload = _deny(
            f"BLOCKED (worker-guard, T-4505): worker-guard raised {name} deciding this command; "
            "refused (fail-closed). Split or simplify the command, and report it to your dispatcher."
        )
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
