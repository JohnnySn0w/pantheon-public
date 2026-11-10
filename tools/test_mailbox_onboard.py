"""Tests for tools/mailbox_onboard.py (T-2683).

Fixture shape mirrors tools/test_consume_onboard.py exactly: real unit
templates read from the REAL repo checkout (tools/systemd/pantheon-mailbox-
wake@.{service,timer}, static, already-reviewed), but every filesystem
mutation lands under a per-test tempdir -- nothing here ever touches this
host's real /home, /etc/{passwd,group}, systemd --user, or tmux. The
CommandRunner and HostIdentity seams are always fake-injected.

The manifest-driven-N tests are the load-bearing proof for the "never
hardcode 8" requirement: they build a fixture instance.yaml with a variable-
length `roles:` mapping and assert the persona count tracked by
mailbox_onboard.provision()/verify() tracks the manifest, not a constant.
"""

from __future__ import annotations

import dataclasses
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import tools.mailbox_onboard as mo

REPO_ROOT = Path(__file__).resolve().parent.parent


def _manifest_dict(
    mailbox_hub: str,
    personas: tuple[tuple[str, str], ...],
    hub_owner: str | None = None,
    group: str = "pantheon-mailbox",
    wake_timer_personas: tuple[str, ...] | None = None,
    wake_timer_secondary_personas: tuple[str, ...] | None = None,
) -> dict:
    mailbox: dict = {"hub": mailbox_hub, "group": group, "probe_peer": "orchestrator"}
    if hub_owner is not None:
        mailbox["hub_owner"] = hub_owner
    if wake_timer_personas is not None:
        mailbox["wake_timer_personas"] = list(wake_timer_personas)
    if wake_timer_secondary_personas is not None:
        mailbox["wake_timer_secondary_personas"] = list(wake_timer_secondary_personas)
    roles = {}
    for index, (persona, account) in enumerate(personas):
        roles[f"role-{index}"] = {"persona": persona, "account": account, "harness": "claude-code"}
    return {
        "manifest_version": 2,
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test-rev"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": mailbox,
            "systemd": {
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
            },
            "policy": {"bundle": ["rulebook/", "sop/", "adr/"]},
            "security": {"credential_mode": "references-only", "prohibit_auth_copy": True},
        },
        "instance": {"id": "test-instance"},
        "roles": roles,
    }


DEFAULT_PERSONAS = (("alice", "acct-alice"), ("bob", "acct-bob"))


class MailboxOnboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.mailbox_hub = self.tmp_path / "mailbox"
        self.manifest_path = self.tmp_path / "instance.yaml"

    def _write_manifest(
        self,
        personas: tuple[tuple[str, str], ...] = DEFAULT_PERSONAS,
        hub_owner: str | None = None,
        group: str = "pantheon-mailbox",
        wake_timer_personas: tuple[str, ...] | None = None,
        wake_timer_secondary_personas: tuple[str, ...] | None = None,
    ) -> None:
        doc = _manifest_dict(
            str(self.mailbox_hub),
            personas,
            hub_owner=hub_owner,
            group=group,
            wake_timer_personas=wake_timer_personas,
            wake_timer_secondary_personas=wake_timer_secondary_personas,
        )
        self.manifest_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    def _load_ctx(self, **kwargs) -> mo.MailboxContext:
        self._write_manifest(**kwargs)
        return mo.load_mailbox_context(self.manifest_path, REPO_ROOT)

    def _make_hub_dir(self, persona: str) -> None:
        for sub in ("new", "cur", "tmp"):
            (self.mailbox_hub / persona / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# load_mailbox_context() / hub_owner resolution
# ---------------------------------------------------------------------------


class LoadMailboxContextTests(MailboxOnboardTestCase):
    def test_hub_owner_field_present_is_used(self) -> None:
        ctx = self._load_ctx(hub_owner="the-ancestor")
        self.assertEqual(ctx.hub_owner, "the-ancestor")

    def test_hub_owner_field_absent_defaults_to_post_office(self) -> None:
        ctx = self._load_ctx(hub_owner=None)
        self.assertEqual(ctx.hub_owner, mo.DEFAULT_HUB_OWNER)
        self.assertEqual(ctx.hub_owner, "post-office")

    def test_missing_mailbox_section_raises(self) -> None:
        doc = _manifest_dict(str(self.mailbox_hub), DEFAULT_PERSONAS)
        del doc["persona_installer"]["mailbox"]
        self.manifest_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with self.assertRaises(mo.OnboardError):
            mo.load_mailbox_context(self.manifest_path, REPO_ROOT)

    def test_group_missing_raises(self) -> None:
        doc = _manifest_dict(str(self.mailbox_hub), DEFAULT_PERSONAS)
        del doc["persona_installer"]["mailbox"]["group"]
        self.manifest_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with self.assertRaises(mo.OnboardError):
            mo.load_mailbox_context(self.manifest_path, REPO_ROOT)

    def test_templates_resolve_under_repo_root(self) -> None:
        ctx = self._load_ctx()
        self.assertTrue(ctx.service_template.is_file())
        self.assertTrue(ctx.timer_template.is_file())


# ---------------------------------------------------------------------------
# _all_persona_bindings() -- the manifest-driven-N proof
# ---------------------------------------------------------------------------


class AllPersonaBindingsTests(MailboxOnboardTestCase):
    def test_three_persona_manifest_yields_three_bindings(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "a1"), ("bob", "a2"), ("cara", "a3")))
        bindings = mo._all_persona_bindings(ctx.manifest)
        self.assertEqual({b.persona for b in bindings}, {"alice", "bob", "cara"})
        self.assertEqual(len(bindings), 3)

    def test_adding_a_fourth_persona_changes_the_count(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "a1"), ("bob", "a2"), ("cara", "a3"), ("dev", "a4")))
        bindings = mo._all_persona_bindings(ctx.manifest)
        self.assertEqual(len(bindings), 4)
        self.assertIn("dev", {b.persona for b in bindings})


# ---------------------------------------------------------------------------
# hub_setup()
# ---------------------------------------------------------------------------


