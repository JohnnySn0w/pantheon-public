"""tools/bringup_acceptance/optionals/recovery.py — Recovery custom-surface optional
acceptance modules (T-2556 phase 12 / T-2567; live-green split to T-2576).

Recovery's custom surface, per the ticket's own three nouns: recovery watchers
(``recovery-telegram-child-watchdog``), self-restart (``self_restart.sh``), and
boot-kick (``recovery_boot_kick.sh``). A fourth named surface, asymmetric sudo,
is NEVER touched or invoked anywhere in this file — it is a pure gap, named
in every module's :meth:`report`, deferred whole to T-2576.

RECOVERY ISOLATION (the crux — ratified by team-lead 2026-08-05). Recovery is the
recovery account and the fleet's single point of failure for it;
"Recovery stays quiet" is load-bearing, not a nicety. Every module below resolves
its ground truth against the REPO-CANONICAL copies in THIS worktree
(``tools/systemd/recovery-*``, ``tools/recovery_boot_kick.sh``, ``tools/self_restart.sh``
— :data:`REPO_ROOT_ENV` overridable, mirrors ``hook_emissions._ACCOUNT_HOME_ENV``
translated from "account home" to "repo root" since the ground truth here is
canonical templates, not a deployed account tree) — NEVER Recovery's real deployed
copies at ``~/.local/bin`` / ``~/.config/systemd/user``, and NEVER Recovery's real
tmux session or mailbox. This sidesteps "touch Recovery's real anything" entirely.

Per-module proof depth differs ON PURPOSE, and that split IS the isolation
answer for each surface:

* :class:`RecoveryRecoveryWatchdogModule` — STATIC contract-only. Reads the
  timer/service/script triad and asserts their WIRING (cadence, OnFailure
  routing, fail-open flags, bounded-remediation constants). Deliberately NOT
  dynamically executed: ``recovery-telegram-child-watchdog.sh`` hardcodes
  ``SESSION="claude-telegram-bot-fallback"`` with no env override, so a
  dynamic run needs something answering ``tmux has-session`` for that EXACT
  name truthfully. A same-named disposable session under a non-recovery account
  is uid-isolated and technically harmless, but manufacturing a session that
  shares Recovery's real session name at all is exactly the kind of collision
  "Recovery stays quiet" exists to prevent — so this module stays static, and the
  full dynamic debounce/cooldown/cap state-machine proof (which
  ``tools/systemd/test_recovery_telegram_child_watchdog.sh`` already does, via
  sandbox-``$HOME`` + ``--simulate-absent --dry-run``) is a NAMED GAP,
  deferred to T-2576, to run only on the real Recovery seat where that session
  legitimately exists.
* :class:`RecoverySelfRestartModule` — dynamic, genuinely safe: ``self_restart.sh``
  takes its target pane/service as caller-supplied flags (no hardcoded
  session name), so this module uses randomly-suffixed disposable tmux
  sessions that never collide with anything real. Proves flag validation,
  both preflight guards, and the full ``--dry-run`` happy path — all
  confirmed (by source inspection AND exit-code assertion) to return before
  ever reaching ``systemd-run``/``systemctl``. No real restart ever fires.
* :class:`RecoveryBootKickModule` — dynamic, fully seam-driven:
  ``recovery_boot_kick.sh`` ships ``RECOVERY_KICK_CAPTURE_CMD``/``RECOVERY_KICK_SEND_CMD``/
  ``RECOVERY_KICK_INFLIGHT_CMD`` test seams for exactly this purpose (see
  ``tools/test_recovery_boot_kick.sh``, which this module's 5 scenarios mirror).
  Zero tmux dependency at all — genuinely off-prod-provable regardless of
  which account runs it. ``RECOVERY_KICK_LOGGER`` is always pointed at a scratch
  stub (T-5075), so a run never writes boot-kick lines into the host journal.

Every module's ``declare()`` self-declares on ``target.persona == "recovery"``
alone (cheap, no filesystem access) — matching ``registry.py``'s own "no
per-persona branches in the REGISTRY" precedent while still letting THIS
persona-specific file branch on its own persona as defense-in-depth, in case
a future caller ever mismatches target/module pairing.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    AcceptanceModule,
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    Tier,
    TargetInventory,
)

PERSONA = "recovery"

# Env override so tests never depend on this file's own on-disk location —
# mirrors hook_emissions.py's PANTHEON_BRINGUP_ACCOUNT_HOME, translated from
# "account home" to "repo root" (see module docstring).
REPO_ROOT_ENV = "PANTHEON_BRINGUP_RECOVERY_REPO_ROOT"

WATCHDOG_TIMER_REL = "tools/systemd/recovery-telegram-child-watchdog.timer"
WATCHDOG_SERVICE_REL = "tools/systemd/recovery-telegram-child-watchdog.service"
WATCHDOG_SCRIPT_REL = "tools/systemd/recovery-telegram-child-watchdog.sh"
SELF_RESTART_REL = "tools/self_restart.sh"
BOOT_KICK_REL = "tools/recovery_boot_kick.sh"


def _repo_root() -> Path:
    """Where to look for Recovery's repo-canonical templates.

    Overridable (tests point this at a scratch fixture tree); the production
    default derives from this file's own location: ``.../tools/
    bringup_acceptance/optionals/recovery.py`` -> repo root is three parents up.
    """
    override = os.environ.get(REPO_ROOT_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3]


def _assert_contains(text: str, needle: str, *, source: str, label: str) -> None:
    if needle not in text:
        raise HardStop(
            f"{label}: {source} does not contain expected contract marker {needle!r}"
        )


def _assert_exit(result: subprocess.CompletedProcess[str], expected: int, *, case: str, label: str) -> None:
    if result.returncode != expected:
        raise HardStop(
            f"{label} ({case}): expected exit {expected}, got {result.returncode} "
            f"(stdout={result.stdout!r} stderr={result.stderr!r})"
        )


# ---------------------------------------------------------------------------
# recovery-recovery-watchdog — STATIC contract-only (see module docstring).
# ---------------------------------------------------------------------------


class RecoveryRecoveryWatchdogModule:
    """Proves the recovery-telegram-child-watchdog timer/service/script WIRING by
    reading the repo-canonical files. Never executes them (see module
    docstring for why: the script's hardcoded real session name)."""

    id = "recovery-recovery-watchdog"
    tier: Tier = "optional"

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root if repo_root is not None else _repo_root()

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        applies = target.persona == PERSONA
        reason = (
            "target persona is recovery -- the recovery-watchdog custom surface applies"
            if applies
            else f"target persona is {target.persona!r}, not recovery -- no recovery-specific recovery-watchdog surface"
        )
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=applies, reason=reason)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        timer_path = self._repo_root / WATCHDOG_TIMER_REL
        service_path = self._repo_root / WATCHDOG_SERVICE_REL
        script_path = self._repo_root / WATCHDOG_SCRIPT_REL
        missing = [str(p) for p in (timer_path, service_path, script_path) if not p.is_file()]
        if missing:
            recorder.append("gap", {"missing": missing})
            return ModuleEvidence(
                module_id=self.id, status="gap", trace_path=recorder.path, evidence_paths=(),
                detail={"missing": missing, "reason": "canonical Recovery recovery-watchdog files not found under repo_root"},
            )

        timer_text = timer_path.read_text(encoding="utf-8")
        service_text = service_path.read_text(encoding="utf-8")
        script_text = script_path.read_text(encoding="utf-8")

        checks: tuple[tuple[str, str, str], ...] = (
            ("timer", "OnUnitActiveSec=90s", timer_text),
            ("timer", "OnBootSec=2min", timer_text),
            ("timer", "Unit=recovery-telegram-child-watchdog.service", timer_text),
            ("timer", "Persistent=false", timer_text),
            ("service", "OnFailure=pantheon-unit-failure-alert@%n.service", service_text),
            ("service", "Type=oneshot", service_text),
            ("service", "ExecStart=/home/claude-fallback/.local/bin/recovery-telegram-child-watchdog.sh", service_text),
            ("script", "set -uo pipefail", script_text),
            ("script", "--dry-run", script_text),
            ("script", "--simulate-absent", script_text),
            ("script", "DEBOUNCE=", script_text),
            ("script", "COOLDOWN=", script_text),
            ("script", "CAP=", script_text),
            ("script", "BOOT_GRACE=", script_text),
            ("script", "SELF_RESTART=", script_text),
            ("script", "self_restart.sh", script_text),
        )
        for source, needle, text in checks:
            _assert_contains(text, needle, source=source, label=self.id)
        recorder.append("contract-verified", {"checks": len(checks)})

        detail: Mapping[str, Any] = {
            "timer_path": str(timer_path),
            "service_path": str(service_path),
            "script_path": str(script_path),
            "checks_verified": len(checks),
            "scope": (
                "STATIC contract-only: unit/service/script WIRING verified by reading the "
                "repo-canonical files. The dynamic debounce/cooldown/cap state machine is NOT "
                "exercised here -- the script hardcodes SESSION=claude-telegram-bot-fallback with "
                "no env override, so a dynamic run needs a real tmux session answering that exact "
                "name; manufacturing one (even under a non-recovery account) risks normalizing a "
                "same-name-collision pattern this module's whole purpose is to avoid. Deferred to "
                "T-2576, to run only on the real Recovery seat (mirrors "
                "tools/systemd/test_recovery_telegram_child_watchdog.sh's own sandbox-HOME + "
                "--simulate-absent --dry-run shape)."
            ),
        }
        evidence_path = write_module_evidence(paths, self.id, "static-contract", detail)
        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=(evidence_path,), detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail={"note": "read-only static contract check against repo-canonical files; no mutation, no restore needed"},
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        if evidence.status == "gap":
            summary = f"{self.id}: canonical files missing -- {evidence.detail.get('missing')}"
        else:
            summary = (
                f"{self.id}: timer/service/script WIRING statically proven against repo-canonical "
                f"files ({evidence.detail.get('checks_verified')} checks). NAMED GAPS (deferred to "
                "T-2576, live/interactive): the dynamic debounce/cooldown/cap state machine "
                "(needs the real Recovery tmux session) and asymmetric-sudo execution -- neither is "
                "touched here."
            )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# recovery-self-restart — dynamic, disposable-tmux-safe (see module docstring).
