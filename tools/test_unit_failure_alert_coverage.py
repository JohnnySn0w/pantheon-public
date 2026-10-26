"""Every systemd unit must ALERT on failure, or say why not (T-2264 / T-2271).

T-2264 wired most units; T-2271 wired the last three. Neither stopped the NEXT
unit landing unwired, and that is the whole failure mode: `agent-monitor.service`
failed 35 times in 14 days — `timeout` and `oom-kill`, on a droplet already
force-restarted once for memory pressure — and nobody was ever told, because it
had no OnFailure. The unit with the most failures was the one with no alerting,
and it stayed that way for as long as nothing checked.

So the rule is enforced rather than swept: a unit either carries
`OnFailure=pantheon-unit-failure-alert@%n.service`, or it names itself here with a
reason. An exclusion that has to be written down twice — once in the unit, once in
this list — is one a reviewer can actually see.

Deliberately a WHITELIST of exclusions rather than a count. A count passes when a
new unit is added and an old exclusion is deleted, which is the arithmetic that
lets coverage drift while the number stays put.
"""
import pathlib
import re
import unittest

SYSTEMD = pathlib.Path(__file__).resolve().parent / "systemd"
ONFAILURE = "OnFailure=pantheon-unit-failure-alert@%n.service"
# ⚠️ Match the ACTIVE directive — a line that IS the setting — not the string
# anywhere in the file. pantheon-unit-failure-alert@.service quotes it in a
# comment explaining how referrers use it, and a substring check read that as
# "wired". Second time today I wrote an assertion satisfiable by a comment; it is
# the same defect the rest of this arc is about, so the pattern is pinned to the
# line start under re.M.
_WIRED = re.compile(r"^OnFailure=pantheon-unit-failure-alert@%n\.service\s*$", re.M)


def _is_wired(path) -> bool:
    return bool(_WIRED.search(path.read_text(encoding="utf-8")))

# unit -> why it must NOT alert. Each also documents the reason in-file.
EXCLUDED = {
    "pantheon-unit-failure-alert@.service":
        "it IS the alerter — alerting on its own failure is the loop it exists to break",
    "pantheon-resource-sampler.service":
        "an evidence recorder must not make the mailbox a dependency of recording",
    "pantheon-agent-state-snapshot.service":
        "same reason as resource-sampler — an evidence recorder must not make the "
        "mailbox a dependency of recording, and this one snapshots the memory dir "
        "the alert path itself reads from (T-2424). A missed snapshot is visible as "
        "a gap in `git log` and in the journal; it is not an operational incident. "
        "Does NOT extend to detectors or restart-capable units.",
    "pantheon-tui-wedge-sampler.service":
        "a temporary diagnostic recorder has no operational duty; a capture gap "
        "invalidates the measured run instead of requiring an alert",
    "pantheon-bridge.service":
        "verified dead: disabled + inactive, code archived at /opt/pantheon-bridge.archived",
    "pantheon-vps-state-backup.service":
        "system/root unit -- it reads every account's home + the kino DB, which no "
        "user-scoped unit can, so it cannot run as the account both the OnFailure "
        "alerter and failed-unit-reconcile assume (both are per-account systemctl "
        "--user, neither covers a system unit). A backup's health signal is commit-"
        "freshness, not unit status: any failure (unit, hang, push, auth, box-down) "
        "shows as a missing daily commit in the backup repo. The staleness check that "
        "alerts on that is T-2886 (T-2178 follow-up).",
    "pantheon-metrics-push.service":
        "self-healing high-cadence pusher (~90s fire). A single missed push recovers "
        "on the next fire, and specialist's >2min stale-fallback covers the gap by design "
        "(T-3626 / ORG-098 BT2). Routing transient ssh/network blips to the mailbox "
        "would spam. Failures still land in the journal (StandardError=journal) for "
        "inspection. The unit file itself carries the equivalent comment.",
}


class EveryUnitAlertsOrSaysWhyNot(unittest.TestCase):
    def setUp(self):
        self.units = sorted(p for p in SYSTEMD.glob("*.service"))
        self.assertTrue(self.units, f"no unit files under {SYSTEMD} — this guard "
                                    "would pass vacuously on an empty glob")

    def test_every_unit_is_wired_or_explicitly_excluded(self):
        unwired = [p.name for p in self.units if not _is_wired(p)]
        self.assertEqual(sorted(unwired), sorted(EXCLUDED),
                         "a unit is unwired and not on the exclusion list (or an "
                         "exclusion was wired without being removed from it). A "
                         "unit that cannot report its own failure is invisible "
                         "exactly when it matters.")

    def test_each_exclusion_states_its_reason_in_the_unit_itself(self):
        """The list here is not enough: someone reading the UNIT must see why it is
        silent, or the omission reads as an oversight and gets 'fixed'."""
        for name in EXCLUDED:
            body = (SYSTEMD / name).read_text(encoding="utf-8")
            self.assertRegex(
                body, r"(?i)#.*(deliberate|exclusion|archived|dead|does not alert|"
                      r"must not|itself)",
                f"{name} is excluded but carries no in-file reason")

    def test_excluded_units_all_exist(self):
        """A stale exclusion silently re-opens a hole: the unit is gone, the name
        lingers, and the next unit to take that name inherits an exemption."""
        for name in EXCLUDED:
            self.assertTrue((SYSTEMD / name).is_file(),
                            f"{name} is on the exclusion list but does not exist")

    def test_the_three_T2271_units_are_wired(self):
        """Pinned by name because they were the residual, and because the evidence
        for wiring them was per-unit rather than general: 0 failures/19 starts,
        0/11, and 35 failures respectively."""
        for name in ("agent-monitor.service", "claude-telegram.service",
                     "claude-telegram-research.service"):
            self.assertTrue(_is_wired(SYSTEMD / name),
                            f"{name} lost its OnFailure (T-2271)")

    def test_seat_units_bound_a_crash_loop(self):
        """OnFailure fires PER FAILURE, so under Restart=always a crash-loop alerts
        every cycle (mason, measured). A StartLimit is what converts that into ONE
        terminal failure — the bound is what makes wiring these safe, so assert it
        rather than trusting it was kept."""
        for name in ("claude-telegram.service", "claude-telegram-research.service"):
            body = (SYSTEMD / name).read_text(encoding="utf-8")
            self.assertIn("Restart=always", body, f"{name}: premise changed")
            self.assertIn("StartLimitIntervalSec=", body,
                          f"{name} is Restart=always with OnFailure and no start "
                          "limit — that is an unbounded alert loop")
            self.assertIn("StartLimitBurst=", body, name)


if __name__ == "__main__":
    unittest.main()
