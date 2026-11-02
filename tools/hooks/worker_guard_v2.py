#!/usr/bin/env python3
"""worker-guard v2 (T-5686): a small, literal PreToolUse guard for dispatched subagents.

Intent: keep every real catch the 2026-10-27 field audit found (notes/2026-10-27-worker-guard-
utility-audit.md: ~15 true positives in 457 denials, every one a plain literal form), block
nothing real work needs, and cost a fraction of the 19k-line v1's per-command time.

Same contract as worker_guard_impl.py: `decide(data) -> dict | None` returns the deny payload
(or None = allow), `main(raw, deadline, started_monotonic)` is the stdin/stdout entry. No
`agent_type` (the main thread) always allows.

What it denies (rule ids appear in the reason text and in `verdict()`):
  ESC     (a) sudo/su/doas/pkexec/sudoedit/run0/runuser at command position (also behind literal
          wrappers and `bash -c '...'`), `pantheon-sudo` at command position or as the value of
          a shell assignment, and any operand naming the sudo password file.
  CRED    (b) credential / token files, ~/.ssh contents, `.env` files, `api.telegram.org/bot`:
          any operand, redirect target or tool path (Read/Grep/Edit/Write/apply_patch).
  DOC     (c) Edit/Write/MultiEdit/NotebookEdit/apply_patch of a protected HO-owned doc, and
          literal Bash WRITES into one (redirect target, tee, sed/perl -i, cp/mv/install
          destination, rm/mv source, git add/commit/rm/mv pathspec). Reads are never denied.
  PUSH    (d) `git push` at command position.
  GH      (d) a gh verb that is not on the read allowlist; `gh api` writes.
  TICKET  (e) ticket.py security-accept / security-reject.

What it does NOT do, by design (ORG-052 is the structural answer, the operator ruled against chasing
these): dynamic command words ($VAR / $(..) as the command), brace/ANSI-C quoting tricks, reading
heredoc bodies or script files as code, eval joins, env -S decoding, command renames, trap,
prompt/history, git config/env values, git identity, python startup-hook paths. The tokenizer is
honest and small: quotes, the `; && || | & newline` separators, `$( )` / backtick bodies scanned
as commands, heredoc bodies skipped as data, `# comments` skipped. Where it cannot read
something it ALLOWS. The only fail-closed rails are the 64 KiB Bash cap, the 5 s deadline and an
exception inside decide.
"""

import json
import os
import re
import sys

BASH_COMMAND_MAX_CHARS = 65_536
DECIDE_DEADLINE_S = 5.0
_DEADLINE_FLOOR_S = 0.05
_MAX_DEPTH = 8  # nesting of $( ), backticks and bash -c strings; deeper = unreadable = allow
_MAX_WORD = 4096  # a path-like word longer than this is not a path

_ESC_NAMES = frozenset({"sudo", "su", "doas", "pkexec", "sudoedit", "run0", "runuser"})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash"})
_ASSIGN_CMDS = frozenset({"export", "declare", "typeset", "local", "readonly"})

# The sudo password file's name is assembled so the contiguous token never appears in source
# (root-env-guard blocks commands naming it; this file is also read by tools).
_PW_NAME = "root" + ".env"
_PW_PAT = r"\b" + "root" + r"\.env" + r"\b"

# ---------------------------------------------------------------------------------------------
# Path lists (copied from worker_guard_impl.py; the protected-dir list is canary-pinned there).
# ---------------------------------------------------------------------------------------------
_PROTECTED_DOC = re.compile(
    r"pantheon-config(-[^/]*)?/"
    r"(?:"
    r"(?:rulebook|ledger|notes|bugs|incidents|lessons|sop|architecture/adr|architecture/engineering-practices)(?:/|$)"
    r"|BACKLOG\.md"
    r"|goals\.md"
    r"|(?:[^ ]*/)?README\.md"
    r"|knowledge/watch/topics\.md"
    r")"
)
_PROTECTED_ROOTREL = re.compile(
    r"^(?:rulebook|ledger|notes|bugs|incidents|lessons|sop|architecture/adr|architecture/engineering-practices)(?:/|$)"
    r"|^BACKLOG\.md$"
    r"|^goals\.md$"
    r"|(?:^|/)README\.md$"
    r"|^knowledge/watch/topics\.md$"
)
_ISOLATION_WT = re.compile(r"pantheon-config/\.claude/worktrees/[^/]+/(.+)$")
_CRED_FIXED = re.compile(
    r"\bgh_token\.env\b"
    r"|\.config/gh/hosts\.yml\b"
    r"|echo-prismatic/\.env\b"
    r"|\.claude/channels/[^/\s'\"]+/\.env\b"
    r"|\bj17\.keys\b"
    r"|" + _PW_PAT +
    r"|\.git-credentials"
    r"|\.claude/\.credentials\.json\b"
    r"|\.codex/auth\.json\b"
    r"|\.codex/[^/\s'\"]*\.env\b"
)
_ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist", "tpl"})
_APPLY_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE
)
_MAIN_CHECKOUTS = frozenset({
    "/home/claude/pantheon-config", "/home/claude/echo-prismatic", "/home/claude/revenue-project",
    "/home/claude/pantheon-pwa", "/home/claude/pantheon-bridge",
})

