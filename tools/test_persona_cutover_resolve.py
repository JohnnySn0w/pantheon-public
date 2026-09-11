"""Tests for tools/persona_cutover_resolve.py (T-2633).

Correctness gate (deliverable D): proves the GENERIC resolver reproduces the
constants tools/t2590_recovery_cutover.sh hardcoded for Recovery WITHOUT re-firing
Recovery, and separately yields Specialist's live-verified facts for the identical
call shape -- the "generic == known-good" proof the recovery-first/specialist-second
generalization needs (there is no prior test that pins either persona's
launch shape against the real checked-in instance.yaml -- see plan/DECISIONS).

Also covers the T-2633 team-lead closure #2: resolve_persona() must fail hard
on a target clone that loads cleanly via persona_bringup.load_binding() but
carries a STALE/pre-edit harness_runtime.roles.<role> stanza (model: null, no
channels/tmux/post_start) -- a clean load is NOT the same as "synced".
"""

from __future__ import annotations

import dataclasses
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from tools.persona_cutover_resolve import (
    CONTROL_PLANE_CHANNELS,
    ManifestPreconditionError,
    _default_manifest_path,
    parse_supervise_wrapper,
    resolve_bespoke_identity,
    resolve_persona,
)
from tools.persona_apply_path import controlled_apply_environment

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_YAML = REPO_ROOT / "instance.yaml"

# Ground-truth ExecStart lines. Read CROSS-ACCOUNT by team-lead (Orchestrator) and
# handed down verbatim (T-2633 sweep) -- this implementer cannot read either
# file directly: both live under 700-permission ~/.config/systemd/user/ dirs
# on accounts this seat has no access to (verified: `ls` returns Permission
# denied for both /home/claude-fallback/... and /home/claude-research/...).
# These are the two REAL, verified shapes, not hand-invented fixtures.
RECOVERY_UNIT_TEXT = textwrap.dedent(
    """\
    [Unit]
    Description=Recovery bespoke telegram bot supervisor

    [Service]
    Type=simple
    ExecStart=/home/claude-fallback/.local/bin/recovery-bot-supervise.sh
    ExecStop=/usr/bin/tmux kill-session -t claude-telegram-bot-fallback
    Restart=on-failure

    [Install]
    WantedBy=default.target
    """
)

SPECIALIST_UNIT_TEXT = textwrap.dedent(
    """\
    [Unit]
    Description=Specialist bespoke telegram bot supervisor

    [Service]
    Type=simple
    ExecStart=/home/claude-research/.local/bin/specialist-bot-supervise.sh
    ExecStop=/usr/bin/tmux kill-session -t claude-research-bot
    Restart=on-failure

    [Install]
    WantedBy=default.target
    """
)


class SupervisorWrapperParserTest(unittest.TestCase):
    """Pure-function tests: the parser never touches a real filesystem."""

    def test_bare_path_recovery_real_fixture(self) -> None:
        self.assertEqual(
            parse_supervise_wrapper(RECOVERY_UNIT_TEXT),
            "/home/claude-fallback/.local/bin/recovery-bot-supervise.sh",
        )

    def test_bare_path_specialist_real_fixture(self) -> None:
        self.assertEqual(
            parse_supervise_wrapper(SPECIALIST_UNIT_TEXT),
            "/home/claude-research/.local/bin/specialist-bot-supervise.sh",
        )

    def test_bash_c_wrapped_shape_synthetic(self) -> None:
        # Neither real unit uses this shape (both are bare-path per T-2633
        # sweep) -- kept as defense for a shape we haven't seen yet.
        text = 'ExecStart=/bin/bash -c "/home/x/.local/bin/foo-supervise.sh --flag"\n'
        self.assertEqual(parse_supervise_wrapper(text), "/home/x/.local/bin/foo-supervise.sh")

    def test_env_prefixed_shape_synthetic(self) -> None:
        text = "ExecStart=SOME_VAR=1 /home/x/.local/bin/foo-supervise.sh\n"
        self.assertEqual(parse_supervise_wrapper(text), "/home/x/.local/bin/foo-supervise.sh")

    def test_missing_execstart_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_supervise_wrapper("[Service]\nType=simple\n")

    def test_no_supervise_shaped_token_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_supervise_wrapper("ExecStart=/usr/bin/true\n")


