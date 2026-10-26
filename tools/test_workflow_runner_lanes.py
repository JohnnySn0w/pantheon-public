"""pantheon-config's runner lanes (T-4355).

CT202 on pve2 runs two pantheon-config runners: the suite runner (label `pve2`) and a LIGHT
runner (label `light`) for the seconds-long diff scans that used to queue behind the suite.
The light runner never gets `pve2`, so these pins keep the suite off it and the scans on it.
Every self-hosted job also repacks its persistent workspace right after checkout:
actions/checkout sets gc.auto=0, and the suite runner's workspace had grown to 6.3G / 815 packs.
"""

from __future__ import annotations

import shlex
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
LIGHT_WORKFLOWS = ("hermes-audit.yml", "adr-scoping-lint.yml", "registry-lint.yml")
LANE_LABELS = {"pve2", "light"}
GC_STEP = "Keep the workspace repo packed"
# Exact, so a weaker repack can't pass: gc.autoPackLimit is what stops the pack-count growth,
# and the step must be able to fail its job (no `|| true`, continue-on-error or if: always()).
GC_RUN = "git -c gc.auto=6700 -c gc.autoPackLimit=50 -c gc.autoDetach=false gc --auto --quiet"


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


class Unjudgeable(ValueError):
    """A runs-on form whose labels can't be read statically."""


def _runs_on(job: dict) -> list:
    """runs-on as casefolded labels: GitHub matches runner labels case-insensitively, so
    [Self-Hosted, linux, x64] is a bare self-hosted job (T-4384). A form whose labels can't
    be read here raises Unjudgeable, so the lane rule fails closed on it: an expression
    (${{ matrix.runner }}, ${{ inputs.x }}) and the {group, labels} dict (a runner group
    can route to any runner in it)."""
    labels = job.get("runs-on", [])
    if isinstance(labels, dict):
        raise Unjudgeable(f"the {{group, labels}} form {labels!r}")
    labels = [labels] if isinstance(labels, str) else list(labels)
    if any("${{" in str(label) for label in labels):
        raise Unjudgeable(f"an expression in {labels!r}")
    return [str(label).casefold() for label in labels]


def _all_workflows() -> dict:
    return {wf.name: _jobs(wf.name) for wf in sorted(WORKFLOWS.glob("*.y*ml"))}


def lane_violations(workflows: dict) -> list:
    """{workflow: jobs} -> the jobs that break the lane rule: every self-hosted job names
    exactly one lane label, and every runs-on can be judged (fail closed)."""
    out = []
    for wf, jobs in workflows.items():
        for name, job in jobs.items():
            try:
                labels = _runs_on(job)
            except Unjudgeable as exc:
                out.append(f"{wf}:{name}: runs-on can't be judged ({exc}); use a literal label list")
                continue
            if "self-hosted" in labels and len(LANE_LABELS & set(labels)) != 1:
                out.append(f"{wf}:{name}: a self-hosted job must name exactly one lane label, got {labels}")
    return out


def _self_hosted_jobs():
    """Every self-hosted job in every workflow, so a new workflow is covered without a list.
    An unjudgeable runs-on is lane_violations' to report, not skipped silently there."""
    for wf, jobs in _all_workflows().items():
        for name, job in jobs.items():
            try:
                labels = _runs_on(job)
            except Unjudgeable:
                continue
            if "self-hosted" in labels:
                yield wf, name, job


class RunnerLanesTest(unittest.TestCase):
    def test_the_suite_stays_on_the_suite_runner(self) -> None:
        for name, job in _jobs("ci.yml").items():
            with self.subTest(job=name):
                self.assertIn("pve2", job["runs-on"])
                self.assertNotIn("light", job["runs-on"])

    def test_the_diff_scans_run_on_the_light_lane(self) -> None:
        for wf in LIGHT_WORKFLOWS:
            for name, job in _jobs(wf).items():
                with self.subTest(workflow=wf, job=name):
                    self.assertIn("light", job["runs-on"])
                    self.assertNotIn("pve2", job["runs-on"])

    def test_every_self_hosted_job_names_exactly_one_lane(self) -> None:
        # rev-t4355 NIT: a bare [self-hosted, linux, x64] job matches ANY runner on the
        # account, echo's included, so it must name its lane.
        self.assertGreaterEqual(len(list(_self_hosted_jobs())), 3)
        self.assertEqual(lane_violations(_all_workflows()), [])

    def test_the_lane_rule_fails_closed_on_forms_it_cannot_read(self) -> None:
        # T-4384 (rev-t4355 r2 N8-N10): each of these used to escape the rule silently.
        bad = {
            "mixed-case bare self-hosted (N8)": ["Self-Hosted", "linux", "x64"],
            "mixed-case, two lanes": ["SELF-HOSTED", "PVE2", "Light"],
            "expression, whole (N9)": "${{ matrix.runner }}",
            "expression, one label (N9)": ["self-hosted", "${{ inputs.lane }}"],
            "group + labels dict (N10)": {"group": "ci", "labels": ["self-hosted", "light"]},
            "group-only dict (N10)": {"group": "ci"},
        }
        for name, runs_on in bad.items():
            with self.subTest(name):
                self.assertEqual(len(lane_violations({"x.yml": {"j": {"runs-on": runs_on}}})), 1)

    def test_the_lane_rule_accepts_what_it_can_read(self) -> None:
        good = {
            "mixed-case with one lane": ["Self-Hosted", "Linux", "X64", "PVE2"],
            "light lane": ["self-hosted", "linux", "x64", "light"],
            "hosted runner": "ubuntu-latest",
            "reusable-workflow call (no runs-on)": None,
        }
        for name, runs_on in good.items():
            with self.subTest(name):
                job = {"uses": "./.github/workflows/w.yml"} if runs_on is None else {"runs-on": runs_on}
                self.assertEqual(lane_violations({"x.yml": {"j": job}}), [])

    def test_every_self_hosted_job_repacks_right_after_checkout(self) -> None:
        jobs = list(_self_hosted_jobs())
        self.assertGreaterEqual(len(jobs), 3)
        for wf, name, job in jobs:
            with self.subTest(workflow=wf, job=name):
                steps = job["steps"]
                first = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(len(first), 1)
                after = steps[first[0] + 1]
                self.assertEqual(set(after), {"name", "run"})
                self.assertEqual(after["name"], GC_STEP)
                self.assertEqual(after["run"], GC_RUN)

    def test_the_suite_keeps_pytest_temp_dirs_off_the_tmpfs(self) -> None:
        # T-4391: CT202's /tmp is a tmpfs capped at 1M inodes, and retained pytest-N
        # basetemps filled it (intermittent ENOSPC, 2026-09-30). Every whole-tree pytest
        # step puts its basetemp in the job's work dir, outside the repo tree; pytest wipes
        # it at the start of each run.
        runs = [s["run"] for job in _jobs("ci.yml").values() for s in job["steps"]
                if "python -m pytest" in str(s.get("run", ""))]
        self.assertGreaterEqual(len(runs), 1)
        for run in runs:
            with self.subTest(run=run):
                args = shlex.split(run)
                values = [a.split("=", 1)[1] for a in args if a.startswith("--basetemp=")]
                values += [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--basetemp"]
                self.assertEqual(len(values), 1, "exactly one --basetemp")
                self.assertEqual(values[0], "../pytest-basetemp")


if __name__ == "__main__":
    unittest.main()
