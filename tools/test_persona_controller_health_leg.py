"""Tests for `tools.persona_controller_health_leg` (T-5137)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import persona_controller_health_leg as leg

REPO = Path(__file__).resolve().parent.parent
LEG_KEYS = {
    "ContractRevision", "Status", "Subcomponents", "Reasons",
    "Revisions", "InvariantListRef", "SecurityDrift",
}
SOURCED = "ORG-099/ticket-persona-registered"


def _binding(role="head-engineering-orchestrator", harness="claude"):
    return SimpleNamespace(role=role, selected_harness=harness, persona="orchestrator")


def _snap(**kw):
    kw.setdefault("binding", _binding())
    kw.setdefault("role_map", {"orchestrator": "x"})
    kw.setdefault("muted_fn", lambda persona: False)
    return leg.snapshot("orchestrator", **kw)


class ContractTests(unittest.TestCase):
    def test_ref_equals_harness_manager_emitter_ref(self) -> None:
        # ORG-080 D-11(b): the computed ref must equal the yaml-pinned one.
        text = (REPO / "tools/germinate/components/harness-manager.yaml").read_text()
        pinned = re.search(r"invariant_list_ref:\s*(\S+)", text).group(1)
        self.assertEqual(leg.invariant_list_ref(), pinned)

    def test_contract_revision_mirrors_health_contract(self) -> None:
        from tools.host_control import health

        self.assertEqual(leg.CONTRACT_REVISION, health.CONTRACT_REVISION)

    def test_keys_and_types(self) -> None:
        out = _snap()
        self.assertEqual(set(out), LEG_KEYS)
        self.assertIsInstance(out["SecurityDrift"], bool)
        for k in ("Subcomponents", "Reasons", "Revisions"):
            self.assertIsInstance(out[k], dict)
            self.assertTrue(all(isinstance(a, str) and isinstance(b, str) for a, b in out[k].items()))
        self.assertEqual(out["Revisions"]["invariant_list"], out["InvariantListRef"])
        self.assertEqual(out["ContractRevision"], "pantheon-health/v1")


class ProjectionTests(unittest.TestCase):
    def test_status_mapping(self) -> None:
        results = [
            {"id": "a", "status": "pass", "on_fail": "unhealthy", "detail": "ok"},
            {"id": "b", "status": "vacuous", "on_fail": "degraded", "detail": "n/a"},
            {"id": "c", "status": "fail", "on_fail": "degraded", "detail": "bad"},
            {"id": "d", "status": "alarm", "on_fail": "unhealthy", "detail": "no obs"},
        ]
        rolled = {"state": "unhealthy", "reasons": {"c": "bad", "d": "no obs"}, "security_drift": False}
        out = leg.project(results, rolled, "ref@x")
        self.assertEqual(
            out["Subcomponents"],
            {"a": "ready", "b": "not_configured", "c": "degraded", "d": "unhealthy"},
        )
        self.assertEqual(out["Reasons"], {"c": "bad", "d": "no obs"})
        self.assertEqual(out["Status"], "unhealthy")

    def test_dark_state_all_unsourced_reads_unhealthy(self) -> None:
        # Pins the accepted dark state: unsourced HARD axes alarm, rollup => unhealthy.
        out = _snap()
        self.assertEqual(out["Status"], "unhealthy")
        self.assertIn("ORG-099/permissions-allow-populated", out["Reasons"])
        self.assertIn("could not ask", out["Reasons"]["ORG-099/permissions-allow-populated"])

    def test_muted_is_suppressed_but_reasons_kept(self) -> None:
        out = _snap(muted_fn=lambda persona: True)
        self.assertEqual(out["Status"], "suppressed")
        self.assertTrue(out["Reasons"])

    def test_no_binding_is_not_configured(self) -> None:
        with mock.patch.object(leg, "_load_binding", return_value=None):
            out = leg.snapshot("orchestrator", binding=None, role_map={}, muted_fn=lambda p: False)
        self.assertEqual(out["Status"], "not_configured")


class SourcedFactTests(unittest.TestCase):
    def test_registered_persona_passes(self) -> None:
        self.assertEqual(_snap(role_map={"orchestrator": "r"})["Subcomponents"][SOURCED], "ready")
        self.assertNotIn(SOURCED, _snap(role_map={"orchestrator": "r"})["Reasons"])

    def test_unregistered_persona_fails_with_on_fail(self) -> None:
        out = _snap(role_map={"other": "r"})
        self.assertEqual(out["Subcomponents"][SOURCED], "unhealthy")
        self.assertIn(SOURCED, out["Reasons"])

    def test_unsourced_mailbox_stays_alarm(self) -> None:
        # axis needs hub dir AND a getfacl grant; a bare stat is not the same claim.
        out = _snap()
        self.assertIn("ORG-099/mailbox-inbox-exists", out["Reasons"])


class FailOpenTests(unittest.TestCase):
    def test_evaluation_exception_yields_degraded_shape(self) -> None:
        with mock.patch.object(leg, "_evaluate", side_effect=RuntimeError("boom")):
            out = _snap()
        self.assertEqual(set(out), LEG_KEYS)
        self.assertEqual(out["Status"], "degraded")
        self.assertEqual(out["Reasons"], {"health-leg": "health leg evaluation failed"})
        self.assertEqual(out["Subcomponents"], {})
        self.assertIs(out["SecurityDrift"], False)


if __name__ == "__main__":
    unittest.main()
