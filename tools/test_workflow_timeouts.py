"""Every self-hosted GitHub Actions job must bound its own runtime (T-4170).

pantheon-config has ONE pve2 runner. A job with no ``timeout-minutes`` inherits
GitHub's 360-minute default, so a Runner.Worker that hangs at startup holds the
whole fleet's CI queue for hours (2026-09-28: a 5-second HERMES scan held it for
57 minutes with ~20 runs queued behind it). A new workflow must not quietly
reintroduce that.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
MAX_MINUTES = 60


def _self_hosted_jobs():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (doc.get("jobs") or {}).items():
            runs_on = job.get("runs-on")
            labels = runs_on if isinstance(runs_on, list) else [runs_on]
            if "self-hosted" in labels:
                yield path.name, name, job


class SelfHostedJobsAreBoundedTest(unittest.TestCase):
    def test_the_sweep_is_not_vacuous(self) -> None:
        # Floor 3, was 4: T-4186 retired cascade-on-merge.yml (it could never push), leaving
        # ci.yml, hermes-audit.yml and adr-scoping-lint.yml. The floor exists only to prove the
        # glob finds real jobs, not to pin the workflow count.
        self.assertGreaterEqual(len(list(_self_hosted_jobs())), 3)

    def test_every_self_hosted_job_sets_a_bounded_timeout(self) -> None:
        for workflow, name, job in _self_hosted_jobs():
            with self.subTest(workflow=workflow, job=name):
                minutes = job.get("timeout-minutes")
                self.assertIsInstance(minutes, int, "missing timeout-minutes (360-min default)")
                self.assertGreater(minutes, 0)
                self.assertLessEqual(minutes, MAX_MINUTES)


if __name__ == "__main__":
    unittest.main()