# ---------------------------------------------------------------------------


class RecoverySelfRestartModule:
    """Proves tools/self_restart.sh's flag-validation + preflight-guard +
    --dry-run contract via randomly-suffixed disposable tmux sessions --
    never Recovery's real session name, never a real restart."""

    id = "recovery-self-restart"
    tier: Tier = "optional"

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root if repo_root is not None else _repo_root()
        self._scratch_root: Path | None = None
        self._created_sessions: list[str] = []

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        applies = target.persona == PERSONA
        reason = (
            "target persona is recovery -- the shared self_restart.sh contract applies"
            if applies
            else f"target persona is {target.persona!r}, not recovery -- module scoped to recovery's optional bundle"
        )
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=applies, reason=reason)

    def _run(self, script_path: Path, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        script = (cwd / "self_restart.sh") if cwd is not None else script_path
        return subprocess.run([str(script), *args], capture_output=True, text=True, timeout=20)

    def _create_session(self, run_id: str) -> str:
        name = f"bringup-acceptance-recovery-self-restart-{run_id}-{uuid.uuid4().hex[:8]}"
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "sleep 300"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise HardStop(f"{self.id}: could not create disposable tmux session {name!r}: {result.stderr!r}")
        self._created_sessions.append(name)
        return name

    def _kill_session(self, name: str) -> None:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True, timeout=10)
        if name in self._created_sessions:
            self._created_sessions.remove(name)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        script_path = self._repo_root / SELF_RESTART_REL
        if not script_path.is_file():
            recorder.append("gap", {"missing": str(script_path)})
            return ModuleEvidence(
                module_id=self.id, status="gap", trace_path=recorder.path, evidence_paths=(),
                detail={"missing": str(script_path)},
            )

        self._scratch_root = Path(tempfile.mkdtemp(prefix=f"bringup-recovery-self-restart-{ctx.run_id}-"))
        cases: dict[str, Mapping[str, Any]] = {}

        # Cases 1-2: required-flag validation -- exit 64, zero side effects.
        result = self._run(script_path, ["--target-pane", "x:0"])
        _assert_exit(result, 64, case="missing---service", label=self.id)
        cases["missing---service"] = {"exit": result.returncode}

        result = self._run(script_path, ["--service", "fake.service"])
        _assert_exit(result, 64, case="missing---target-pane", label=self.id)
        cases["missing---target-pane"] = {"exit": result.returncode}

        # Case 3: bad --mode.
        result = self._run(script_path, ["--service", "fake.service", "--target-pane", "x:0", "--mode", "bogus"])
        _assert_exit(result, 64, case="invalid---mode", label=self.id)
        cases["invalid---mode"] = {"exit": result.returncode}
        recorder.append("flag-validation-verified", dict(cases))

        # Case 4: missing tmux session (no session created for this pane name) -> preflight 1.
        absent_pane = f"bringup-acceptance-absent-{uuid.uuid4().hex[:8]}:0"
        result = self._run(script_path, ["--service", "fake.service", "--target-pane", absent_pane, "--dry-run"])
        _assert_exit(result, 1, case="missing-tmux-session", label=self.id)
        cases["missing-tmux-session"] = {"exit": result.returncode}
        recorder.append("preflight-1-verified", cases["missing-tmux-session"])

        # Case 5: missing restart-script sibling -> preflight 2. Isolated scratch
        # copy WITHOUT handoff_and_restart.sh alongside it -- RESTART_SCRIPT is
        # derived from BASH_SOURCE, so this forces the not-found branch.
        isolated_dir = self._scratch_root / "missing-sibling"
        isolated_dir.mkdir(parents=True, exist_ok=True)
        isolated_script = isolated_dir / "self_restart.sh"
        shutil.copy2(script_path, isolated_script)
        isolated_script.chmod(isolated_script.stat().st_mode | stat.S_IEXEC)
        session = self._create_session(ctx.run_id)
        try:
            result = self._run(
                script_path, ["--service", "fake.service", "--target-pane", f"{session}:0", "--dry-run"],
                cwd=isolated_dir,
            )
        finally:
            self._kill_session(session)
        _assert_exit(result, 1, case="missing-restart-script", label=self.id)
        cases["missing-restart-script"] = {"exit": result.returncode}
        recorder.append("preflight-2-verified", cases["missing-restart-script"])

        # Case 6: full happy path, --dry-run -- must return before ever reaching
        # systemd-run/systemctl (source-verified: the DRY_RUN branch exits first).
        session2 = self._create_session(ctx.run_id)
        try:
            result = self._run(
                script_path, ["--service", "fake.service", "--target-pane", f"{session2}:0", "--dry-run"],
            )
        finally:
            self._kill_session(session2)
        _assert_exit(result, 0, case="happy-path-dry-run", label=self.id)
        if "DRY RUN" not in result.stdout:
            raise HardStop(
                f"{self.id} (happy-path-dry-run): expected a 'DRY RUN' log line, got stdout={result.stdout!r}"
            )
        cases["happy-path-dry-run"] = {"exit": result.returncode, "stdout": result.stdout}
        recorder.append("dry-run-happy-path-verified", {"exit": result.returncode})

        evidence_path = write_module_evidence(paths, self.id, "cases", {"cases": cases})
        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=(evidence_path,),
            detail={
                "cases": list(cases),
                "scope": (
                    "proven against the shared tools/self_restart.sh contract via disposable, "
                    "randomly-suffixed tmux sessions (never Recovery's real session name) -- flag "
                    "validation, both preflight guards, and the --dry-run happy path all confirmed "
                    "to exit BEFORE any systemd-run/systemctl call. Real restart execution against "
                    "the live Recovery account/session is deferred to T-2576."
                ),
            },
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        residue: list[str] = []
        for name in list(self._created_sessions):
            self._kill_session(name)
        for name in self._created_sessions:
            residue.append(f"tmux session {name}")
        removed = str(self._scratch_root) if self._scratch_root is not None else None
        if self._scratch_root is not None and self._scratch_root.exists():
            shutil.rmtree(self._scratch_root)
        if self._scratch_root is not None and self._scratch_root.exists():
            residue.append(f"scratch root {self._scratch_root}")
        if residue:
            raise HardStop(f"{self.id}: restore residue: " + "; ".join(residue))
        self._scratch_root = None
        return RestoreEvidence(module_id=self.id, restored=True, detail={"scratch_root_removed": removed})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        if evidence.status == "gap":
            summary = f"{self.id}: repo-canonical self_restart.sh not found -- {evidence.detail.get('missing')}"
        else:
            summary = (
                f"{self.id}: flag validation + both preflight guards + --dry-run happy path all "
                "proven via disposable tmux sessions. NAMED GAP (deferred to T-2576, "
                "live/interactive): real self-restart execution against the live Recovery "
                "account/session and asymmetric sudo are never exercised here."
            )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# recovery-boot-kick — dynamic, fully seam-driven, zero tmux dependency.
