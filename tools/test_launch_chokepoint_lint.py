"""ORG-106 L3a (T-5526): structural lint for the DEEP-SLEEP launch chokepoint.

ORG-106's riskiest assumption is "one unrouted launcher turns DEEP-SLEEP into a sleep that
wakes itself". This registry is the answer: every file under tools/ that can start a tmux
session (the pattern scan) or restart one (the explicit launcher-equivalents) carries a
disposition and a one-line EVIDENCE string a reviewer can check. A NEW launcher that is not
registered fails the scan; a gated entry that loses its gate fails its check.

Dispositions:
  gated-shell   the script calls `seat_state.py launch-check` before it starts anything
  chokepoint    python launcher behind runtime_controller's shared predicate
  delegates     restarts only by running another registered launcher (named in the file)
  not-a-seat    not a launcher of an HC seat session (evidence says why)
  outside-repo  a live launcher outside this repo: cannot be gated here (listed so it is seen)
"""

from __future__ import annotations

import ast
import os
import re
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
# The reviewer's extra terms, narrowed to EXECUTABLE occurrences so prose does not bury the
# signal: a quoted `--resume` argv element, `codex app-server`, `exec codex`.
PATTERN = re.compile(r"""new-session|respawn-pane|respawn-window|exec codex|["']--resume["']|codex app-server""")
GATE = re.compile(r'seat_state\.py"? launch-check')

