"""Tests for codex_hook_trust_check.py (T-4155).

Hook dicts mirror what `codex app-server` hooks/list returned live on codex-cli
0.147.0 for the codex seat (T-4144). Run: python3 -m unittest tools.test_codex_hook_trust_check
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import codex_hook_trust_check as chk

GUARD_CMD = "/usr/bin/python3 /home/codex/pantheon-config/tools/hooks/worker-guard.py"
START_CMD = "/usr/bin/python3 /home/codex/codex-telegram/run_codex_session_start_hook.py"
STOP_CMD = "/usr/bin/python3 /home/codex/codex-telegram/run_codex_stop_hook.py"


def _hook(event, command, *, trust="trusted", matcher=None, enabled=True, key=None):
    return {"key": key or f"/home/codex/.codex/config.toml:{event}:0:0", "eventName": event,
            "command": command, "matcher": matcher, "trustStatus": trust, "enabled": enabled}


def _live_seat():
    return [
        _hook("preToolUse", GUARD_CMD, matcher="Bash|apply_patch"),
        _hook("sessionStart", START_CMD, matcher="startup|resume|clear|compact"),
        _hook("stop", STOP_CMD),
    ]


EXPECTED = {START_CMD, STOP_CMD}


class Evaluate(unittest.TestCase):
    def test_live_seat_shape_passes(self):
        self.assertEqual(chk.evaluate(_live_seat(), EXPECTED), [])

    def test_missing_guard_fails(self):
        hooks = [h for h in _live_seat() if h["eventName"] != "preToolUse"]
        self.assertIn("no PreToolUse worker-guard hook configured", chk.evaluate(hooks, EXPECTED))

    def test_untrusted_disabled_or_narrow_guard_fails(self):
        for change in ({"trustStatus": "untrusted"}, {"enabled": False},
                       {"matcher": "Bash"}, {"matcher": "apply_patch"}):
            with self.subTest(change=change):
                hooks = _live_seat()
                hooks[0] = {**hooks[0], **change}
                problems = chk.evaluate(hooks, EXPECTED)
                self.assertTrue(problems and "not live" in problems[0], problems)

    def test_matcherless_guard_covers_every_tool(self):
        hooks = _live_seat()
        hooks[0] = {**hooks[0], "matcher": None}
        self.assertEqual(chk.evaluate(hooks, EXPECTED), [])

    def test_undeclared_trusted_hook_is_flagged(self):
        hooks = _live_seat() + [_hook("preToolUse", "/usr/bin/python3 /tmp/evil.py",
                                      key="/home/codex/.codex/config.toml:pre_tool_use:0:1")]
        problems = chk.evaluate(hooks, EXPECTED)
        self.assertTrue(any("/tmp/evil.py" in p for p in problems), problems)

    def test_undeclared_untrusted_hook_is_still_an_injection(self):
        # T-4160: the control-plane codex launch passes --dangerously-bypass-hook-trust,
        # so an untrusted hook RUNS there; it counts like a trusted stray.
        hooks = _live_seat() + [_hook("stop", "/usr/bin/python3 /tmp/x.py", trust="untrusted")]
        problems = chk.evaluate(hooks, EXPECTED)
        self.assertTrue(any("/tmp/x.py" in p and "trust=untrusted" in p for p in problems), problems)

    def test_disabled_undeclared_hook_is_not_an_injection(self):
        hooks = _live_seat() + [_hook("stop", "/usr/bin/python3 /tmp/x.py", enabled=False)]
        self.assertEqual(chk.evaluate(hooks, EXPECTED), [])

    def test_claude_seat_fallback_carries_only_the_guard(self):
        # A Claude seat's codex fallback declares nothing else (no codex-config clone).
        guard_only = [h for h in _live_seat() if chk._is_guard(h)]
        self.assertEqual(chk.evaluate(guard_only, set()), [])
        self.assertTrue(chk.evaluate(_live_seat(), set()))  # the codex-telegram hooks would be strays


class SnippetCommands(unittest.TestCase):
    def test_binds_codex_home_and_skips_state_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            snippet = Path(tmp) / "config.toml.snippet"
            snippet.write_text(
                '[features]\nhooks = true\n\n'
                '[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                'command = "/usr/bin/python3 {{ codex_home }}/codex-telegram/run_codex_stop_hook.py"\n\n'
                '[hooks.state]\n', encoding="utf-8")
            self.assertEqual(chk.snippet_commands(snippet, Path("/home/codex")), {STOP_CMD})

    def test_unreadable_snippet_is_unavailable(self):
        with self.assertRaises(chk.Unavailable):
            chk.snippet_commands(Path("/nonexistent/config.toml.snippet"), Path("/home/codex"))


def _instance(tmp: str, roles: dict, default: str | None = "claude-code") -> Path:
    """A minimal instance.yaml binding each account to a harness (None = omit the key)."""
    lines = ["instance:", f"  harness_default: {default}" if default else "  id: x", "roles:"]
    for role, (account, harness) in roles.items():
        lines += [f"  {role}:", f"    account: {account}"]
        if harness is not None:
            lines.append(f"    harness: {harness}")
    path = Path(tmp) / "instance.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class SeatHarness(unittest.TestCase):
    """T-4763: the seat type comes from instance.yaml, not from a snippet's presence."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_codex_and_claude_bindings(self):
        inst = _instance(self.tmp, {"peer-daemon-codex": ("codex", "codex"),
                                    "specialist": ("claude-research", "claude-code")})
        self.assertEqual(chk.seat_harness("codex", inst), "codex")
        self.assertEqual(chk.seat_harness("claude-research", inst), "claude-code")

    def test_missing_harness_key_takes_harness_default(self):
        inst = _instance(self.tmp, {"r": ("acct", None)}, default="codex")
        self.assertEqual(chk.seat_harness("acct", inst), "codex")

    def test_doubt_is_unavailable_never_a_guess(self):
        cases = {
            "unbound account": (_instance(self.tmp, {"r": ("other", "codex")}), "acct"),
            "roles disagree": (_instance(self.tmp, {"a": ("acct", "codex"),
                                                    "b": ("acct", "claude-code")}), "acct"),
            "unknown harness": (_instance(self.tmp, {"r": ("acct", "gemini")}), "acct"),
            "no harness, no default": (_instance(self.tmp, {"r": ("acct", None)}, default=None),
                                       "acct"),
            "missing manifest": (Path(self.tmp) / "absent.yaml", "acct"),
        }
        for name, (inst, account) in cases.items():
            with self.subTest(name), self.assertRaises(chk.Unavailable):
                chk.seat_harness(account, inst)

    def test_bad_yaml_is_unavailable(self):
        bad = Path(self.tmp) / "instance.yaml"
        bad.write_text("roles: [unclosed\n", encoding="utf-8")
        with self.assertRaises(chk.Unavailable):
            chk.seat_harness("acct", bad)

    def test_live_instance_matches_the_axis_seats(self):
        # Every seat the HARD axis applies to resolves; only the codex peer daemon is
        # codex-primary, the five Claude seats carry codex as a fallback (T-4160).
        import yaml
        repo = Path(chk.__file__).resolve().parents[1]
        axes = yaml.safe_load((repo / "sop/standard/seat-parity/axes.yaml").read_text())
        axis = next(a for a in axes["axes"] if a["axis_id"] == "codex-worker-guard-hook-trusted")
        roles = yaml.safe_load(chk.DEFAULT_INSTANCE.read_text())["roles"]
        got = {key: chk.seat_harness(roles[key]["account"], chk.DEFAULT_INSTANCE)
               for key in axis["applies_to"]}
        self.assertEqual(got.pop("peer-daemon-codex"), "codex")
        self.assertEqual(len(got), 5)
        self.assertEqual(set(got.values()), {"claude-code"}, got)

    def test_in_repo_snippet_matches_codex_config_at_the_bridge_pin(self):
        # Verdict parity for the codex seat: before T-4763 it declared the hooks of its
        # live ~/codex-config clone (held at the bridge pin 1f84f64, C1 kept that history
        # in this repo); now it declares harness/codex's. They must be the same set.
        import subprocess
        repo = Path(chk.__file__).resolve().parents[1]
        r = subprocess.run(["git", "-C", str(repo), "show", "1f84f64:templates/config.toml.snippet"],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            self.skipTest(f"codex-config history at 1f84f64 not in this clone: {r.stderr.strip()}")
        pinned = Path(self.tmp) / "pinned.snippet"
        pinned.write_text(r.stdout, encoding="utf-8")
        home = Path("/home/codex")
        self.assertEqual(chk.snippet_commands(chk.DEFAULT_SNIPPET, home),
                         chk.snippet_commands(pinned, home))
        self.assertEqual(chk.DEFAULT_SNIPPET,
                         repo / "harness" / "codex" / "templates" / "config.toml.snippet")


class SeatTypeVerdicts(unittest.TestCase):
    """T-4763: main()'s verdict per seat type, with no --snippet (the axis invocation).
    Every seat runs with a fake home that DOES hold a ~/codex-config snippet, so the
    pre-T-4763 presence signal would have called each one a codex seat; the instance.yaml
    binding must win."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.home = Path(self.tmp) / "home"
        stale = self.home / "codex-config" / "templates" / "config.toml.snippet"
        stale.parent.mkdir(parents=True)
        stale.write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "/tmp/stale.py"\n',
                         encoding="utf-8")
        self.inst = _instance(self.tmp, {"peer-daemon-codex": ("codex", "codex"),
                                         "head-engineering-orchestrator": ("claude", "claude-code")})

    def _seat(self):
        """The live codex-seat hook shape, bound to the fake home."""
        h = self.home
        return [
            _hook("preToolUse", f"/usr/bin/python3 {h}/pantheon-config/tools/hooks/worker-guard.py",
                  matcher="Bash|apply_patch"),
            _hook("sessionStart", f"/usr/bin/python3 {h}/codex-telegram/run_codex_session_start_hook.py",
                  matcher="startup|resume|clear|compact"),
            _hook("stop", f"/usr/bin/python3 {h}/codex-telegram/run_codex_stop_hook.py"),
        ]

    def _run(self, account, hooks, *, snippet=None, unavailable=None, instance=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk.Path, "home", return_value=self.home), \
             mock.patch.object(chk, "_account", return_value=account), \
             mock.patch.object(chk, "DEFAULT_SNIPPET", snippet or chk.DEFAULT_SNIPPET), \
             mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             mock.patch.object(chk, "list_hooks", side_effect=unavailable, return_value=hooks), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(["--instance", str(instance or self.inst)])
        return rc, out.getvalue().strip(), err.getvalue()

    def _verdict(self, rc, out, err):
        from tools import parity_render as pr
        return pr.classify_outcome("file_grep", pr.ExecResult(rc=rc, stdout=out, stderr=err))[0]

    def test_codex_primary_seat_declares_the_in_repo_snippet(self):
        # Live codex seat shape (guard + codex-telegram start/stop hooks) passes.
        self.assertEqual(self._run("codex", self._seat())[:2], (0, "true"))
        self.assertEqual(self._run("codex", self._seat()[:1])[:2], (0, "true"))
        # ~/codex-config's snippet is no longer consulted: its hook is a stray now.
        rc, out, err = self._run("codex", self._seat() + [_hook("stop", "/tmp/stale.py")])
        self.assertEqual((rc, out), (0, "false"))
        self.assertIn("/tmp/stale.py", err)
        self.assertEqual(self._verdict(rc, out, err), "FAIL")

    def test_claude_seat_codex_fallback_is_guard_only(self):
        self.assertEqual(self._run("claude", self._seat()[:1])[:2], (0, "true"))
        # The codex-telegram hooks are strays on a Claude seat even though the in-repo
        # snippet (which every seat now clones) and its ~/codex-config both declare them.
        rc, out, err = self._run("claude", self._seat())
        self.assertEqual((rc, out), (0, "false"))
        self.assertIn("run_codex_session_start_hook", err)
        self.assertEqual(self._run("claude", [])[:2], (0, "false"))  # no guard at all

    def test_unbound_account_is_unmeasurable(self):
        rc, out, err = self._run("runner", self._seat())
        self.assertEqual((rc, out), (chk.EXIT_UNMEASURABLE, ""))
        self.assertEqual(self._verdict(rc, out, err), "UNMEASURABLE")

    def test_missing_instance_is_unmeasurable_not_fail(self):
        rc, out, err = self._run("codex", self._seat(), instance=Path(self.tmp) / "absent.yaml")
        self.assertEqual((rc, out), (chk.EXIT_UNMEASURABLE, ""))
        self.assertEqual(self._verdict(rc, out, err), "UNMEASURABLE", err)

    def test_codex_seat_with_missing_in_repo_snippet_is_unmeasurable(self):
        rc, out, err = self._run("codex", self._seat(),
                                 snippet=Path(self.tmp) / "nope" / "config.toml.snippet")
        self.assertEqual((rc, out), (chk.EXIT_UNMEASURABLE, ""))
        self.assertEqual(self._verdict(rc, out, err), "UNMEASURABLE", err)

    def test_claude_seat_never_reads_the_snippet(self):
        # A Claude seat stays measurable with no readable snippet anywhere.
        rc, out, _ = self._run("claude", self._seat()[:1],
                               snippet=Path(self.tmp) / "nope" / "config.toml.snippet")
        self.assertEqual((rc, out), (0, "true"))

    def test_app_server_unavailable_is_unmeasurable_for_both_seat_types(self):
        for account in ("codex", "claude"):
            with self.subTest(account):
                rc, out, err = self._run(account, None, unavailable=chk.Unavailable("gone"))
                self.assertEqual((rc, out), (chk.EXIT_UNMEASURABLE, ""))
                self.assertEqual(self._verdict(rc, out, err), "UNMEASURABLE")


class MainContract(unittest.TestCase):
    """Parity contract: stdout true/false only when evaluated; otherwise empty stdout
    and exit 3, which parity_render classifies UNMEASURABLE (file_grep)."""

    def _main(self, *, hooks=None, unavailable=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk, "snippet_commands", return_value=EXPECTED), \
             mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             mock.patch.object(chk, "list_hooks",
                               side_effect=unavailable, return_value=hooks), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # Explicit --snippet: the patched snippet_commands is used whichever account
            # runs the suite (the default path's existence differs per account).
            rc = chk.main(["--snippet", "/patched/config.toml.snippet"])
        return rc, out.getvalue().strip(), err.getvalue()

    def test_pass_prints_true(self):
        self.assertEqual(self._main(hooks=_live_seat())[:2], (0, "true"))

    def test_fail_prints_false_with_reasons_on_stderr(self):
        rc, out, err = self._main(hooks=_live_seat()[1:])
        self.assertEqual((rc, out), (0, "false"))
        self.assertIn("worker-guard", err)

    def test_explicit_unreadable_snippet_stays_unmeasurable(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(["--snippet", "/nonexistent/config.toml.snippet"])
        self.assertEqual((rc, out.getvalue().strip()), (chk.EXIT_UNMEASURABLE, ""))

    def test_real_unmeasurable_stderr_classifies_unmeasurable(self):
        # T-4160: the actual stderr of an unreadable snippet (errno text included) must
        # not trip parity_render's file_grep "required file absent" -> FAIL branch.
        from tools import parity_render as pr
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(["--snippet", "/nonexistent/config.toml.snippet"])
        verdict, _ = pr.classify_outcome(
            "file_grep", pr.ExecResult(rc=rc, stdout=out.getvalue(), stderr=err.getvalue()))
        self.assertEqual(verdict, "UNMEASURABLE", err.getvalue())
        self.assertIn("ENOENT", err.getvalue())

    def test_unexpected_crash_is_unmeasurable(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk, "_codex_bin", side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'x'")), \
             mock.patch.object(chk, "snippet_commands", return_value=EXPECTED), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(["--snippet", "/patched"])
        self.assertEqual((rc, out.getvalue()), (chk.EXIT_UNMEASURABLE, ""))
        self.assertNotIn("No such file", err.getvalue())

    def test_axis_check_ref_gates_on_the_contract_marker(self):
        # The axes.yaml check_ref refuses a seat-clone tool without CHECK_CONTRACT >= 2.
        import re
        src = Path(chk.__file__).read_text(encoding="utf-8")
        self.assertTrue(re.search(r"^CHECK_CONTRACT = [2-9]$", src, re.M))

    def test_unavailable_is_unmeasurable_not_false(self):
        rc, out, err = self._main(unavailable=chk.Unavailable("app-server gone"))
        self.assertEqual((rc, out), (chk.EXIT_UNMEASURABLE, ""))
        self.assertIn("app-server gone", err)

    def test_parity_classifies_each_outcome(self):
        from tools import parity_render as pr
        for rc, out, want in ((0, "true", "PASS"), (0, "false", "FAIL"),
                              (chk.EXIT_UNMEASURABLE, "", "UNMEASURABLE")):
            with self.subTest(want=want):
                verdict, _ = pr.classify_outcome(
                    "file_grep", pr.ExecResult(rc=rc, stdout=out, stderr='{"unmeasurable": "x"}'))
                self.assertEqual(verdict, want)


if __name__ == "__main__":
    unittest.main()
