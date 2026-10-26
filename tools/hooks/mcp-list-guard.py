#!/usr/bin/env python3
"""PreToolUse hook — refuse `claude mcp list` when THIS account's telegram poller is live (T-0894 prong A / T-0996).

The single-poller-409 failure mode: a transient `claude` that initializes the
telegram plugin (chiefly `claude mcp list`) opens a SECOND getUpdates poll on the
same bot → 409 → the persistent inbound poller exits → the agent goes comms-dark
(the 2026-06-06/07 Orchestrator incident). The discipline is "never `claude mcp list` a
live bot; diagnose via `ps` + `curl getMe`." This hook ENFORCES that structurally.

Scope: checks the CURRENT account's own bridge (`~/.claude/channels/telegram/
bot.pid` → a live `bun ... server.ts`). `claude mcp list` in this account would
load this account's plugin and poll this account's bot, so the relevant poller is
the local one — no cross-account/sudo needed. The poller probe itself stays
fail-open (an unreadable poller state → allow). T-4392 phase 2a: the HOOK is
FAIL-CLOSED — an exception, an ambiguous payload, its own 15s deadline or the
262,144-character command cap DENIES (exit 2), via tools/hooks/_failclosed.py (which also
holds the break-glass); see main().

Wire as a PreToolUse hook with matcher "Bash" in each agent's settings.json
(deferred careful per-agent deploy — see T-0996). Modeled on act-vs-ask-gate.py.

Match scope (WIDENED by T-1623; was `claude mcp list` only): ANY transient
`claude` invocation with the plugin enabled steals the telegram token (the
ratified no-second-claude rule — `--bare` included; self-inflicted twice), so
the guard now blocks `claude` in COMMAND POSITION (start of command / after
; && || | backtick or subshell-open). Quoted spans are stripped before
matching, which fixes the T-1623 false-positive class: the guard once fired on
the STRING naming the subcommand inside a quoted ticket-body argument — a
mention is not an invocation. Exempted safe shapes: --version/--help/-h (no
session, no plugin) and --print/-p WITH --safe-mode (the veneration resurrect
design — plugin-less by construction). `sudo -u claude …` does not match
(username argument, not a command). Blocking still requires a CONFIRMED-live
local poller.

T-3192: the command-position match ALSO chains through common exec-through
wrappers (`command`/`exec`/`nohup`/`setsid`/`time`/`timeout`/`nice`/`env`/
`stdbuf`/`ionice`/`chrt`, in any order, any combination) before it reaches
`claude` — `timeout 45 claude mcp list` bypassed the guard entirely prior to
this fix, and a wrapped transient `claude` killing a live poller is exactly
the 2026-08-24 comms-dark incident this guard exists to prevent. Each
wrapper's own arguments are matched by a deliberately TIGHT grammar (option
flags, numeric/duration tokens, ALL-CAPS signal/env names only — never a bare
word), so `nice -n 5 du -sh /home/claude` still passes: on an account
literally named `claude`, a looser wrapper grammar would reintroduce the
T-2251 false-positive class this guard already fixed once. `sudo` is
deliberately NOT a wrapper here — cross-account sudo stays a non-goal.

T-3193: heredoc BODIES are DATA, not command text — a quoted-heredoc whose
prose mentions `claude` (backticked, or as a literal line meant to be
written to a file) must not be read as an invocation, exactly like a
quoted span. Before the quote/substitution passes run, `<<`/`<<-` operators
(bare, 'single', or "double"-quoted tags) are located and their bodies —
from the first newline after the operator's line through the terminator
line (the tag alone, leading tabs allowed for `<<-`) — are dropped
entirely; the operator's own line is kept (so `claude <<'EOF'` still
matches), and anything after a real terminator is kept too. `<<<`
here-strings are excluded structurally (a `<<` immediately adjacent to a
third `<` never opens a heredoc). An operator with NO matching terminator
line is NOT treated as a heredoc at all (T-3193 REBOUND 1, 2026-08-25):
quoted prose containing a bare `<<` — `echo "left << shift"` followed by a
real invocation on a later line — was misread as an unterminated heredoc
and swallowed the invocation as fail-open data. Nothing is stripped for an
unmatched tag; the remainder of the command is matched normally. This is
DENY-biased on purpose, reversing this guard's original "unterminated ->
fail open" posture for heredocs specifically: a misparse must land on the
deny side (a false deny costs a re-shaped command, a false allow costs
comms-dark — the asymmetric cost this whole guard exists to respect).
Accepted residual, contrived and documented rather than fixed: quoted
prose containing `<<TAG` WITH a coincidental exact `TAG`-only line later in
the same command still over-strips. Non-goal: an interpreter heredoc that
launches the binary INDIRECTLY (`python3 - <<'PY'\nsubprocess.run(["claude",
"mcp","list"])\nPY`) still passes — but it already did before this change
too, since the token sits inside quotes; this guard is a shell-syntax
discipline aid, not a sandbox. Rebalance trigger: an observed indirect
launch from a live seat.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# T-3193: heredoc BODIES are DATA, not command text (same posture as a
# quoted span) -- must be removed BEFORE _QUOTED_RE/_SUBST_RE run, or a
# quoted tag gets destroyed and a backtick inside the body gets read as a
# real segment boundary (see module docstring). `(?<!<)` / `(?!<)` excludes
# `<<<` (here-string) structurally, not by incident. The bareword tag
# branch requires an identifier-shaped lead (`[A-Za-z_]`) rather than the
# looser `\w+`, so `$((1<<3))` never matches an operator at all -- it
# doesn't need to fall through to the "no following newline" fallback.
_HEREDOC_OP_RE = re.compile(
    r"(?<!<)<<(?!<)(-)?\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([A-Za-z_]\w*))"
)


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc BODIES (the lines between the operator and the
    terminator) so they're never seen as command text (T-3193). The
    operator's own line is kept verbatim (`claude <<'EOF'` still matches
    on that line); text after a real terminator line is kept too. An
    operator with NO matching terminator line is NOT a heredoc (T-3193
    REBOUND 1) -- nothing is stripped for that tag, and scanning resumes
    normally right after the operator's line. DENY-biased on purpose: a
    misparse must land on the deny side (a false deny costs a re-shaped
    command, a false allow costs comms-dark).

    T-3193 REBOUND 2: terminator lookup is an O(log n) index bisect, not
    an O(n) regex re-scan per candidate tag -- ordinary multi-line text
    with many `<<"literal"`-shaped false candidates (C++ `std::cout << "x"
    << y;` echoed in a command) made the old per-tag `re.search` quadratic,
    and this hook gates every Bash call on the account."""
    if "<<" not in command:
        return command
    n = len(command)
    # Terminator index, built ONCE per call in a single O(n) pass: maps a
    # line's exact content (or, for `<<-` tags, its content with leading
    # tabs stripped) to the ASCENDING list of offsets where that line
    # STARTS -- ascending because lines are appended top-to-bottom.
    # `bisect_left` then finds the first candidate terminator at or after
    # a given body_pos in O(log k) instead of scanning the string.
    exact = defaultdict(list)
    stripped = defaultdict(list)
    line_start = 0
    while line_start <= n:
        nl = command.find("\n", line_start)
        line_end = nl if nl != -1 else n
        line = command[line_start:line_end]
        exact[line].append(line_start)
        stripped[line.lstrip("\t")].append(line_start)
        if nl == -1:
            break
        line_start = nl + 1

    out = []
    pos = 0
    while pos < n:
        m = _HEREDOC_OP_RE.search(command, pos)
        if m is None:
            out.append(command[pos:])
            break
        line_end = command.find("\n", m.end())
        if line_end == -1:
            # no newline anywhere after the operator -> no body is possible
            out.append(command[pos:])
            break
        # multiple heredocs can stack on one line (`cat <<'A' <<'B'`) --
        # collect every operator on THIS line, in order; each consumes the
        # next body in turn.
        tags = []
        scan_pos = m.start()
        while True:
            mm = _HEREDOC_OP_RE.search(command, scan_pos, line_end)
            if mm is None:
                break
            tag = mm.group(2) or mm.group(3) or mm.group(4)
            tags.append((tag, mm.group(1) is not None))
            scan_pos = mm.end()
        out.append(command[pos:line_end + 1])  # keep the operator line
        body_pos = line_end + 1
        for tag, dash in tags:
            candidates = stripped[tag] if dash else exact[tag]
            i = bisect.bisect_left(candidates, body_pos)
            if i == len(candidates):
                # no terminator at/after body_pos -> not a heredoc (T-3193
                # REBOUND 1): strip nothing for this tag, leave body_pos
                # untouched, and let any remaining tags (or the outer
                # scan) proceed normally.
                continue
            term_start = candidates[i]
            term_nl = command.find("\n", term_start)  # bounded to ONE line
            body_pos = term_nl + 1 if term_nl != -1 else n
        pos = body_pos
    return "".join(out)


