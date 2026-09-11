"""tools/bringup_acceptance/optionals/specialist.py — Specialist custom-surface acceptance
modules (T-2556 phase 13 / T-2565).

The FIRST persona plug-in against the T-2561 optionals contract
(:mod:`tools.bringup_acceptance.registry`). Proves the Specialist-specific surface
named in this ticket's ask -- the watch-runner cadence/dispatch machinery, the
specialist unattended auto-commit-and-push loop (CLAUDE.md), and researcher
dispatch -- while a bare/non-Specialist daemon bringup cleanly skips this file's
modules entirely (:func:`_declare_for` below).

declare() persona-gating. :mod:`tools.bringup_acceptance.inventory` carries no
Specialist-specific knowledge -- it never tags any :class:`TargetInventory` entry
for this custom surface -- so the only ground-truth signal available at
declare-time is ``target.persona`` itself. Both modules here declare
``applies`` iff ``target.persona == "specialist"``; any other persona declares
absent. The persona branch lives HERE, in the plug-in, by design -- core
(inventory.py / registry.py) never needs to know Specialist exists.

Off-prod isolation, by module:

* :class:`ArgusWatchRunnerModule` -- proves ``tools/watch_runner.py``'s
  cadence-computation + dispatch-brief-construction contract
  (``compute_due_briefs()`` / ``build_brief()``) against a DISPOSABLE fixture
  ``topics.md`` (the module-level ``watch_runner.TOPICS_FILE`` global is
  monkeypatched for the call's duration, never the real production topic
  list -- so this proof never drifts with topics.md content changes), plus a
  static, read-only wiring check of ``tools/systemd/pantheon-watch.service``/
  ``.timer``. Researcher dispatch is folded in here and proven as
  WIRING/CONTRACT ONLY: the in-session Agent-tool dispatch itself (ORG-025)
  has no off-prod-provable code path short of actually invoking a subagent,
  which this module must never do -- ``report()`` names this explicitly so it
  reads as accounted-for, not silently dropped.
* :class:`ArgusWatchAutocommitModule` -- proves ``watch_runner.git_commit_and_
  push()`` end-to-end against a fully disposable git topology: a scratch
  working repo + a scratch ``git init --bare`` "origin", so the real
  pantheon-config repo/remote is never touched. ``PANTHEON_GIT_MUTATION_
  LOCKFILE`` is env-overridden for each call: ``git_mutation_lock()``'s own
  lockfile resolution is repo-anchored (via ``_git_common_dir()``), NOT
  derived from the caller's ``cwd`` -- without this override, a disposable-
  cwd call would still briefly acquire the REAL repo's shared mutation lock.
  Both directions of the HERMES guard are proven: a clean report lands and
  pushes, and a report carrying the forbidden classifier trigger is refused
  pre-commit with nothing landed on the disposable origin. The trigger is
  built via source-time concatenation (``"HERME" + "S.md"``), mirroring
  ``watch_runner.py``'s own ``_CLASSIFIER_TRIGGER`` convention, so the
  contiguous byte sequence never appears in this file's source.

Neither module touches the real pantheon-config repo, the real mailbox hub,
or invokes a live subagent/systemd unit -- both ``restore()``s are just
"rmtree the scratch tree, assert it's gone" (mirrors ``hook_emissions.py``'s
own "zero real mutation by construction, no restore.py Baseline needed"
precedent).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Mapping

from tools import watch_runner
from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
    Tier,
)


MODULE_ID_WATCH_RUNNER = "specialist-watch-runner"
MODULE_ID_AUTOCOMMIT = "specialist-watch-autocommit"

# Built via source-time concatenation, never contiguous in this file's
# source -- mirrors watch_runner.py's own `_CLASSIFIER_TRIGGER` convention
# (CLAUDE.md byte-sequence discipline).
_CLASSIFIER_TRIGGER = "HERME" + "S.md"

# Padding to clear watch_runner.REPORT_MIN_BYTES (500) alongside the
# frontmatter/heading text every fixture report already carries.
_REPORT_PADDING = "x" * 480

_GIT_MUTATION_LOCKFILE_ENV = "PANTHEON_GIT_MUTATION_LOCKFILE"


def _declare_for(module_id: str, tier: Tier, target: TargetInventory) -> ModuleDeclaration:
    applies = target.persona == "specialist"
    reason = (
        "target persona is 'specialist' -- the Specialist watch-runner/auto-commit/"
        "researcher-dispatch surface is present"
        if applies
        else f"target persona is {target.persona!r}, not 'specialist' -- this surface only "
        "exists on the specialist account; bare/other-persona daemons skip it"
    )
    return ModuleDeclaration(module_id=module_id, tier=tier, applies=applies, reason=reason)


@contextmanager
def _patched_topics_file(path: Path) -> Iterator[None]:
    """Redirect watch_runner.TOPICS_FILE to a disposable fixture for the block's
    duration, restoring the original afterward. Never touches the real
    knowledge/watch/topics.md."""

    original = watch_runner.TOPICS_FILE
    watch_runner.TOPICS_FILE = path
    try:
        yield
    finally:
        watch_runner.TOPICS_FILE = original


@contextmanager
def _lockfile_env(path: Path) -> Iterator[None]:
    """Force PANTHEON_GIT_MUTATION_LOCKFILE to a disposable path for the block's
    duration, restoring whatever was there after. Belt-and-suspenders against
    git_mutation_lock()'s repo-anchored (not cwd-anchored) default resolution
    -- see module docstring."""

    saved = os.environ.get(_GIT_MUTATION_LOCKFILE_ENV)
    os.environ[_GIT_MUTATION_LOCKFILE_ENV] = str(path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_GIT_MUTATION_LOCKFILE_ENV, None)
        else:
            os.environ[_GIT_MUTATION_LOCKFILE_ENV] = saved


def _valid_report_body(nonce: str) -> str:
    """A structurally-valid watch report (frontmatter headline + `## ` heading,
    >=REPORT_MIN_BYTES) -- matches build_brief()'s documented report template."""

    return (
        "---\n"
        "surface: routine\n"
        f"headline: bringup-acceptance disposable probe {nonce}\n"
        "---\n\n"
        f"# Disposable probe report {nonce}\n\n"
        "## Findings\n\n"
        f"Synthetic fixture body for T-2565's off-prod auto-commit proof. {_REPORT_PADDING}\n"
    )


