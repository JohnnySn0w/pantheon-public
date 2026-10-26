#!/usr/bin/env python3
"""self_sudo_guard_lint — flag per-seat `sudo -u <var>` constructions that lack an
own-account (getuser/getuid) guard, the self-sudo-to-own-account false-null class.

Moves bugs/sudo-u-to-runners-own-account-fails-per-seat-cross-account-tool-false-null.md
from a grep-if-you-remember bug entry to structural enforcement (T-4032, from T-4009 item 4).

THE CLASS. A fleet tool that iterates the seats and prepends `sudo -n -u <seat_account>`
uniformly for EVERY seat works for the peer seats but FAILS on the ONE seat whose unix
account == the account the tool runs under: each agent's NOPASSWD sudo is scoped as
OTHER agent-users only (acting as yourself needs no sudo), so `sudo -n -u <self>` hits
an unconfigured rule and errors. The denial goes to stderr, rc is non-zero, and stdout
is EMPTY — byte-identical to a successful probe that legitimately found nothing — so a
tool keying its verdict off stdout reports a fabricated negative (UNKNOWN currency, "no
activity", "not capped") for its OWN seat while every peer reads fine. It has bitten
FOUR unrelated per-seat tools (delivery_reachability, agent_committed_recently,
cap_mute_driver, pantheon_metrics_gen) because each author hand-rolled sudo-per-seat
without the guard and did not grep bugs/ first — a recurring class, not a one-off.

THE FIX the guard encodes (see the bug entry): special-case the runner's own seat —
run DIRECTLY, no sudo, when the target account is our own. In Python, adopt the shared
helper (T-4066), which does exactly that in one place:

    from seat_exec import seat_argv
    argv = seat_argv(account, base_cmd)   # base_cmd if own account, else sudo -n -u account …

A tool that adopts seat_argv has no raw `["sudo", "-u", var]` list, so it passes this lint
by construction. Known-correct hand-rolled references this lint must NOT flag:
cap_mute_driver (_read_state_raw), cap_modal_reaper — their self branches are direct file
reads that seat_argv does not model. Each calls getpass.getuser() to make the own-account
discrimination — which is exactly the signal this lint keys on.

WHAT IS FLAGGED. A Python source that constructs a `sudo -u <variable>` command
(a list whose string elements include "sudo" and a `-u`-bearing flag, with the account
supplied by a NON-literal element — the per-seat variable) AND that calls NONE of
getpass.getuser / os.getuid / os.geteuid ANYWHERE in the module. That module-scope
guard test is deliberately COARSE, and deliberately so: it is a commit gate, where a
false positive blocks a legitimate commit fleet-wide (T-1102 blast radius), so the bar
to flag is "the author never reached for the own-account primitive at all" — which is
precisely the shape of all four historical instances (none called getuser). A tool that
uses a `_sudo(account)` factory with the guard at the CALL site is therefore NOT flagged
(getuser is present in the module), and neither is any tool that added the guard.

WHAT IS DELIBERATELY NOT FLAGGED (flagging any would make the lint noise):
  - A `sudo -u <literal>` with a constant account ("sudo","-u","claude",...): a
    fixed-target invocation, not a per-seat sweep — only the VARIABLE-account form is
    the false-null class.
  - Any module that calls getuser/getuid/geteuid: it reached for the own-account
    primitive, so the discrimination is presumed present (the correct references all do).
  - Comments, docstrings, and string literals: prose describing the trap (this file, the
    bug entry) is parsed as AST, so only a real list construction qualifies, never text.
  - Test sources (test_*.py / test_*.sh): a detector's own tests must carry synthetic bad
    samples, which would make the live-tree guard unsatisfiable; and a fixture's sudo is
    its own controlled construct.

SHELL (.sh) HALF (T-4066). tools/**/*.sh get the same coarse rule via lint_shell_source:
a `sudo [opts] -u "$var"` in COMMAND position (not inside a string, comment or heredoc)
in a script whose outer shell never evaluates $(id -un)/$(id -u)/whoami/$USER/$UID/$EUID.
The idiom it wants is parity_render's run_as: `[ "$(whoami)" = "$account" ]` -> run
direct, else `sudo -n -u "$account" …`. Not seen: `--user`, `-u"$x"` without a space,
`$SUDO -u`, `sudo -g g -u`, xargs `{}`. Known masker limits (T-4066 review; rare, fail
toward a FLAG): an apostrophe inside `case` within `"$(…)"`, nested quotes in
`"${x:-"…"}"`, an UNQUOTED prose `echo … sudo -u "$a"`. A fixed-target variable
(`T=claude; sudo -u "$T"`) is flagged by design, same as the Python half: take the
ALLOWLIST route with a reason.

ALLOWLIST. Curated deliberately, never auto-derived. Add an entry only with a reason
that meets the bar: a genuine per-seat sudo construction whose own-account case is
provably handled by a mechanism this AST check cannot see (e.g. the account is resolved
to a guaranteed-non-self value before construction). "It is existing code" is not a
reason. A KNOWN-VIOLATION parked so a guard can land born-green must carry a
machine-readable `tracked: T-NNNN` marker (same discipline as pgrep_predicate_lint).

Report-only / read-only: scans committed sources and authors nothing.

Usage:
    python3 tools/self_sudo_guard_lint.py            # scan tools/, exit 1 on findings
    python3 tools/self_sudo_guard_lint.py --quiet     # exit code only
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# tools/**/*.py (AST) and tools/**/*.sh (masked regex; see the SHELL half below).
SCAN_DIR = "tools"
TEST_PREFIXES = ("test_",)

# Grandfathered instances (T-4032): the constructions that PREDATE this gate, frozen so
# the gate lands born-green and blocks only NEW unguarded sudo-per-seat (the class-killer
# goal — "so the 5th tool never ships broken"). Each is keyed by (relpath, enclosing-func)
# — line-independent, so an unrelated edit to a baselined file does not re-flag it, while
# a genuinely NEW construction (new function, or a rename) is caught. This is a DEBT list
# to triage down to zero (fix the real false-nulls, convert the safe-by-context ones to
# a justified ALLOWLIST entry), NOT a justification — same discipline as
# cited_hash_lint_baseline.txt. Triage tracked separately.
BASELINE_FILENAME = "self_sudo_guard_lint_baseline.txt"

# The own-account primitives whose presence ANYWHERE in a module marks it as
# having reached for the self-vs-other discrimination.
_GUARD_ATTRS = frozenset({"getuser", "getuid", "geteuid"})

# A `-u`-bearing sudo flag element: "-u", or a combined short-flag cluster that
# contains a bare 'u' (e.g. "-nu"). NOT "--user=..." (a single joined token has no
# separate variable account element, so it fails the has_var test anyway).
_U_FLAG_RE = re.compile(r"^-[a-zA-Z]*u[a-zA-Z]*$")

#: (path relative to repo root, reason). Curated — see ALLOWLIST in the docstring.
#: These sudo to a FIXED foreign account (never the runner's own), so the self-sudo
#: false-null cannot occur; triaged out of the T-4058 baseline.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "tools/kino_pwa_autodeploy.py",
        "T-4058: _sudo_claude sudo's to the FIXED literal DEPLOY_USER = 'claude' (a "
        "module constant), never a per-seat variable. Recovery runs the autodeploy and "
        "sudo's into claude to touch claude-owned build/rsync dirs; recovery != claude, so "
        "the own-account case never arises. Not a per-seat sweep.",
    ),
    (
        "tools/mail_staleness_monitor.py",
        "T-4058: discover_inboxes sudo's to the 0710 hub's owner (post-office since "
        "T-3421, derived from the hub path — 'borrow the owner's eyes', documented in "
        "its docstring), a foreign service account. capture_pane_idle already self-guards via "
        "`account == os.environ.get('USER')` (the $USER idiom the lint now recognizes). "
        "Neither can self-sudo the runner.",
    ),
    (
        "tools/activity_monitor.py",
        "T-4058: _sudo(user) is per-seat, but AGENT_CONFIG covers orchestrator/artisan/mason "
        "ONLY — never recovery, the runner — so the own-account case is unreachable by "
        "configuration, AND the probe fail-CLOSES (a sudo failure degrades to 'no "
        "recent write' -> the P1.b poke still fires, never a false silence). Documented "
        "in bugs/sudo-u-to-runners-own-account-...md scope note. CAVEAT: if recovery self-"
        "monitoring is ever added to AGENT_CONFIG, apply the own-account-direct guard.",
    ),
    (
        "tools/fleet_reboot.py",
        "T-4058: quiesce_handoff_pause/quiesce_repo_clean sudo to cfg['user'] (per-seat), "
        "but orchestrate() dispatches on cfg['strategy'] — recovery is the DRIVER and takes "
        "the 'driver' strategy (quiesce_drv: chronicles its own HANDOFF directly, NO "
        "sudo), so these two sudo funcs run ONLY for the 'handoff_pause'/'repo_clean' "
        "strategies = the foreign agents (orchestrator/specialist/codex), never recovery. Safe by "
        "dispatch invariant (verified in the QUIESCE_ORDER loop). Also an ACTION, not a "
        "read-treated-as-absence: a self-sudo would fail VISIBLY (dry-run), not false-null.",
    ),
    (
        "tools/ticket_lint.py",
        "T-4058: ratifiable_path_status sudo's to the fixed 'claude' and runs on every "
        "account (so on the claude/orchestrator account it IS self), but a sudo failure returns "
        "None = UNRESOLVABLE/could-not-verify (fail-OPEN), never a fabricated negative — "
        "so it is not the false-null class the gate targets. (An own-account-direct guard "
        "would let it verify claude-owned repos on the claude account too, but that is a "
        "strengthening, not a false-null fix.)",
    ),
    (
        "tools/welltrod_stats.py",
        "T-4058: list_sessions_in_window sudo's to cfg['user'] (per-seat, possibly the "
        "runner), BUT it already honours the companion rule — 'Returns None (NOT []) when "
        "the listing itself failed' — so a self-sudo denial reads as None/unknown, never a "
        "false empty. Not the false-null class.",
    ),
    (
        "tools/t3277_persona_timer_reconcile.sh",
        "T-4066 (shell half): one-shot T-3277 migration (ticket done). It reads its sudo "
        "password from /home/claude/root.env in the RUNNER's shell — a 700 claude-owned "
        "home — so it can only run as claude, and DRIFTED_ACCTS (claude-research/"
        "claude-fallback/suborch1/suborch2) never includes claude: the own-account case is "
        "unreachable. CAVEAT: its preflight SKIPs on empty grep stdout (the false-null "
        "shape) — guard it before any re-run from another account.",
    ),
)


@dataclass(frozen=True)
class Finding:
    lineno: int
    func: str
    detail: str


def _is_sudo_seat_list(node: ast.AST) -> bool:
    """True iff `node` is a list literal building a `sudo -u <variable>` command:
    string elements include "sudo" AND a `-u`-bearing flag, and at least one element is
    NON-constant (the per-seat account variable). An all-constant list (fixed literal
    target) is not the per-seat false-null class and is not flagged."""
    if not isinstance(node, ast.List):
        return False
    str_consts = [e.value for e in node.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if "sudo" not in str_consts:
        return False
    if not any(_U_FLAG_RE.match(s) for s in str_consts):
        return False
    return any(not isinstance(e, ast.Constant) for e in node.elts)


def _reads_user_env(tree: ast.Module) -> bool:
    """True iff the module reads the USER environment variable — the other valid
    own-account idiom (`account == os.environ.get("USER")` / `os.environ["USER"]` /
    `os.getenv("USER")`). Matched precisely (the key must be "USER" AND come from
    os.environ / os.getenv) so an unrelated `d.get("USER")` / `d["USER"]` never counts."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn, args = n.func, n.args
            first_is_user = bool(args) and isinstance(args[0], ast.Constant) and args[0].value == "USER"
            if not first_is_user:
                continue
            if isinstance(fn, ast.Attribute) and fn.attr == "get":
                v = fn.value  # os.environ.get("USER")
                if (isinstance(v, ast.Attribute) and v.attr == "environ") or \
                   (isinstance(v, ast.Name) and v.id == "environ"):
                    return True
            elif (isinstance(fn, ast.Attribute) and fn.attr == "getenv") or \
                 (isinstance(fn, ast.Name) and fn.id == "getenv"):  # os.getenv("USER")
                return True
        elif isinstance(n, ast.Subscript):  # os.environ["USER"]
            v, sl = n.value, n.slice
            is_environ = (isinstance(v, ast.Attribute) and v.attr == "environ") or \
                         (isinstance(v, ast.Name) and v.id == "environ")
            if is_environ and isinstance(sl, ast.Constant) and sl.value == "USER":
                return True
    return False