class HubSetupTests(MailboxOnboardTestCase):
    def test_dry_run_makes_no_calls_and_no_mutations(self) -> None:
        ctx = self._load_ctx()
        runner = mo.FakeCommandRunner()
        identity = mo.FakeHostIdentity()
        report = mo.hub_setup(ctx, runner, identity, execute=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(runner.calls, [])
        self.assertFalse(ctx.mailbox_hub.exists())
        self.assertTrue(report.planned)

    def test_dry_run_reports_missing_template_without_raising(self) -> None:
        # dataclasses.replace() rather than reconstructing every field --
        # keeps this fixture from needing a new keyword every time
        # MailboxContext grows a field (T-2702 added wake_timer_personas).
        ctx = self._load_ctx()
        broken = dataclasses.replace(
            ctx,
            service_template=self.tmp_path / "nope.service",
            timer_template=self.tmp_path / "nope.timer",
        )
        report = mo.hub_setup(broken, mo.FakeCommandRunner(), mo.FakeHostIdentity(), execute=False)
        self.assertTrue(report.dry_run)
        self.assertTrue(any(not c.ok for c in report.template_checks))

    def test_execute_with_missing_template_raises(self) -> None:
        ctx = self._load_ctx()
        broken = dataclasses.replace(
            ctx,
            service_template=self.tmp_path / "nope.service",
            timer_template=self.tmp_path / "nope.timer",
        )
        with self.assertRaises(mo.OnboardError):
            mo.hub_setup(broken, mo.FakeCommandRunner(), mo.FakeHostIdentity(), execute=True)

    def test_execute_creates_group_user_hub_dir_and_linger(self) -> None:
        ctx = self._load_ctx(hub_owner="the-ancestor")
        runner = mo.FakeCommandRunner()
        identity = mo.FakeHostIdentity(users=(), groups=())
        report = mo.hub_setup(ctx, runner, identity, execute=True)
        self.assertFalse(report.dry_run)
        self.assertIn(("groupadd", "pantheon-mailbox"), runner.calls)
        self.assertIn(("useradd", "-m", "-G", "pantheon-mailbox", "the-ancestor"), runner.calls)
        self.assertIn(("loginctl", "enable-linger", "the-ancestor"), runner.calls)
        self.assertTrue(ctx.mailbox_hub.is_dir())

    def test_owner_home_made_group_traversable_like_the_ancestor(self) -> None:
        """T-4096/T-2693 prep: `useradd -m` leaves the new hub-owner's home 0750
        owner:owner, so no mailbox-group member (every seat) could traverse into the
        hub after a move. hub-setup must leave the owner's tree at the live
        the-ancestor shape: group = mailbox group, group bits traverse-only (0710)."""
        import getpass
        import grp as _grp
        home = self.tmp_path / "po-home"
        home.mkdir(mode=0o750)
        os.chmod(home, 0o750)
        ctx = dataclasses.replace(
            self._load_ctx(hub_owner=getpass.getuser()),
            mailbox_hub=home / "mailbox",
            mailbox_group=_grp.getgrgid(os.getgid()).gr_name,   # a group we may chgrp to
        )
        report = mo.hub_setup(ctx, mo.FakeCommandRunner(),
                              mo.FakeHostIdentity(users=(getpass.getuser(),), groups=(ctx.mailbox_group,)),
                              execute=True)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o710)
        self.assertTrue(any("group-traversable" in c for c in report.changed))

    def test_group_traverse_stops_at_a_dir_the_owner_does_not_own(self) -> None:
        home = self.tmp_path / "po-home"
        hub = home / "mailbox"
        hub.mkdir(parents=True)
        os.chmod(home, 0o750)
        # An owner uid that owns none of these dirs: nothing is touched.
        changed = mo._ensure_group_traverse(hub, os.getuid() + 1, "nogroup")
        self.assertEqual(changed, [])
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o750)

    def test_group_traverse_never_climbs_past_the_owner_tree(self) -> None:
        # /tmp (root-owned) bounds the walk: it must be left exactly as it was.
        hub = self.tmp_path / "po-home" / "mailbox"
        hub.mkdir(parents=True)
        tmp_root_mode = stat.S_IMODE(Path("/tmp").stat().st_mode)
        mo._ensure_group_traverse(hub, os.getuid(), "nogroup")
        self.assertEqual(stat.S_IMODE(Path("/tmp").stat().st_mode), tmp_root_mode)

    def test_second_execute_is_idempotent_no_redundant_group_or_user_calls(self) -> None:
        ctx = self._load_ctx(hub_owner="the-ancestor")
        identity_already_present = mo.FakeHostIdentity(users=("the-ancestor",), groups=("pantheon-mailbox",))
        runner = mo.FakeCommandRunner()
        mo.hub_setup(ctx, runner, identity_already_present, execute=True)
        self.assertFalse(any(call[0] in ("groupadd", "useradd") for call in runner.calls))
        self.assertIn(("loginctl", "enable-linger", "the-ancestor"), runner.calls)
        self.assertTrue(ctx.mailbox_hub.is_dir())


# ---------------------------------------------------------------------------
# provision()
# ---------------------------------------------------------------------------


class ProvisionDryRunTests(MailboxOnboardTestCase):
    def test_dry_run_makes_no_mutations_and_no_runner_calls(self) -> None:
        # Both DEFAULT_PERSONAS allowlisted here -- this test's scope is
        # dry-run safety (no mutation, no runner calls, planned-line shape),
        # not the T-2702 allowlist logic, which has its own dedicated tests.
        ctx = self._load_ctx(wake_timer_personas=("alice", "bob"))
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        report = mo.provision(ctx, bindings, runner, execute=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(runner.calls, [])
        self.assertFalse(ctx.mailbox_hub.exists())
        self.assertTrue(all(r.hub_leg.startswith("planned") for r in report.personas))
        self.assertTrue(all(r.unit_leg.startswith("planned") for r in report.personas))


class ProvisionManifestDrivenNTests(MailboxOnboardTestCase):
    def test_three_personas_yields_three_planned_results(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "a1"), ("bob", "a2"), ("cara", "a3")))
        bindings = mo._all_persona_bindings(ctx.manifest)
        report = mo.provision(ctx, bindings, mo.FakeCommandRunner(), execute=False)
        self.assertEqual(len(report.personas), 3)

    def test_adding_a_fourth_persona_changes_the_planned_count(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "a1"), ("bob", "a2"), ("cara", "a3"), ("dev", "a4")))
        bindings = mo._all_persona_bindings(ctx.manifest)
        report = mo.provision(ctx, bindings, mo.FakeCommandRunner(), execute=False)
        self.assertEqual(len(report.personas), 4)
        self.assertIn("dev", {r.persona for r in report.personas})


class ProvisionHubLegTests(MailboxOnboardTestCase):
    def test_running_as_hub_owner_provisions_maildirs_and_skips_unit_leg(self) -> None:
        ctx = self._load_ctx(hub_owner="the-ancestor")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        report = mo.provision(ctx, bindings, runner, execute=True, current_account="the-ancestor")
        for result in report.personas:
            self.assertEqual(result.hub_leg, "done")
            self.assertTrue(result.unit_leg.startswith("skipped"))
            for sub in ("tmp", "new", "cur"):
                self.assertTrue((self.mailbox_hub / result.persona / sub).is_dir())
        self.assertEqual(runner.calls, [])  # no systemctl calls on the hub-owner-only leg


class ProvisionUnitLegTests(MailboxOnboardTestCase):
    def test_running_as_persona_account_installs_and_enables_only_that_persona(self) -> None:
        # alice allowlisted, bob not -- this test's scope is the unit-leg
        # install mechanics (write, daemon-reload, enable) for an already-
        # allowlisted persona; the allowlist gate itself has dedicated tests
        # below (ProvisionAllowlistTests).
        ctx = self._load_ctx(hub_owner="the-ancestor", wake_timer_personas=("alice",))
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        home = self.tmp_path / "home-alice"
        report = mo.provision(
            ctx, bindings, runner, execute=True, current_account="acct-alice", home=home
        )
        by_persona = {r.persona: r for r in report.personas}
        self.assertEqual(by_persona["alice"].unit_leg, "done")
        self.assertTrue(by_persona["bob"].unit_leg.startswith("skipped"))
        self.assertTrue(by_persona["alice"].hub_leg.startswith("skipped"))

        service_path = home / ".config" / "systemd" / "user" / "pantheon-mailbox-wake@.service"
        timer_path = home / ".config" / "systemd" / "user" / "pantheon-mailbox-wake@.timer"
        self.assertTrue(service_path.is_file())
        self.assertTrue(timer_path.is_file())
        content = service_path.read_text(encoding="utf-8")
        self.assertIn("# Managed by pantheon persona installer", content)
        self.assertIn("persona=alice", content)
        self.assertIn(("systemctl", "--user", "daemon-reload"), runner.calls)
        self.assertIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@alice.timer"), runner.calls
        )
        # bob's account never ran here -- no bob-scoped systemctl call at all
        self.assertNotIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@bob.timer"), runner.calls
        )

    def test_second_execute_is_idempotent_no_redundant_writes(self) -> None:
        ctx = self._load_ctx(hub_owner="the-ancestor", wake_timer_personas=("alice",))
        bindings = mo._all_persona_bindings(ctx.manifest)
        home = self.tmp_path / "home-alice"
        mo.provision(ctx, bindings, mo.FakeCommandRunner(), execute=True, current_account="acct-alice", home=home)

        runner2 = mo.FakeCommandRunner()
        report2 = mo.provision(
            ctx, bindings, runner2, execute=True, current_account="acct-alice", home=home
        )
        alice = next(r for r in report2.personas if r.persona == "alice")
        self.assertFalse(any(item.startswith("wrote ") for item in alice.changed))
        # daemon-reload + enable --now still run every time (cheap, idempotent commands)
        self.assertIn(("systemctl", "--user", "daemon-reload"), runner2.calls)


