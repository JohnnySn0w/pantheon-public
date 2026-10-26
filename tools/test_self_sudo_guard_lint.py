"""Tests for self_sudo_guard_lint.py (T-4032).

Covers the detector (flags unguarded per-seat `sudo -u <var>`, passes the guarded/
literal/os.getuid forms), the baseline mechanism, a LIVE-TREE guard (the real tools/
tree must be clean modulo the baseline — the backstop for a --no-verify / hookless push),
and baseline integrity (no stale entries, which would silently mask a new violation)."""
import unittest
from pathlib import Path

try:
    from tools import self_sudo_guard_lint as L
except ImportError:  # bare run with cwd=tools/
    import self_sudo_guard_lint as L  # type: ignore


# --- the flagged class: sudo -u <variable>, no own-account primitive anywhere ---
BAD = '''
import subprocess
def read_seat(account):
    return subprocess.run(["sudo", "-n", "-u", account, "cat", "/x"], capture_output=True)
'''

# --- guarded: getpass.getuser() present -> presumed safe (self reads direct) ---
GUARDED = '''
import getpass, subprocess
def read_seat(account):
    if account == getpass.getuser():
        return subprocess.run(["cat", "/x"])
    return subprocess.run(["sudo", "-n", "-u", account, "cat", "/x"])
'''

# --- guarded via os.getuid() (the ho_resumer style) ---
GUARDED_UID = '''
import os, subprocess
def go(account):
    _ = os.getuid()
    return subprocess.run(["sudo", "-u", account, "true"])
'''

# --- fixed literal account: not a per-seat variable -> not the false-null class ---
LITERAL = '''
import subprocess
def deploy():
    return subprocess.run(["sudo", "-u", "claude", "systemctl", "restart", "x"])
'''

# --- prose / no list construction -> never flagged (AST, not text) ---
PROSE = '''
"""We must never run sudo -u <account> for the runner's own seat."""
X = "sudo -u account is a trap"
'''

# --- combined short flag -nu still matches the u-flag ---
BAD_COMBINED = '''
import subprocess
def f(acct):
    return subprocess.run(["sudo", "-nu", acct, "true"])
'''


class TestDetection(unittest.TestCase):
    def test_flags_unguarded_sudo_seat(self):
        fs = L.lint_source(BAD, "tools/x.py")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].func, "read_seat")

    def test_passes_getuser_guarded(self):
        self.assertEqual(L.lint_source(GUARDED, "tools/x.py"), [])

    def test_passes_getuid_guarded(self):
        self.assertEqual(L.lint_source(GUARDED_UID, "tools/x.py"), [])

    def test_passes_fixed_literal_account(self):
        self.assertEqual(L.lint_source(LITERAL, "tools/x.py"), [])

    def test_prose_not_flagged(self):
        self.assertEqual(L.lint_source(PROSE, "tools/x.py"), [])

    def test_combined_short_flag_flagged(self):
        fs = L.lint_source(BAD_COMBINED, "tools/x.py")
        self.assertEqual([f.func for f in fs], ["f"])


USER_ENV_GUARD = '''
import os, subprocess
def read_seat(account):
    if account == os.environ.get("USER", ""):
        return subprocess.run(["cat", "/x"])
    return subprocess.run(["sudo", "-n", "-u", account, "cat", "/x"])
'''
USER_SUBSCRIPT_GUARD = '''
import os, subprocess
def go(a):
    me = os.environ["USER"]
    return subprocess.run(["sudo", "-u", a, "true"])
'''
GETENV_GUARD = '''
import os, subprocess
def go(a):
    _ = os.getenv("USER")
    return subprocess.run(["sudo", "-u", a, "true"])
'''
# A dict .get("USER") / d["USER"] is NOT the $USER env idiom -> must NOT count as a guard.
FALSE_USER_GUARD = '''
import subprocess
def go(a, d):
    _ = d.get("USER")     # unrelated dict, not os.environ
    return subprocess.run(["sudo", "-n", "-u", a, "true"])
'''


class TestUserEnvGuard(unittest.TestCase):
    def test_environ_get_user_guards(self):
        self.assertEqual(L.lint_source(USER_ENV_GUARD, "tools/x.py"), [])

    def test_environ_subscript_user_guards(self):
        self.assertEqual(L.lint_source(USER_SUBSCRIPT_GUARD, "tools/x.py"), [])

    def test_getenv_user_guards(self):
        self.assertEqual(L.lint_source(GETENV_GUARD, "tools/x.py"), [])

    def test_unrelated_dict_get_user_is_not_a_guard(self):
        fs = L.lint_source(FALSE_USER_GUARD, "tools/x.py")
        self.assertEqual([f.func for f in fs], ["go"])  # still flagged