def _report_body_with_trigger(nonce: str) -> str:
    """The same valid report, with the forbidden classifier trigger spliced in --
    used only to prove the HERMES guard's refusal path; never committed anywhere
    (git_commit_and_push refuses it pre-commit) and never written inside
    pantheon-config's own tree (lives entirely under a disposable scratch dir)."""

    base = _valid_report_body(nonce)
    return base.replace(
        "## Findings",
        f"## Findings\n\nReferences {_CLASSIFIER_TRIGGER} directly (probe).",
    )


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20, check=False,
    )


def _init_disposable_repo(scratch: Path, *, label: str) -> tuple[Path, Path]:
    """Stand up one disposable `git init --bare` "origin" + one disposable
    working repo pointing at it. Returns (work_dir, bare_dir). Never touches
    the real pantheon-config repo or any real remote."""

    bare_dir = scratch / f"origin-{label}.git"
    work_dir = scratch / f"work-{label}"
    for args in (
        ["init", "--quiet", "--bare", str(bare_dir)],
        ["init", "--quiet", str(work_dir)],
    ):
        result = _run_git(args)
        if result.returncode != 0:
            raise HardStop(f"specialist-watch-autocommit: git {args} failed: {result.stderr}")
    for args in (
        ["config", "user.email", "bringup-acceptance@example.invalid"],
        ["config", "user.name", "bringup acceptance"],
        ["remote", "add", "origin", str(bare_dir)],
    ):
        result = _run_git(args, cwd=work_dir)
        if result.returncode != 0:
            raise HardStop(f"specialist-watch-autocommit: git {args} in {work_dir} failed: {result.stderr}")
    return work_dir, bare_dir