# ---------------------------------------------------------------------------

IDLE_PANE = "╭" + "─" * 28 + "╮\n❯ \n╰" + "─" * 28 + "╯"
DIALOG_PANE = (
    "╭" + "─" * 28 + "╮\n❯ Yes\n  No\nEnter to confirm · Esc to cancel"
)
JUNK_PANE = "booting... please wait"


class RecoveryBootKickModule:
    """Proves recovery_boot_kick.sh's 5 guards via its own injectable seams --
    mirrors tools/test_recovery_boot_kick.sh exactly, zero tmux dependency. The
    RECOVERY_KICK_LOGGER seam is set to a scratch stub on every run (T-5075)."""

    id = "recovery-boot-kick"
    tier: Tier = "optional"

    # Hang guard: each scenario's subprocess may run this many seconds past its
    # own sentinel timeout before it is cut off. A class attribute so tests on a
    # loaded runner can widen it without loosening the production value (T-4774).
    RUN_CASE_GRACE_S: int = 10

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root if repo_root is not None else _repo_root()
        self._scratch_root: Path | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        applies = target.persona == PERSONA
        reason = (
            "target persona is recovery -- recovery_boot_kick.sh's guard contract applies"
            if applies
            else f"target persona is {target.persona!r}, not recovery -- module scoped to recovery's optional bundle"
        )
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=applies, reason=reason)

    def _run_case(
        self, script_path: Path, witness: Path, stub: Path, *,
        logger: Path, inflight: str, capture: str, send_rc: int, timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        witness.write_text("", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "RECOVERY_KICK_SEND_CMD": str(stub),
            # T-5075: always this run's scratch stub, overriding any inherited value,
            # so an acceptance run never reaches the real `logger` (host journal).
            "RECOVERY_KICK_LOGGER": str(logger),
            "RECOVERY_KICK_SENTINEL_TIMEOUT": str(timeout),
            "RECOVERY_KICK_POLL_INTERVAL": "1",
            "RECOVERY_KICK_INFLIGHT_CMD": inflight,
            "RECOVERY_KICK_CAPTURE_CMD": capture,
            "STUB_RC": str(send_rc),
        })
        pane = "bringup-acceptance-recovery-boot-kick-probe:0"
        return subprocess.run(
            [str(script_path), pane, "recovery"], capture_output=True, text=True, env=env, timeout=timeout + self.RUN_CASE_GRACE_S,
        )

    @staticmethod
    def _kick_count(witness: Path) -> int:
        text = witness.read_text(encoding="utf-8") if witness.is_file() else ""
        return sum(1 for line in text.splitlines() if line.startswith("SENT "))

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        script_path = self._repo_root / BOOT_KICK_REL
        if not script_path.is_file():
            recorder.append("gap", {"missing": str(script_path)})
            return ModuleEvidence(
                module_id=self.id, status="gap", trace_path=recorder.path, evidence_paths=(),
                detail={"missing": str(script_path)},
            )

        self._scratch_root = Path(tempfile.mkdtemp(prefix=f"bringup-recovery-boot-kick-{ctx.run_id}-"))
        witness = self._scratch_root / "witness"
        stub = self._scratch_root / "fake_send.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "SENT pane=$1" >> "{witness}"\n'
            'exit "${STUB_RC:-0}"\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        # T-5075: no-op journal stub. recovery_boot_kick.sh's log() otherwise calls
        # `logger -t pantheon-recovery-boot-kick`, writing fake boot-kick lines into the
        # target host's journal. The script's exit status and output do not depend on
        # the logger (`|| true`); the stub exits 0 anyway, and records each line under
        # scratch so the evidence shows the seam took.
        journal = self._scratch_root / "journal"
        logger_stub = self._scratch_root / "fake_logger.sh"
        logger_stub.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$1\" >> {shlex.quote(str(journal))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        logger_stub.chmod(logger_stub.stat().st_mode | stat.S_IEXEC)

        def _capture_cmd(text: str) -> str:
            return f"printf %s {shlex.quote(text)}"

        scenarios: tuple[tuple[str, str, str, int, int, int], ...] = (
            ("in-flight-kicker-skip", "true", _capture_cmd(IDLE_PANE), 0, 3, 0),
            ("bare-idle-kicks-once", "false", _capture_cmd(IDLE_PANE), 0, 3, 1),
            ("no-sentinel-timeout", "false", _capture_cmd(JUNK_PANE), 0, 2, 0),
            ("dialog-modal-excluded", "false", _capture_cmd(DIALOG_PANE), 0, 2, 0),
            ("send-fails-fail-open", "false", _capture_cmd(IDLE_PANE), 1, 3, 1),
        )
        results: dict[str, Mapping[str, Any]] = {}
        for name, inflight, capture, send_rc, timeout, expected_sent in scenarios:
            journal.write_text("", encoding="utf-8")
            result = self._run_case(
                script_path, witness, stub, logger=logger_stub, inflight=inflight,
                capture=capture, send_rc=send_rc, timeout=timeout,
            )
            sent = self._kick_count(witness)
            logged = len(journal.read_text(encoding="utf-8").splitlines()) if journal.is_file() else 0
            if result.returncode != 0:
                raise HardStop(
                    f"{self.id} ({name}): expected exit 0 (fail-open by contract), got "
                    f"{result.returncode} (stdout={result.stdout!r})"
                )
            if sent != expected_sent:
                raise HardStop(
                    f"{self.id} ({name}): expected {expected_sent} send-attempt(s), observed {sent}"
                )
            results[name] = {"exit": result.returncode, "sent": sent, "logged": logged}
            recorder.append(name, results[name])

        evidence_path = write_module_evidence(paths, self.id, "guard-scenarios", {"scenarios": results})
        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=(evidence_path,),
            detail={
                "scenarios": results,
                "scope": (
                    "proven via recovery_boot_kick.sh's own injectable seams "
                    "(RECOVERY_KICK_CAPTURE_CMD/RECOVERY_KICK_SEND_CMD/RECOVERY_KICK_INFLIGHT_CMD) -- zero "
                    "tmux dependency, mirrors tools/test_recovery_boot_kick.sh's own 5 scenarios. Real "
                    "delivery into a live Recovery pane is deferred to T-2576."
                ),
            },
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
        if evidence.status == "gap":
            summary = f"{self.id}: repo-canonical recovery_boot_kick.sh not found -- {evidence.detail.get('missing')}"
        else:
            summary = (
                f"{self.id}: all 5 guard scenarios proven (in-flight skip, idle-confirmed kick, "
                "timeout-no-kick, dialog-modal-exclude, send-fails-fail-open) via injected seams, "
                "zero tmux dependency. NAMED GAP (deferred to T-2576, live/interactive): real "
                "delivery into a live Recovery pane and asymmetric sudo are never exercised here."
            )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


def build_optional_modules() -> tuple[AcceptanceModule, ...]:
    """Recovery's three custom-surface optional modules. Asymmetric sudo is
    deliberately not a fourth module here -- see module docstring."""
    return (
        RecoveryRecoveryWatchdogModule(),
        RecoverySelfRestartModule(),
        RecoveryBootKickModule(),
    )
