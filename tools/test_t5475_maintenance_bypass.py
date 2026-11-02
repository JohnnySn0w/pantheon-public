"""T-5475: persona_installer --maintenance-bypass.

Intent: the installer can bootstrap a seat that is in maintenance, but only when the operator
explicitly asks and that choice is bound into the approved plan digest. It writes consume's
one-shot bypass token (the same one the hand-move seat script writes) around the bootstrap
unit-fire and verifies consume consumed it. The default (refuse while armed) is unchanged.

Fakes / tmp roots only: a fake ``systemctl --user`` behind ``LocalHostBackend._run``, injected
maintenance / deep-sleep predicates, ``SimulatedHostBackend``. No real token, seat, or manager.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import persona_bringup as pb
from tools import persona_installer as pim
from tools import persona_installer_apply as pia
from tools.persona_installer import OperationFailed, SimulatedHostBackend
from tools import test_persona_installer_converge as conv

MASON = conv.MASON
RECEIPT = conv.BaselineAwareFireTests.RECEIPT
SCRIPT = [
    conv._poll("inactive", conv.OLD_INVOCATION, exit_status="5"),  # pre-enqueue baseline
    conv._poll("activating", conv.NEW_INVOCATION),                 # captured
    conv._poll("inactive", conv.NEW_INVOCATION, exit_status="5"),  # completion, in policy
]


class ConsumingSystemd(conv.FakeSystemd):
    """``systemctl --user start`` runs the REAL ``consume_maintenance_bypass`` (what consume does
    first on every fire), or does not read the token at all (``reads=False``)."""

    def __init__(self, *args, state_root: Path, persona: str = "recovery", reads: bool = True,
                 start_rc: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.state_root, self.persona, self.reads, self.start_rc = state_root, persona, reads, start_rc
        self.token_at_start: bool | None = None
        self.consume_result: bool | None = None

    def __call__(self, command):
        if command[:3] == ("systemctl", "--user", "start"):
            token = pb.maintenance_bypass_path(self.state_root, self.persona)
            self.token_at_start = token.exists()
            if self.reads:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.consume_result = pb.consume_maintenance_bypass(self.state_root, self.persona)
            if self.start_rc:
                self.log.append(command)
                return conv.cp(command, self.start_rc, "", "start refused")
        return super().__call__(command)


class Backend(conv.GatesBackend):
    """Real bootstrap gates; maintenance answers from a script (last entry sticky)."""

    armed_script: list[bool] = [True]
    deep = False

    def _seat_in_maintenance(self, persona: str) -> bool:  # type: ignore[override]
        script = self.armed_script
        value = script[0]
        if len(script) > 1:
            script.pop(0)
        return value

    def _seat_deep_sleeping(self, persona: str) -> bool:
        return self.deep


class Case(conv.ConvergeCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_on = self.installer.plan(self.manifest, "recovery", root=self.root / "projection",
                                           maintenance_bypass=True)
        fire = self.rooted(self.op(self.plan_on, "connective.unit-fire"))
        self.fire_on = dataclasses.replace(
            fire, desired={k: v for k, v in fire.desired.items() if k not in ("residue_cleared_by", "trust_set_by")}
        )
        self.fire_off = dataclasses.replace(
            self.fire_on, desired={k: v for k, v in self.fire_on.desired.items() if k != "maintenance_bypass"}
        )
        viability = pim._parse_unit_fire_initial_viability_desired(self.fire_on)
        self.state_root = viability.persona_state_root
        self.token = pb.maintenance_bypass_path(self.state_root, "recovery")
        viability.account_home.mkdir(parents=True, exist_ok=True)
        (viability.account_home / ".claude.json").write_text(
            json.dumps({"projects": {str(viability.account_home / "pantheon-config"): {"hasTrustDialogAccepted": True}}}),
            encoding="utf-8",
        )

    def make(self, *, armed=(True,), deep=False, reads=True, start_rc=0, script=SCRIPT):
        fake = ConsumingSystemd({self.SERVICE: MASON}, poll_script=list(script) if script else None, state_root=self.state_root,
                                reads=reads, start_rc=start_rc)
        backend = Backend([fake])
        backend.armed_script = list(armed)
        backend.deep = deep
        return backend, fake

    def fire(self, backend, operation=None):
        with mock.patch("tools.runtime_controller.validate_initial_viability_invocation_receipt",
                        return_value=RECEIPT):
            return backend.execute(operation or self.fire_on, conv.context(str(self.root)))


class PlanShapeTests(Case):
    def test_default_plan_and_digest_are_byte_identical_and_key_absent(self) -> None:
        default = self.plan("recovery")
        explicit_off = self.installer.plan(self.manifest, "recovery", root=self.root / "projection",
                                           maintenance_bypass=False)
        self.assertEqual(default.digest, explicit_off.digest)
        self.assertNotIn("maintenance_bypass", self.op(default, "connective.unit-fire").desired)
        self.assertNotIn("maintenance_bypass", self.op(explicit_off, "connective.unit-fire").desired)

    def test_flag_sets_one_key_and_changes_the_digest(self) -> None:
        default = self.plan("recovery")
        self.assertEqual(self.op(self.plan_on, "connective.unit-fire").desired["maintenance_bypass"], "true")
        self.assertNotEqual(default.digest, self.plan_on.digest)
        self.assertEqual(self.ids(default), self.ids(self.plan_on))

    def test_an_approved_digest_is_refused_in_the_other_mode(self) -> None:
        kwargs = dict(root=self.root / "projection", euid=1000, uid_of=lambda _a: 1000)
        _i, off, _m = pia.build_plan(self.manifest_path, "recovery", root=self.root / "projection")
        _i, on, _m = pia.build_plan(self.manifest_path, "recovery", root=self.root / "projection",
                                    maintenance_bypass=True)
        self.assertNotEqual(off.digest, on.digest)
        with self.assertRaises(pia.ApplyRefused):
            pia.execute(self.manifest_path, "recovery", plan_digest=off.digest, maintenance_bypass=True, **kwargs)
        with self.assertRaises(pia.ApplyRefused):
            pia.execute(self.manifest_path, "recovery", plan_digest=on.digest, maintenance_bypass=False, **kwargs)

    def test_cli_flag_is_registered_on_apply(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        pim._seat_verbs(parser, parser.add_subparsers(dest="command"))
        args = parser.parse_args(["apply", "--persona", "recovery", "--maintenance-bypass"])
        self.assertTrue(args.maintenance_bypass)
        self.assertFalse(parser.parse_args(["apply", "--persona", "recovery"]).maintenance_bypass)


class GateTests(Case):
    def test_dry_run_passes_when_armed_with_bypass(self) -> None:
        backend, fake = self.make(script=None)
        verdict = backend.preflight(self.fire_on)
        self.assertTrue(verdict.ok, verdict)
        self.assertIn("one-shot bypass token will be written", verdict.detail)
        self.assertFalse(self.token.exists())
        self.assertEqual(fake.mutations(), [])

    def test_dry_run_refuses_armed_without_bypass_unchanged(self) -> None:
        backend, _ = self.make(script=None)
        verdict = backend.preflight(self.fire_off)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.detail, pim.MAINTENANCE_REFUSAL.format(persona="recovery"))

    def test_deep_sleep_refuses_even_with_bypass(self) -> None:
        backend, _ = self.make(deep=True, script=None)
        verdict = backend.preflight(self.fire_on)
        self.assertFalse(verdict.ok)
        self.assertIn("deep-sleep", verdict.detail)
        with self.assertRaisesRegex(OperationFailed, "deep-sleep"):
            self.fire(backend)
        self.assertFalse(self.token.exists())

    def test_deep_sleep_without_bypass_is_not_a_new_refusal(self) -> None:
        backend, _ = self.make(armed=(False,), deep=True, script=None)
        self.assertIsNone(backend._unit_fire_bootstrap_blocker(self.fire_off))

    def test_not_armed_passes_with_or_without_bypass(self) -> None:
        backend, _ = self.make(armed=(False,), script=None)
        self.assertTrue(backend.preflight(self.fire_on).ok)
        self.assertTrue(backend.preflight(self.fire_off).ok)


class ExecuteLocalTests(Case):
    def test_writes_token_consume_reads_it_and_the_run_passes(self) -> None:
        backend, fake = self.make()
        mutation, evidence = self.fire(backend)
        self.assertTrue(fake.token_at_start)
        self.assertTrue(fake.consume_result)
        self.assertFalse(self.token.exists())
        self.assertEqual(evidence.evidence["maintenance_bypass_token"], "consumed")
        self.assertEqual(mutation.before["maintenance_bypass_token"], "written")
        self.assertEqual(mutation.before["persona"], "recovery")
        self.assertEqual(mutation.before["persona_state_root"], str(self.state_root))

    def test_token_file_mode_is_0600_and_it_is_not_world_readable(self) -> None:
        seen = {}
        real = pim._write_bypass_token

        def spy(*a, **k):
            path = real(*a, **k)
            seen["mode"] = os.stat(path).st_mode & 0o777
            return path

        backend, _ = self.make()
        with mock.patch.object(pim, "_write_bypass_token", spy):
            self.fire(backend)
        self.assertEqual(seen["mode"], 0o600)

    def test_token_surviving_the_fire_is_removed_and_fails_the_op(self) -> None:
        backend, fake = self.make(reads=False)
        with self.assertRaisesRegex(OperationFailed, "consume did not read the maintenance bypass token"):
            self.fire(backend)
        self.assertTrue(fake.token_at_start)
        self.assertFalse(self.token.exists())
        self.assertTrue([c for c in fake.log if c[2:3] == ("stop",)])  # existing cleanup ran

    def test_exception_during_the_fire_removes_the_token(self) -> None:
        backend, fake = self.make(reads=False, start_rc=1)
        with self.assertRaisesRegex(OperationFailed, "start refused"):
            self.fire(backend)
        self.assertTrue(fake.token_at_start)
        self.assertFalse(self.token.exists())

    def test_pre_existing_token_is_refused_and_not_overwritten(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.token.write_text("someone else's token", encoding="utf-8")
        backend, fake = self.make()
        with self.assertRaisesRegex(OperationFailed, "refusing to overwrite"):
            self.fire(backend)
        self.assertEqual(self.token.read_text(encoding="utf-8"), "someone else's token")
        self.assertEqual([c for c in fake.log if c[2:3] == ("start",)], [])

    def test_maintenance_lapsed_before_the_write_means_no_token_and_a_normal_fire(self) -> None:
        backend, fake = self.make(armed=(True, False))
        mutation, evidence = self.fire(backend)
        self.assertFalse(fake.token_at_start)
        self.assertFalse(self.token.exists())
        self.assertEqual(evidence.evidence["maintenance_bypass_token"], "not-needed-maintenance-lapsed")
        self.assertNotIn("maintenance_bypass_token", mutation.before)

    def test_default_run_is_untouched_no_token_no_evidence_key(self) -> None:
        backend, fake = self.make(armed=(False,))
        _mutation, evidence = self.fire(backend, self.fire_off)
        self.assertFalse(fake.token_at_start)
        self.assertNotIn("maintenance_bypass_token", evidence.evidence)

    def test_armed_without_bypass_still_refuses_at_execute(self) -> None:
        backend, fake = self.make()
        with self.assertRaisesRegex(OperationFailed, "seat maintenance"):
            self.fire(backend, self.fire_off)
        self.assertEqual([c for c in fake.log if c[2:3] == ("start",)], [])


class TokenShapeTests(unittest.TestCase):
    def test_payload_round_trips_through_real_consume_as_valid(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pim._write_bypass_token(root, "recovery", "persona_installer connective.unit-fire")
            payload = json.loads(pb.maintenance_bypass_path(root, "recovery").read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"persona", "expires", "by"})
            expires = datetime.strptime(payload["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            remaining = (expires - datetime.now(timezone.utc)).total_seconds()
            self.assertTrue(0 < remaining <= pb.MAINTENANCE_BYPASS_MAX_TTL_S, remaining)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(pb.consume_maintenance_bypass(root, "recovery"))
            self.assertFalse(pb.maintenance_bypass_path(root, "recovery").exists())

    def test_expiry_uses_the_live_clock_not_a_pinned_now(self) -> None:
        early = pim._bypass_token_payload("recovery", "x", now=datetime(2001, 1, 1, tzinfo=timezone.utc))
        live = pim._bypass_token_payload("recovery", "x")
        self.assertTrue(early["expires"].startswith("2001-01-01T00:03:00"))
        self.assertGreater(live["expires"], "2026-")
        self.assertGreater(live["expires"], early["expires"])

    def test_other_persona_token_is_refused_by_consume(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pim._write_bypass_token(Path(tmp), "recovery", "x")
            (Path(tmp) / "mason.maintenance-bypass.json").write_bytes(
                pb.maintenance_bypass_path(Path(tmp), "recovery").read_bytes()
            )
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(pb.consume_maintenance_bypass(Path(tmp), "mason"))


class RollbackLocalTests(Case):
    def written_mutation(self):
        backend, _ = self.make()
        mutation, _ = self.fire(backend)
        return backend, mutation

    def rollback(self, backend, mutation):
        backend.rollback_mutation(mutation, allowed_roots=(str(self.root),))

    def test_rollback_removes_a_leftover_token_when_the_receipt_says_we_wrote_it(self) -> None:
        backend, mutation = self.written_mutation()
        self.token.write_text("leftover", encoding="utf-8")
        self.assertTrue(backend.can_rollback(mutation).ok)
        self.rollback(backend, mutation)
        self.assertFalse(self.token.exists())

    def test_rollback_leaves_a_token_when_the_receipt_does_not_show_a_write(self) -> None:
        backend, mutation = self.written_mutation()
        # a receipt WITHOUT the written marker (a plain launch identity whose session is gone)
        plain = dataclasses.replace(
            mutation,
            before={"launched": "true", "pane_pid": "999999999", "pane_start_ticks": "1",
                    "persona": "recovery", "persona_state_root": str(self.state_root)},
        )
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.token.write_text("an operator's token", encoding="utf-8")
        self.rollback(backend, plain)
        self.assertEqual(self.token.read_text(encoding="utf-8"), "an operator's token")

    def test_rollback_does_not_trust_a_receipt_path_outside_the_allowed_roots(self) -> None:
        backend, mutation = self.written_mutation()
        forged = dataclasses.replace(
            mutation, before={**mutation.before, "persona_state_root": "/etc"}
        )
        with self.assertRaises(OperationFailed):
            self.rollback(backend, forged)


class SimulatedTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.case = self
        self.on = self.fire_on
        self.off = self.fire_off
        self.ctx = conv.context(str(self.root))

    def sim(self, *, armed=True, deep=False, reads=True):
        backend = SimulatedHostBackend()
        backend.in_maintenance = armed
        backend.deep_sleeping = deep
        backend.consume_reads_token = reads
        return backend

    def test_dry_run_matrix(self) -> None:
        self.assertTrue(self.sim().preflight(self.on).ok)
        self.assertIn("one-shot bypass token will be written", self.sim().preflight(self.on).detail)
        refused = self.sim().preflight(self.off)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.detail, pim.MAINTENANCE_REFUSAL.format(persona="recovery"))
        self.assertIn("deep-sleep", self.sim(deep=True).preflight(self.on).detail)
        self.assertTrue(self.sim(armed=False).preflight(self.off).ok)

    def test_execute_writes_and_consumes_the_token(self) -> None:
        backend = self.sim()
        mutation, evidence = backend.execute(self.on, self.ctx)
        self.assertIsNone(backend.bypass_token)
        self.assertEqual(evidence.evidence["maintenance_bypass_token"], "consumed")
        self.assertEqual(mutation.before["maintenance_bypass_token"], "written")

    def test_token_surviving_fails_the_op_and_leaves_nothing(self) -> None:
        backend = self.sim(reads=False)
        with self.assertRaisesRegex(OperationFailed, "consume did not read the maintenance bypass token"):
            backend.execute(self.on, self.ctx)
        self.assertIsNone(backend.bypass_token)

    def test_preexisting_token_is_refused_and_kept(self) -> None:
        backend = self.sim()
        backend.bypass_token = "foreign"
        with self.assertRaisesRegex(OperationFailed, "refusing to overwrite"):
            backend.execute(self.on, self.ctx)
        self.assertEqual(backend.bypass_token, "foreign")

    def test_lapsed_maintenance_writes_no_token(self) -> None:
        backend = self.sim()
        backend.maintenance_lapses_before_write = True
        mutation, evidence = backend.execute(self.on, self.ctx)
        self.assertEqual(evidence.evidence["maintenance_bypass_token"], "not-needed-maintenance-lapsed")
        self.assertNotIn("maintenance_bypass_token", mutation.before)

    def test_rollback_is_conditional_on_the_receipt_recording_a_write(self) -> None:
        backend = self.sim()
        mutation, _ = backend.execute(self.on, self.ctx)
        backend.bypass_token = "leftover"
        backend.rollback_mutation(mutation, allowed_roots=(str(self.case.root),))
        self.assertIsNone(backend.bypass_token)
        plain = dataclasses.replace(mutation, before={"state": "not-applicable"})
        backend.bypass_token = "operator's"
        backend.rollback_mutation(plain, allowed_roots=(str(self.case.root),))
        self.assertEqual(backend.bypass_token, "operator's")


if __name__ == "__main__":
    unittest.main()