def _module_has_guard(tree: ast.Module) -> bool:
    """True iff the module reaches for an own-account primitive: getpass.getuser /
    os.getuid / os.geteuid is CALLED anywhere, OR it reads the USER env var (the two
    valid self-vs-other discrimination idioms; the correct references use one or the other)."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in _GUARD_ATTRS:
                return True
            if isinstance(f, ast.Name) and f.id in _GUARD_ATTRS:
                return True
    return _reads_user_env(tree)


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.func_stack: list[str] = []
        self.hits: list[tuple[int, str]] = []  # (lineno, enclosing-func-name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_List(self, node: ast.List) -> None:
        if _is_sudo_seat_list(node):
            self.hits.append((node.lineno, self.func_stack[-1] if self.func_stack else "<module>"))
        self.generic_visit(node)


def load_baseline(root: Path) -> frozenset[tuple[str, str]]:
    """Load the grandfathered (relpath, func) pairs from BASELINE_FILENAME. Lines are
    `relpath::func`; blanks and `#`-comments are ignored. Absent file => empty set."""
    path = root / SCAN_DIR / BASELINE_FILENAME
    pairs: set[tuple[str, str]] = set()
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return frozenset()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "::" not in line:
            continue
        rel, func = line.split("::", 1)
        pairs.add((rel.strip(), func.strip()))
    return frozenset(pairs)


def lint_source(src: str, path: str,
                baseline: frozenset[tuple[str, str]] = frozenset()) -> list[Finding]:
    """Return findings for one Python source. A finding = a `sudo -u <var>` construction
    in a module that never calls an own-account primitive, EXCLUDING any (path, func) in
    `baseline`. Raises on unparseable source (the caller decides fail-open); returns []
    for a clean/fully-baselined module."""
    tree = ast.parse(src)
    if _module_has_guard(tree):
        return []  # module reached for the own-account discrimination — presumed guarded
    v = _Visitor()
    v.visit(tree)
    return [Finding(lineno, func,
                    "sudo -u <variable> per-seat construction with no getpass.getuser()/"
                    "os.getuid() own-account guard anywhere in the module")
            for lineno, func in v.hits
            if (path, func) not in baseline]


# --- Shell (.sh) half (T-4066) ----------------------------------------------------------
# Same class, same COARSE file-level guard semantics as the Python half: flag a per-seat
# `sudo … -u "$var"` in a script that never reaches for an own-account primitive anywhere.
# Shell has no AST, so _shell_views masks the source first: `sudo` only counts in command
# position (unquoted — never inside a "…" / '…' string, comment, or heredoc body), and a
# guard only counts where the OUTER shell evaluates it (not single-quoted, not `\$`-escaped
# — `sudo -u "$a" bash -c "…/run/user/\$(id -u)…"` runs `id` as the TARGET, not a guard).

# The own-account primitives, matched against the outer-shell-evaluated view.
_SH_GUARD_RE = re.compile(
    r"(?:\$\(|`)\s*(?:\S*/)?(?:id\s+(?:-[a-zA-Z]*u[a-zA-Z]*|--user)|whoami|logname)\b"  # $(id -un)
    r"|\$\{?(?:USER|LOGNAME|UID|EUID)\b")                            # $USER / ${EUID}
# The XDG idiom takes the caller's uid for a PATH, not a self-vs-other test — not a guard.
_SH_XDG_IDIOM_RE = re.compile(r"/run/user/(?:\$\(\s*id\s+-u\s*\)|\$\{?UID\}?)")
# `sudo [opts] -u "$var"` / `-nu $var` / `-S -k -u "${var}"`, continuation-tolerant.
_SH_WS = r"(?:[ \t]|\\\n)+"
_SH_SUDO_VAR_RE = re.compile(
    rf"sudo((?:{_SH_WS}--?[a-zA-Z][\w-]*(?:=\S+)?)*?){_SH_WS}-[a-zA-Z]*u{_SH_WS}\"?\$")
_SH_SUDO_WORD_RE = re.compile(r"(?<![\w.$-])sudo(?![\w.-])")   # also /usr/bin/sudo
_SH_FUNC_RE = re.compile(r"^(?:function[ \t]+([A-Za-z_][\w:-]*)|([A-Za-z_][\w:-]*)[ \t]*\([ \t]*\))")
# <<EOF / <<-EOF / <<'EOF' / <<"EOF" / <<\EOF, hyphens allowed in the word (END-TEXT).
_SH_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:\\|(['\"]?))([A-Za-z_][\w-]*)(?(2)\2)")


def _in_arithmetic(src: str, i: int) -> bool:
    """True iff offset `i` sits inside an unclosed `((` / `$((` opened earlier on its line —
    where `<<` is a bit shift, not a heredoc."""
    head = src[src.rfind("\n", 0, i) + 1:i]
    return head.count("((") > head.count("))")


def _shell_views(src: str) -> tuple[str, str]:
    """Return (command_view, live_view), each the length of `src` with newlines kept (so
    offsets map back to line numbers). command_view keeps only UNQUOTED shell text — where
    a `sudo` is really a command. live_view additionally keeps double-quoted text — what
    the outer shell expands. Both blank comments, heredoc bodies, single-quoted text,
    backslash escapes and line continuations. `$(…)` / backticks nest as unquoted even
    inside "…" (their content is code). Heuristic, not a parser: good enough for a gate
    that must be low-noise, and fails toward NOT flagging on exotic quoting."""
    n = len(src)
    cmd, live = [" "] * n, [" "] * n
    stack: list[str] = ["N"]          # N normal, C $(…), B `…`, D "…", S '…', A $'…'
    depth: list[int] = []             # paren depth per open C frame
    heredocs: list[tuple[str, bool]] = []
    i = 0
    while i < n:
        c, top = src[i], stack[-1]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "\n":
            cmd[i] = live[i] = "\n"
            if top in ("S", "A", "D") or not heredocs:
                i += 1
                continue
            j = i + 1                  # skip each pending heredoc body (newlines kept)
            for delim, strip_tabs in heredocs:
                while j < n:
                    end = src.find("\n", j)
                    end = n if end < 0 else end
                    line = src[j:end]
                    if end < n:
                        cmd[end] = live[end] = "\n"
                    j = end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
            heredocs.clear()
            i = j
            continue
        if top == "S" or top == "A":
            if c == "\\" and top == "A":   # $'…' honours backslash escapes; '…' does not
                i += 2
                continue
            if c == "'":
                stack.pop()
            i += 1
            continue
        if c == "\\":                  # escape / continuation: blank both chars
            if nxt == "\n":            # (a continuation keeps its newline: line mapping)
                cmd[i + 1] = live[i + 1] = "\n"
            i += 2
            continue
        if top == "D":
            if c == '"':
                stack.pop()
            elif c == "$" and nxt == "(":
                stack.append("C"); depth.append(0)
                cmd[i] = live[i] = c
            elif c == "`":
                stack.append("B")
                live[i] = c
            else:
                live[i] = c
            i += 1
            continue
        # normal-like: N / C / B
        if c == "#" and (i == 0 or src[i - 1] in " \t\n;|&("):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "'":
            stack.append("A" if i > 0 and src[i - 1] == "$" else "S")
        elif c == '"':
            stack.append("D")
        elif c == "`":
            if top == "B":
                stack.pop()
            else:
                stack.append("B")
            live[i] = c
        else:
            if (c == "<" and nxt == "<" and src[i + 2:i + 3] != "<"
                    and (i == 0 or src[i - 1] != "<")           # not a <<< herestring
                    and not _in_arithmetic(src, i)):            # not a (( 1 << n )) shift
                m = _SH_HEREDOC_RE.match(src, i)
                if m:
                    heredocs.append((m.group(3), m.group(1) == "-"))
            elif c == "$" and nxt == "(":
                stack.append("C"); depth.append(0)
            elif top == "C" and c == "(":
                depth[-1] += 1
            elif top == "C" and c == ")":
                depth[-1] -= 1
                if depth[-1] <= 0:
                    depth.pop(); stack.pop()
            cmd[i] = live[i] = c
        i += 1
    return "".join(cmd), "".join(live)


def _shell_has_guard(live: str) -> bool:
    """True iff the outer shell evaluates an own-account primitive anywhere — $(id -un),
    $(id -u), whoami, $USER, $UID, $EUID — excluding the /run/user/<uid> XDG path idiom."""
    return bool(_SH_GUARD_RE.search(_SH_XDG_IDIOM_RE.sub(" ", live)))


def lint_shell_source(src: str, path: str,
                      baseline: frozenset[tuple[str, str]] = frozenset()) -> list[Finding]:
    """Shell counterpart of lint_source: findings for `sudo … -u "$var"` in command position
    in a script that evaluates no own-account primitive anywhere. `func` is the enclosing
    shell function (a column-0 `name() {` … `}` block), else "<script>"."""
    cmd, live = _shell_views(src)
    if _shell_has_guard(live):
        return []
    # enclosing-function map, by line (column-0 header opens, column-0 `}` closes)
    func_at: list[str] = []
    cur = "<script>"
    for line in cmd.split("\n"):
        m = _SH_FUNC_RE.match(line)
        if m:
            cur = m.group(1) or m.group(2)
        func_at.append(cur)
        if line.rstrip() == "}":
            cur = "<script>"
    out: list[Finding] = []
    for m in _SH_SUDO_WORD_RE.finditer(cmd):
        if not _SH_SUDO_VAR_RE.match(src, m.start()):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        func = func_at[lineno - 1]
        if (path, func) in baseline:
            continue
        out.append(Finding(lineno, func,
                           'sudo -u "$var" per-seat construction with no $(id -un)/$USER/'
                           "$EUID own-account guard anywhere in the script"))
    return out


def scan(root: Path, allowlist: tuple[tuple[str, str], ...] = ALLOWLIST) -> list[tuple[str, int, str]]:
    """Return [(relpath, lineno, detail)] for every non-allowlisted finding under
    <root>/tools — Python via lint_source, shell (*.sh) via lint_shell_source. Per-file
    parse errors are skipped (a syntactically-broken file is not this lint's concern), so
    the scan never crashes on one bad file."""
    allowed = {p for p, _reason in allowlist}
    baseline = load_baseline(root)
    out: list[tuple[str, int, str]] = []
    tools_dir = root / SCAN_DIR
    paths = sorted([*tools_dir.rglob("*.py"), *tools_dir.rglob("*.sh")])
    for path in paths:
        if not path.is_file() or path.name.startswith(TEST_PREFIXES):
            continue
        # Test-support dirs are out of scope (a fixture switching its own sandbox uids
        # is not the fleet-seat false-null class; and the detector's own tests carry
        # synthetic bad samples). Covers conftest.py etc. under any tests/ dir.
        if "tests" in path.relative_to(tools_dir).parts[:-1]:
            continue
        rel = str(path.relative_to(root))
        if rel in allowed:
            continue
        linter = lint_shell_source if path.suffix == ".sh" else lint_source
        try:
            findings = linter(path.read_text(), rel, baseline=baseline)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        for f in findings:
            out.append((rel, f.lineno, f"{f.func}: {f.detail}"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flag per-seat `sudo -u <var>` constructions lacking an own-account guard (T-4032).")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="tree to scan")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args(argv)

    findings = scan(args.root)
    if args.quiet:
        return 1 if findings else 0
    if not findings:
        print(f"[self_sudo_guard_lint] clean — no unguarded per-seat sudo constructions "
              f"outside the {len(ALLOWLIST)} allowlisted file(s)")
        return 0

    print(f"[self_sudo_guard_lint] {len(findings)} unguarded per-seat sudo construction(s) "
          f"— these false-null on the runner's OWN seat:\n")
    for rel, lineno, detail in findings:
        print(f"  {rel}:{lineno}\n      {detail}")
    print(
        "\n`sudo -n -u <self>` is REJECTED (each account's NOPASSWD sudo is as-other-users\n"
        "only), so the probe false-nulls for the runner's own seat. Special-case it:\n"
        "    Python: from seat_exec import seat_argv; argv = seat_argv(account, base)\n"
        '    shell:  if [ "$(whoami)" = "$acct" ]; then cmd...; else sudo -n -u "$acct" cmd...; fi\n'
        "(a FIXED foreign target? add an ALLOWLIST entry with a reason instead).\n"
        "and never treat empty stdout as absence unless rc==0. See\n"
        "bugs/sudo-u-to-runners-own-account-fails-per-seat-cross-account-tool-false-null.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