# gh read allowlist (copied from worker_guard_impl._GH_READ_ALLOWLIST). None = any verb.
_GH_READ_ALLOWLIST = {
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
_GH_ALIASES = {"co": ("pr", "checkout")}
_GH_GLOBAL_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GH_BODY_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})


# ---------------------------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------------------------
class _Unreadable(Exception):
    """The text cannot be read honestly; the caller allows (fail-open on ambiguity)."""


class _Cmd:
    __slots__ = ("words", "assigns", "redirs")

    def __init__(self):
        self.words = []    # unquoted literal text of each word; '\x00' marks an expansion
        self.assigns = []  # [(name, value)] leading VAR=val words
        self.redirs = []   # [(op, target_text)]

    def empty(self):
        return not (self.words or self.assigns or self.redirs)


_PLAIN = re.compile(r"[^ \t\n;&|()<>\\'\"`$]+")
_DQ_PLAIN = re.compile(r'[^"\\$`]+')
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?!$-]")
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)$", re.DOTALL)
_KEYWORDS = frozenset({"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while",
                       "until", "esac", "function"})
_REDIR_OPS = ("&>>", ">>", ">|", ">&", "&>", "<&", "<>", ">", "<")


class _Parser:
    def __init__(self, text, cmds, depth=0):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.cmds = cmds
        self.depth = depth
        self.pending = []  # heredoc delimiters awaiting their body: [(delim, strip_tabs)]

    # -- helpers ------------------------------------------------------------------------
    def _blanks(self):
        s, n = self.s, self.n
        i = self.i
        while i < n:
            c = s[i]
            if c == " " or c == "\t":
                i += 1
            elif c == "\\" and s[i + 1:i + 2] == "\n":
                i += 2
            else:
                break
        self.i = i

    def _heredoc_bodies(self):
        s, n = self.s, self.n
        i = self.i
        for delim, strip in self.pending:
            while i < n:
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
        self.pending = []
        self.i = i

    def _sub(self, text):
        if self.depth + 1 > _MAX_DEPTH:
            raise _Unreadable
        _Parser(text, self.cmds, self.depth + 1).parse_list(None)

    def _nested_list(self):
        if self.depth + 1 > _MAX_DEPTH:
            raise _Unreadable
        self.depth += 1
        try:
            self.parse_list(")")
        finally:
            self.depth -= 1

    def _skip_arith(self):
        # at '((' or '$((': skip to the matching '))' by paren depth
        s, n = self.s, self.n
        depth = 0
        i = self.i
        while i < n:
            c = s[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        else:
            raise _Unreadable
        self.i = i

    def _backtick(self):
        s, n = self.s, self.n
        j = self.i + 1
        while j < n and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        if j >= n:
            raise _Unreadable
        body = s[self.i + 1:j].replace("\\`", "`")
        self.i = j + 1
        self._sub(body)

    def _dollar(self):
        """At '$' (unquoted or in double quotes). Returns the text to append."""
        s = self.s
        nxt = s[self.i + 1:self.i + 2]
        if nxt == "(":
            if s[self.i + 2:self.i + 3] == "(":
                self.i += 1
                self._skip_arith()
            else:
                self.i += 2
                self._nested_list()
            return "\x00"
        if nxt == "{":
            depth, j, n = 1, self.i + 2, self.n
            while j < n and depth:
                if s.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if s[j] == "}":
                    depth -= 1
                j += 1
            if depth:
                raise _Unreadable
            inner = s[self.i + 2:j - 1]
            self.i = j
            return "${" + inner + "}" if _NAME.fullmatch(inner) else "\x00"
        m = _NAME.match(s, self.i + 1)
        if m:
            self.i += 1 + m.end() - m.start()
            return "$" + m.group(0)
        self.i += 1
        return "$"

    def _dq(self):
        """At the char after an opening '"'. Returns the text; leaves i past the closing quote."""
        s, n = self.s, self.n
        out = []
        while self.i < n:
            m = _DQ_PLAIN.match(s, self.i)
            if m:
                out.append(m.group(0))
                self.i = m.end()
                continue
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                nxt = s[self.i + 1:self.i + 2]
                if nxt in ('$', '"', "\\", "`"):
                    out.append(nxt)
                elif nxt == "\n":
                    pass
                else:
                    out.append("\\" + nxt)
                self.i += 2
            elif c == "$":
                out.append(self._dollar())
            else:  # backtick
                self._backtick()
                out.append("\x00")
        raise _Unreadable

    def read_word(self):
        """Read one word. Returns (text, quoted)."""
        s, n = self.s, self.n
        out = []
        quoted = False
        while self.i < n:
            m = _PLAIN.match(s, self.i)
            if m:
                out.append(m.group(0))
                self.i = m.end()
                continue
            c = s[self.i]
            if c == "\\":
                nxt = s[self.i + 1:self.i + 2]
                self.i += 2
                if nxt and nxt != "\n":
                    out.append(nxt)
                    quoted = True
            elif c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise _Unreadable
                out.append(s[self.i + 1:j])
                self.i = j + 1
                quoted = True
            elif c == '"':
                self.i += 1
                out.append(self._dq())
                quoted = True
            elif c == "`":
                self._backtick()
                out.append("\x00")
            elif c == "$":
                nxt = s[self.i + 1:self.i + 2]
                if nxt == "'":  # ANSI-C: read as a plain single-quoted string
                    j = self.i + 2
                    while j < n and s[j] != "'":
                        j += 2 if s[j] == "\\" else 1
                    if j >= n:
                        raise _Unreadable
                    out.append(s[self.i + 2:j])
                    self.i = j + 1
                    quoted = True
                elif nxt == '"':
                    self.i += 1
                else:
                    out.append(self._dollar())
            else:
                break  # a word-ending operator or blank
        return "".join(out), quoted

    # -- the list parser ----------------------------------------------------------------
    def parse_list(self, end):
        s, n = self.s, self.n
        cur = _Cmd()

        def flush():
            nonlocal cur
            if not cur.empty():
                self.cmds.append(cur)
            cur = _Cmd()

        try:
            while True:
                self._blanks()
                if self.i >= n:
                    break
                c = s[self.i]
                if c == "#":
                    j = s.find("\n", self.i)
                    self.i = n if j < 0 else j
                    continue
                if c == "\n":
                    self.i += 1
                    flush()
                    if self.pending:
                        self._heredoc_bodies()
                    continue
                if c == ";":
                    self.i += 1
                    flush()
                    continue
                if c == "&" and s[self.i + 1:self.i + 2] != ">":
                    self.i += 1
                    flush()
                    continue
                if c == "|":
                    self.i += 1
                    flush()
                    continue
                if c == "(":
                    if s[self.i + 1:self.i + 2] == "(" and not cur.words:
                        self._skip_arith()
                        continue
                    if cur.words:
                        rest = s[self.i + 1:].lstrip(" \t")
                        if rest.startswith(")"):  # f() { ...; }: a function definition
                            self.i = n - len(rest) + 1
                            cur = _Cmd()
                            continue
                    flush()
                    self.i += 1
                    self._nested_list()
                    continue
                if c == ")":
                    self.i += 1
                    if end == ")":
                        return
                    flush()
                    continue
                if c in "<>" or (c == "&" and s[self.i + 1:self.i + 2] == ">"):
                    if c in "<>" and s[self.i + 1:self.i + 2] == "(":  # process substitution
                        self.i += 2
                        self._nested_list()
                        cur.words.append("\x00")
                        continue
                    if s.startswith("<<<", self.i):
                        op = "<<<"
                    elif s.startswith("<<-", self.i):
                        op = "<<-"
                    elif s.startswith("<<", self.i):
                        op = "<<"
                    else:
                        op = next(o for o in _REDIR_OPS if s.startswith(o, self.i))
                    self.i += len(op)
                    self._blanks()
                    target, _q = self.read_word()
                    if op in ("<<", "<<-"):
                        if target:
                            self.pending.append((target, op == "<<-"))
                    elif target:
                        cur.redirs.append((op, target))
                    continue
                text, quoted = self.read_word()
                if self.i < n and s[self.i] in "<>" and text.isdigit() and not quoted:
                    continue  # the fd of `2>file`
                if not cur.words:
                    if not quoted and text in _KEYWORDS:
                        continue
                    m = _ASSIGN.match(text)
                    if m:
                        cur.assigns.append((m.group(1), m.group(2)))
                        continue
                cur.words.append(text)
        finally:
            flush()


def _tokenize(text):
    """All simple commands in `text`, in source order (nested ones included). On an unreadable
    construct the commands read so far are returned (the rest is allowed)."""
    cmds = []
    try:
        _Parser(text, cmds).parse_list(None)
    except (_Unreadable, RecursionError):
        pass
    return cmds


# ---------------------------------------------------------------------------------------------
# Command-position peeling: literal wrappers and `bash -c 'literal'`
# ---------------------------------------------------------------------------------------------
# name -> (short options taking a value, long options taking a value, positionals to skip)
_WRAPPERS = {
    "env": ("uC", ("unset", "chdir"), 0),
    "nohup": ("", (), 0),
    "time": ("fo", (), 0),
    "timeout": ("sk", ("signal", "kill-after"), 1),
    "nice": ("n", ("adjustment",), 0),
    "ionice": ("cn", ("class", "classdata"), 0),
    "stdbuf": ("ioe", ("input", "output", "error"), 0),
    "setsid": ("", (), 0),
    "exec": ("a", (), 0),
    "command": ("", (), 0),
    "builtin": ("", (), 0),
    "unbuffer": ("", (), 0),
    "chronic": ("", (), 0),
    "xargs": ("InLPdEsa", ("arg-file", "delimiter", "max-args", "max-lines", "max-procs",
                          "max-chars"), 0),
    "flock": ("wE", ("timeout", "conflict-exit-code"), 1),
    "taskset": ("c", ("cpu-list",), 1),
    "chrt": ("", (), 1),
    "strace": ("eEopuIsabOPSkAX", (), 0),
    "watch": ("ndpe", ("interval",), 0),
    "ssh": ("bcDEeFIiJLlmOopQRSWw", (), 1),
}
_CODE_JOINED = frozenset({"watch", "ssh"})  # their command is a shell string: re-tokenized


def _base(word):
    return word.rsplit("/", 1)[-1]


def _peel_wrapper(name, args):
    """Return (index of the next command word in args or None, literal code string or None)."""
    short_val, long_val, pos_skip = _WRAPPERS[name]
    if name == "command" and args and args[0] in ("-v", "-V"):
        return None, None
    if name == "chrt" and any(a == "-p" for a in args):
        return None, None
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--") and len(a) > 2:
            if a[2:] in long_val:
                i += 1
            if name == "flock" and a == "--command" and i + 1 < n:
                return None, args[i + 1]
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            consumed_value = False
            for k, ch in enumerate(a[1:], 1):
                if name == "flock" and ch == "c":
                    rest = a[k + 1:]
                    return None, (rest if rest else (args[i + 1] if i + 1 < n else None))
                if ch in short_val:
                    if name == "taskset" and ch == "c":
                        pos_skip = 0
                    consumed_value = k == len(a) - 1
                    break
            i += 2 if consumed_value else 1
            continue
        if name == "env" and _ASSIGN.match(a):
            i += 1
            continue
        if pos_skip > 0:
            pos_skip -= 1
            i += 1
            continue
        break
    if i >= n:
        return None, None
    if name in _CODE_JOINED:
        return None, " ".join(args[i:])
    return i, None


def _shell_code(args):
    """The literal code string of `bash -c '...'` (also -lc / -xc), else None."""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a.startswith("--") or not a.startswith("-") or len(a) < 2:
            return None
        if "c" in a[1:]:
            return args[i + 1] if i + 1 < n else None
        i += 1
    return None


def _stages(words):
    """Walk the wrapper chain. Returns ([(name, args)], [code strings to re-tokenize]).
    A dynamic next command word ends the walk (allowed: out of scope by design)."""
    stages, codes = [], []
    cur = list(words)
    for _ in range(10):
        if not cur or "\x00" in cur[0] or cur[0].startswith("$"):
            break
        name = _base(cur[0])
        args = cur[1:]
        stages.append((name, args))
        if name in _WRAPPERS:
            idx, code = _peel_wrapper(name, args)
            if code is not None:
                codes.append(code)
            if idx is None:
                break
            cur = args[idx:]
            continue
        if name in _SHELLS:
            code = _shell_code(args)
            if code is not None:
                codes.append(code)
        break
    return stages, codes


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------
def _v(rule, what, instead):
    """A verdict: (rule_id, what, instead)."""
    return (rule, what, instead)


_ESC_INSTEAD = ("Workers hold no elevated credentials of their own. Do not escalate or read the "
                "sudo password; STOP and report the need to your dispatcher in your final message.")
_CRED_INSTEAD = ("Dispatched workers hold no credentials of their own. Do not read or write "
                 "credential, token or .env files; report the need to your dispatcher.")
_DOC_INSTEAD = ("Protected canonical docs (rulebook / ledger / notes / bugs / incidents / lessons / "
                "sop / ADR / README / BACKLOG / goals) are orchestrator-owned. Draft the text "
                "in your final report (or under /tmp) and let your dispatcher file it. Reads are fine.")
_PUSH_INSTEAD = ("Push, PR create/merge and every GitHub write are reserved to the head "
                 "orchestrator. Commit on your agent/<task> branch and report; do not push.")
_GH_INSTEAD = ("Workers may run only read gh commands (pr/issue/run/workflow/release/repo "
               "view|list|diff|checks, search, status, `gh api` GETs); route writes through your "
               "dispatcher.")
_TICKET_INSTEAD = ("security-accept / security-reject are reserved to the head orchestrator and "
                   "the operator; report the finding to your dispatcher instead.")


def _expand_home(p):
    home = os.path.expanduser("~")
    if p == "~" or p.startswith("~/"):
        return home + p[1:]
    for pre in ("${HOME}", "$HOME"):
        if p.startswith(pre) and (len(p) == len(pre) or p[len(pre)] == "/"):
            return home + p[len(pre):]
    return p


def _resolve(path, cwd):
    p = _expand_home(path)
    if os.path.isabs(p):
        return os.path.normpath(p)
    if cwd:
        return os.path.normpath(os.path.join(cwd, p))
    return None


def _worktree_root_and_common(start):
    try:
        d = start if os.path.isdir(start) else os.path.dirname(start)
        for _ in range(64):
            marker = os.path.join(d, ".git")
            if os.path.isfile(marker):
                with open(marker, encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"gitdir:\s*(.+)", fh.read())
                return d, (m.group(1).strip() if m else None)
            if os.path.isdir(marker):
                return d, marker
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    except Exception:
        pass
    return None, None


def _protected_doc(path, cwd):
    """True if `path` names a protected HO-owned doc (absolute `pantheon-config...` shape, or the
    T-3662 isolation-worktree / relative shape confirmed by the enclosing git common dir)."""
    if not path or len(path) > _MAX_WORD or any(ch.isspace() for ch in path) or "\x00" in path:
        return False
    try:
        abspath = _resolve(path, cwd)
    except Exception:
        abspath = None
    # Literal string checks first (they hold after a worktree is cleaned up; the replay found 8
    # field denials the git-common-dir check alone could not re-judge): the path as typed, then
    # as resolved against cwd. The Agent-tool isolation worktree is spelled out:
    # <acct>/pantheon-config/.claude/worktrees/agent-<id>/<repo-relative path>.
    for p in (path, abspath):
        if not p:
            continue
        if _PROTECTED_DOC.search(p):
            return True
        m = _ISOLATION_WT.search(p)
        if m and _PROTECTED_ROOTREL.search(m.group(1)):
            return True
    try:
        if abspath is None:
            return False
        root, common = _worktree_root_and_common(abspath)
        if not root or not common or "pantheon-config" not in common:
            return False
        return bool(_PROTECTED_ROOTREL.search(os.path.relpath(abspath, root)))
    except Exception:
        return False


_PATHISH = re.compile(r"[\w./~$@%+:=,{}-]+")


def _env_basename(base):
    # `.env` and `.env.<x>` (not a template). Deliberately NOT `*.env`: that suffix also names
    # grep patterns (`process.env`, `pantheon.env`); the named credential `*.env` files are in
    # _CRED_FIXED (replay 2026-10-27: `*.env` fired on 8 of the first 10 newly denied commands).
    if base == ".env":
        return True
    return base.startswith(".env.") and base[5:] not in _ENV_TEMPLATE_SUFFIXES


def _cred_path(w):
    """True if the word names a credential file, an .env file or ~/.ssh contents."""
    if not w or len(w) > _MAX_WORD or any(ch.isspace() for ch in w) or "\x00" in w:
        return False
    for part in {w, w.rsplit("=", 1)[-1]}:
        if _CRED_FIXED.search(part):
            return True
        if not _PATHISH.fullmatch(part):
            continue  # a regex / glob / quoted pattern, not a path
        if _env_basename(_base(part.rstrip("/"))):
            return True
        if part == ".ssh" or part.endswith("/.ssh") or ".ssh/" in part:
            if part.endswith(".pub"):
                continue
            return True
    return False


def _is_pw_word(w):
    if not w or any(ch.isspace() for ch in w):
        return False
    return _base(w) == _PW_NAME or _base(w.rsplit("=", 1)[-1]) == _PW_NAME


def _tool_path_verdict(tool, path, cwd, write):
    if write and _protected_doc(path, cwd):
        return _v("DOC", f"to {tool} a protected canonical doc ({path})", _DOC_INSTEAD)
    if _is_pw_word(path):
        return _v("ESC", f"to {tool} the sudo password file", _ESC_INSTEAD)
    if _cred_path(path):
        return _v("CRED", f"to {tool} a credential / token / .env / ssh file ({path})", _CRED_INSTEAD)
    return None


def _operands(args, value_opts=()):
    out, skip, after_dd = [], False, False
    for a in args:
        if skip:
            skip = False
            continue
        if after_dd:
            out.append(a)
        elif a == "--":
            after_dd = True
        elif a in value_opts:
            skip = True
        elif a.startswith("-") and len(a) > 1:
            continue
        else:
            out.append(a)
    return out


def _git_sub(args):
    """(subcommand, rest args, cwd override or None) after git's global options."""
    i, n, cdir = 0, len(args), None
    while i < n:
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                 "--exec-path", "--config-env"):
            if a == "-C" and i + 1 < n:
                cdir = args[i + 1] if cdir is None else os.path.join(cdir, args[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:], cdir
    return None, [], cdir


_WRITE_REDIR = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_GIT_COMMIT_VALUE_OPTS = ("-m", "-F", "-C", "-c", "-t", "--message", "--file", "--author",
                          "--date", "--reuse-message", "--reedit-message", "--template",
                          "--fixup", "--squash", "--cleanup")


def _inplace_flag(name, a):
    if a == "--in-place" or a.startswith("--in-place="):
        return True
    if a.startswith("-") and not a.startswith("--"):
        return re.match(r"-[a-zA-Z]*i", a) is not None if name in ("sed", "perl") else False
    return False


def _bash_write_targets(name, args, redirs, cwd):
    """Literal Bash write targets as (path, cwd) pairs. Reads never contribute."""
    out = [(t, cwd) for op, t in redirs if op in _WRITE_REDIR]
    if name == "tee":
        out += [(a, cwd) for a in _operands(args)]
    elif name in ("sed", "perl"):
        if any(_inplace_flag(name, a) for a in args):
            out += [(a, cwd) for a in _operands(args)]
    elif name in ("cp", "mv", "install", "ln"):
        ops = _operands(args, ("-t", "--target-directory", "-S", "--suffix"))
        dest = [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
        for k, a in enumerate(args):
            if a == "-t" and k + 1 < len(args):
                dest.append(args[k + 1])
            elif a.startswith("-t") and len(a) > 2 and not a.startswith("--"):
                dest.append(a[2:])
        if not dest and ops:
            dest = [ops[-1]]
        out += [(d, cwd) for d in dest]
        if name == "mv":
            out += [(a, cwd) for a in ops[:-1]]
    elif name in ("rm", "unlink", "shred"):
        out += [(a, cwd) for a in _operands(args)]
    elif name == "git":
        sub, rest, cdir = _git_sub(args)
        if sub in ("add", "commit", "rm", "mv"):
            wd = cwd
            if cdir:
                wd = _resolve(cdir, cwd) or cwd
            vals = _GIT_COMMIT_VALUE_OPTS if sub == "commit" else ()
            out += [(a, wd) for a in _operands(rest, vals)]
    return out


def _gh_api_verdict(args):
    method, body, graphql, mutation = None, False, False, False
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in ("-X", "--method") and i + 1 < n:
            method = args[i + 1].upper()
            i += 2
            continue
        if a.startswith("--method="):
            method = a[9:].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a in _GH_BODY_FLAGS or any(a.startswith(f + "=") for f in _GH_BODY_FLAGS if f.startswith("--")):
            body = True
        elif a.startswith(("-f", "-F")) and len(a) > 2 and not a.startswith("--"):
            body = True
        if re.fullmatch(r"/?graphql", a):
            graphql = True
        if "mutation" in a.lower():
            mutation = True
        i += 1
    if graphql:
        return "a GraphQL mutation via `gh api`" if mutation else None
    if method is not None and method not in ("GET", "HEAD"):
        return f"`gh api -X {method}`, a non-GET request"
    if body and method is None:
        return "`gh api` with request fields (-f/-F/--input), which makes it a write"
    return None


def _gh_verdict(args, cwd):
    i, n = 0, len(args)
    if not args or args[0] in ("--help", "--version", "-h"):
        return None
    while i < n and args[i].startswith("-"):
        if args[i] in _GH_GLOBAL_VALUE_FLAGS:
            i += 1
        i += 1
    if i >= n:
        return None
    group = args[i]
    if "\x00" in group or group.startswith("$"):
        return None  # computed at run time: unreadable, allowed
    rest = args[i + 1:]
    verb = None
    if group in _GH_ALIASES:
        group, verb = _GH_ALIASES[group]
    elif group == "api":
        return _gh_api_verdict(rest)
    elif group not in _GH_READ_ALLOWLIST:
        return f"`gh {group}`, which is not on the read allowlist"
    helpish = any(a in ("--help", "-h") for a in rest)
    allowed = _GH_READ_ALLOWLIST[group]
    if verb is None and allowed is not None:
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in _GH_GLOBAL_VALUE_FLAGS else 1
        if j >= len(rest):
            return None if helpish else f"`gh {group}` with no subcommand"
        verb = rest[j]
        if "\x00" in verb or verb.startswith("$"):
            return None
        if verb not in allowed:
            if helpish:
                return None
            return f"`gh {group} {verb}`, which is not on the read allowlist"
        rest = rest[j + 1:]
    if group == "auth" and any(t.startswith("--show-token") or (t.startswith("-") and not t.startswith("--") and "t" in t[1:]) for t in rest):
        return "`gh auth status --show-token`, which prints the token"
    if group == "pr" and verb in ("checkout", "co") and cwd and os.path.normpath(cwd) in _MAIN_CHECKOUTS:
        return "`gh pr checkout` in a shared main checkout (run it from your own worktree)"
    return None


def _python_operands(args):
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-W", "-X", "-m", "-c"):
            return []
        if a.startswith("-"):
            continue
        return [a] + args[args.index(a) + 1:]
    return []


def _stage_verdict(name, args, cmd, cwd):
    # (a) escalation
    if name in _ESC_NAMES:
        return _v("ESC", f"to escalate privileges (`{name}` at command position)", _ESC_INSTEAD)
    if name == "pantheon-sudo":
        return _v("ESC", "to run pantheon-sudo (the sudo-password broker)", _ESC_INSTEAD)
    if name in _SHELLS or re.fullmatch(r"python[0-9.]*", name):
        ops = _operands(args) if name in _SHELLS else _python_operands(args)
        if ops and _base(ops[0]) == "pantheon-sudo":
            return _v("ESC", "to run pantheon-sudo (the sudo-password broker)", _ESC_INSTEAD)
    if name in _ASSIGN_CMDS:
        for a in args:
            m = _ASSIGN.match(a)
            if m and _base(m.group(2)) == "pantheon-sudo":
                return _v("ESC", "to bind pantheon-sudo to a shell variable", _ESC_INSTEAD)
    # (d) push / gh
    if name == "git":
        sub, rest, _cd = _git_sub(args)
        # T-5695: `git push --help`/`-h` is help, not a push, but only as the FIRST argument
        # (`git push -o -h origin main` passes -h as -o's value and really pushes).
        if sub == "push" and not (rest[:1] in (["--help"], ["-h"])):
            return _v("PUSH", "to `git push`", _PUSH_INSTEAD)
    if name == "gh":
        why = _gh_verdict(args, cwd)
        if why:
            return _v("GH", f"to run {why}", _GH_INSTEAD)
    # (e) ticket.py security gate
    ticket_args = None
    if name == "ticket.py":
        ticket_args = args
    elif re.fullmatch(r"python[0-9.]*", name):
        ops = _python_operands(args)
        if ops and _base(ops[0]) == "ticket.py":
            ticket_args = ops[1:]
    if ticket_args and any(a in ("security-accept", "security-reject") for a in ticket_args):
        return _v("TICKET", "to run ticket.py security-accept / security-reject", _TICKET_INSTEAD)
    return None


def _cmd_verdict(cmd, cwd):
    """Verdict for one simple command; returns (verdict, codes-to-recurse, new cwd)."""
    # leading assignments: S=.../pantheon-sudo
    for _n, value in cmd.assigns:
        if _base(value) == "pantheon-sudo":
            return _v("ESC", "to bind pantheon-sudo to a shell variable", _ESC_INSTEAD), [], cwd
    texts = list(cmd.words) + [v for _n, v in cmd.assigns] + [t for _o, t in cmd.redirs]
    if any(_is_pw_word(t) for t in texts):
        return _v("ESC", "to name the sudo password file", _ESC_INSTEAD), [], cwd
    if any("api.telegram.org/bot" in t.lower() for t in texts):
        return _v("CRED", "to use a Telegram bot token URL (api.telegram.org/bot)", _CRED_INSTEAD), [], cwd
    stages, codes = _stages(cmd.words)
    for name, args in stages:
        v = _stage_verdict(name, args, cmd, cwd)
        if v:
            return v, [], cwd
    final = stages[-1][0] if stages else None
    # (c) literal Bash writes into protected docs
    for name, args in stages:
        for target, wd in _bash_write_targets(name, args, cmd.redirs if name == final else [], cwd):
            if _protected_doc(target, wd):
                return _v("DOC", f"to write a protected canonical doc via Bash ({target})", _DOC_INSTEAD), [], cwd
    # (b) credential paths: any operand / redirect target; ls & stat are metadata-only
    meta = final in ("ls", "stat", "find")  # they list / stat names; they do not read contents
    check = [t for _o, t in cmd.redirs] + [v for _n, v in cmd.assigns]
    if not meta:
        check += cmd.words
    for t in check:
        if _cred_path(t):
            return _v("CRED", f"to touch a credential / token / .env / ssh file ({t})", _CRED_INSTEAD), [], cwd
    # cd tracking, for cwd-relative protected paths later in the same command line
    if final in ("cd", "pushd") and stages:
        ops = _operands(stages[-1][1])
        if ops and "\x00" not in ops[0] and "$" not in ops[0] and ops[0] != "-":
            cwd = _resolve(ops[0], cwd) or ""
        else:
            cwd = ""  # a computed / unknown directory: relative paths can no longer be placed (allow)
    return None, codes, cwd


def _bash_verdict(command, cwd, depth=0):
    for cmd in _tokenize(command):
        v, codes, cwd = _cmd_verdict(cmd, cwd)
        if v:
            return v
        if depth < _MAX_DEPTH:
            for code in codes:
                v = _bash_verdict(code, cwd, depth + 1)
                if v:
                    return v
    return None


def _apply_patch_paths(patch):
    return [m.group(1) for m in _APPLY_PATCH_PATH.finditer(patch)]


def verdict(data):
    """(rule_id, what, instead) for a deny, else None. The main thread (no agent_type) allows."""
    if not isinstance(data, dict) or not data.get("agent_type"):
        return None
    tool = data.get("tool_name")
    tin = data.get("tool_input") or {}
    if not isinstance(tin, dict):
        return None
    cwd = data.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""
    if tool == "Bash":
        command = tin.get("command")
        if not isinstance(command, str):
            return None
        if len(command) > BASH_COMMAND_MAX_CHARS:
            return _v("CAP", f"to run a Bash command over {BASH_COMMAND_MAX_CHARS} characters",
                      "Write file content with the Write tool and run short commands.")
        return _bash_verdict(command, cwd)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"):
        if tool == "apply_patch":
            paths = _apply_patch_paths(str(tin.get("command") or ""))
        else:
            paths = [tin.get("file_path") or tin.get("notebook_path") or ""]
        for p in paths:
            if isinstance(p, str) and p:
                v = _tool_path_verdict("Edit/Write/patch", p, cwd, True)
                if v:
                    return v
        return None
    if tool in ("Read", "Grep"):
        p = tin.get("file_path") if tool == "Read" else tin.get("path")
        if isinstance(p, str) and p:
            return _tool_path_verdict(tool, p, cwd, False)
    return None


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _payload(v):
    rule, what, instead = v
    return _deny(f"BLOCKED (worker-guard v2, T-5686): a subagent attempted {what} [rule {rule}]. {instead}")


def decide(data):
    v = verdict(data)
    return _payload(v) if v else None


# ---------------------------------------------------------------------------------------------
# Entry: stdin -> one PreToolUse decision, with the deadline / exception rails
# ---------------------------------------------------------------------------------------------
class _DeadlineExpired(BaseException):
    pass


def _on_alarm(signum, frame):
    raise _DeadlineExpired


def _elapsed_since_process_start(started_monotonic=None):
    try:
        import time

        with open("/proc/self/stat", "rb") as f:
            stat = f.read()
        fields = stat[stat.rindex(b")") + 2:].split()
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - started)
    except Exception:
        pass
    if started_monotonic is not None:
        import time

        return max(0.0, time.monotonic() - started_monotonic)
    return 0.0


_TIMED_OUT = _deny(
    "BLOCKED (worker-guard v2, T-5686): the guard ran out of time deciding this command; refused "
    "(fail-closed). Split or simplify the command, and report it to your dispatcher."
)


def _decide_by_deadline(data, deadline):
    import signal

    if hasattr(signal, "setitimer"):
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
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
    return decide(data)


def main(raw=None, deadline=None, started_monotonic=None):
    try:
        if raw is None:
            raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # unparseable: fail-open, as v1
    is_sub = isinstance(data, dict) and bool(data.get("agent_type"))
    try:
        if is_sub:
            limit = deadline if deadline is not None and 0 < deadline <= DECIDE_DEADLINE_S else DECIDE_DEADLINE_S
            remaining = limit - _elapsed_since_process_start(started_monotonic)
            if not remaining > 0:
                payload = _TIMED_OUT
            else:
                payload = _decide_by_deadline(data, min(limit, max(_DEADLINE_FLOOR_S, remaining)))
        else:
            payload = None
    except Exception as exc:
        name = type(exc).__name__
        try:
            sys.stderr.write(f"worker-guard-v2: fail-closed: decide raised {name}\n")
        except Exception:
            pass
        payload = _deny(
            f"BLOCKED (worker-guard v2, T-5686): the guard raised {name} deciding this command; "
            "refused (fail-closed). Split or simplify the command, and report it to your dispatcher."
        )
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