# ---------------------------------------------------------------------------
# T-2702: wake_timer_personas allowlist gating on provision()'s unit leg.
# The load-bearing proof that the installer stopped indiscriminately enabling
# the standalone wake timer for every manifest persona.
# ---------------------------------------------------------------------------


class ProvisionAllowlistTests(MailboxOnboardTestCase):
    def test_enables_only_allowlisted_personas(self) -> None:
        personas = (("orchestrator", "acct-orchestrator"), ("codex", "acct-codex"), ("specialist", "acct-specialist"))
        ctx = self._load_ctx(personas=personas, hub_owner="the-ancestor", wake_timer_personas=("orchestrator", "codex"))
        bindings = mo._all_persona_bindings(ctx.manifest)

        for persona, account in personas:
            runner = mo.FakeCommandRunner()
            home = self.tmp_path / f"home-{persona}"
            mo.provision(ctx, bindings, runner, execute=True, current_account=account, home=home)
            enable_call = ("systemctl", "--user", "enable", "--now", f"pantheon-mailbox-wake@{persona}.timer")
            if persona in ("orchestrator", "codex"):
                self.assertIn(enable_call, runner.calls, f"{persona} should be enabled")
            else:
                self.assertNotIn(enable_call, runner.calls, f"{persona} should NOT be enabled")

    def test_never_enables_archivist_the_safety_driver_case(self) -> None:
        # T-2702 safety driver: archivist's mailbox_wake.AGENT_PANE points at
        # the operator's own chat tmux target (claude-telegram-bot-exocortex:0). A
        # standalone wake timer poking her would send-keys into the operator's own
        # conversation and guarantee a unit failure (exit 64, unknown agent).
        # This is the case the whole ticket exists to prevent -- named and
        # asserted explicitly, not folded into a generic allowlist test, so
        # the WHY survives in the suite.
        ctx = self._load_ctx(
            personas=(("archivist", "acct-archivist"),), hub_owner="the-ancestor", wake_timer_personas=()
        )
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        home = self.tmp_path / "home-archivist"
        report = mo.provision(ctx, bindings, runner, execute=True, current_account="acct-archivist", home=home)

        self.assertNotIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@archivist.timer"), runner.calls
        )
        archivist_result = next(r for r in report.personas if r.persona == "archivist")
        self.assertTrue(archivist_result.unit_leg.startswith("skipped"))
        service_path = home / ".config" / "systemd" / "user" / f"{mo.WAKE_UNIT_STEM}@.service"
        self.assertFalse(service_path.exists())

    def test_skips_consume_plane_persona(self) -> None:
        # Consume-plane seats (specialist/recovery/mason/artisan) share archivist's
        # "excluded" bucket but for a different reason: they're woken by the
        # consume runtime, not by absence of a wake path at all. mason stands
        # in for the class.
        personas = (("orchestrator", "acct-orchestrator"), ("mason", "acct-mason"))
        ctx = self._load_ctx(personas=personas, hub_owner="the-ancestor", wake_timer_personas=("orchestrator",))
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        home = self.tmp_path / "home-mason"
        report = mo.provision(ctx, bindings, runner, execute=True, current_account="acct-mason", home=home)

        self.assertNotIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@mason.timer"), runner.calls
        )
        mason_result = next(r for r in report.personas if r.persona == "mason")
        self.assertTrue(mason_result.unit_leg.startswith("skipped"))

    def test_default_absent_wake_timer_personas_enables_nobody(self) -> None:
        # Default-deny: a fresh seed with no `wake_timer_personas` key at all
        # (not even an empty list) must not blanket-enable for every persona
        # -- that silent fallback would just relocate the T-2702 bug.
        personas = (("alice", "acct-alice"), ("bob", "acct-bob"))
        ctx = self._load_ctx(personas=personas, hub_owner="the-ancestor")  # wake_timer_personas omitted
        self.assertEqual(ctx.wake_timer_personas, frozenset())
        bindings = mo._all_persona_bindings(ctx.manifest)

        for persona, account in personas:
            runner = mo.FakeCommandRunner()
            home = self.tmp_path / f"home-{persona}"
            mo.provision(ctx, bindings, runner, execute=True, current_account=account, home=home)
            self.assertFalse(
                any(call[:3] == ("systemctl", "--user", "enable") for call in runner.calls),
                f"no enable call expected for {persona}",
            )

    def test_dry_run_planned_line_reflects_skip_for_non_allowlisted_persona(self) -> None:
        ctx = self._load_ctx(
            personas=(("archivist", "acct-archivist"),), hub_owner="the-ancestor", wake_timer_personas=()
        )
        bindings = mo._all_persona_bindings(ctx.manifest)
        report = mo.provision(ctx, bindings, mo.FakeCommandRunner(), execute=False)
        archivist_result = next(r for r in report.personas if r.persona == "archivist")
        self.assertTrue(archivist_result.unit_leg.startswith("skipped"))
        self.assertNotIn("enable --now", archivist_result.unit_leg)


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


class VerifyTests(MailboxOnboardTestCase):
    def test_all_green(self) -> None:
        # alice allowlisted -- this test's scope is the "healthy allowlisted
        # seat" shape; the allowlist-vs-verify interaction has its own
        # dedicated tests below (VerifyAllowlistTests).
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), wake_timer_personas=("alice",))
        self._make_hub_dir("alice")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@alice.timer"): mo.CommandResult(
                    0, "enabled\n", ""
                )
            }
        )
        hub = self.mailbox_hub / "alice"  # T-6009: the maildir_acl row needs a scripted real-shape ACL to be green
        acl = _acl_text().replace(f"{LIVE_HUB}/artisan", str(hub))
        runner.scripted[("getfacl", "-p", str(hub), str(hub / "new"), str(hub / "tmp"))] = mo.CommandResult(0, acl, "")
        identity = mo.FakeHostIdentity(
            memberships={("acct-alice", "pantheon-mailbox"): (True, "ok")}, members={"pantheon-mailbox": MEMBERS}
        )
        checks = mo.verify(ctx, bindings, runner, identity)
        self.assertTrue(all(c.ok for c in checks), [c for c in checks if not c.ok])
        self.assertIn("maildir_acl", [c.name for c in checks])

    def test_red_case_missing_hub_dir_and_disabled_timer(self) -> None:
        # alice allowlisted -- disabled-when-should-be-enabled is the red
        # case this test targets (name "wake_timer.enabled" only applies to
        # allowlisted personas after T-2702).
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), wake_timer_personas=("alice",))
        # deliberately skip _make_hub_dir
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@alice.timer"): mo.CommandResult(
                    1, "disabled\n", ""
                )
            }
        )
        identity = mo.FakeHostIdentity()
        checks = mo.verify(ctx, bindings, runner, identity)
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["hub.new"].ok)
        self.assertFalse(by_name["wake_timer.enabled"].ok)
        self.assertFalse(by_name["group_membership"].ok)

    def test_verify_covers_every_persona_in_the_manifest(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "a1"), ("bob", "a2"), ("cara", "a3")))
        for persona in ("alice", "bob", "cara"):
            self._make_hub_dir(persona)
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        identity = mo.FakeHostIdentity()
        checks = mo.verify(ctx, bindings, runner, identity)
        self.assertEqual({c.persona for c in checks}, {"alice", "bob", "cara"})