# Quoted spans are stripped before matching: a quoted mention of a claude
# command is prose/an argument, not an invocation (the T-1623 false positive).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# T-2251: command-substitution delimiters are SEGMENT boundaries, and must be
# normalised to one before the command-position match runs.
#
# Without this, the env-assignment prefix below swallows `out=$(cd ` as though it
# were a plain `VAR=value ` pair — `out` is the name, `$(cd` is a perfectly good
# \S* value, the space closes it — which leaves the PATH token `/home/claude`
# sitting in command position. `out=$(cd /home/claude && ...)`, an extremely
# common shape for the account literally NAMED claude, was therefore read as an
# invocation and denied. Neither `$( )` nor the `cd` alone does it; only together,
# which is exactly what the T-2251 bisection found.
#
# Normalising `$(`, `)` and backtick to `;` makes them true separators. That fixes
# the false positive AND keeps the deny posture where it belongs: `FOO=$(date)
# claude -p hi` becomes `FOO=;date; claude -p hi`, where `claude` is genuinely in
# command position and still blocks. Adding boundaries can only ever create more
# command positions, never fewer — so this cannot turn a real invocation into a
# miss, which is the direction that would reintroduce the 409 bridge-death.
_SUBST_RE = re.compile(r"\$\(|\)|`")