class ResolvePersonaCorrectnessGateTest(unittest.TestCase):
    """Drives resolve_persona() against the REAL checked-in instance.yaml for
    BOTH recovery and specialist -- the generic==known-good proof (T-2633 deliverable
    D). `unit_text=` is the documented test seam on resolve_persona() (see
    its docstring) standing in for the cross-account file read this seat
    cannot perform; the content passed is the real, verified line, not an
    invention."""

    def test_recovery_matches_t2590_hardcoded_constants(self) -> None:
        identity = resolve_persona("recovery", manifest_path=INSTANCE_YAML, unit_text=RECOVERY_UNIT_TEXT)
        self.assertEqual(identity.role, "recovery")
        self.assertEqual(identity.account, "claude-fallback")
        self.assertEqual(identity.uid, 1001)
        self.assertEqual(identity.home, Path("/home/claude-fallback"))
        self.assertEqual(identity.xdg_runtime_dir, Path("/run/user/1001"))
        self.assertEqual(identity.tmux_session, "claude-telegram-bot-fallback")
        self.assertEqual(identity.service, "claude-telegram-fallback.service")
        self.assertEqual(
            identity.service_unit_path,
            Path("/home/claude-fallback/.config/systemd/user/claude-telegram-fallback.service"),
        )
        self.assertEqual(
            identity.settings_json_path, Path("/home/claude-fallback/.claude/settings.json")
        )
        self.assertEqual(
            identity.supervise_wrapper_path,
            Path("/home/claude-fallback/.local/bin/recovery-bot-supervise.sh"),
        )
        self.assertEqual(identity.model, "claude-opus-4-8")
        self.assertEqual(identity.channels, CONTROL_PLANE_CHANNELS)
        self.assertEqual(identity.consume_timer, "pantheon-persona-consume@recovery.timer")
        self.assertEqual(identity.consume_service, "pantheon-persona-consume@recovery.service")
        self.assertEqual(
            identity.apply_path,
            "/home/claude-fallback/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        )
        # T-2648: recovery has no declared companion units -- empty, not absent/error.
        self.assertEqual(identity.companion_units, "")

    def test_specialist_matches_live_verified_facts(self) -> None:
        identity = resolve_persona("specialist", manifest_path=INSTANCE_YAML, unit_text=SPECIALIST_UNIT_TEXT)
        self.assertEqual(identity.role, "specialist")
        self.assertEqual(identity.account, "claude-research")
        self.assertEqual(identity.uid, 1002)
        self.assertEqual(identity.home, Path("/home/claude-research"))
        self.assertEqual(identity.xdg_runtime_dir, Path("/run/user/1002"))
        self.assertEqual(identity.tmux_session, "claude-research-bot")
        self.assertEqual(identity.service, "claude-telegram-research.service")
        self.assertEqual(
            identity.service_unit_path,
            Path("/home/claude-research/.config/systemd/user/claude-telegram-research.service"),
        )
        self.assertEqual(
            identity.settings_json_path, Path("/home/claude-research/.claude/settings.json")
        )
        self.assertEqual(
            identity.supervise_wrapper_path,
            Path("/home/claude-research/.local/bin/specialist-bot-supervise.sh"),
        )
        self.assertEqual(identity.model, "claude-opus-4-8")
        self.assertEqual(identity.channels, CONTROL_PLANE_CHANNELS)
        self.assertEqual(identity.consume_timer, "pantheon-persona-consume@specialist.timer")
        self.assertEqual(identity.consume_service, "pantheon-persona-consume@specialist.service")
        self.assertEqual(
            identity.apply_path,
            "/home/claude-research/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        )
        # T-2648: specialist's acp-bridge shares the default tmux socket with its
        # bespoke TUI session (T-1689) -- the cutover must know to stop/restart
        # it. See BespokeIdentity.companion_units and its own field docstring
        # for the encoding.
        self.assertEqual(
            identity.companion_units, "pantheon-acp-bridge@specialist.service=acp-bridge-specialist"
        )


class ManifestPreconditionAssertionTest(unittest.TestCase):
    """T-2633 closure #2: persona_bringup.load_binding() alone can load a
    STALE/pre-edit target clone CLEANLY (model: null, no channels/tmux/
    post_start) -- resolve_persona() must fail hard on that gap, not merely
    on a yaml parse error, since that is exactly the "edit hasn't synced to
    the target clone yet" failure mode t2590's own precondition existed to
    catch."""

    @staticmethod
    def _fixture_manifest(tmp: Path, *, cut_over: bool) -> Path:
        specialist_shape: dict = (
            {
                "tmux": {"session": "claude-research-bot"},
                "post_start": [
                    {
                        "command": [
                            "{account_home}/pantheon-config/tools/arm_pane_log.sh",
                            "{tmux_target}",
                            "{persona}",
                        ]
                    },
                    {
                        "command": [
                            "{account_home}/pantheon-config/tools/recovery_boot_kick.sh",
                            "{tmux_target}",
                            "{persona}",
                        ],
                        "background": True,
                    },
                ],
                "claude": {
                    "profile": "interactive-subscription",
                    "model": "claude-opus-5",
                    "native": {
                        "permission_mode": "default",
                        "channels": CONTROL_PLANE_CHANNELS,
                    },
                },
            }
            if cut_over
            else {
                "claude": {
                    "profile": "interactive-subscription",
                    "model": None,
                    "native": {"permission_mode": "default"},
                }
            }
        )
        manifest = {
            "manifest_version": 2,
            "harness_runtime": {
                "schema": "pantheon-harness-runtime/v1",
                "controller": {
                    "tmux": {"session": "pantheon-{persona}", "window": "harness", "pane": "0"},
                    "quota": {"weekly_remaining_percent": 5},
                },
                "harnesses": {
                    "claude": {
                        "default_profile": "interactive-subscription",
                        "profiles": {
                            "interactive-subscription": {
                                "executable": "{account_home}/.npm-global/bin/claude",
                                "mode": "interactive",
                                "meter": "subscription",
                                "quota_source": "statusline",
                            }
                        },
                    }
                },
                "roles": {"specialist": specialist_shape},
            },
            "roles": {
                "specialist": {
                    "persona": "specialist",
                    "account": "claude-research",
                    "harness": "claude-code",
                }
            },
        }
        path = tmp / "instance.yaml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return path

    def test_stale_target_clone_fails_hard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._fixture_manifest(Path(tmp), cut_over=False)
            with self.assertRaises(ManifestPreconditionError):
                resolve_persona("specialist", manifest_path=manifest_path, unit_text=SPECIALIST_UNIT_TEXT)

    def test_stale_target_clone_error_names_every_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._fixture_manifest(Path(tmp), cut_over=False)
            with self.assertRaises(ManifestPreconditionError) as ctx:
                resolve_persona("specialist", manifest_path=manifest_path, unit_text=SPECIALIST_UNIT_TEXT)
            message = str(ctx.exception)
            for expected_fragment in ("channels", "model", "tmux", "arm_pane_log.sh", "recovery_boot_kick.sh"):
                self.assertIn(expected_fragment, message, message)

    def test_cut_over_target_clone_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._fixture_manifest(Path(tmp), cut_over=True)
            identity = resolve_persona("specialist", manifest_path=manifest_path, unit_text=SPECIALIST_UNIT_TEXT)
            self.assertEqual(identity.channels, CONTROL_PLANE_CHANNELS)
            self.assertEqual(identity.model, "claude-opus-5")
            # Bespoke pre-cutover session (from instance_binding's restart_pane,
            # sourced from the REAL repo instance.yaml since the fixture only
            # covers harness_runtime) must match the post-cutover
            # harness_runtime tmux.session read off THIS fixture -- proving the
            # cross-check the precondition performs, not just "is non-empty".
            self.assertEqual(identity.tmux_session, "claude-research-bot")

    def test_tmux_session_mismatch_between_bespoke_and_harness_runtime_fails(self) -> None:
        # A cut-over-shaped stanza whose tmux.session does NOT match the
        # persona's bespoke pre-cutover session is a real misconfiguration
        # (the control plane would create the WRONG pane, never inheriting
        # from the killed bespoke session) -- must fail, not merely "is set".
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._fixture_manifest(Path(tmp), cut_over=True)
            text = manifest_path.read_text(encoding="utf-8").replace(
                "claude-research-bot", "some-other-session"
            )
            manifest_path.write_text(text, encoding="utf-8")
            with self.assertRaises(ManifestPreconditionError):
                resolve_persona("specialist", manifest_path=manifest_path, unit_text=SPECIALIST_UNIT_TEXT)


class AsEnvBashSafetyTest(unittest.TestCase):
    """as_env()'s PCX_ prefix exists specifically to avoid colliding with
    bash's own special variables ($UID is readonly, $HOME is load-bearing
    for every subsequent ~-relative path) when persona_cutover.sh/
    persona_rollback.sh `eval` the resolver's env-format output. This is a
    structural, not incidental, safety property -- test it as one."""

    def test_no_bare_uid_or_home_keys(self) -> None:
        identity = resolve_persona("recovery", manifest_path=INSTANCE_YAML, unit_text=RECOVERY_UNIT_TEXT)
        env = identity.as_env()
        self.assertNotIn("UID", env)
        self.assertNotIn("HOME", env)
        self.assertIn("PCX_UID", env)
        self.assertIn("PCX_HOME", env)

    def test_env_blob_is_eval_safe_in_a_real_subshell(self) -> None:
        import subprocess

        identity = resolve_persona("recovery", manifest_path=INSTANCE_YAML, unit_text=RECOVERY_UNIT_TEXT)
        blob = "\n".join(f"{k}={v}" for k, v in identity.as_env().items())
        script = f'{blob}\necho "$PCX_UID:$PCX_HOME:$PCX_SERVICE"\n'
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        self.assertEqual(
            result.stdout.strip(),
            "1001:/home/claude-fallback:claude-telegram-fallback.service",
        )

    def test_cutover_and_rollback_export_resolver_dbus_address(self) -> None:
        for script in ("persona_cutover.sh", "persona_rollback.sh"):
            with self.subTest(script=script):
                source = (REPO_ROOT / "tools" / script).read_text(encoding="utf-8")
                self.assertIn('DBUS="$PCX_DBUS_SESSION_BUS_ADDRESS"', source)
                self.assertIn('export DBUS_SESSION_BUS_ADDRESS="$DBUS"', source)


class CompanionUnitsEncodingTest(unittest.TestCase):
    """T-2648: companion_units is a space-joined ``unit=session`` token string
    (not JSON) specifically so it stays safe inside the SAME unquoted
    ``KEY=value`` eval AsEnvBashSafetyTest guards. This proves the encoding
    round-trips through as_env()/eval AND that the exact split idiom
    persona_cutover.sh/persona_rollback.sh use (``${tok%%=*}`` /
    ``${tok#*=}`` over an unquoted ``for tok in $PCX_COMPANION_UNITS``)
    correctly recovers multiple entries -- specialist's real manifest only
    declares one today, so this is the only coverage of the multi-entry
    shape without inventing a live second companion unit."""

    def test_multi_entry_round_trips_through_eval_and_shell_split(self) -> None:
        import subprocess

        base = resolve_bespoke_identity("specialist", unit_text=SPECIALIST_UNIT_TEXT)
        identity = dataclasses.replace(
            base, companion_units="unitA=sessionA unitB=sessionB"
        )
        blob = "\n".join(f"{k}={v}" for k, v in identity.as_env().items())
        script = blob + "\n" + textwrap.dedent(
            """
            out=""
            for tok in $PCX_COMPANION_UNITS; do
              unit="${tok%%=*}"
              session="${tok#*=}"
              out="${out}${unit}:${session};"
            done
            echo "$out"
            """
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "unitA:sessionA;unitB:sessionB;")


class ResolveBespokeIdentityTest(unittest.TestCase):
    """tools/persona_rollback.sh's actual dependency surface -- must work
    with ZERO manifest access, matching tools/t2590_recovery_rollback.sh's own
    precedent (grep-verified: no MANIFEST reference anywhere in that
    script)."""

    def test_controlled_apply_environment_matches_cutover_resolver_for_arbitrary_uid_home(self) -> None:
        home = Path("/srv/pantheon/accounts/example-seat")
        passwd = SimpleNamespace(pw_uid=4242, pw_dir=str(home))
        with mock.patch("tools.persona_cutover_resolve.pwd.getpwnam", return_value=passwd):
            identity = resolve_bespoke_identity("recovery", unit_text=RECOVERY_UNIT_TEXT)
        expected = controlled_apply_environment(home, passwd.pw_uid)
        self.assertEqual(identity.home, home)
        self.assertEqual(identity.apply_path, expected["PATH"])
        self.assertEqual(identity.xdg_runtime_dir, Path(expected["XDG_RUNTIME_DIR"]))
        self.assertEqual(identity.dbus_session_bus_address, expected["DBUS_SESSION_BUS_ADDRESS"])
        self.assertEqual(
            identity.as_env()["PCX_DBUS_SESSION_BUS_ADDRESS"],
            expected["DBUS_SESSION_BUS_ADDRESS"],
        )

    def test_recovery_bespoke_identity_matches_hardcoded_constants(self) -> None:
        identity = resolve_bespoke_identity("recovery", unit_text=RECOVERY_UNIT_TEXT)
        self.assertEqual(identity.role, "recovery")
        self.assertEqual(identity.account, "claude-fallback")
        self.assertEqual(identity.uid, 1001)
        self.assertEqual(identity.home, Path("/home/claude-fallback"))
        self.assertEqual(identity.tmux_session, "claude-telegram-bot-fallback")
        self.assertEqual(identity.service, "claude-telegram-fallback.service")
        self.assertEqual(
            identity.supervise_wrapper_path,
            Path("/home/claude-fallback/.local/bin/recovery-bot-supervise.sh"),
        )
        self.assertEqual(identity.consume_timer, "pantheon-persona-consume@recovery.timer")
        self.assertEqual(identity.companion_units, "")

    def test_specialist_bespoke_identity_matches_live_verified_facts(self) -> None:
        identity = resolve_bespoke_identity("specialist", unit_text=SPECIALIST_UNIT_TEXT)
        self.assertEqual(identity.role, "specialist")
        self.assertEqual(identity.account, "claude-research")
        self.assertEqual(identity.uid, 1002)
        self.assertEqual(identity.home, Path("/home/claude-research"))
        self.assertEqual(identity.tmux_session, "claude-research-bot")
        self.assertEqual(identity.service, "claude-telegram-research.service")
        self.assertEqual(
            identity.supervise_wrapper_path,
            Path("/home/claude-research/.local/bin/specialist-bot-supervise.sh"),
        )
        self.assertEqual(identity.consume_timer, "pantheon-persona-consume@specialist.timer")
        self.assertEqual(
            identity.companion_units, "pantheon-acp-bridge@specialist.service=acp-bridge-specialist"
        )

    def test_never_calls_load_binding_or_touches_any_manifest(self) -> None:
        # Structural proof, not just "happens not to fail": patch load_binding
        # to raise if called at all, then confirm resolve_bespoke_identity
        # still succeeds -- demonstrating rollback's independence is real,
        # not merely untested.
        with mock.patch(
            "tools.persona_cutover_resolve.load_binding",
            side_effect=AssertionError("resolve_bespoke_identity must never call load_binding"),
        ):
            identity = resolve_bespoke_identity("specialist", unit_text=SPECIALIST_UNIT_TEXT)
        self.assertEqual(identity.account, "claude-research")


class DefaultManifestPathTest(unittest.TestCase):
    """The real generic-CLI invocation shape (no --manifest given) must
    default to the TARGET account's own clone, computed dynamically -- NOT a
    path baked into whichever checkout the resolver's own code happens to
    live in. Generalizes t2590_recovery_cutover.sh's hardcoded per-account
    MANIFEST default."""

    def test_default_manifest_path_is_under_target_home(self) -> None:
        self.assertEqual(
            _default_manifest_path(Path("/home/claude-research")),
            Path("/home/claude-research/pantheon-config/instance.yaml"),
        )

    def test_resolve_persona_with_no_manifest_uses_target_default(self) -> None:
        # This seat cannot read Recovery's own clone (cross-account, verified
        # Permission denied) -- the point of this test is that resolve_persona
        # computes and ATTEMPTS the correct default path (proven by the
        # resulting error naming that exact path), not that the read
        # succeeds from here.
        with self.assertRaises(OSError) as ctx:
            resolve_persona("recovery", unit_text=RECOVERY_UNIT_TEXT)
        self.assertIn(
            "/home/claude-fallback/pantheon-config/instance.yaml", str(ctx.exception)
        )


class CliUnreadableUnitFileTest(unittest.TestCase):
    """main()'s real invocation shape (no unit_text override) hits a real
    PermissionError when NOT run as the target account -- exactly this
    seat's own situation against specialist's/recovery's live unit files. Must
    surface a clean RESOLVE-FAIL line + exit 1, not a raw traceback."""

    def test_permission_denied_surfaces_clean_failure(self) -> None:
        import contextlib
        import io

        from tools import persona_cutover_resolve as resolver_module

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = resolver_module.main(
                ["resolve", "--persona", "specialist", "--manifest", str(INSTANCE_YAML)]
            )
        self.assertEqual(rc, 1)
        self.assertIn("RESOLVE-FAIL:", stderr.getvalue())
        self.assertIn("Permission denied", stderr.getvalue())

    def test_unit_file_flag_skips_real_cross_account_read(self) -> None:
        import contextlib
        import io

        from tools import persona_cutover_resolve as resolver_module

        with tempfile.TemporaryDirectory() as tmp:
            unit_file = Path(tmp) / "recovery.service"
            unit_file.write_text(RECOVERY_UNIT_TEXT, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = resolver_module.main(
                    ["bespoke-identity", "--persona", "recovery", "--unit-file", str(unit_file)]
                )
        self.assertEqual(rc, 0)
        self.assertIn(
            "PCX_SUPERVISE_WRAPPER_PATH=/home/claude-fallback/.local/bin/recovery-bot-supervise.sh",
            stdout.getvalue(),
        )

    def test_bespoke_identity_subcommand_same_clean_failure_shape(self) -> None:
        import contextlib
        import io

        from tools import persona_cutover_resolve as resolver_module

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = resolver_module.main(["bespoke-identity", "--persona", "recovery"])
        self.assertEqual(rc, 1)
        self.assertIn("RESOLVE-FAIL:", stderr.getvalue())
        self.assertIn("Permission denied", stderr.getvalue())
        # And, structurally, this failure came from the unit-file read, not
        # from any manifest access -- there is no manifest-shaped path
        # fragment in the message at all.
        self.assertNotIn("instance.yaml", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