# path (relative to tools/) -> (disposition, evidence). Evidence is checked by a human.
REGISTRY: dict[str, tuple[str, str]] = {
    # --- gated shell launchers -------------------------------------------------------
    "tools/seat_spawn.sh": ("gated-shell", "manual seat spawn (mason/artisan); launch-check --account before the tmux exec"),
    "tools/pantheon-seat-relaunch.sh": ("gated-shell", "graceful ctxfill relaunch; launch-check <persona> before capture/kill (skipped only for --record-only, which launches nothing)"),
    "tools/handoff_and_restart.sh": ("gated-shell", "the restart primitive behind restart_drain / context_fill_monitor / channel_liveness / self_restart; launch-check before dry-run and every step"),
    "tools/persona_cutover.sh": ("gated-shell", "cutover starts the session via persona_installer apply; launch-check <persona> before any change"),
    "tools/persona_rollback.sh": ("gated-shell", "rollback restores the bespoke supervisor that starts the session; must not wake a sleeping seat"),
    # --- python launchers behind the shared predicate --------------------------------
    "tools/runtime_controller.py": ("chokepoint", "_start_runtime (lowest tmux primitive), _controller_start_runtime and _refuse_held_runtime_launch all call _refuse_sleeping_seat_launch; selection skips with cause seat-sleep"),
    # --- delegating restart paths -----------------------------------------------------
    "tools/self_restart.sh": ("delegates", "runs handoff_and_restart.sh --skip-ack from a transient unit"),
    "tools/restart_drain.py": ("delegates", "fires handoff_and_restart.sh for the target agent"),
    "tools/restart_request.py": ("delegates", "writes the tombstone restart_drain.py acts on; starts nothing itself"),
    "tools/context_fill_monitor.py": ("delegates", "restarts only via HANDOFF_RESTART_BIN = handoff_and_restart.sh; also skips on remediation_vetoed (L4)"),
    # --- not launchers of an HC seat session -----------------------------------------
    "tools/persona_installer.py": ("not-a-seat", "starts/enables controller, consume and companion UNITS only; the session itself is created by _start_runtime (gated); a controller on a sleeping seat waits, consume skips"),
    "tools/verify_startup.sh": ("not-a-seat", "poller_escalate kills a control-plane session (the gated controller relaunches it); its systemctl restart branch is the bespoke supervisor path of non-HC seats"),
    "tools/recovery_boot_kick.sh": ("not-a-seat", "capture-pane + send-keys kick into an existing pane after a respawn; creates no session"),
    "tools/arm_pane_log.sh": ("not-a-seat", "tmux pipe-pane log tap on an existing pane; 'new-session' appears in a comment only"),
    "tools/acp-bridge-supervise.sh": ("not-a-seat", "ACP bridge session, build-only and not installed (its header); not an HC seat"),
    "tools/claude_quota_probe.py": ("not-a-seat", "ephemeral one-turn probe TUI (tmux new-session at ~312) under the seat's account, NOT the seat runtime. Its caller `persona_bringup quota-check` (pantheon-persona-quota@.service, refresh_primary_quota_detailed; can also begin_planned_transition) now skips on deep-sleep before quota_check_supported with decision seat-sleep (tested); selection usability probes are skipped because selection returns None on a sleeping seat"),
    "tools/session_chronicle.py": ("not-a-seat", "respawn-pane named in a docstring only"),
    "tools/pantheon_oom_shield.sh": ("not-a-seat", "a protected-process pattern list ('codex:tmux new-session ...'), starts nothing"),
    "tools/t2590_cutover_verify_lib.sh": ("not-a-seat", "pure verify helpers; new-session appears in comments"),
    "tools/bringup_acceptance/harness_runtime.py": ("not-a-seat", "acceptance fixture sessions on a private tmux socket"),
    "tools/bringup_acceptance/optionals/recovery.py": ("not-a-seat", "acceptance fixture: a 'sleep 300' session"),
    "tools/systemd/claude-telegram-research.service": ("not-a-seat", "'new-session' in a comment; unit is the disabled bespoke supervisor of a control-plane seat"),
    "tools/systemd/claude-bot-supervise.sh": ("gated-shell", "bespoke supervisor of orchestrator (unit disabled today, but the repo ships a re-enable recipe, hosts/swarm/systemd/README.md): launch-check WAIT loop before the first launch; the LIVE copy in ~/.local/bin must be re-synced"),
    "tools/systemd/specialist-bot-supervise.sh": ("gated-shell", "bespoke supervisor of specialist (unit disabled today, but the repo ships a re-enable recipe, hosts/swarm/systemd/README.md): launch-check WAIT loop before the first launch; the LIVE copy in ~/.local/bin must be re-synced"),
    "tools/systemd/exo-bot-supervise.sh": ("gated-shell", "bespoke supervisor of archivist (unit disabled today, but the repo ships a re-enable recipe, hosts/swarm/systemd/README.md): launch-check WAIT loop before the first launch; the LIVE copy in ~/.local/bin must be re-synced"),
    "tools/systemd/recovery-bot-supervise.sh": ("gated-shell", "bespoke supervisor of recovery (unit disabled today, but the repo ships a re-enable recipe, hosts/swarm/systemd/README.md): launch-check WAIT loop before the first launch; the LIVE copy in ~/.local/bin must be re-synced"),
    "tools/systemd/recovery-telegram-child-watchdog.sh": ("not-a-seat", "restarts the telegram child via the registered handoff path; 'new-session' is in a pgrep comment"),
    # --- rebound 1: every other path that starts a unit, a session or a process ----------
    "hosts/swarm/systemd/claude-telegram-exocortex.service": ("delegates", "re-enable recipe unit (hosts/swarm/systemd/README.md); ExecStart is exo-bot-supervise.sh, which is gated-shell above"),
    "harness/claude/mirror/overlay/provisioning/systemd/pantheon-agent@.service.example": ("not-a-seat", "generic mirror example, not wired to any seat on this host"),
    "harness/claude/mirror/overlay/provisioning/tmux-launch.example.sh": ("not-a-seat", "generic mirror example, not wired to any seat on this host"),
    "harness/codex/appserver_supervise.py": ("not-a-seat", "codex app-server supervisor: respawn loop (~303) and start_pane new-session (~316); non-HC seat (codex/Consultant, peer-daemon-codex, no supervisor field), same row as codex-supervise.sh: `harness codex sleep` is refused at the verb (L3b)"),
    "harness/codex/codex_self_restart.sh": ("not-a-seat", "codex seat self-restart: systemd-run restart of the codex daemon unit (~127); non-HC seat, same row as codex-supervise.sh"),
    "harness/codex/appserver_cutover.sh": ("not-a-seat", "installs the codex app-server supervisor (ORG-103 L2, T-5303); non-HC seat, same row as codex-supervise.sh"),
    "tools/codex_delivery.py": ("not-a-seat", "verified delivery of input to a codex seat over the app-server; starts no session; non-HC codex seat"),
    "tools/codex_seat_delivery.py": ("not-a-seat", "single writer of input to a codex seat; `_systemctl_restart` (~712) restarts its own delivery unit; non-HC codex seat"),
    "tools/codex_hook_trust_check.py": ("not-a-seat", "drives `codex app-server` over stdio as a read-only check; starts no seat session"),
    "tools/codex_schema_contract.py": ("not-a-seat", "pins the codex version per app-server seat; spawns `codex app-server` to read its schema; no seat session"),
    "tools/codex_recovery.py": ("not-a-seat", "Recovery's recovery dispatcher: the restart verb (build_restart_argv ~1262, dispatch ~2109) is `systemctl --user restart -- <unit>` limited to the target's manifest restart_units and the session-owner denylist, so never a session-owning unit. Restarting a controller/consume unit on a sleeping seat is harmless: the controller waits in-process, consume skips"),
    "tools/codex_telegram_bridge.py": ("not-a-seat", "operator-run cutover command sheet (steps at ~1826, ~3196, ~3809 start the codex receiver unit and pantheon-persona-consume@<persona>.service). The consume start skips on deep-sleep; a receiver start is a poller, not a session; a launch under deep-sleep still pages as launch_breach"),
    "tools/fleet_reboot.py": ("not-a-seat", "Recovery-driven machine reboot after quiesce; nothing starts a session itself. After boot the controller waits in-process on a sleeping seat and consume skips"),
    "tools/harness_adapter/base.py": ("not-a-seat", "adapter interface docs; 'app-server' is prose"),
    "tools/harness_adapter/claude.py": ("not-a-seat", "builds the headless/batch `claude -p --resume` argv (~1017); batch is not the seat runtime; its only automated caller, consume, skips on deep-sleep; operator `run` is explicit"),
    "tools/harness_adapter/hermes.py": ("not-a-seat", "builds the headless/batch argv with --resume (~400); same row as claude.py"),
    "tools/ho_resumer.py": ("not-a-seat", "fires `claude --resume` on a cap-hit file; decommissioned on orchestrator by the installer (op service.ho-resumer.decommission, T-5435); the control plane owns cap-hits"),
    "tools/sudoers.d/pantheon-s3star-tmux": ("not-a-seat", "audit-session wrapper (T-1884, new-session at ~94): an audit session, not a seat"),
    "tools/sudoers.d/claude-codex-audit": ("not-a-seat", "sudoers source whose comment names `tmux new-session`; grants nothing to a seat launcher"),
    "tools/recovery_self_restart.sh": ("delegates", "back-compat wrapper that execs self_restart.sh (-> handoff_and_restart.sh, gated)"),
    # --- L3a review fold-ins (ORG-106 L3b): launcher-equivalents with no tmux pattern ---------
    "tools/channel_liveness_monitor.py": ("delegates", "remediates a dead Telegram bridge only through fire_restart_detached -> handoff_and_restart.sh (gated); a deep-sleep seat is classified asleep and skipped (L4b), a live one pages as sleep_breach"),
    "tools/systemd/claude-telegram.service": ("delegates", "orchestrator's bespoke supervisor unit (disabled today; re-enable recipe in hosts/swarm/systemd/README.md): ExecStart is claude-bot-supervise.sh, gated-shell above"),
    "tools/consume_onboard.py": ("not-a-seat", "enables / disables the pantheon-persona-consume@ timer only (systemctl --user enable --now, ~296, ~397); the fire it schedules is persona_bringup consume, which skips the whole cycle on deep-sleep (tested)"),
    "tools/oom/sysorch_restart.py": ("not-a-seat", "installs the ORG-104 rule-3 restart drop-ins for recovery and watchman; the drop-ins carry ConditionPathExists=!/var/lib/pantheon/oom-hold/<persona>, the flag `harness sleep` writes (L3b) and wake / the janitor remove through the same seat_sleep.is_sysorch, so systemd does not restart a slept system orchestrator; the controller above it waits in-process on deep-sleep anyway"),
    "tools/t2693_hub_cutover.sh": ("not-a-seat", "one-shot ORG-087 mailbox-hub migration, already executed (T-2693); its stage 4 restarts the long-lived codex sessions via sudo. Operator-run, never scheduled; a session it restarts on a sleeping seat is not relaunched by anything (the controller waits in-process on deep-sleep, and a live one pages as launch_breach)"),
    # --- live, outside the repo -------------------------------------------------------
    "<outside-repo>/codex-supervise.sh": ("outside-repo", "live on account codex (peer-daemon-codex, no supervisor field; codex-daemon.service enabled). NOT an HC seat: `harness codex sleep` is refused at the verb (L3b); pause is intake-only and unaffected"),
}