# ---------------------------------------------------------------------------
# T-2702: verify()'s assertion direction flips with the allowlist.
# ---------------------------------------------------------------------------


class VerifyAllowlistTests(MailboxOnboardTestCase):
    def test_allowlisted_persona_enabled_is_green(self) -> None:
        ctx = self._load_ctx(personas=(("orchestrator", "acct-orchestrator"),), wake_timer_personas=("orchestrator",))
        self._make_hub_dir("orchestrator")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@orchestrator.timer"): mo.CommandResult(
                    0, "enabled\n", ""
                )
            }
        )
        identity = mo.FakeHostIdentity(memberships={("acct-orchestrator", "pantheon-mailbox"): (True, "ok")})
        checks = mo.verify(ctx, bindings, runner, identity)
        by_name = {c.name: c for c in checks}
        self.assertIn("wake_timer.enabled", by_name)
        self.assertTrue(by_name["wake_timer.enabled"].ok)

    def test_non_allowlisted_persona_not_enabled_is_green(self) -> None:
        # archivist, correctly never enabled -- verify must treat this as
        # healthy, not flag it.
        ctx = self._load_ctx(personas=(("archivist", "acct-archivist"),), wake_timer_personas=())
        self._make_hub_dir("archivist")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@archivist.timer"): mo.CommandResult(
                    1, "disabled\n", ""
                )
            }
        )
        identity = mo.FakeHostIdentity(memberships={("acct-archivist", "pantheon-mailbox"): (True, "ok")})
        checks = mo.verify(ctx, bindings, runner, identity)
        by_name = {c.name: c for c in checks}
        self.assertIn("wake_timer.not_enabled", by_name)
        self.assertNotIn("wake_timer.enabled", by_name)
        self.assertTrue(by_name["wake_timer.not_enabled"].ok)

    def test_non_allowlisted_persona_anomalously_enabled_is_red(self) -> None:
        # Drift case: a non-allowlisted persona's timer is somehow enabled
        # anyway (e.g. stale state from before this ticket). verify must
        # catch that as a failure, not silently treat it as fine.
        ctx = self._load_ctx(personas=(("mason", "acct-mason"),), wake_timer_personas=())
        self._make_hub_dir("mason")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@mason.timer"): mo.CommandResult(
                    0, "enabled\n", ""
                )
            }
        )
        identity = mo.FakeHostIdentity(memberships={("acct-mason", "pantheon-mailbox"): (True, "ok")})
        checks = mo.verify(ctx, bindings, runner, identity)
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["wake_timer.not_enabled"].ok)


# ---------------------------------------------------------------------------
# T-6092: a missing observation binary is a failed check row, never an escape.
# ---------------------------------------------------------------------------


class _MissingBinaryRunner(mo.FakeCommandRunner):
    """FakeCommandRunner whose host lacks one binary: run() raises like SubprocessCommandRunner does."""

    def __init__(self, missing: str) -> None:
        super().__init__()
        self.missing = missing

    def run(self, argv):
        if argv[0] == self.missing:
            self.calls.append(tuple(argv))
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        return super().run(argv)


class VerifyMissingBinaryTests(MailboxOnboardTestCase):
    def _verify(self, missing: str, **ctx_kwargs):
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), **ctx_kwargs)
        self._make_hub_dir("alice")
        bindings = mo._all_persona_bindings(ctx.manifest)
        identity = mo.FakeHostIdentity(
            memberships={("acct-alice", "pantheon-mailbox"): (True, "ok")}, members={"pantheon-mailbox": MEMBERS}
        )
        return {c.name: c for c in mo.verify(ctx, bindings, _MissingBinaryRunner(missing), identity)}

    def test_acl_check_missing_getfacl_fails_naming_binary(self) -> None:
        by_name = self._verify("getfacl", wake_timer_personas=("alice",))
        self.assertFalse(by_name["maildir_acl"].ok)
        self.assertIn("getfacl", by_name["maildir_acl"].detail)

    def test_wake_timer_missing_systemctl_fails_closed_allowlisted(self) -> None:
        by_name = self._verify("systemctl", wake_timer_personas=("alice",))
        self.assertFalse(by_name["wake_timer.enabled"].ok)
        self.assertIn("systemctl", by_name["wake_timer.enabled"].detail)

    def test_wake_timer_missing_systemctl_fails_closed_not_allowlisted(self) -> None:
        by_name = self._verify("systemctl", wake_timer_personas=())
        self.assertFalse(by_name["wake_timer.not_enabled"].ok)
        self.assertIn("systemctl", by_name["wake_timer.not_enabled"].detail)

    def test_wake_timer_missing_systemctl_fails_closed_secondary(self) -> None:
        by_name = self._verify("systemctl", wake_timer_personas=(), wake_timer_secondary_personas=("alice",))
        self.assertFalse(by_name["wake_timer.secondary"].ok)
        self.assertIn("systemctl", by_name["wake_timer.secondary"].detail)
        self.assertNotIn("allowed secondary waker", by_name["wake_timer.secondary"].detail)


# ---------------------------------------------------------------------------
# teardown()
# ---------------------------------------------------------------------------