# T-3192: exec-through PROCESS WRAPPERS chain in front of `claude` in the wild
# (`timeout 45 claude mcp list`, `nohup nice -n 5 claude`, …) and must not hide
# the invocation from command-position matching. A wrapper's own arguments are
# matched by a DELIBERATELY TIGHT per-token grammar — this is the load-bearing
# invariant, not an implementation detail:
#   - option tokens:     -\S*                     (-n, -5, -oL, --signal=KILL)
#   - numeric/duration:    -?\d+(?:\.\d+)?[smhd]?  (45, 1.5m, chrt -f 10)
#   - ALL-CAPS tokens:      [A-Z][A-Z0-9_]*        (-s KILL, env -u FOO)
# It NEVER consumes an arbitrary bare (lowercase-leading) word — a program
# name or a path. That's required because this account is literally named
# `claude`: `nice -n 5 du -sh /home/claude` / `timeout 30 ls /home/claude`
# MUST stay non-matching (T-2251's false-positive class) — a wrapper grammar
# loose enough to swallow a bare argument would make ordinary commands on the
# account's own home directory look like invocations, and a guard that
# false-positives on ordinary commands gets routed around, which protects
# nothing. Exotic false negatives on shapes like `exec -a name claude` (a
# wrapper flag whose OWN value is a bare word) are an accepted trade for that.
_WRAPPER_ARG = r"(?:-\S*|-?\d+(?:\.\d+)?[smhd]?|[A-Z][A-Z0-9_]*)"
# `timeout` listed before `time` (and other overlapping prefixes ordered
# longest-first) purely to document intent — Python's re backtracks across
# alternation on failure, so match correctness does not depend on this order,
# but a reader shouldn't have to prove that to trust the list.
#
# Path-prefix excludes `=` (not just whitespace): T-3192 bisection found that
# once a bare wrapper name like `env` can be peeled off on its own, the
# regex engine backtracks and reattempts the WHOLE match starting mid-string —
# and the (otherwise-correct) `\S*/`-style path-prefix doesn't care that an
# `=` sign is in that run, so `env HOME=/home/claude ls` let `\S*/` swallow
# `HOME=/home/` as if it were a real path, landing on the `claude` inside the
# VALUE `/home/claude`. Two individually-sound patterns composed into a hole
# neither had alone (same shape as the T-2896 lesson). Excluding `=` closes
# it: an env-assignment's `name=value` boundary can never be crossed by a
# path-prefix, in the wrapper name or in the final claude match below.
_WRAPPER_NAMES = frozenset(
    ("command", "exec", "nohup", "setsid", "timeout", "time", "nice", "env",
     "stdbuf", "ionice", "chrt")
)
_WRAPPER_NAME_MAX = max(len(w) for w in _WRAPPER_NAMES)

# env assignments. T-2251: the VALUE must not be able to cross a shell
# separator. It was `\S*`, which happily swallowed `$(cd ` (and, after the
# substitution normalisation above, `;cd `) as though it were one value —
# leaving the next token, a mere PATH like /home/claude, parsed as the
# command. Both halves are needed: normalisation alone still lost to `\S*`
# eating the `;` it had just inserted.
_ENV_ASSIGN = r"[A-Za-z_][A-Za-z0-9_]*=[^\s;&|`\n()]*"