KINDS = {"gated-shell", "chokepoint", "delegates", "not-a-seat", "outside-repo"}
DELEGATE_TOKEN = {
    "tools/self_restart.sh": "handoff_and_restart",
    "tools/restart_drain.py": "handoff_and_restart",
    "tools/restart_request.py": "restart_drain",
    "tools/context_fill_monitor.py": "handoff_and_restart",
    "tools/recovery_self_restart.sh": "self_restart.sh",
    "hosts/swarm/systemd/claude-telegram-exocortex.service": "exo-bot-supervise.sh",
    "tools/channel_liveness_monitor.py": "fire_restart_detached",
    "tools/systemd/claude-telegram.service": "claude-bot-supervise.sh",
}
# Explicit launcher-equivalents that do not contain the tmux pattern.
EXPLICIT = {"tools/handoff_and_restart.sh", "tools/persona_rollback.sh", "tools/self_restart.sh",
            "tools/restart_drain.py", "tools/restart_request.py", "tools/context_fill_monitor.py",
            "tools/verify_startup.sh", "tools/recovery_boot_kick.sh", "tools/channel_liveness_monitor.py",
            "tools/systemd/claude-telegram.service", "tools/consume_onboard.py", "tools/oom/sysorch_restart.py",
            "tools/t2693_hub_cutover.sh"}