class TestBaseline(unittest.TestCase):
    def test_baseline_excludes_matching_pathfunc(self):
        base = frozenset({("tools/x.py", "read_seat")})
        self.assertEqual(L.lint_source(BAD, "tools/x.py", baseline=base), [])

    def test_baseline_is_pathfunc_specific(self):
        # same func name, different path -> still flagged
        base = frozenset({("tools/other.py", "read_seat")})
        self.assertEqual(len(L.lint_source(BAD, "tools/x.py", baseline=base)), 1)

    def test_new_func_in_baselined_file_still_flagged(self):
        src = BAD + '\ndef fresh_seat(a):\n    import subprocess\n    subprocess.run(["sudo","-u",a,"true"])\n'
        base = frozenset({("tools/x.py", "read_seat")})  # only the old func grandfathered
        fs = L.lint_source(src, "tools/x.py", baseline=base)
        self.assertEqual([f.func for f in fs], ["fresh_seat"])


# --- shell half (T-4066) ---------------------------------------------------------------
SH_BAD = '''#!/bin/bash
read_seat() {
    local acct="$1"
    sudo -n -u "$acct" cat /x
}
'''
SH_GUARDED_WHOAMI = '''#!/bin/bash
run_as() {
  local account="$1"; shift
  if [ "$(whoami)" = "$account" ]; then bash -c "$*"
  else sudo -n -u "$account" bash -c "$*"; fi
}
'''
SH_GUARDED_ID_UN = 'ME="$(id -un)"\n[[ "$a" == "$ME" ]] || sudo -n -u "$a" true\n'
SH_GUARDED_USER = '[[ "$a" == "$USER" ]] || sudo -n -u "${a}" true\n'
SH_LITERAL = 'sudo -n -u claude systemctl --user restart x\n'
SH_IN_STRING = 'die "must run as ${A}. Wrap with sudo -u ${A}."\n'
SH_COMMENT = '# for each seat: sudo -n -u "$acct" cat /x\ntrue\n'
SH_HEREDOC = 'cat <<EOF\nrun: sudo -n -u "$acct" cat /x\nEOF\ntrue\n'
SH_HEREDOC_THEN_BAD = 'cat <<-\'EOF\'\n\tsudo -u "$a" x\n\tEOF\nsudo -u "$b" y\n'
# `\\$(id -u)` / '...$(id -u)...' run as the TARGET inside the sudo'd shell — not a guard.
SH_ESCAPED_ID = ('sudo -n -u "$a" bash -c "XDG_RUNTIME_DIR=/run/user/\\$(id -u) '
                 'systemctl --user is-active x"\n')
SH_SINGLE_QUOTED_ID = "sudo -n -u \"$a\" bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) x'\n"
# The outer XDG path idiom takes the caller's uid for a path — not a self-vs-other test.
SH_XDG_IDIOM = 'export XDG_RUNTIME_DIR=/run/user/$(id -u)\nsudo -n -u "$a" true\n'
SH_CMDSUBST_IN_DQ = 'out="$(timeout 5 sudo -n -u "$a" cat /x 2>/dev/null || true)"\n'
SH_PIPE_S_K = 'echo "$pw" | sudo -S -k -u "$acct" cat /x\n'
SH_COMBINED_NU = 'sudo -nu "$a" true\n'
SH_ABS_PATH = '/usr/bin/sudo -n -u "$a" true\n'
SH_CALLER_ONLY = 'sudo_run "$acct" cat /x\n'   # a helper NAME, not a sudo command
# A continuation must keep its newline in the masked views, or every later line maps to the
# wrong line/function (here the script-level sudo on line 5 would map to third()).
SH_CONTINUATION = ('first() {\n  sudo -n \\\n    -u "$a" true\n}\n'
                   'sudo -n -u "$b" true\n'
                   'third() {\n  true\n}\n')


class TestShellDetection(unittest.TestCase):
    def _funcs(self, src):
        return [f.func for f in L.lint_shell_source(src, "tools/x.sh")]

    def test_flags_unguarded_sudo_var(self):
        fs = L.lint_shell_source(SH_BAD, "tools/x.sh")
        self.assertEqual([(f.lineno, f.func) for f in fs], [(4, "read_seat")])

    def test_guards_pass(self):
        for src in (SH_GUARDED_WHOAMI, SH_GUARDED_ID_UN, SH_GUARDED_USER):
            with self.subTest(src=src):
                self.assertEqual(self._funcs(src), [])

    def test_non_commands_not_flagged(self):
        for src in (SH_LITERAL, SH_IN_STRING, SH_COMMENT, SH_HEREDOC, SH_CALLER_ONLY):
            with self.subTest(src=src):
                self.assertEqual(self._funcs(src), [])

    def test_heredoc_body_skipped_but_code_after_it_scanned(self):
        fs = L.lint_shell_source(SH_HEREDOC_THEN_BAD, "tools/x.sh")
        self.assertEqual([f.lineno for f in fs], [4])

    def test_target_side_id_is_not_a_guard(self):
        for src in (SH_ESCAPED_ID, SH_SINGLE_QUOTED_ID, SH_XDG_IDIOM):
            with self.subTest(src=src):
                self.assertEqual(self._funcs(src), ["<script>"])

    def test_sudo_shapes_flagged(self):
        for src in (SH_CMDSUBST_IN_DQ, SH_PIPE_S_K, SH_COMBINED_NU, SH_ABS_PATH):
            with self.subTest(src=src):
                self.assertEqual(self._funcs(src), ["<script>"])

    def test_continuation_keeps_line_and_func_mapping(self):
        fs = L.lint_shell_source(SH_CONTINUATION, "tools/x.sh")
        self.assertEqual([(f.lineno, f.func) for f in fs], [(2, "first"), (5, "<script>")])

    def test_shell_baseline_is_pathfunc_keyed(self):
        base = frozenset({("tools/x.sh", "first")})
        fs = L.lint_shell_source(SH_CONTINUATION, "tools/x.sh", baseline=base)
        self.assertEqual([f.func for f in fs], ["<script>"])