# `claude` (bare or path-invoked) in COMMAND position: start-of-string or after
# a shell separator/substitution opener, optionally via any number/combination
# of process wrappers and env assignments, in ANY ORDER (T-3192 — was: at most
# one `command`/`exec` wrapper, then env assignments, fixed order). The rest of
# that command segment is captured for the safe-flag test.
#
# T-4392 phase 2b: this grammar USED to be one backtracking regex,
#
#   (?:^|[;&|`\n(])\s*                                 command position
#   (?:(?:WRAPPER|ENV_ASSIGN)\s+)*                     wrappers + env assigns, any order
#   (?:[^\s=]*/)?claude(?=\s|[;&|`)\n]|$)              bare or path-invoked, full token
#   ([^;&|`\n]*)                                       args until the next separator
#
#   WRAPPER = (?:[^\s=]*/)?(?:command|exec|...|chrt)(?:\s+WRAPPER_ARG)*
#
# run under re.finditer. On a hook that gates EVERY Bash call it was
# super-linear: QUADRATIC because `\s` includes `\n`, so every newline was a
# fresh command position whose `\s*` rescanned the whole whitespace run, and
# every failed start re-walked the same wrapper/assign chain (`"\n"*4096` =
# 3.3s CPU; orchestrator measured 18.6s at 5 KB); and EXPONENTIAL on a wrapper
# followed by `-1`-shaped tokens, which match two WRAPPER_ARG alternatives
# each (`"timeout " + "-1 "*22`, 74 bytes = 6.4s, doubling per 4 tokens).
#
# `_claude_invocation_args` below computes EXACTLY what that regex's finditer
# yielded (the same group(1) sequence, in order, for every input; the old
# regex is kept verbatim in tools/test_mcp_list_guard.py as the differential
# oracle), in O(n log n). It works because every choice inside the chain is a
# WHOLE whitespace-delimited token (each item and each wrapper arg must be
# followed by `\s+`, so a partial-token match can never continue), which makes
# each regex decision a per-token predicate, and a failed chain walk
# memoizable by position:
#   * Loop(x): the `(...)*` loop state at position x. The regex's result from
#     it depends only on x, so it is computed once per token (right to left)
#     and reused by every command position that lands there.
#   * the wrapper-arg repetition is greedy: it tries the LONGEST run of arg
#     tokens first, so the first success is the HIGHEST-index successful Loop
#     in the run's range -- one bisect, not a walk.
#   * the `claude` exit's greedy `[^\s=]*/` prefix picks the RIGHTMOST
#     `/claude<lookahead>` before the first `=` (or the token's end), else a
#     bare `claude<lookahead>` at x.
_ASSIGN_RE = re.compile(_ENV_ASSIGN)
_WRAPPER_ARG_RE = re.compile(_WRAPPER_ARG)
_TOKEN_RE = re.compile(r"\S+")
_EQ_RE = re.compile(r"=")
_SLASH_RE = re.compile(r"/")
# every `/claude` whose next char satisfies the old lookahead (zero-width, so
# overlapping candidates are all found)
_SLASH_CLAUDE_RE = re.compile(r"(?=/claude(?:\s|[;&|`)\n]|$))")
_CMD_POS_RE = re.compile(r"[;&|`\n(]")  # command-position openers
_ARGS_END_RE = re.compile(r"[;&|`\n]")  # where a match's args stop
_CLAUDE_LOOKAHEAD = frozenset(";&|`)\n")


