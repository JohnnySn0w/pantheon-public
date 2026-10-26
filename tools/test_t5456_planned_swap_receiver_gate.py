"""T-5456: the planned-quota (95%) transition is readiness-gated for comms seats.

Applies the T-5451 shared predicate (`runtime_controller._comms_receiver_ready`) to
`persona_bringup.quota_check`, closing the asymmetry with the mid-session cap swap.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import mailbox, persona_bringup, runtime_controller as rc
from tools.test_runtime_controller import (
    REPO_ROOT, _Adapter, _binding, _quota_probe_result, _quota_snapshot, _telegram_comms,
)
from tools.test_subscription_usability import snap
from tools.test_subscription_usability_wiring import NOW
from tools import subscription_usability as su

STARTED = SimpleNamespace(status="started", phase="preparing")
EVENT = "planned_swap_receiver_not_ready"
STAMP = "planned-swap-receiver-not-ready-alert.json"


class Rig:
    def __init__(self, tc: unittest.TestCase, *, comms: bool = True, fallbacks=("codex/a", "codex/b")):
        tmp = tempfile.TemporaryDirectory(prefix="t5456-")
        tc.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.primary = _binding(
            self.root, harness="claude", profile="primary", fallbacks=fallbacks,
            communications=_telegram_comms() if comms else None,
        )
        self.a = _binding(self.root, harness="codex", profile="a")
        self.b = _binding(self.root, harness="codex", profile="b")
        self.claude_b = _binding(self.root, harness="claude", profile="b")
        mailbox.provision(self.primary.mailbox)
        self.probed: list[str] = []

    def deps(self):
        def codex(binding):
            self.probed.append(su.runtime_ref(binding))
            return snap(binding)

        return su.ProbeDeps(codex_snapshot=codex)

    def check(self, now=NOW, *, gated: bool = True, fallback=None):
        probe = _quota_probe_result(self.primary, self.state)
        snapshot = _quota_snapshot(
            self.primary, self.root,
            evidence={"authoritative_limit": "false", "provider": "claude", "path": probe.statusline_path,
                      "sha256": str(probe.statusline_sha256), "session_id": probe.session_id},
        )
        kwargs = dict(fallback_bindings=(self.a, self.b), probe_deps=self.deps()) if gated else {"fallback_binding": fallback}
        return persona_bringup.quota_check(
            self.primary, adapter=_Adapter(self.primary, snapshot), primary_binding=self.primary,
            state_dir=self.state, cwd=self.root, now=now,
            claude_probe_runner=lambda **_: probe, manifest_path=REPO_ROOT / "instance.yaml", **kwargs,
        )

    def evidence_count(self) -> int:
        path = rc._evidence_log_path("recovery", self.state)
        return path.read_text().count(EVENT) if path.exists() else 0


def _patches(ready):
    return (
        mock.patch.object(rc, "_comms_receiver_ready", **ready),
        mock.patch.object(rc, "begin_planned_transition", return_value=STARTED),
        mock.patch.object(rc, "_route_failure_alert"),
    )


class PlannedSwapReceiverGateTests(unittest.TestCase):
    def test_not_ready_refuses_alerts_once_per_cooldown(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (False, "release not at pin")})
        with ready, begin as begin_m, alert as alert_m:
            first = r.check()
            second = r.check(now=NOW + timedelta(minutes=1))
            self.assertEqual(second.decision, "receiver-not-ready")
            self.assertEqual(alert_m.call_count, 1)
            rc._atomic_json(
                rc._persona_state("recovery", r.state) / STAMP,
                {"last_alert_at": rc._iso(NOW - timedelta(seconds=rc.PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS + 1))},
            )
            r.check(now=NOW + timedelta(minutes=2))
            self.assertEqual(alert_m.call_count, 2)
        begin_m.assert_not_called()
        self.assertEqual(first.decision, "receiver-not-ready")
        self.assertFalse(first.acted)
        self.assertIn("release not at pin", first.evidence["receiver_not_ready"])
        self.assertEqual(r.evidence_count(), 2)
        self.assertTrue((rc._persona_state("recovery", r.state) / STAMP).exists())
        self.assertFalse(rc._transition_path("recovery", r.state).exists())

    def test_first_run_emits_exactly_one_event(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready, begin, alert:
            r.check()
            r.check(now=NOW + timedelta(minutes=1))
        self.assertEqual(r.evidence_count(), 1)

    def test_ready_starts_transition_as_today(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (True, "")})
        with ready as ready_m, begin as begin_m, alert as alert_m:
            result = r.check()
        ready_m.assert_called_once()
        begin_m.assert_called_once()
        alert_m.assert_not_called()
        self.assertEqual(result.decision, "planned-transition-started")
        self.assertTrue(result.acted)

    def test_non_comms_never_calls_predicate(self):
        r = Rig(self, comms=False)
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready as ready_m, begin as begin_m, alert:
            result = r.check()
        ready_m.assert_not_called()
        begin_m.assert_called_once()
        self.assertEqual(result.decision, "planned-transition-started")

    def test_claude_to_claude_fallback_not_gated(self):
        r = Rig(self, fallbacks=("claude/b",))
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready as ready_m, begin as begin_m, alert:
            result = r.check(gated=False, fallback=r.claude_b)
        ready_m.assert_not_called()
        begin_m.assert_called_once()
        self.assertEqual(result.decision, "planned-transition-started")

    def test_legacy_codex_fallback_is_gated(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready, begin as begin_m, alert:
            result = r.check(gated=False, fallback=r.a)
        begin_m.assert_not_called()
        self.assertEqual(result.decision, "receiver-not-ready")

    def test_predicate_raises_is_not_ready(self):
        r = Rig(self)
        ready, begin, alert = _patches({"side_effect": RuntimeError("boom")})
        with ready, begin as begin_m, alert:
            result = r.check()
        begin_m.assert_not_called()
        self.assertEqual(result.decision, "receiver-not-ready")
        self.assertIn("boom", result.evidence["receiver_not_ready"])

    def test_all_codex_fallbacks_refuse_before_probe_spend(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready, begin, alert:
            result = r.check()
        self.assertEqual(result.decision, "receiver-not-ready")
        self.assertEqual(r.probed, [])

    def test_ready_probes_as_today(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (True, "")})
        with ready, begin, alert:
            r.check()
        self.assertTrue(r.probed)

    def test_alert_helper_failure_still_refuses(self):
        r = Rig(self)
        ready, begin, alert = _patches({"return_value": (False, "x")})
        with ready, begin as begin_m, alert as alert_m:
            alert_m.side_effect = RuntimeError("alert down")
            result = r.check()
        begin_m.assert_not_called()
        self.assertEqual(result.decision, "receiver-not-ready")


if __name__ == "__main__":
    unittest.main()