def _bare_main_log(bare_dir: Path) -> str | None:
    """The commit message at refs/heads/main in a disposable bare repo, or None
    if that ref does not exist (i.e. nothing has landed there yet)."""

    ref_check = _run_git(["show-ref", "--verify", "--quiet", "refs/heads/main"], cwd=bare_dir)
    if ref_check.returncode != 0:
        return None
    log = _run_git(["log", "-1", "--format=%B", "refs/heads/main"], cwd=bare_dir)
    if log.returncode != 0:
        raise HardStop(f"specialist-watch-autocommit: could not read refs/heads/main in {bare_dir}: {log.stderr}")
    return log.stdout


# ---------------------------------------------------------------------------
# specialist-watch-runner
# ---------------------------------------------------------------------------


class ArgusWatchRunnerModule:
    """Proves the watch-runner cadence/dispatch-brief contract + its systemd
    wiring. See module docstring for the researcher-dispatch fold-in and the
    disposable-fixture-topics.md rationale."""

    id = MODULE_ID_WATCH_RUNNER
    tier: Tier = "optional"

    def __init__(self) -> None:
        self._scratch_root: Path | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return _declare_for(self.id, self.tier, target)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)

        # 1. Static wiring proof -- read-only, no live systemctl.
        service_path = watch_runner.REPO_ROOT / "tools" / "systemd" / "pantheon-watch.service"
        timer_path = watch_runner.REPO_ROOT / "tools" / "systemd" / "pantheon-watch.timer"
        service_text = service_path.read_text(encoding="utf-8")
        timer_text = timer_path.read_text(encoding="utf-8")
        if "watch_runner.py" not in service_text or "--file-watch-ticket" not in service_text:
            raise HardStop(
                f"{self.id}: pantheon-watch.service does not wire "
                f"watch_runner.py --file-watch-ticket: {service_text!r}"
            )
        if "pantheon-watch.service" not in timer_text:
            raise HardStop(
                f"{self.id}: pantheon-watch.timer does not target pantheon-watch.service: {timer_text!r}"
            )
        recorder.append("observe", {"phase": "unit-wiring", "status": "wired"})

        # 2. compute_due_briefs()/build_brief() contract, against a DISPOSABLE
        #    fixture topics.md (never the real one -- see module docstring).
        #    Scratch is allocated HERE (not at prove() entry) so an early
        #    HardStop in the wiring proof above cannot leak an unused tmpdir --
        #    restore() only runs on the success path, and callers that test the
        #    hard-stop path (no restore) would otherwise strand the mkdtemp.
        self._scratch_root = Path(tempfile.mkdtemp(prefix=f"bringup-{self.id}-"))
        nonce = f"t2565-{uuid.uuid4().hex[:8]}"
        fixture_root = self._scratch_root / "fixture"
        fixture_root.mkdir(parents=True)
        topics_fixture = fixture_root / "topics.md"
        topics_fixture.write_text(
            "# Watch topics (bringup-acceptance fixture)\n\n"
            f"## {nonce}\n"
            "- **cadence:** daily\n"
            "- **budget:** 300 lines\n"
            "- **scope:** synthetic bringup-acceptance probe topic, not a real watch subject\n"
            "- **seed sources:**\n"
            f"  - https://example.invalid/{nonce}\n"
            "- **notes:** disposable fixture for T-2565's off-prod watch-runner proof\n",
            encoding="utf-8",
        )
        reports_root = self._scratch_root / "reports"

        with _patched_topics_file(topics_fixture):
            records = watch_runner.compute_due_briefs(date.today(), force=True, reports_root=reports_root)
        recorder.append("observe", {"phase": "compute_due_briefs", "record_count": len(records)})

        matches = [record for record in records if record.get("slug") == nonce]
        if len(matches) != 1:
            raise HardStop(
                f"{self.id}: compute_due_briefs() did not return exactly one record for the "
                f"disposable fixture topic {nonce!r}: {records!r}"
            )
        record = matches[0]

        required_keys = {"slug", "cadence", "output_path", "brief"}
        missing_keys = required_keys - set(record)
        if missing_keys:
            raise HardStop(f"{self.id}: compute_due_briefs() record missing key(s): {missing_keys}")

        output_path = Path(record["output_path"])
        try:
            output_path.relative_to(reports_root)
        except ValueError as exc:
            raise HardStop(
                f"{self.id}: compute_due_briefs() output_path {output_path} escaped the "
                f"disposable reports_root {reports_root}"
            ) from exc

        brief = record["brief"]
        contract_markers = ("TL;DR:", "## Follow-ups", "escaped form", "researcher")
        missing_markers = [marker for marker in contract_markers if marker not in brief]
        if missing_markers:
            raise HardStop(
                f"{self.id}: build_brief() output missing researcher-dispatch contract "
                f"marker(s) {missing_markers}: {brief[:200]!r}"
            )
        recorder.append("observe", {"phase": "build_brief-contract", "status": "proven"})

        detail = {
            "unit_wiring": "proven",
            "compute_due_briefs_contract": "proven",
            "researcher_dispatch": (
                "proven as WIRING/CONTRACT ONLY (build_brief()'s persona/TL;DR/Follow-ups/"
                "HERMES-escape shape) -- no live subagent dispatch was invoked; the in-session "
                "Agent-tool dispatch itself has no off-prod-provable code path"
            ),
            "fixture_topic": nonce,
            "output_path": str(output_path),
        }
        evidence_paths = (write_module_evidence(paths, self.id, "watch-runner", detail),)
        recorder.append("result", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths, detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        removed = str(self._scratch_root) if self._scratch_root is not None else None
        if self._scratch_root is not None and self._scratch_root.exists():
            shutil.rmtree(self._scratch_root)
        if self._scratch_root is not None and self._scratch_root.exists():
            raise HardStop(f"{self.id}: restore residue -- scratch root still exists: {self._scratch_root}")
        self._scratch_root = None
        return RestoreEvidence(module_id=self.id, restored=True, detail={"scratch_root_removed": removed})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            "specialist-watch-runner: pantheon-watch.service/.timer wiring proven; "
            "compute_due_briefs()/build_brief() contract proven against a disposable fixture "
            "topic; researcher-dispatch proven as wiring/contract only (no live dispatch)."
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# specialist-watch-autocommit
# ---------------------------------------------------------------------------


class ArgusWatchAutocommitModule:
    """Proves watch_runner.git_commit_and_push() end-to-end against a fully
    disposable git topology. See module docstring for the lockfile-override
    rationale and the HERMES-guard both-directions proof."""

    id = MODULE_ID_AUTOCOMMIT
    tier: Tier = "optional"

    def __init__(self) -> None:
        self._scratch_root: Path | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return _declare_for(self.id, self.tier, target)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        self._scratch_root = Path(tempfile.mkdtemp(prefix=f"bringup-{self.id}-"))

        success_detail = self._prove_success_case(recorder)
        refusal_detail = self._prove_refusal_case(recorder)

        detail = {"success_case": success_detail, "hermes_refusal_case": refusal_detail}
        evidence_paths = (write_module_evidence(paths, self.id, "autocommit", detail),)
        recorder.append("result", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths, detail=detail,
        )

    def _prove_success_case(self, recorder: TraceRecorder) -> Mapping[str, Any]:
        assert self._scratch_root is not None
        work_dir, bare_dir = _init_disposable_repo(self._scratch_root, label="success")
        nonce = f"t2565-{uuid.uuid4().hex[:8]}"
        slug = f"probe-{nonce}"
        date_iso = f"t2565-{nonce}"
        reports_dir = work_dir / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True)
        (reports_dir / f"{slug}.md").write_text(_valid_report_body(nonce), encoding="utf-8")

        topic = watch_runner.Topic(
            slug=slug, cadence="daily", budget=300, scope="probe", seed_sources=[], notes="",
        )
        lockfile = self._scratch_root / "lock-success.lock"
        with _lockfile_env(lockfile):
            ok, msg = watch_runner.git_commit_and_push(date_iso, [topic], {slug: "ok"}, cwd=work_dir)
        recorder.append("observe", {"case": "success", "ok": ok, "msg": msg})
        if not ok:
            raise HardStop(f"{self.id}: success-case git_commit_and_push refused unexpectedly: {msg}")

        landed_message = _bare_main_log(bare_dir)
        if landed_message is None:
            raise HardStop(
                f"{self.id}: success-case reported ok but nothing landed on the disposable origin's main"
            )
        if "research(watch): land" not in landed_message or date_iso not in landed_message:
            raise HardStop(
                f"{self.id}: success-case commit message did not match the expected shape: {landed_message!r}"
            )
        return {"nonce": nonce, "push_message": msg, "landed_commit_message": landed_message}

    def _prove_refusal_case(self, recorder: TraceRecorder) -> Mapping[str, Any]:
        assert self._scratch_root is not None
        work_dir, bare_dir = _init_disposable_repo(self._scratch_root, label="refusal")
        nonce = f"t2565-{uuid.uuid4().hex[:8]}"
        slug = f"probe-{nonce}"
        date_iso = f"t2565-{nonce}"
        reports_dir = work_dir / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True)
        (reports_dir / f"{slug}.md").write_text(_report_body_with_trigger(nonce), encoding="utf-8")

        topic = watch_runner.Topic(
            slug=slug, cadence="daily", budget=300, scope="probe", seed_sources=[], notes="",
        )
        lockfile = self._scratch_root / "lock-refusal.lock"
        with _lockfile_env(lockfile):
            ok, msg = watch_runner.git_commit_and_push(date_iso, [topic], {slug: "ok"}, cwd=work_dir)
        recorder.append("observe", {"case": "hermes_refusal", "ok": ok, "msg": msg})
        if ok:
            raise HardStop(
                f"{self.id}: refusal-case git_commit_and_push should have refused a "
                f"HERMES-tripping report, but reported ok: {msg}"
            )
        if "HERMES guard refused" not in msg:
            raise HardStop(f"{self.id}: refusal-case refused for the wrong reason: {msg!r}")

        if _bare_main_log(bare_dir) is not None:
            raise HardStop(
                f"{self.id}: refusal-case reported refused but something landed on the "
                "disposable origin's main anyway"
            )
        return {"nonce": nonce, "refusal_message": msg}

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        removed = str(self._scratch_root) if self._scratch_root is not None else None
        if self._scratch_root is not None and self._scratch_root.exists():
            shutil.rmtree(self._scratch_root)
        if self._scratch_root is not None and self._scratch_root.exists():
            raise HardStop(f"{self.id}: restore residue -- scratch root still exists: {self._scratch_root}")
        self._scratch_root = None
        return RestoreEvidence(module_id=self.id, restored=True, detail={"scratch_root_removed": removed})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            "specialist-watch-autocommit: git_commit_and_push() proven end-to-end against a "
            "disposable git topology (own bare origin, own mutation lockfile) -- both a "
            "clean report landing+pushing and a HERMES-tripping report being refused "
            "pre-commit with nothing landed."
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


def build_optional_modules() -> tuple[ArgusWatchRunnerModule, ArgusWatchAutocommitModule]:
    """The T-2561 optionals entry point: Specialist's optional-tier acceptance modules."""

    return (ArgusWatchRunnerModule(), ArgusWatchAutocommitModule())