def _claude_invocation_args(s: str):
    """Yield the args of each `claude` invocation in command position, in
    order -- exactly the group(1) sequence the pre-T-4392 `_CLAUDE_CMD_RE`
    finditer produced (see the comment above), in O(n log n).

    Tokenizing on `\\S+` and testing `str.isspace()` matches the regex's `\\s`
    EXACTLY: for str patterns the two agree on every code point 0..0x10FFFF
    (checked in the T-4392 review), and the equivalence argument depends on it."""
    if "claude" not in s:
        return  # every match contains the literal `claude`
    n = len(s)
    starts, ends = [], []
    for m in _TOKEN_RE.finditer(s):
        starts.append(m.start())
        ends.append(m.end())
    ntok = len(starts)
    eqs = [m.start() for m in _EQ_RE.finditer(s)]
    slashes = [m.start() for m in _SLASH_RE.finditer(s)]
    good = [m.start() for m in _SLASH_CLAUDE_RE.finditer(s)]

    def first_eq(x):
        i = bisect.bisect_left(eqs, x)
        return eqs[i] if i < len(eqs) else n

    def is_wrapper(x, e):
        # `(?:[^\s=]*/)?NAME` then whitespace: NAME is everything after the
        # token's last `/`, and the prefix may not contain `=`.
        i = bisect.bisect_left(slashes, e) - 1
        if i >= 0 and slashes[i] >= x:
            last = slashes[i]
            if first_eq(x) < last:
                return False
            return e - last - 1 <= _WRAPPER_NAME_MAX and s[last + 1:e] in _WRAPPER_NAMES
        return e - x <= _WRAPPER_NAME_MAX and s[x:e] in _WRAPPER_NAMES

    def is_assign(x, e):
        # The match stops at the first separator, so total work over every
        # start inside one token stays linear.
        m = _ASSIGN_RE.match(s, x)
        return m is not None and m.end() == e

    def claude_end(x, e):
        bound = min(first_eq(x), e)
        i = bisect.bisect_left(good, bound) - 1
        if i >= 0 and good[i] >= x:
            return good[i] + 7  # rightmost `/claude` (greedy prefix)
        if s.startswith("claude", x) and (
            x + 6 == n or s[x + 6].isspace() or s[x + 6] in _CLAUDE_LOOKAHEAD
        ):
            return x + 6
        return None

    # run_end[t]: first token index >= t that is NOT a whole-token wrapper arg.
    run_end = [ntok] * (ntok + 1)
    for t in range(ntok - 1, -1, -1):
        is_arg = _WRAPPER_ARG_RE.fullmatch(s, starts[t], ends[t]) is not None
        run_end[t] = run_end[t + 1] if is_arg else t

    res = [None] * ntok  # Loop(starts[t]) -> claude end offset, or None
    succ_neg = []  # -t for every t with res[t] set; ascending by construction

    def best_in(lo, hi):
        """res of the HIGHEST successful token index in [lo, hi] (greedy args)."""
        if lo > hi:
            return None
        k = bisect.bisect_left(succ_neg, -hi)
        if k < len(succ_neg) and -succ_neg[k] >= lo:
            return res[-succ_neg[k]]
        return None

    def loop_at(x, e, t_next):
        # Loop(x): first try one item then recurse (greedy `*`), else exit.
        if e < n:  # an item must be followed by whitespace
            if is_wrapper(x, e):
                if t_next < ntok:
                    r = best_in(t_next, min(run_end[t_next], ntok - 1))
                    if r is not None:
                        return r
            elif is_assign(x, e) and t_next < ntok:
                r = res[t_next]
                if r is not None:
                    return r
        return claude_end(x, e)

    for t in range(ntok - 1, -1, -1):
        res[t] = loop_at(starts[t], ends[t], t + 1)
        if res[t] is not None:
            succ_neg.append(-t)

    def loop_pos(x):
        if x >= n or s[x].isspace():
            return None
        t = bisect.bisect_right(starts, x) - 1
        if starts[t] == x:
            return res[t]
        return loop_at(x, ends[t], t + 1)  # command position mid-token (`a;claude`)

    def landing(y):  # `\s*`: first non-whitespace offset >= y
        if y < n and not s[y].isspace():
            return y
        i = bisect.bisect_left(starts, y)
        return starts[i] if i < ntok else n

    cmd_pos = [m.start() for m in _CMD_POS_RE.finditer(s)]
    pos = 0
    while True:
        c = loop_pos(landing(0)) if pos == 0 else None  # the `^` alternative
        if c is None:
            for k in range(bisect.bisect_left(cmd_pos, pos), len(cmd_pos)):
                c = loop_pos(landing(cmd_pos[k] + 1))
                if c is not None:
                    break
        if c is None:
            return
        m = _ARGS_END_RE.search(s, c)
        end = m.start() if m else n
        yield s[c:end]
        pos = end

# Safe invocation shapes (tested against the matched segment's args):
_SAFE_ARGS_RE = re.compile(r"--version\b|--help\b|(?:^|\s)-h\b")
_PRINT_RE = re.compile(r"--print\b|(?:^|\s)-p\b")
_SAFE_MODE_RE = re.compile(r"--safe-mode\b")

_BOT_PID = Path.home() / ".claude" / "channels" / "telegram" / "bot.pid"