class TeardownTests(MailboxOnboardTestCase):
    def test_dry_run_makes_no_calls(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        self._make_hub_dir("alice")
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        runner = mo.FakeCommandRunner()
        report = mo.teardown(ctx, binding, runner, execute=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(runner.calls, [])
        self.assertTrue((self.mailbox_hub / "alice").is_dir())

    def test_execute_disables_unit_as_persona_account(self) -> None:
        # alice allowlisted -- this test's scope is the disable mechanics for
        # an allowlisted persona; the allowlist gate itself has dedicated
        # tests below (TeardownAllowlistTests).
        ctx = self._load_ctx(
            personas=(("alice", "acct-alice"),), hub_owner="the-ancestor", wake_timer_personas=("alice",)
        )
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        runner = mo.FakeCommandRunner()
        report = mo.teardown(ctx, binding, runner, execute=True, current_account="acct-alice")
        self.assertIn(("systemctl", "--user", "disable", "--now", "pantheon-mailbox-wake@alice.timer"), runner.calls)
        self.assertFalse(report.dry_run)

    def test_execute_wrong_account_skips_unit_leg(self) -> None:
        # alice allowlisted, so the skip here is specifically the wrong-
        # account path, not the T-2702 allowlist gate (which would also
        # produce a "skipped unit-leg" message but for a different reason --
        # see TeardownAllowlistTests for that case).
        ctx = self._load_ctx(
            personas=(("alice", "acct-alice"),), hub_owner="the-ancestor", wake_timer_personas=("alice",)
        )
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        runner = mo.FakeCommandRunner()
        report = mo.teardown(ctx, binding, runner, execute=True, current_account="someone-else")
        self.assertEqual(runner.calls, [])
        self.assertTrue(any("skipped unit-leg: wrong account" in item for item in report.changed))

    def test_purge_hub_removes_maildir_only_as_hub_owner(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        self._make_hub_dir("alice")
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        report = mo.teardown(
            ctx, binding, mo.FakeCommandRunner(), execute=True, purge_hub=True, current_account="the-ancestor"
        )
        self.assertFalse((self.mailbox_hub / "alice").exists())
        self.assertTrue(any(item.startswith("removed") for item in report.changed))

    def test_purge_hub_skipped_when_running_as_wrong_account(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        self._make_hub_dir("alice")
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        mo.teardown(
            ctx, binding, mo.FakeCommandRunner(), execute=True, purge_hub=True, current_account="acct-alice"
        )
        self.assertTrue((self.mailbox_hub / "alice").is_dir())  # untouched: wrong account for the hub leg

    def test_remove_hub_root_only_when_empty_and_as_hub_owner(self) -> None:
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        self.mailbox_hub.mkdir(parents=True)
        report = mo.teardown(
            ctx,
            mo._all_persona_bindings(ctx.manifest)[0],
            mo.FakeCommandRunner(),
            execute=True,
            remove_hub_root=True,
            current_account="the-ancestor",
        )
        self.assertFalse(ctx.mailbox_hub.exists())
        self.assertTrue(any("removed" in item for item in report.changed))

    def test_remove_hub_root_never_userdels_hub_owner(self) -> None:
        # Documented scope trim: teardown never shells useradd/userdel at all --
        # only filesystem + systemctl mutations. Confirms no such call appears
        # even with every destructive flag set.
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        self._make_hub_dir("alice")
        runner = mo.FakeCommandRunner()
        mo.teardown(
            ctx,
            mo._all_persona_bindings(ctx.manifest)[0],
            runner,
            execute=True,
            purge_hub=True,
            remove_hub_root=True,
            current_account="the-ancestor",
        )
        self.assertFalse(any(call[0] == "userdel" for call in runner.calls))


# ---------------------------------------------------------------------------
# T-2702: teardown()'s unit-disable leg mirrors the provision-side allowlist
# gate -- skip is surfaced, never silently attempted or silently dropped.
# ---------------------------------------------------------------------------


class TeardownAllowlistTests(MailboxOnboardTestCase):
    def test_execute_skips_disable_for_non_allowlisted_persona(self) -> None:
        ctx = self._load_ctx(
            personas=(("archivist", "acct-archivist"),), hub_owner="the-ancestor", wake_timer_personas=()
        )
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        runner = mo.FakeCommandRunner()
        report = mo.teardown(ctx, binding, runner, execute=True, current_account="acct-archivist")
        self.assertNotIn(
            ("systemctl", "--user", "disable", "--now", "pantheon-mailbox-wake@archivist.timer"), runner.calls
        )
        self.assertEqual(runner.calls, [])
        self.assertTrue(
            any("not a standalone-wake seat" in item for item in report.changed),
            report.changed,
        )

    def test_dry_run_plan_reflects_skip_not_a_misleading_disable_line(self) -> None:
        ctx = self._load_ctx(
            personas=(("archivist", "acct-archivist"),), hub_owner="the-ancestor", wake_timer_personas=()
        )
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        report = mo.teardown(ctx, binding, mo.FakeCommandRunner(), execute=False)
        self.assertTrue(report.dry_run)
        self.assertFalse(any("disable --now" in item for item in report.planned))
        self.assertTrue(any("not a standalone-wake seat" in item for item in report.planned))


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class CliTests(MailboxOnboardTestCase):
    def test_hub_setup_dry_run_via_main(self) -> None:
        self._write_manifest(hub_owner="the-ancestor")
        rc = mo.main(
            ["hub-setup", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT)],
            runner=mo.FakeCommandRunner(),
            host_identity=mo.FakeHostIdentity(),
        )
        self.assertEqual(rc, 0)

    def test_provision_dry_run_via_main(self) -> None:
        self._write_manifest()
        rc = mo.main(
            ["provision", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT)],
            runner=mo.FakeCommandRunner(),
            host_identity=mo.FakeHostIdentity(),
        )
        self.assertEqual(rc, 0)

    def test_provision_unknown_persona_filter_errors(self) -> None:
        self._write_manifest()
        rc = mo.main(
            [
                "provision",
                "--manifest",
                str(self.manifest_path),
                "--repo-root",
                str(REPO_ROOT),
                "--persona",
                "nonexistent",
            ],
            runner=mo.FakeCommandRunner(),
            host_identity=mo.FakeHostIdentity(),
        )
        self.assertEqual(rc, 1)

    def test_verify_via_main_returns_nonzero_on_red(self) -> None:
        self._write_manifest(personas=(("alice", "acct-alice"),))
        # no hub dir created -> verify must fail
        rc = mo.main(
            ["verify", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT)],
            runner=mo.FakeCommandRunner(),
            host_identity=mo.FakeHostIdentity(),
        )
        self.assertEqual(rc, 1)

    def test_teardown_dry_run_via_main(self) -> None:
        self._write_manifest(personas=(("alice", "acct-alice"),))
        rc = mo.main(
            [
                "teardown",
                "--persona",
                "alice",
                "--manifest",
                str(self.manifest_path),
                "--repo-root",
                str(REPO_ROOT),
            ],
            runner=mo.FakeCommandRunner(),
            host_identity=mo.FakeHostIdentity(),
        )
        self.assertEqual(rc, 0)


class CliExecuteIdentityResolutionTests(MailboxOnboardTestCase):
    """T-2683 REBOUND #1: main() never resolved current_account from the OS,
    so `--execute` silently no-op'd every leg (`current_account` stayed None,
    every per-leg equality check was vacuously False). These drive --execute
    through the real CLI entry point (mo.main) with os.getuid/pwd.getpwuid
    mocked, so a regression back to "current_account is always None" fails
    the suite instead of passing silently (CliTests only drove dry-run; the
    account-gated unit tests above call provision()/teardown() directly with
    a hand-supplied current_account, bypassing main()'s resolution path
    entirely)."""

    def test_provision_execute_via_main_resolves_persona_account_for_unit_leg(self) -> None:
        self._write_manifest(
            personas=(("alice", "acct-alice"), ("bob", "acct-bob")),
            hub_owner="the-ancestor",
            wake_timer_personas=("alice",),
        )
        home = self.tmp_path / "home-alice"
        runner = mo.FakeCommandRunner()
        fake_pw = type("FakePw", (), {"pw_name": "acct-alice"})()
        with patch("tools.mailbox_onboard.os.getuid", return_value=4242), patch(
            "tools.mailbox_onboard.pwd.getpwuid", return_value=fake_pw
        ) as getpwuid:
            rc = mo.main(
                [
                    "provision",
                    "--manifest",
                    str(self.manifest_path),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--execute",
                    "--home",
                    str(home),
                ],
                runner=runner,
                host_identity=mo.FakeHostIdentity(),
                # current_account intentionally omitted -- must resolve via mocked OS identity
            )
        self.assertEqual(rc, 0)
        getpwuid.assert_called_with(4242)

        service_path = home / ".config" / "systemd" / "user" / "pantheon-mailbox-wake@.service"
        self.assertTrue(service_path.is_file())
        self.assertIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@alice.timer"), runner.calls
        )
        self.assertNotIn(
            ("systemctl", "--user", "enable", "--now", "pantheon-mailbox-wake@bob.timer"), runner.calls
        )
        # hub leg never executes here -- resolved identity is acct-alice, not the hub owner.
        self.assertFalse((self.mailbox_hub / "alice" / "new").exists())

    def test_provision_execute_via_main_resolves_hub_owner_account_for_hub_leg(self) -> None:
        self._write_manifest(personas=(("alice", "acct-alice"),), hub_owner="the-ancestor")
        runner = mo.FakeCommandRunner()
        fake_pw = type("FakePw", (), {"pw_name": "the-ancestor"})()
        with patch("tools.mailbox_onboard.os.getuid", return_value=7), patch(
            "tools.mailbox_onboard.pwd.getpwuid", return_value=fake_pw
        ):
            rc = mo.main(
                ["provision", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT), "--execute"],
                runner=runner,
                host_identity=mo.FakeHostIdentity(),
            )
        self.assertEqual(rc, 0)
        for sub in ("tmp", "new", "cur"):
            self.assertTrue((self.mailbox_hub / "alice" / sub).is_dir())
        # unit leg never executes here -- resolved identity is the hub owner, not acct-alice.
        self.assertEqual(runner.calls, [])

    def test_teardown_execute_via_main_resolves_persona_account(self) -> None:
        self._write_manifest(
            personas=(("alice", "acct-alice"),), hub_owner="the-ancestor", wake_timer_personas=("alice",)
        )
        runner = mo.FakeCommandRunner()
        fake_pw = type("FakePw", (), {"pw_name": "acct-alice"})()
        with patch("tools.mailbox_onboard.os.getuid", return_value=99), patch(
            "tools.mailbox_onboard.pwd.getpwuid", return_value=fake_pw
        ):
            rc = mo.main(
                [
                    "teardown",
                    "--persona",
                    "alice",
                    "--manifest",
                    str(self.manifest_path),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--execute",
                ],
                runner=runner,
                host_identity=mo.FakeHostIdentity(),
            )
        self.assertEqual(rc, 0)
        self.assertIn(
            ("systemctl", "--user", "disable", "--now", "pantheon-mailbox-wake@alice.timer"), runner.calls
        )


