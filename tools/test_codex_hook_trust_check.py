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
from tools.harness_adapter import codex as codex_adapter

GUARD_CMD = "/usr/bin/python3 -I /home/codex/pantheon-config/tools/hooks/worker-guard-v2.py"
GUARD_V1_CMD = "/usr/bin/python3 /home/codex/pantheon-config/tools/hooks/worker-guard.py"
GUARD_V1_I_CMD = "/usr/bin/python3 -I /home/codex/pantheon-config/tools/hooks/worker-guard.py"
SHELLBOX_CMD = "/usr/bin/python3 -I /home/codex/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py"
START_CMD = "/usr/bin/python3 /home/codex/codex-telegram/run_codex_session_start_hook.py"
STOP_CMD = "/usr/bin/python3 /home/codex/codex-telegram/run_codex_stop_hook.py"


def _hook(event, command, *, trust="trusted", matcher=None, enabled=True, key=None):
    return {"key": key or f"/home/codex/.codex/config.toml:{event}:0:0", "eventName": event,
            "command": command, "matcher": matcher, "trustStatus": trust, "enabled": enabled,
            "timeoutSec": 5 if event in {"sessionStart", "stop"} else 30}


def _live_seat():
    return [
        _hook("preToolUse", GUARD_CMD, matcher="Bash|apply_patch",
              key="/home/codex/.codex/config.toml:pre_tool_use:0:0"),
        _hook("preToolUse", SHELLBOX_CMD, matcher="Bash",
              key="/home/codex/.codex/config.toml:pre_tool_use:1:0"),
        _hook("sessionStart", START_CMD, matcher="startup|resume|clear|compact"),
        _hook("stop", STOP_CMD),
    ]


EXPECTED = {START_CMD, STOP_CMD, SHELLBOX_CMD}


class Evaluate(unittest.TestCase):
    def test_live_seat_shape_passes(self):
        self.assertEqual(chk.evaluate(_live_seat(), EXPECTED), [])

    def test_missing_guard_fails(self):
        hooks = [h for h in _live_seat() if h["eventName"] != "preToolUse"]
        self.assertIn("no PreToolUse worker-guard hook configured", chk.evaluate(hooks, EXPECTED))

    def test_shell_box_alone_is_never_a_guard(self):
        hooks = [h for h in _live_seat() if h["command"] != GUARD_CMD]
        self.assertIn("no PreToolUse worker-guard hook configured", chk.evaluate(hooks, EXPECTED))

    def test_v1_only_guard_is_the_rollback_and_passes(self):
        for v1 in (GUARD_V1_CMD, GUARD_V1_I_CMD):
            with self.subTest(v1=v1):
                hooks = _live_seat()
                hooks[0] = {**hooks[0], "command": v1}
                self.assertEqual(chk.evaluate(hooks, EXPECTED), [])

    def test_v1_alongside_v2_is_not_a_guard_and_is_undeclared(self):
        hooks = _live_seat() + [_hook("preToolUse", GUARD_V1_CMD, matcher="Bash|apply_patch",
                                      key="/home/codex/.codex/config.toml:pre_tool_use:2:0")]
        problems = chk.evaluate(hooks, EXPECTED)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("hook not declared", problems[0])
        self.assertIn("worker-guard.py", problems[0])

    def test_extra_undeclared_pretooluse_hook_still_fails(self):
        hooks = _live_seat() + [_hook("preToolUse", "/usr/bin/python3 /home/codex/evil.py",
                                      key="/home/codex/.codex/config.toml:pre_tool_use:2:0")]
        problems = chk.evaluate(hooks, EXPECTED)
        self.assertTrue(any("evil.py" in p for p in problems), problems)

    def test_shell_box_not_declared_is_flagged(self):
        problems = chk.evaluate(_live_seat(), {START_CMD, STOP_CMD})
        self.assertTrue(any("pantheon-codex-shell-box-hook.py" in p for p in problems), problems)

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

    def test_claude_seat_fallback_declares_lifecycle_hooks_too(self):
        guard_box = [h for h in _live_seat() if h["eventName"] == "preToolUse"]
        self.assertEqual(chk.evaluate(guard_box, {SHELLBOX_CMD}), [])
        self.assertEqual(chk.evaluate(_live_seat(), EXPECTED), [])