def command_targets_mcp_list(command: str) -> bool:
    """True iff the bash command INVOKES a plugin-initializing `claude` (T-1623:
    any second claude with the plugin enabled steals the token — not just the
    mcp-list subcommand). Quoted mentions don't count; safe shapes exempted.
    (Name kept for wiring/test compatibility.)"""
    if not command:
        return False
    stripped = _SUBST_RE.sub(";", _QUOTED_RE.sub(" ", _strip_heredoc_bodies(command)))
    for args in _claude_invocation_args(stripped):
        if _SAFE_ARGS_RE.search(args):
            continue  # --version/--help: no session, no plugin
        if _PRINT_RE.search(args) and _SAFE_MODE_RE.search(args):
            continue  # headless safe-mode: plugin-less by construction
        return True
    return False


def own_poller_live() -> bool:
    """True iff this account's telegram bridge poller is live (bot.pid → live bun
    server.ts). FAIL-OPEN inverse: any uncertainty returns False (→ allow), so a
    flaky check never blocks legitimately. Only a CONFIRMED-live poller blocks."""
    try:
        if not _BOT_PID.exists():
            return False
        pid = int(_BOT_PID.read_text(encoding="utf-8").split()[0].strip())
        os.kill(pid, 0)  # raises if dead / not ours
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        return "bun" in cmdline and "server.ts" in cmdline
    except Exception:
        return False


def decide(data: dict, poller_live: bool) -> dict | None:
    """Return a deny-payload dict to BLOCK, or None to allow.

    Blocks only when ALL hold: the tool is Bash, the command invokes
    `claude mcp list`, and this account's poller is confirmed live.
    """
    if data.get("tool_name") != "Bash":
        return None
    command = (data.get("tool_input") or {}).get("command") or ""
    if not command_targets_mcp_list(command):
        return None
    if not poller_live:
        return None
    reason = (
        "BLOCKED: launching a second `claude` while this account's Telegram "
        "poller is LIVE. Any transient claude with the plugin enabled opens a "
        "second getUpdates poll on the same bot → 409 → the live inbound poller "
        "EXITS and the agent goes comms-dark (the 2026-06-06/07 bridge-death "
        "incident; the ratified no-second-claude rule) — even via timeout/"
        "nohup/nice/env/time wrappers, which do not hide the invocation. "
        "Diagnose bridges with `ps` (the `bun server.ts` child) + "
        "`curl .../getMe`. Safe shapes that pass: --version/--help, and "
        "--print WITH --safe-mode. (T-0894/T-0996/T-1623/T-3192 guard.)"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# T-4392 phase 2a: the most this guard will read. The scanner is linear (~0.23s CPU at 64 KiB),
# so 262,144 characters (~1s) sits well inside the 15s fail-closed budget while leaving room for main-thread
# orchestrator heredocs; tools/test_mcp_list_guard_failclosed.py pins the worst-shape timing.
MAX_COMMAND_CHARS = 262_144


def _entry_decide(data: dict) -> dict | None:
    return decide(data, own_poller_live())


def main() -> None:
    """Run the guard FAIL-CLOSED (T-4392 phase 2a) through tools/hooks/_failclosed.py.

    The verdict logic above is unchanged, including own_poller_live()'s internal fail-open
    (an unreadable poller state still allows). What changed is the HOOK-level failure mode: an
    exception, an ambiguous payload, the 15s deadline from process start, or a command over
    MAX_COMMAND_CHARS now DENIES (exit 2, stderr names the cause) instead of exiting 0. The
    helper is imported from THIS file's real directory, first on sys.path (a decoy on
    PYTHONPATH cannot win); if that import fails, the guard denies too, since an ImportError
    would exit 1, which fails open."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import _failclosed
    except BaseException as exc:  # noqa: BLE001 - an import failure must DENY
        missing = isinstance(exc, FileNotFoundError) or (
            isinstance(exc, ModuleNotFoundError) and exc.name == "_failclosed")
        if missing:
            hint = ("The hook's directory lacks _failclosed.py: run the hook from the "
                    "pantheon-config clone, or unwire it via the Edit tool.")
        else:
            hint = (f"the hook's _failclosed.py failed to import ({type(exc).__name__}): "
                    "fix it, or unwire the hook via the Edit tool.")
        sys.stderr.write(
            "BLOCKED (mcp-list-guard, fail-closed, T-4392): cannot import _failclosed "
            f"({exc!r}). {hint}\n"
        )
        sys.exit(2)
    _failclosed.run("mcp-list-guard", _entry_decide, max_command_chars=MAX_COMMAND_CHARS)


if __name__ == "__main__":
    main()