class TestShellMaskerReviewFindings(unittest.TestCase):
    """Review-confirmed masker false positives (T-4066 review). Each misread started a fake
    heredoc that blanked the REST of the file — including a later guard — so every case
    below puts an unguarded-looking sudo BEFORE the construct and a guard AFTER it."""
    GUARD_AFTER = 'sudo -n -u "$a" true\n%s\nME="$(whoami)"\n'

    def test_constructs_do_not_start_a_fake_heredoc(self):
        for construct in ('read x <<< hello', 'grep -q x <<<"abc"',
                          'm=$(( 1 << bits ))', '(( m = 1 << bits ))'):
            with self.subTest(construct=construct):
                self.assertEqual(
                    L.lint_shell_source(self.GUARD_AFTER % construct, "tools/x.sh"), [])

    def test_ansi_c_escaped_quote_does_not_end_the_string(self):
        for construct in ("echo $'it\\'s'", "echo $'\\''"):
            with self.subTest(construct=construct):
                self.assertEqual(
                    L.lint_shell_source(self.GUARD_AFTER % construct, "tools/x.sh"), [])

    def test_long_and_absolute_id_forms_guard(self):
        for guard in ('ME="$(id --user --name)"', 'ME="$(/usr/bin/id -un)"'):
            with self.subTest(guard=guard):
                src = f'{guard}\nsudo -n -u "$a" true\n'
                self.assertEqual(L.lint_shell_source(src, "tools/x.sh"), [])

    def test_backslash_and_hyphen_heredoc_delimiters_are_skipped(self):
        for opener, delim in (("<<\\EOF", "EOF"), ("<<'END-TEXT'", "END-TEXT"),
                              ("<<EOF-X", "EOF-X")):
            with self.subTest(opener=opener):
                src = f'cat {opener}\nusage: sudo -u "$acct" cmd\n{delim}\ntrue\n'
                self.assertEqual(L.lint_shell_source(src, "tools/x.sh"), [])


class TestScanCoversShell(unittest.TestCase):
    def test_scan_flags_sh_skips_test_sh_and_allowlisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "seat.sh").write_text(SH_BAD)
            (tools / "test_seat.sh").write_text(SH_BAD)
            (tools / "ok.sh").write_text(SH_BAD)
            allow = (("tools/ok.sh", "test reason"),)
            self.assertEqual([r for r, _, _ in L.scan(Path(tmp), allowlist=allow)],
                             ["tools/seat.sh"])


class TestLiveTree(unittest.TestCase):
    """The real tools/ tree must be clean once the baseline is applied — this is the
    backstop that catches a violation pushed with --no-verify (bypassing the pre-commit
    hook), the same role pgrep_predicate_lint's live-tree test plays."""

    def test_repo_tree_clean_modulo_baseline(self):
        findings = L.scan(L.REPO_ROOT)
        self.assertEqual(
            findings, [],
            f"NEW unguarded per-seat sudo construction(s): {findings}\n"
            "Add the getpass.getuser() own-account guard (or adopt the shared helper). "
            "Do NOT add to the baseline to silence this — the baseline is pre-gate debt only.")

    def test_baseline_has_no_stale_entries(self):
        """Every baseline (path, func) must still correspond to a REAL raw finding. A
        stale entry (its func fixed or removed) would silently mask a future NEW finding
        that happens to reuse the same key — the allowlist-needs-its-own-guard lesson."""
        baseline = L.load_baseline(L.REPO_ROOT)
        raw: set[tuple[str, str]] = set()
        for rel, func in baseline:
            p = L.REPO_ROOT / rel
            try:
                for f in L.lint_source(p.read_text(), rel):  # no baseline -> raw
                    raw.add((rel, f.func))
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                continue
        stale = sorted(baseline - raw)
        self.assertEqual(stale, [], f"stale baseline entries (remove them): {stale}")


if __name__ == "__main__":
    unittest.main()