class RealHostIdentityTests(unittest.TestCase):
    """Light sanity check of the real (non-faked) impl -- safe on any host:
    queries a group name that should never plausibly exist."""

    def test_group_exists_false_for_a_made_up_group(self) -> None:
        identity = mo.RealHostIdentity()
        self.assertFalse(identity.group_exists("t2683-mailbox-onboard-test-nonexistent-group"))

    def test_user_exists_false_for_a_made_up_user(self) -> None:
        identity = mo.RealHostIdentity()
        self.assertFalse(identity.user_exists("t2683-mailbox-onboard-test-nonexistent-user"))


# ---------------------------------------------------------------------------
# T-5323: wake_timer_secondary_personas -- seats whose standalone
# pantheon-mailbox-wake@<p>.timer is ALLOWED (a deliberate second waker,
# serialized with consume by the T-5283 .wake.lock) but NOT provisioned by
# this tool. verify accepts either state; provision/teardown skip them.
# ---------------------------------------------------------------------------


class SecondaryWakeTimerTests(MailboxOnboardTestCase):
    def _verify_with_state(self, persona: str, rc: int, state: str) -> dict:
        ctx = self._load_ctx(
            personas=((persona, f"acct-{persona}"),),
            wake_timer_personas=("orchestrator",),
            wake_timer_secondary_personas=(persona,),
        )
        self._make_hub_dir(persona)
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", f"pantheon-mailbox-wake@{persona}.timer"): mo.CommandResult(
                    rc, f"{state}\n", ""
                )
            }
        )
        hub = self.mailbox_hub / persona  # T-6009: a scripted real-shape ACL keeps the maildir_acl row green
        runner.scripted[("getfacl", "-p", str(hub), str(hub / "new"), str(hub / "tmp"))] = mo.CommandResult(
            0, _acl_text().replace(f"{LIVE_HUB}/artisan", str(hub)), ""
        )
        identity = mo.FakeHostIdentity(
            memberships={(f"acct-{persona}", "pantheon-mailbox"): (True, "ok")}, members={"pantheon-mailbox": MEMBERS}
        )
        checks = mo.verify(ctx, bindings, runner, identity)
        return {c.name: c for c in checks}

    def test_absent_key_resolves_to_empty_set(self) -> None:
        ctx = self._load_ctx(wake_timer_personas=("alice",))
        self.assertEqual(ctx.wake_timer_secondary_personas, frozenset())

    def test_key_parsed_into_frozenset(self) -> None:
        ctx = self._load_ctx(wake_timer_personas=("orchestrator",), wake_timer_secondary_personas=("artisan", "recovery"))
        self.assertEqual(ctx.wake_timer_secondary_personas, frozenset({"artisan", "recovery"}))

    def test_malformed_key_is_rejected(self) -> None:
        for bad in ("artisan", ["artisan", ""], ["artisan", 3], {"artisan": True}):
            with self.subTest(bad=bad):
                self._write_manifest(wake_timer_personas=("orchestrator",))
                doc = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
                doc["persona_installer"]["mailbox"]["wake_timer_secondary_personas"] = bad
                self.manifest_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
                with self.assertRaisesRegex(mo.OnboardError, "wake_timer_secondary_personas"):
                    mo.load_mailbox_context(self.manifest_path, REPO_ROOT)

    def test_persona_in_both_lists_is_a_manifest_error(self) -> None:
        with self.assertRaises(mo.OnboardError) as caught:
            self._load_ctx(wake_timer_personas=("orchestrator", "artisan"), wake_timer_secondary_personas=("artisan", "recovery"))
        message = str(caught.exception)
        self.assertIn("artisan", message)
        self.assertIn("wake_timer_personas", message)
        self.assertIn("wake_timer_secondary_personas", message)
        self.assertNotIn("recovery", message)

    def test_verify_secondary_persona_enabled_passes(self) -> None:
        by_name = self._verify_with_state("artisan", 0, "enabled")
        self.assertNotIn("wake_timer.not_enabled", by_name)
        self.assertNotIn("wake_timer.enabled", by_name)
        self.assertTrue(by_name["wake_timer.secondary"].ok)
        self.assertIn("enabled", by_name["wake_timer.secondary"].detail)
        self.assertTrue(all(c.ok for c in by_name.values()))

    def test_verify_secondary_persona_disabled_passes(self) -> None:
        by_name = self._verify_with_state("artisan", 1, "disabled")
        self.assertTrue(by_name["wake_timer.secondary"].ok)
        self.assertIn("disabled", by_name["wake_timer.secondary"].detail)
        self.assertTrue(all(c.ok for c in by_name.values()))

    def test_verify_unlisted_persona_still_default_deny(self) -> None:
        # A persona on NEITHER list keeps the T-2702 not_enabled assertion.
        ctx = self._load_ctx(
            personas=(("mason", "acct-mason"),), wake_timer_personas=("orchestrator",), wake_timer_secondary_personas=("artisan",)
        )
        self._make_hub_dir("mason")
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@mason.timer"): mo.CommandResult(
                    0, "enabled\n", ""
                )
            }
        )
        identity = mo.FakeHostIdentity(memberships={("acct-mason", "pantheon-mailbox"): (True, "ok")})
        by_name = {c.name: c for c in mo.verify(ctx, bindings, runner, identity)}
        self.assertFalse(by_name["wake_timer.not_enabled"].ok)
        self.assertNotIn("wake_timer.secondary", by_name)

    def test_provision_skips_secondary_persona(self) -> None:
        ctx = self._load_ctx(
            personas=(("artisan", "acct-artisan"),), hub_owner="the-ancestor", wake_timer_secondary_personas=("artisan",)
        )
        bindings = mo._all_persona_bindings(ctx.manifest)
        runner = mo.FakeCommandRunner()
        home = self.tmp_path / "home-artisan"
        report = mo.provision(ctx, bindings, runner, execute=True, current_account="acct-artisan", home=home)
        self.assertFalse(any(call[:3] == ("systemctl", "--user", "enable") for call in runner.calls), runner.calls)
        result = next(r for r in report.personas if r.persona == "artisan")
        self.assertTrue(result.unit_leg.startswith("skipped"))
        self.assertFalse((home / ".config" / "systemd" / "user" / f"{mo.WAKE_UNIT_STEM}@.timer").exists())
        dry = mo.provision(ctx, bindings, mo.FakeCommandRunner(), execute=False)
        dry_result = next(r for r in dry.personas if r.persona == "artisan")
        self.assertTrue(dry_result.unit_leg.startswith("skipped"))

    def test_teardown_skips_secondary_persona(self) -> None:
        ctx = self._load_ctx(
            personas=(("artisan", "acct-artisan"),), hub_owner="the-ancestor", wake_timer_secondary_personas=("artisan",)
        )
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        runner = mo.FakeCommandRunner()
        report = mo.teardown(ctx, binding, runner, execute=True, current_account="acct-artisan")
        self.assertEqual(runner.calls, [])
        self.assertTrue(any(item.startswith("skipped unit-leg") for item in report.changed), report.changed)
        dry = mo.teardown(ctx, binding, mo.FakeCommandRunner(), execute=False)
        self.assertFalse(any("disable --now" in item for item in dry.planned))

    def test_live_instance_yaml_lists_consume_plane_secondaries(self) -> None:
        ctx = mo.load_mailbox_context(REPO_ROOT / "instance.yaml", REPO_ROOT)
        self.assertEqual(
            ctx.wake_timer_secondary_personas,
            frozenset({"artisan", "specialist", "recovery", "mason", "watchman"}),
        )
        self.assertFalse(ctx.wake_timer_personas & ctx.wake_timer_secondary_personas)


