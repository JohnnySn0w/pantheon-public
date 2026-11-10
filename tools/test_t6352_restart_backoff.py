"""T-6352 — network-facing units restart with exponential backoff and a start limit.

Before T-6352 these units ran Restart=always with RestartSec=5 and no StartLimit, so
systemd's default 5-starts-in-10s limit could never trip at 5s spacing: a dead
upstream meant a reconnect attempt every 5s, forever (and, where OnFailure is wired,
an alert per cycle). The fix is RestartSteps/RestartMaxDelaySec plus a StartLimit
that ends a sustained failure in ONE latched failed state.

Measured on swarm's systemd 255 (T-6352 probe): NRestarts, which drives the backoff
step, is CUMULATIVE. It does not reset after a healthy run, only on a manual
start/restart. So RestartMaxDelaySec is the steady per-crash delay of a long-lived
unit and must stay small (a comms gap per crash), which this test pins.

pantheon-runtime-controller@ is deliberately excluded: its exits are swap-on-death
control signals (ORG-068), and backoff would delay every runtime swap.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = [
    "tools/systemd/pantheon-acp-bridge@.service",
    "harness/codex/templates/codex-telegram-poller.service",
    "harness/codex/mirror/shadows/templates/codex-telegram-poller.service",
    "harness/codex/templates/codex-seat-delivery.service",
    "tools/systemd/claude-telegram.service",
    "tools/systemd/claude-telegram-research.service",
    "hosts/swarm/systemd/claude-telegram-exocortex.service",
    "hosts/swarm/systemd/exocortex.service",
    "tools/systemd/agent-monitor.service",
    "tools/systemd/pantheon-health-watcher.service",
    "harness/hermes/systemd/pantheon-deepseek-egress-proxy.service",
    "tools/systemd/system/mosquitto.service.d/zz-restart-always.conf",
    # T-6359 follow-up
    "tools/host_control/dist/pantheon-host-approval-telegram-ingress.service",
    "tools/a2a/specialist-a2a.service",
    "tools/echo-tap-recorder/echo-tap-recorder.service",
]
MAX_STEADY_DELAY_S = 120   # per-crash comms gap once NRestarts has ramped (cumulative)


def _sections(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    cur = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = out.setdefault(line[1:-1], {})
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return out


def _seconds(v: str) -> float:
    v = v.strip()
    for suf, mul in (("min", 60), ("ms", 0.001), ("s", 1), ("h", 3600)):
        if v.endswith(suf):
            return float(v[: -len(suf)]) * mul
    return float(v)


def _start_times(first: float, steps: int, cap: float, horizon: float) -> list[float]:
    """Start timestamps of a unit that fails instantly every time, per systemd's
    RestartSteps interpolation: delay_n = first * (cap/first)^(n/steps), n capped."""
    t, times, n = 0.0, [0.0], 0
    while True:
        n_eff = min(n, steps)
        t += first * (cap / first) ** (n_eff / steps)
        if t > horizon:
            return times
        times.append(t)
        n += 1


def _trip_time(times: list[float], interval: float, burst: int) -> float | None:
    """First start that systemd refuses: more than `burst` starts within `interval`."""
    for i in range(burst, len(times)):
        if times[i] - times[i - burst] <= interval:
            return times[i]
    return None


class RestartBackoffContract(unittest.TestCase):
    def test_units_exist(self):
        for rel in UNITS:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_backoff_present_and_steady_delay_small(self):
        for rel in UNITS:
            svc = _sections(ROOT / rel).get("Service", {})
            with self.subTest(unit=rel):
                self.assertIn(svc.get("Restart"), ("always", "on-failure"), "premise changed")
                self.assertIn("RestartSteps", svc, "no exponential backoff")
                self.assertGreater(int(svc["RestartSteps"]), 0)
                self.assertLessEqual(_seconds(svc["RestartMaxDelaySec"]), MAX_STEADY_DELAY_S)
                self.assertLessEqual(_seconds(svc.get("RestartSec", "0.1")),
                                     _seconds(svc["RestartMaxDelaySec"]))

    def test_start_limit_in_unit_section_and_trips_on_sustained_failure(self):
        for rel in UNITS:
            sec = _sections(ROOT / rel)
            unit, svc = sec.get("Unit", {}), sec.get("Service", {})
            with self.subTest(unit=rel):
                # StartLimit* in [Service] is silently ignored by systemd: pin the section.
                self.assertNotIn("StartLimitBurst", svc)
                self.assertIn("StartLimitIntervalSec", unit)
                self.assertIn("StartLimitBurst", unit)
                interval = _seconds(unit["StartLimitIntervalSec"])
                burst = int(unit["StartLimitBurst"])
                times = _start_times(_seconds(svc.get("RestartSec", "0.1")),
                                     int(svc["RestartSteps"]),
                                     _seconds(svc["RestartMaxDelaySec"]), 6 * 3600)
                trip = _trip_time(times, interval, burst)
                # Bounded: a dead upstream ends in a latched failure within 90 min ...
                self.assertIsNotNone(trip, "start limit can never trip under this backoff")
                self.assertLessEqual(trip, 90 * 60)
                # ... but a short outage (the backoff ramp plus 15 min) does not latch.
                self.assertGreater(trip, 15 * 60)

    def test_installer_rendered_codex_poller_unit(self):
        """The control-plane codex poller is rendered in code, not from a template."""
        import sys, tempfile
        sys.path.insert(0, str(ROOT / "tools"))
        import codex_telegram_bridge as bridge
        text = bridge.render_unit(type("B", (), {"effective_pane": "x:0"})())
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as fh:
            fh.write(text)
        sec = _sections(Path(fh.name))
        Path(fh.name).unlink()
        self.assertEqual(sec["Service"].get("RestartSteps"), "5")
        self.assertEqual(sec["Service"].get("RestartMaxDelaySec"), "120")
        self.assertEqual(sec["Unit"].get("StartLimitBurst"), "30")
        self.assertEqual(sec["Unit"].get("StartLimitIntervalSec"), "3600")

    def test_runtime_controller_stays_out_of_backoff(self):
        svc = _sections(ROOT / "tools/systemd/pantheon-runtime-controller@.service")["Service"]
        self.assertNotIn("RestartSteps", svc,
                         "controller exits are swap signals; backoff delays runtime swaps")


if __name__ == "__main__":
    unittest.main()
