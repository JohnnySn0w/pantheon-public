"""T-4449: the scheduled nightly must not share a concurrency group with push runs.

With `group: <workflow>-<ref>`, a push to main within the nightly's run cancelled it
(cancel-in-progress), and the py3.11 leg, which runs only on schedule/dispatch, went
unmeasured on 3 of 21 nights. Keying the group by event keeps each event on its own lane.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _group() -> str:
    doc = yaml.safe_load(CI.read_text())
    return doc["concurrency"]["group"]


class ScheduleHasItsOwnConcurrencyGroupTest(unittest.TestCase):
    def test_group_is_keyed_by_event_as_well_as_ref(self) -> None:
        group = _group()
        self.assertIn("${{ github.ref }}", group)
        self.assertIn("${{ github.event_name }}", group)

    def test_schedule_and_push_on_main_resolve_to_different_groups(self) -> None:
        group = _group()

        def render(event: str, ref: str) -> str:
            return (group.replace("${{ github.workflow }}", "CI")
                         .replace("${{ github.ref }}", ref)
                         .replace("${{ github.event_name }}", event))

        self.assertNotIn("${{", render("push", "refs/heads/main"))
        self.assertNotEqual(render("schedule", "refs/heads/main"), render("push", "refs/heads/main"))
        self.assertNotEqual(render("workflow_dispatch", "refs/heads/main"), render("push", "refs/heads/main"))


if __name__ == "__main__":
    unittest.main()