class EvaluateLifecycle(unittest.TestCase):
    def test_trusted_legacy_pair_passes(self):
        self.assertEqual(chk.evaluate_lifecycle(_live_seat(), {START_CMD}, {STOP_CMD}), [])

    def test_missing_untrusted_or_stale_definition_fails(self):
        cases = (
            _live_seat()[:2],
            [{**h, "trustStatus": "untrusted"} if h["eventName"] == "sessionStart" else h
             for h in _live_seat()],
            [{**h, "timeoutSec": 60} if h["eventName"] == "stop" else h for h in _live_seat()],
            [{**h, "matcher": "startup"} if h["eventName"] == "sessionStart" else h
             for h in _live_seat()],
        )
        for hooks in cases:
            with self.subTest(hooks=hooks):
                self.assertTrue(chk.evaluate_lifecycle(hooks, {START_CMD}, {STOP_CMD}))

    def test_wrong_source_and_coexistence_are_not_filtered(self):
        home = Path("/tmp/t4990-checker-seat")
        repo_start = home / "pantheon-config/harness/codex/run_codex_session_start_hook.py"
        canonical = (
            f"/usr/bin/python3 {repo_start} --source "
            f"{home}/pantheon-config/pinned-rules/watchman.md"
        )
        wrong = (
            f"/usr/bin/python3 {repo_start} --source "
            f"{home}/pantheon-config/pinned-rules/codex.md"
        )
        malformed = f"/usr/bin/python3 {repo_start} --source \""
        quoted_malformed = f'/usr/bin/python3 "{repo_start}" --source "'
        extra = f"{canonical} --extra nope"
        stop = f"/usr/bin/python3 {home}/pantheon-config/harness/codex/run_codex_stop_hook.py"
        good = [_hook("sessionStart", canonical, matcher="startup|resume|clear|compact"),
                _hook("stop", stop)]
        cases = {
            "wrong only": [good[0] | {"command": wrong}, good[1]],
            "canonical plus wrong": good + [
                _hook("sessionStart", wrong, trust="untrusted",
                      matcher="startup|resume|clear|compact")
            ],
            "disabled wrong still coexists": good + [
                _hook("sessionStart", wrong, enabled=False,
                      matcher="startup|resume|clear|compact")
            ],
            "malformed exact entrypoint": [good[0] | {"command": malformed}, good[1]],
            "quoted malformed exact entrypoint": [
                good[0] | {"command": quoted_malformed}, good[1]
            ],
            "extra arguments": [good[0] | {"command": extra}, good[1]],
            "stop extra arguments": [good[0], good[1] | {"command": f"{stop} --extra nope"}],
        }
        for name, hooks in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    chk.evaluate_lifecycle(hooks, {canonical}, {stop}, home=home),
                    hooks,
                )

    def test_unmatched_opening_quote_entrypoints_are_not_filtered(self):
        home = Path("/tmp/t4990-checker-unmatched")
        repo_start = home / "pantheon-config/harness/codex/run_codex_session_start_hook.py"
        source = home / "pantheon-config/pinned-rules/watchman-codex.md"
        canonical = f"/usr/bin/python3 {repo_start} --source {source}"
        stop = f"/usr/bin/python3 {home}/pantheon-config/harness/codex/run_codex_stop_hook.py"
        for label, quote in (("single", "'"), ("double", '"')):
            with self.subTest(quote=label):
                malformed = f"/usr/bin/python3 {quote}{repo_start} --source {source}"
                problems = chk.evaluate_lifecycle(
                    [_hook("sessionStart", malformed,
                           matcher="startup|resume|clear|compact"),
                     _hook("stop", stop)],
                    {canonical}, {stop}, home=home,
                )
                self.assertTrue(any(
                    "sessionStart lifecycle hook uses a non-canonical" in problem
                    for problem in problems
                ), problems)

    def test_balanced_quotes_and_strict_entrypoint_boundaries(self):
        home = Path("/tmp/t4990-checker-boundaries")
        repo_start = home / "pantheon-config/harness/codex/run_codex_session_start_hook.py"
        source = home / "pantheon-config/pinned-rules/watchman-codex.md"
        canonical = f"/usr/bin/python3 {repo_start} --source {source}"
        for quote in ("'", '"'):
            balanced = f"/usr/bin/python3 {quote}{repo_start}{quote} --source {source}"
            self.assertTrue(
                codex_adapter.lifecycle_command_in_family(home, "SessionStart", balanced)
            )
        for unrelated in (
            f"/usr/bin/python3 {repo_start}.backup --source {source}",
            f"/usr/bin/python3 {repo_start.with_name(f'prefix-{repo_start.name}')} --source {source}",
        ):
            self.assertFalse(
                codex_adapter.lifecycle_command_in_family(home, "SessionStart", unrelated)
            )


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


