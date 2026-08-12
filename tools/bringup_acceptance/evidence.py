"""On-disk evidence schema for one bringup-acceptance run.

Layout, fixed by T-2558's ask::

    run/<run_id>/
        run.json
        inventory.json
        modules/<module_id>/
            trace.jsonl          (written by TraceRecorder)
            evidence/*.json      (written by write_module_evidence)
        closure.json
        closure.md
        restore.json

:class:`RunPaths` computes every path in that layout; the ``write_*``/
``read_*`` helpers populate and re-open it.  Actual module evidence
population is later phases -- this phase only builds the plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.bringup_acceptance.closure import ClosureResult, render_closure_markdown
from tools.bringup_acceptance.recorder import validate_token, write_json_evidence
from tools.bringup_acceptance.types import RunContext, TargetInventory


@dataclass(frozen=True)
class RunPaths:
    """Every path in one run's evidence tree, keyed off ``root``/``run_id``."""

    root: Path
    run_id: str

    def __post_init__(self) -> None:
        validate_token(self.run_id, label="run_id")

    @property
    def run_dir(self) -> Path:
        return self.root / self.run_id

    @property
    def run_json(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def inventory_json(self) -> Path:
        return self.run_dir / "inventory.json"

    @property
    def modules_dir(self) -> Path:
        return self.run_dir / "modules"

    def module_dir(self, module_id: str) -> Path:
        validate_token(module_id, label="module_id")
        return self.modules_dir / module_id

    def module_trace_path(self, module_id: str) -> Path:
        return self.module_dir(module_id) / "trace.jsonl"

    def module_evidence_dir(self, module_id: str) -> Path:
        return self.module_dir(module_id) / "evidence"

    @property
    def closure_json(self) -> Path:
        return self.run_dir / "closure.json"

    @property
    def closure_md(self) -> Path:
        return self.run_dir / "closure.md"

    @property
    def restore_json(self) -> Path:
        return self.run_dir / "restore.json"


def write_run_manifest(paths: RunPaths, ctx: RunContext) -> None:
    write_json_evidence(
        paths.run_json,
        {
            "schema": "pantheon-bringup-acceptance-run/v1",
            "run_id": ctx.run_id,
            "persona": ctx.persona,
            "account": ctx.account,
            "harness": ctx.harness,
            "profile": ctx.profile,
            "hub_path": ctx.hub_path,
            "state_path": ctx.state_path,
            "evidence_root": ctx.evidence_root,
            "baseline_ref": ctx.baseline_ref,
        },
    )


def read_run_manifest(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.run_json.read_text(encoding="utf-8"))


def write_inventory(paths: RunPaths, inventory: TargetInventory) -> None:
    write_json_evidence(
        paths.inventory_json,
        {
            "schema": "pantheon-bringup-acceptance-inventory/v1",
            "persona": inventory.persona,
            "entries": [
                {
                    "path": entry.path,
                    "tier": entry.tier,
                    "status": entry.status,
                    "module_id": entry.module_id,
                }
                for entry in inventory.entries
            ],
        },
    )


def read_inventory(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.inventory_json.read_text(encoding="utf-8"))


def write_module_evidence(
    paths: RunPaths, module_id: str, name: str, payload: Mapping[str, Any]
) -> Path:
    """Write one named evidence document for ``module_id`` and return its path."""

    validate_token(name, label="evidence name")
    target = paths.module_evidence_dir(module_id) / f"{name}.json"
    write_json_evidence(target, payload)
    return target


def write_closure(paths: RunPaths, closure: ClosureResult) -> None:
    write_json_evidence(
        paths.closure_json,
        {
            "schema": "pantheon-bringup-acceptance-closure/v1",
            "run_id": closure.run_id,
            "status": closure.status,
            "rows": [
                {
                    "module_id": row.module_id,
                    "tier": row.tier,
                    "status": row.status,
                    "summary": row.summary,
                    "evidence_refs": [str(ref) for ref in row.evidence_refs],
                }
                for row in closure.rows
            ],
            "composite_rows": [
                {
                    "capability_id": composite.capability_id,
                    "status": composite.status,
                    "required_module_ids": list(composite.required_module_ids),
                    "missing_module_ids": list(composite.missing_module_ids),
                }
                for composite in closure.composite_rows
            ],
            "gaps": list(closure.gaps),
        },
    )
    paths.closure_md.parent.mkdir(parents=True, exist_ok=True)
    paths.closure_md.write_text(render_closure_markdown(closure), encoding="utf-8")


def read_closure(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.closure_json.read_text(encoding="utf-8"))


def write_restore(paths: RunPaths, restore_by_module: Mapping[str, Mapping[str, Any]]) -> None:
    write_json_evidence(
        paths.restore_json,
        {
            "schema": "pantheon-bringup-acceptance-restore/v1",
            "modules": dict(restore_by_module),
        },
    )


def read_restore(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.restore_json.read_text(encoding="utf-8"))