# ---------------------------------------------------------------------------
# T-6009: maildir ACL (parse_getfacl / acl_gaps / the verify_persona maildir_acl row).
# FIXTURE is real `getfacl -p` output captured from the live hub (artisan, orchestrator, mason).
# ---------------------------------------------------------------------------

ACL_FIXTURE = REPO_ROOT / "tools" / "germinate" / "components" / "tests" / "fixtures" / "mailbox_getfacl_real.txt"
LIVE_HUB = "/home/post-office/mailbox"
MEMBERS = ("claude", "claude-research", "claude-fallback", "the-ancestor", "codex", "exocortex", "suborch1", "suborch2", "post-office", "codex-fallback")
ARTISAN = (f"{LIVE_HUB}/artisan", f"{LIVE_HUB}/artisan/new", f"{LIVE_HUB}/artisan/tmp")


def _acl_text() -> str:
    return ACL_FIXTURE.read_text(encoding="utf-8")


def _edit_block(text: str, path: str, fn) -> str:
    """Apply fn(lines) -> lines to the one `# file: <path>` block of getfacl text."""

    blocks = text.strip("\n").split("\n\n")
    out = [("\n".join(fn(b.split("\n"))) if b.startswith(f"# file: {path}\n") else b) for b in blocks]
    return "\n\n".join(out) + "\n"


class ParseGetfaclTests(unittest.TestCase):
    def test_parses_the_real_capture(self) -> None:
        parsed = mo.parse_getfacl(_acl_text())
        self.assertEqual(len(parsed), 9)
        artisan = parsed[ARTISAN[0]]
        self.assertEqual(artisan["owner"], "post-office")
        self.assertEqual(artisan["access"]["suborch2"], "rwx")
        self.assertEqual(artisan["default"]["suborch2"], "rwx")
        self.assertEqual((artisan["mask"], artisan["default_mask"]), ("rwx", "rwx"))
        self.assertEqual((artisan["owner_access"], artisan["owner_default"]), ("rwx", "rwx"))

    def test_effective_comments_are_ignored(self) -> None:
        parsed = mo.parse_getfacl("# file: /x\n# owner: o\nuser::rwx\nuser:a:rwx\t\t#effective:r--\nmask::r--\n")
        self.assertEqual(parsed["/x"]["access"], {"a": "rwx"})
        self.assertEqual(parsed["/x"]["mask"], "r--")

    def test_empty_and_garbage_parse_to_nothing(self) -> None:
        self.assertEqual(mo.parse_getfacl(""), {})
        self.assertEqual(mo.parse_getfacl("getfacl: nope: No such file\nrubbish\n"), {})


class AclGapsTests(unittest.TestCase):
    def gaps(self, text: str, members=MEMBERS) -> tuple[str, ...]:
        return mo.acl_gaps(mo.parse_getfacl(text), ARTISAN, members, "post-office")

    def test_real_capture_has_no_gaps(self) -> None:
        self.assertEqual(self.gaps(_acl_text()), ())

    def test_missing_access_entry_names_account_path_and_kind(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[1], lambda ls: [x for x in ls if x != "user:suborch2:rwx"])
        [gap] = self.gaps(text)
        self.assertIn("suborch2", gap)
        self.assertIn(ARTISAN[1], gap)
        self.assertIn("access", gap)

    def test_missing_default_entry_names_account_path_and_kind(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[2], lambda ls: [x for x in ls if x != "default:user:suborch2:rwx"])
        [gap] = self.gaps(text)
        self.assertTrue("suborch2" in gap and ARTISAN[2] in gap and "default" in gap, gap)

    def test_short_entry_is_a_gap(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[0], lambda ls: ["user:suborch2:r-x" if x == "user:suborch2:rwx" else x for x in ls])
        [gap] = self.gaps(text)
        self.assertIn("r-x", gap)

    def test_mask_restriction_gaps_every_named_user_but_not_the_owner(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[1], lambda ls: ["mask::r-x" if x == "mask::rwx" else x for x in ls])
        gaps = self.gaps(text)
        self.assertEqual(len(gaps), len(MEMBERS) - 1)  # post-office is satisfied by user::
        self.assertTrue(all("masked by mask::r-x" in g and ARTISAN[1] in g and "access" in g for g in gaps), gaps)
        self.assertFalse(any(g.startswith("post-office ") for g in gaps))

    def test_default_mask_restriction_is_a_default_gap(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[2], lambda ls: ["default:mask::r-x" if x == "default:mask::rwx" else x for x in ls])
        gaps = self.gaps(text)
        self.assertTrue(gaps and all("masked by default:mask::r-x" in g and "default" in g for g in gaps), gaps)

    def test_a_path_absent_from_the_output_is_no_acl_observed(self) -> None:
        blocks = _acl_text().strip("\n").split("\n\n")
        text = "\n\n".join(b for b in blocks if not b.startswith(f"# file: {ARTISAN[2]}\n")) + "\n"
        self.assertEqual(self.gaps(text), (f"{ARTISAN[2]}: no ACL observed",))

    def test_empty_output_fails_closed_for_every_path(self) -> None:
        self.assertEqual(self.gaps(""), tuple(f"{p}: no ACL observed" for p in ARTISAN))

    def test_owner_needs_a_user_entry_only_when_it_owns_the_path(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[0], lambda ls: [x for x in ls if x != "user::rwx"])
        [gap] = self.gaps(text)
        self.assertTrue("post-office" in gap and ARTISAN[0] in gap, gap)
        self.assertEqual(self.gaps(_acl_text(), members=("post-office",)), ())
        self.assertTrue(self.gaps(_acl_text(), members=("stranger",)))  # a member with no entry at all

    def test_no_members_fails_closed(self) -> None:
        [gap] = self.gaps(_acl_text(), members=())
        self.assertIn("no mailbox group members", gap)