class ShellBoxCommands(unittest.TestCase):
    def test_single_source_is_codex_shell_box(self):
        from tools.harness_adapter import codex_shell_box
        home = Path("/home/codex")
        self.assertEqual(chk.shell_box_commands(home), {codex_shell_box.hook_command(home)})
        self.assertEqual(chk.shell_box_commands(home), {SHELLBOX_CMD})

    def test_import_failure_is_unavailable(self):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name.endswith("codex_shell_box") or name == "tools.harness_adapter":
                raise ImportError("boom")
            return real(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=fake), \
             self.assertRaises(chk.Unavailable):
            chk.shell_box_commands(Path("/home/codex"))


class LifecycleCommands(unittest.TestCase):
    def test_persona_canonical_source_matches_installer_for_artisan(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "suborch2"
            source = home / "pantheon-config/pinned-rules/artisan.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Artisan\n")
            starts, stops = chk.lifecycle_commands(home, "suborch2")
            self.assertEqual(starts, {
                f"/usr/bin/python3 {home}/pantheon-config/harness/codex/"
                f"run_codex_session_start_hook.py --source {source}"
            })
            self.assertEqual(stops, {
                f"/usr/bin/python3 {home}/pantheon-config/harness/codex/run_codex_stop_hook.py"
            })

    def test_consultant_source_alone_is_unavailable_for_artisan(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "suborch2"
            source = home / "pantheon-config/pinned-rules/codex.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Consultant\n")
            with self.assertRaises(chk.Unavailable):
                chk.lifecycle_commands(home, "suborch2")

    def test_legacy_pair_is_consultant_only_and_requires_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            shared = home / "pantheon-config/pinned-rules/codex.md"
            shared.parent.mkdir(parents=True)
            shared.write_text("# codex\n")
            legacy = home / "codex-telegram"
            legacy.mkdir()
            (legacy / "run_codex_session_start_hook.py").write_text("# start\n")
            consultant_starts, _ = chk.lifecycle_commands(home, "codex")
            self.assertFalse(any("/codex-telegram/" in command for command in consultant_starts))
            (legacy / "run_codex_stop_hook.py").write_text("# stop\n")
            consultant_starts, consultant_stops = chk.lifecycle_commands(home, "codex")
            self.assertTrue(any("/codex-telegram/" in command for command in consultant_starts))
            self.assertTrue(any("/codex-telegram/" in command for command in consultant_stops))
            (shared.parent / "specialist-codex.md").write_text("# Specialist\n")
            with mock.patch("tools.agent_role_map.account_persona", return_value="specialist"):
                specialist_starts, specialist_stops = chk.lifecycle_commands(home, "claude-research")
            self.assertFalse(any("/codex-telegram/" in command for command in specialist_starts | specialist_stops))


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

    def test_lifecycle_axis_exactly_covers_every_primary_or_fallback_codex_role(self):
        import yaml
        repo = Path(chk.__file__).resolve().parents[1]
        axes = yaml.safe_load((repo / "sop/standard/seat-parity/axes.yaml").read_text())
        axis = next(a for a in axes["axes"] if a["axis_id"] == "codex-lifecycle-hooks-trusted")
        instance = yaml.safe_load(chk.DEFAULT_INSTANCE.read_text())
        default = instance["instance"]["harness_default"]
        runtime_roles = instance["harness_runtime"]["roles"]
        expected = {
            role for role, binding in instance["roles"].items()
            if binding.get("harness", default) == "codex"
            or any(str(item).split("/", 1)[0] == "codex"
                   for item in (runtime_roles.get(role, {}).get("fallbacks") or []))
        }
        self.assertEqual(set(axis["applies_to"]), expected)

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
        # The worker-guard command intentionally differs from the pin now (v2 + -I,
        # T-6122; the pin names v1), so compare the sets with guard commands excluded.
        def non_guard(cmds):
            return {c for c in cmds if "/tools/hooks/worker-guard" not in c}
        self.assertNotEqual(non_guard(chk.snippet_commands(chk.DEFAULT_SNIPPET, home)),
                            non_guard(chk.snippet_commands(pinned, home)))
        self.assertIn(GUARD_CMD, chk.snippet_commands(chk.DEFAULT_SNIPPET, home))
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
        shared = self.home / "pantheon-config" / "pinned-rules" / "codex.md"
        shared.parent.mkdir(parents=True)
        shared.write_text("# shared\n")
        (shared.parent / "orchestrator-codex.md").write_text("# Orchestrator\n")
        legacy = self.home / "codex-telegram"
        legacy.mkdir()
        (legacy / "run_codex_session_start_hook.py").write_text("# start\n")
        (legacy / "run_codex_stop_hook.py").write_text("# stop\n")
        self.inst = _instance(self.tmp, {"peer-daemon-codex": ("codex", "codex"),
                                         "head-engineering-orchestrator": ("claude", "claude-code")})

    def _seat(self, *, legacy=True):
        """The live codex-seat hook shape, bound to the fake home."""
        h = self.home
        return [
            _hook("preToolUse", f"/usr/bin/python3 -I {h}/pantheon-config/tools/hooks/worker-guard-v2.py",
                  matcher="Bash|apply_patch", key=f"{h}/.codex/config.toml:pre_tool_use:0:0"),
            _hook("preToolUse", f"/usr/bin/python3 -I {h}/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py",
                  matcher="Bash", key=f"{h}/.codex/config.toml:pre_tool_use:1:0"),
            _hook("sessionStart", (f"/usr/bin/python3 {h}/codex-telegram/run_codex_session_start_hook.py"
                                   if legacy else f"/usr/bin/python3 {h}/pantheon-config/harness/codex/"
                                   f"run_codex_session_start_hook.py --source {h}/pantheon-config/pinned-rules/orchestrator-codex.md"),
                  matcher="startup|resume|clear|compact"),
            _hook("stop", (f"/usr/bin/python3 {h}/codex-telegram/run_codex_stop_hook.py"
                           if legacy else f"/usr/bin/python3 {h}/pantheon-config/harness/codex/"
                           "run_codex_stop_hook.py")),
        ]

    def _run(self, account, hooks, *, snippet=None, unavailable=None, instance=None,
             lifecycle_only=False):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--instance", str(instance or self.inst)]
        if lifecycle_only:
            argv.append("--lifecycle-only")
        with mock.patch.object(chk.Path, "home", return_value=self.home), \
             mock.patch.object(chk, "_account", return_value=account), \
             mock.patch.object(chk, "DEFAULT_SNIPPET", snippet or chk.DEFAULT_SNIPPET), \
             mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             mock.patch.object(chk, "list_hooks", side_effect=unavailable, return_value=hooks), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(argv)
        return rc, out.getvalue().strip(), err.getvalue()

    def _verdict(self, rc, out, err):
        from tools import parity_render as pr
        return pr.classify_outcome("file_grep", pr.ExecResult(rc=rc, stdout=out, stderr=err))[0]

    def test_live_consultant_shape_passes_with_no_ticket_failure_lines(self):
        # T-6122: v2 -I guard + shell-box + start/stop, the real in-repo snippet.
        rc, out, err = self._run("codex", self._seat())
        self.assertEqual((rc, out), (0, "true"), err)
        for line in ("no PreToolUse worker-guard hook configured", "hook not declared"):
            self.assertNotIn(line, err)

    def test_codex_primary_seat_declares_the_in_repo_snippet(self):
        # Live codex seat shape (guard + codex-telegram start/stop hooks) passes.
        self.assertEqual(self._run("codex", self._seat())[:2], (0, "true"))
        self.assertEqual(self._run("codex", self._seat()[:2])[:2], (0, "false"))
        # ~/codex-config's snippet is no longer consulted: its hook is a stray now.
        rc, out, err = self._run("codex", self._seat() + [_hook("stop", "/tmp/stale.py")])
        self.assertEqual((rc, out), (0, "false"))
        self.assertIn("/tmp/stale.py", err)
        self.assertEqual(self._verdict(rc, out, err), "FAIL")

    def test_claude_seat_codex_fallback_requires_lifecycle_pair(self):
        self.assertEqual(self._run("claude", self._seat(legacy=False)[:2])[:2], (0, "false"))
        rc, out, err = self._run("claude", self._seat(legacy=False))
        self.assertEqual((rc, out), (0, "true"), err)
        self.assertEqual(self._run("claude", [])[:2], (0, "false"))  # no guard at all

    def test_lifecycle_only_rejects_wrong_source_coexistence(self):
        hooks = self._seat(legacy=False)
        wrong = (
            f"/usr/bin/python3 {self.home}/pantheon-config/harness/codex/"
            f"run_codex_session_start_hook.py --source "
            f"{self.home}/pantheon-config/pinned-rules/codex.md"
        )
        hooks.append(_hook(
            "sessionStart", wrong, trust="untrusted",
            matcher="startup|resume|clear|compact",
            key=f"{self.home}/.codex/config.toml:session_start:1:0",
        ))
        rc, out, err = self._run("claude", hooks, lifecycle_only=True)
        self.assertEqual((rc, out), (0, "false"), err)
        self.assertIn("expected exactly one sessionStart lifecycle hook, found 2", err)

    def test_lifecycle_only_rejects_unmatched_opening_quote_coexistence(self):
        repo_start = (
            self.home / "pantheon-config/harness/codex/run_codex_session_start_hook.py"
        )
        source = self.home / "pantheon-config/pinned-rules/orchestrator-codex.md"
        for label, quote in (("single", "'"), ("double", '"')):
            with self.subTest(quote=label):
                hooks = self._seat(legacy=False)
                malformed = f"/usr/bin/python3 {quote}{repo_start} --source {source}"
                hooks.append(_hook(
                    "sessionStart", malformed, trust="untrusted",
                    matcher="startup|resume|clear|compact",
                    key=f"{self.home}/.codex/config.toml:session_start:1:0",
                ))
                rc, out, err = self._run("claude", hooks, lifecycle_only=True)
                self.assertEqual((rc, out), (0, "false"), err)
                self.assertIn(
                    "expected exactly one sessionStart lifecycle hook, found 2", err,
                )

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
        rc, out, _ = self._run("claude", self._seat(legacy=False),
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
        with mock.patch.object(chk.Path, "home", return_value=Path("/home/codex")), \
             mock.patch.object(chk, "snippet_commands", return_value=EXPECTED), \
             mock.patch.object(chk, "shell_box_commands", return_value={SHELLBOX_CMD}), \
             mock.patch.object(chk, "lifecycle_commands", return_value=({START_CMD}, {STOP_CMD})), \
             mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             mock.patch.object(chk, "list_hooks",
                               side_effect=unavailable, return_value=hooks), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # Explicit --snippet: the patched snippet_commands is used whichever account
            # runs the suite (the default path's existence differs per account).
            rc = chk.main(["--snippet", "/patched/config.toml.snippet"])
        return rc, out.getvalue().strip(), err.getvalue()

    def test_shell_box_import_failure_is_unmeasurable(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chk, "shell_box_commands", side_effect=chk.Unavailable("no shell box")), \
             mock.patch.object(chk, "snippet_commands", return_value=EXPECTED), \
             mock.patch.object(chk, "lifecycle_commands", return_value=({START_CMD}, {STOP_CMD})), \
             mock.patch.object(chk, "_codex_bin", return_value="codex"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main(["--snippet", "/patched"])
        self.assertEqual((rc, out.getvalue()), (chk.EXIT_UNMEASURABLE, ""))
        self.assertIn("no shell box", err.getvalue())

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
             mock.patch.object(chk, "lifecycle_commands", return_value=({START_CMD}, {STOP_CMD})), \
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


class HighFdCeiling(unittest.TestCase):
    """T-5872: list_hooks must work when the app-server pipe fd is >= 1024."""

    def test_list_hooks_with_pipe_fd_above_1024(self) -> None:
        import os
        import stat
        import sys

        from tools._test_fixtures import fds_above_select_ceiling
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import sys, json\n"
                "for line in sys.stdin:\n"
                "    m = json.loads(line)\n"
                "    if m.get('id') == 1:\n"
                "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
                "    elif m.get('id') == 2:\n"
                "        print(json.dumps({'id': 2, 'result': {'data': []}}), flush=True)\n"
                "        break\n"
            )
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            with fds_above_select_ceiling():
                self.assertEqual(chk.list_hooks(codex_bin=str(fake), cwd=Path(tmp), timeout=10), [])


if __name__ == "__main__":
    unittest.main()