REPO = TOOLS.parent
# Whole-repo scan (reviewer rebound 1, finding 3). Skipped: history/prose trees, VCS, worktrees.
SCAN_EXCLUDE_DIRS = {"tickets", "ledger", "knowledge", "notes", ".git", ".claude", "node_modules",
                     "__pycache__",
                     # binary/prose/vendored trees with no launchers (keeps the scan to seconds)
                     "assets", "screenshots", "archive", "vendor-patches", "checkouts"}
SCAN_SUFFIXES = {".sh", ".py", ".service", ".timer", ".conf", ".example", ""}  # "" = extensionless


def _scan() -> set[str]:
    """Repo-relative paths (posix) of every non-test file that matches PATTERN."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SCAN_EXCLUDE_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if name.startswith("test_") or path.suffix not in SCAN_SUFFIXES:
                continue
            try:
                if PATTERN.search(path.read_text(encoding="utf-8", errors="replace")):
                    found.add(path.relative_to(REPO).as_posix())
            except OSError:
                continue
    return found


class LaunchChokepointLint(unittest.TestCase):
    def test_every_tmux_launcher_is_registered(self) -> None:
        unregistered = sorted(_scan() - set(REGISTRY))
        self.assertEqual(
            unregistered, [],
            "a file that can start a tmux session is not in REGISTRY: gate it with "
            "`seat_state.py launch-check` (shell) or the runtime_controller predicate (python), "
            "or register it as not-a-seat WITH evidence")

    def test_registry_entries_are_live_and_well_formed(self) -> None:
        for rel, (kind, evidence) in REGISTRY.items():
            self.assertIn(kind, KINDS, rel)
            self.assertTrue(evidence.strip(), f"{rel}: evidence required")
            if kind != "outside-repo":
                self.assertTrue((REPO / rel).is_file(), f"{rel}: stale registry entry")
        for rel in EXPLICIT:
            self.assertIn(rel, REGISTRY, rel)

    def test_gated_shell_launchers_call_launch_check(self) -> None:
        for rel, (kind, _e) in REGISTRY.items():
            if kind == "gated-shell":
                self.assertRegex((REPO / rel).read_text(encoding="utf-8"), GATE, rel)

    def test_gate_comes_before_the_first_launch_verb(self) -> None:
        """The gate's offset must precede the first launch verb in every gated script
        (comments excluded), so a gate placed after the launch cannot pass."""
        launch = re.compile(
            r"tmux\s+new-session|respawn-pane|^\s*exec\s+(?![{0-9>])|"
            r"systemctl\s+(--user\s+)?(start|restart)\b|\buctl\s+(start|restart)\b|kill-server|"
            r"persona_installer\s+apply|\bkill-session\b")
        for rel, (kind, _e) in REGISTRY.items():
            if kind != "gated-shell":
                continue
            offset = 0
            gate_at = launch_at = None
            heredoc_end = None
            for line in (REPO / rel).read_text(encoding="utf-8").splitlines(keepends=True):
                if heredoc_end is not None:  # usage text etc.: not code
                    if line.strip() == heredoc_end:
                        heredoc_end = None
                    offset += len(line)
                    continue
                code = "" if line.lstrip().startswith("#") else re.sub(r"\s#.*$", "", line)
                opener = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", code)
                if opener:
                    heredoc_end = opener.group(1)
                if gate_at is None and GATE.search(code):
                    gate_at = offset
                if launch_at is None and launch.search(code):
                    launch_at = offset
                offset += len(line)
            self.assertIsNotNone(gate_at, f"{rel}: no gate")
            if launch_at is not None:
                self.assertLess(gate_at, launch_at, f"{rel}: the launch-check gate must precede the first launch verb")

    def test_delegates_still_name_their_launcher(self) -> None:
        for rel, token in DELEGATE_TOKEN.items():
            self.assertIn(token, (REPO / rel).read_text(encoding="utf-8"), rel)

    def test_runtime_controller_launch_seams_call_the_predicate(self) -> None:
        tree = ast.parse((TOOLS / "runtime_controller.py").read_text(encoding="utf-8"))
        funcs = {n.name: ast.get_source_segment((TOOLS / "runtime_controller.py").read_text(encoding="utf-8"), n)
                 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ("_start_runtime", "_controller_start_runtime", "_refuse_held_runtime_launch"):
            self.assertIn("_refuse_sleeping_seat_launch(", funcs[name], name)
        self.assertIn("_seat_launch_block(", funcs["_select_viable_runtime"])
        self.assertIn("_seat_launch_block(", funcs["ensure_viable_runtime"])

    def test_consume_skips_deep_sleep_before_the_launch_steps(self) -> None:
        src = (TOOLS / "persona_bringup.py").read_text(encoding="utf-8")
        sleep_at = src.index("_seat_deep_sleeping(primary_binding.persona)")
        self.assertLess(sleep_at, src.index("wake_persistent_runtime(\n                    binding,"))
        self.assertLess(sleep_at, src.index("_seat_in_maintenance(primary_binding.persona) and not bypass"))


if __name__ == "__main__":
    unittest.main()