class VerifyPersonaAclTests(MailboxOnboardTestCase):
    def _run(self, text: str, members=MEMBERS, make_dir: bool = True):
        ctx = self._load_ctx(personas=(("alice", "acct-alice"),), wake_timer_personas=("alice",), hub_owner="post-office")
        if make_dir:
            self._make_hub_dir("alice")
        hub = self.mailbox_hub / "alice"
        argv = ("getfacl", "-p", str(hub), str(hub / "new"), str(hub / "tmp"))
        runner = mo.FakeCommandRunner(scripted={argv: mo.CommandResult(0, text.replace(f"{LIVE_HUB}/artisan", str(hub)), "")})
        identity = mo.FakeHostIdentity(members={"pantheon-mailbox": members})
        binding = mo._all_persona_bindings(ctx.manifest)[0]
        return mo.verify_persona(ctx, binding, runner, identity), runner, argv

    def test_row_is_after_hub_and_before_group_membership(self) -> None:
        checks, runner, argv = self._run(_acl_text())
        self.assertEqual([c.name for c in checks][:6], ["hub.tmp", "hub.new", "hub.cur", "maildir_acl", "group_membership", "wake_timer.enabled"])
        [acl] = [c for c in checks if c.name == "maildir_acl"]
        self.assertTrue(acl.ok, acl.detail)
        self.assertIn(argv, runner.calls)

    def test_gap_makes_the_row_fail_and_the_detail_lists_it(self) -> None:
        text = _edit_block(_acl_text(), ARTISAN[0], lambda ls: [x for x in ls if x != "user:codex:rwx"])
        [acl] = [c for c in self._run(text)[0] if c.name == "maildir_acl"]
        self.assertFalse(acl.ok)
        self.assertIn("codex", acl.detail)

    def test_unscripted_getfacl_fails_closed(self) -> None:
        checks, _r, _a = self._run("")
        [acl] = [c for c in checks if c.name == "maildir_acl"]
        self.assertFalse(acl.ok)
        self.assertIn("no ACL observed", acl.detail)

    def test_absent_maildir_emits_no_acl_row_and_runs_no_getfacl(self) -> None:
        checks, runner, argv = self._run(_acl_text(), make_dir=False)
        self.assertNotIn("maildir_acl", [c.name for c in checks])
        self.assertNotIn(argv, runner.calls)

    def test_host_identity_group_members(self) -> None:
        self.assertEqual(mo.FakeHostIdentity(members={"g": ["a", "b"]}).group_members("g"), ("a", "b"))
        self.assertEqual(mo.FakeHostIdentity().group_members("g"), ())
        self.assertEqual(mo.RealHostIdentity().group_members("no-such-group-t6009"), ())
        with patch("tools.mailbox_onboard.grp.getgrnam") as getgrnam, patch("tools.mailbox_onboard.pwd.getpwall") as getpwall:
            getgrnam.return_value.gr_mem = ["x", "y"]
            getgrnam.return_value.gr_gid = 1006
            getpwall.return_value = ()
            self.assertEqual(mo.RealHostIdentity().group_members("g"), ("x", "y"))

    def test_host_identity_group_members_includes_primary_gid_accounts(self) -> None:
        """recovery D15: gr_mem omits accounts whose PRIMARY gid is the group; they must be counted."""
        with patch("tools.mailbox_onboard.grp.getgrnam") as getgrnam, patch("tools.mailbox_onboard.pwd.getpwall") as getpwall:
            getgrnam.return_value.gr_mem = ["x"]
            getgrnam.return_value.gr_gid = 1006
            getpwall.return_value = (
                SimpleNamespace(pw_name="x", pw_gid=1006),       # both: listed once
                SimpleNamespace(pw_name="prim", pw_gid=1006),    # primary-gid only
                SimpleNamespace(pw_name="other", pw_gid=1000),   # not in the group
            )
            self.assertEqual(mo.RealHostIdentity().group_members("g"), ("x", "prim"))

    def test_primary_gid_only_member_without_an_acl_entry_is_a_gap(self) -> None:
        """recovery D15: an account in the group only via its primary gid, with no ACL entry, is named."""
        parsed = mo.parse_getfacl(
            "# file: /hub/alice\n# owner: post-office\n# group: pantheon-mailbox\nuser::rwx\n"
            "user:x:rwx\ngroup::---\nmask::rwx\nother::---\n"
            "default:user::rwx\ndefault:user:x:rwx\ndefault:group::---\ndefault:mask::rwx\ndefault:other::---\n"
        )
        gaps = mo.acl_gaps(parsed, ("/hub/alice",), ("x", "prim"), "post-office")
        self.assertTrue(gaps)
        self.assertTrue(all("prim" in g for g in gaps), gaps)
        self.assertTrue(any("access" in g for g in gaps) and any("default" in g for g in gaps), gaps)

    def test_cli_verify_exits_nonzero_on_an_acl_gap(self) -> None:
        self._write_manifest(personas=(("alice", "acct-alice"),), wake_timer_personas=("alice",), hub_owner="post-office")
        self._make_hub_dir("alice")
        hub = self.mailbox_hub / "alice"
        argv = ("getfacl", "-p", str(hub), str(hub / "new"), str(hub / "tmp"))
        runner = mo.FakeCommandRunner(scripted={("systemctl", "--user", "is-enabled", "pantheon-mailbox-wake@alice.timer"): mo.CommandResult(0, "enabled\n", "")})
        identity = mo.FakeHostIdentity(memberships={("acct-alice", "pantheon-mailbox"): (True, "ok")}, members={"pantheon-mailbox": MEMBERS})
        rc = mo.main(["verify", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT)], runner=runner, host_identity=identity)
        self.assertEqual(rc, 1)  # unscripted getfacl: empty output, every path a gap
        runner.scripted[argv] = mo.CommandResult(0, _acl_text().replace(f"{LIVE_HUB}/artisan", str(hub)), "")
        rc = mo.main(["verify", "--manifest", str(self.manifest_path), "--repo-root", str(REPO_ROOT)], runner=runner, host_identity=identity)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
