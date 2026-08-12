"""Tests for the ORG-048 codex-recovery broker (T-2805, increment 1 — inert).

Increment 1 builds the broker artifact only: nothing is installed, no sudoers
entry exists, codex stays in group sudo. These tests exercise the broker's
own logic in-process, as the current unprivileged test user — every
"expected owner" / "expected uid" constant the real deployment would pin to
root (uid 0) is injected here as the test process's own euid, mirroring the
DI seam tools/test_credential_broker.py uses for the same reason.

Security-critical surfaces get exhaustive coverage per the brief:
manifest validation (open-time TOCTOU closure, strict parsing, account
resolution), verb/target refusal + audit, kill-session argv construction,
unstick foreground-process-group validation (the pure decision predicate,
tested with synthetic snapshots — never a real cross-account process),
audit fail-closed, and rate-limiting.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.codex_recovery import (
    AuditSink,
    AuditWriteError,
    DENYLIST_FOREGROUND_BASENAMES,
    DispatchRefusedError,
    DispatchResult,
    ForegroundSnapshot,
    ManifestValidationError,
    RateLimiter,
    TargetSpec,
    _tty_match,
    _validate_foreground_snapshot,
    build_kill_session_argv,
    build_unstick_argv,
    cmd_selftest,
    dispatch,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers.
# ---------------------------------------------------------------------------

RECOVERY_ACCOUNT = "codex-recovery-test-target"
RECOVERY_UID = 4242
RECOVERY_GID = 4242
RECOVERY_ALLOWED_EXE = "/opt/deploy/claude-wrapper/claude.exe"


def _fake_pwnam(name: str):
    table = {
        RECOVERY_ACCOUNT: SimpleNamespace(
            pw_name=RECOVERY_ACCOUNT, pw_uid=RECOVERY_UID, pw_gid=RECOVERY_GID,
            pw_dir=f"/home/{RECOVERY_ACCOUNT}", pw_shell="/bin/bash",
        ),
        "root": SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0, pw_dir="/root", pw_shell="/bin/bash"),
        "codex": SimpleNamespace(pw_name="codex", pw_uid=1003, pw_gid=1004, pw_dir="/home/codex", pw_shell="/bin/bash"),
        "daemon": SimpleNamespace(pw_name="daemon", pw_uid=1, pw_gid=1, pw_dir="/", pw_shell="/usr/sbin/nologin"),
        "nobody": SimpleNamespace(pw_name="nobody", pw_uid=65534, pw_gid=65534, pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin"),
    }
    if name not in table:
        raise KeyError(name)
    return table[name]


def _valid_target_payload() -> dict:
    return {
        "account": RECOVERY_ACCOUNT,
        "uid": RECOVERY_UID,
        "tmux_session": "claude-telegram-bot-fallback",
        "user_unit": "claude-telegram-fallback.service",
        "foreground_exe_allowlist": [RECOVERY_ALLOWED_EXE],
    }


def _manifest_payload(**target_overrides) -> dict:
    target = _valid_target_payload()
    target.update(target_overrides)
    return {"targets": {"claude-fallback": target}}


class ManifestFixtureMixin:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest_dir = self.root / "etc" / "codex-recovery"
        self.manifest_dir.mkdir(parents=True)
        self.manifest_dir.chmod(0o755)
        self.manifest_path = self.manifest_dir / "manifest.json"
        self.owner_uid = os.geteuid()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_manifest(self, payload: dict, *, mode: int = 0o644) -> None:
        self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        self.manifest_path.chmod(mode)

    def write_manifest_raw(self, raw: str, *, mode: int = 0o644) -> None:
        self.manifest_path.write_text(raw, encoding="utf-8")
        self.manifest_path.chmod(mode)

    def load(self, **kwargs):
        kwargs.setdefault("expected_owner_uid", self.owner_uid)
        kwargs.setdefault("pwnam", _fake_pwnam)
        return load_manifest(self.manifest_path, **kwargs)


# ---------------------------------------------------------------------------
# Manifest validation.
# ---------------------------------------------------------------------------

class ManifestValidationTests(ManifestFixtureMixin, unittest.TestCase):
    def test_valid_single_target_parses(self) -> None:
        self.write_manifest(_manifest_payload())
        targets = self.load()
        self.assertEqual(set(targets), {"claude-fallback"})
        target = targets["claude-fallback"]
        self.assertIsInstance(target, TargetSpec)
        self.assertEqual(target.account, RECOVERY_ACCOUNT)
        self.assertEqual(target.uid, RECOVERY_UID)
        self.assertEqual(target.tmux_session, "claude-telegram-bot-fallback")
        self.assertEqual(target.user_unit, "claude-telegram-fallback.service")
        self.assertEqual(target.foreground_exe_allowlist, (RECOVERY_ALLOWED_EXE,))

    def test_rejects_symlinked_manifest(self) -> None:
        real = self.manifest_dir / "real.json"
        real.write_text(json.dumps(_manifest_payload()), encoding="utf-8")
        real.chmod(0o644)
        self.manifest_path.symlink_to(real)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_world_writable_manifest(self) -> None:
        self.write_manifest(_manifest_payload(), mode=0o646)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_group_writable_manifest(self) -> None:
        self.write_manifest(_manifest_payload(), mode=0o664)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_bad_owner(self) -> None:
        self.write_manifest(_manifest_payload())
        with self.assertRaises(ManifestValidationError):
            self.load(expected_owner_uid=self.owner_uid + 12345)

    def test_rejects_world_writable_parent(self) -> None:
        self.write_manifest(_manifest_payload())
        self.manifest_dir.chmod(0o777)
        with self.assertRaises(ManifestValidationError):
            self.load(expected_parent_mode=0o755)

    def test_rejects_bad_parent_owner_mode(self) -> None:
        # The parent-owner UID always matches in this fixture (both are the
        # test process's own euid); exercise the parent MODE mismatch arm
        # instead — a parent directory whose permissions don't match what
        # the caller expects (e.g. 0750 instead of the real 0755).
        self.write_manifest(_manifest_payload())
        with self.assertRaises(ManifestValidationError):
            load_manifest(
                self.manifest_path,
                expected_owner_uid=self.owner_uid,
                expected_mode=stat.S_IMODE(self.manifest_path.stat().st_mode),
                expected_parent_mode=0o750,
                pwnam=_fake_pwnam,
            )

    def test_rejects_duplicate_keys(self) -> None:
        raw = '{"targets": {"claude-fallback": {"account": "a", "account": "b", "uid": 1, "tmux_session": "s", "user_unit": "u", "foreground_exe_allowlist": ["/a"]}}}'
        self.write_manifest_raw(raw)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_unknown_target_field(self) -> None:
        payload = _manifest_payload()
        payload["targets"]["claude-fallback"]["extra_field"] = "nope"
        self.write_manifest(payload)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_unknown_top_level_field(self) -> None:
        payload = _manifest_payload()
        payload["unexpected"] = 1
        self.write_manifest(payload)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_missing_target_field(self) -> None:
        payload = _manifest_payload()
        del payload["targets"]["claude-fallback"]["user_unit"]
        self.write_manifest(payload)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_shell_metacharacters_in_session(self) -> None:
        self.write_manifest(_manifest_payload(tmux_session="evil; rm -rf /"))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_shell_metacharacters_in_exe_allowlist(self) -> None:
        self.write_manifest(_manifest_payload(
            foreground_exe_allowlist=["/opt/deploy/claude$(whoami)"]
        ))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_root_account(self) -> None:
        self.write_manifest(_manifest_payload(account="root", uid=0))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_codex_account(self) -> None:
        self.write_manifest(_manifest_payload(account="codex", uid=1003))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_system_account(self) -> None:
        self.write_manifest(_manifest_payload(account="daemon", uid=1))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_nobody_pseudo_account(self) -> None:
        self.write_manifest(_manifest_payload(account="nobody", uid=65534))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_uid_mismatch(self) -> None:
        self.write_manifest(_manifest_payload(uid=RECOVERY_UID + 1))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_empty_manifest(self) -> None:
        self.write_manifest_raw("")
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_overlong_manifest(self) -> None:
        payload = _manifest_payload()
        # Pad well past MAX_MANIFEST_BYTES with a bogus-but-structurally-valid
        # blob so the size check, not the schema check, is what trips.
        raw = json.dumps(payload)
        raw = raw[:-1] + (",\"padding\": \"" + ("x" * 200000) + "\"}")
        self.write_manifest_raw(raw)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_control_chars(self) -> None:
        raw = json.dumps(_manifest_payload())
        raw = raw[:5] + "\x01" + raw[5:]
        self.write_manifest_raw(raw)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_non_dict_target(self) -> None:
        raw = '{"targets": {"claude-fallback": "not-an-object"}}'
        self.write_manifest_raw(raw)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_rejects_manifest_path_not_absolute(self) -> None:
        with self.assertRaises(ManifestValidationError):
            load_manifest(Path("relative/manifest.json"), expected_owner_uid=self.owner_uid, pwnam=_fake_pwnam)


# ---------------------------------------------------------------------------
# Verb / target dispatch refusals.
# ---------------------------------------------------------------------------

class DispatchFixtureMixin(ManifestFixtureMixin):
    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_manifest_payload())
        self.audit_path = self.root / "var" / "audit.jsonl"
        self.audit = AuditSink(self.audit_path, clock=lambda: 1000.0)
        self.rate_dir = self.root / "run" / "codex-recovery"
        self.rate_limiter = RateLimiter(self.rate_dir, clock=lambda: 1000.0)
        self.runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))

    def audit_records(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]

    def dispatch(self, **overrides):
        kwargs = dict(
            verb="kill-session",
            target_key="claude-fallback",
            action=None,
            manifest_path=self.manifest_path,
            audit=self.audit,
            rate_limiter=self.rate_limiter,
            runner=self.runner,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
            pwnam=_fake_pwnam,
        )
        kwargs.update(overrides)
        return dispatch(**kwargs)


class VerbTargetRefusalTests(DispatchFixtureMixin, unittest.TestCase):
    def test_unknown_verb_refused(self) -> None:
        result = self.dispatch(verb="restart-unit")
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()
        self.assertTrue(any(r["event"] == "refuse" for r in self.audit_records()))

    def test_unknown_target_refused(self) -> None:
        result = self.dispatch(target_key="no-such-target")
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()
        self.assertTrue(any(r["event"] == "refuse" for r in self.audit_records()))

    def test_selftest_is_not_a_dispatchable_verb(self) -> None:
        result = self.dispatch(verb="selftest")
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()


# ---------------------------------------------------------------------------
# kill-session argv construction.
# ---------------------------------------------------------------------------

class KillSessionArgvTests(unittest.TestCase):
    def test_builds_exact_expected_argv(self) -> None:
        target = TargetSpec(
            key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
            tmux_session="claude-telegram-bot-fallback",
            user_unit="claude-telegram-fallback.service",
            foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
        )
        argv = build_kill_session_argv(target, pwnam=_fake_pwnam)
        self.assertEqual(argv, [
            "/usr/bin/setpriv",
            "--reuid", str(RECOVERY_UID),
            "--regid", str(RECOVERY_GID),
            "--init-groups",
            "--reset-env",
            "--no-new-privs",
            "--",
            "/usr/bin/tmux",
            "kill-session",
            "-t", "claude-telegram-bot-fallback",
        ])

    def test_refuses_when_live_uid_no_longer_matches_manifest(self) -> None:
        target = TargetSpec(
            key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID + 1,
            tmux_session="s", user_unit="u.service",
            foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
        )
        with self.assertRaises(DispatchRefusedError):
            build_kill_session_argv(target, pwnam=_fake_pwnam)


# ---------------------------------------------------------------------------
# unstick: foreground-process-group validation (the pure predicate).
# ---------------------------------------------------------------------------

def _target() -> TargetSpec:
    return TargetSpec(
        key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
        tmux_session="claude-telegram-bot-fallback",
        user_unit="claude-telegram-fallback.service",
        foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
    )


def _good_snapshot(**overrides) -> ForegroundSnapshot:
    values = dict(
        pid=555, pgid=555, exe_path=RECOVERY_ALLOWED_EXE,
        exe_owner_uid=0, exe_mode=0o755,
        cmdline=(RECOVERY_ALLOWED_EXE,), sid=500, pane_sid=500,
        tty_matches_pane=True,
    )
    values.update(overrides)
    return ForegroundSnapshot(**values)


class ForegroundValidationTests(unittest.TestCase):
    def test_proceeds_on_allowlisted_root_owned_exe(self) -> None:
        _validate_foreground_snapshot(_good_snapshot(), _target())  # must not raise

    def test_refuses_on_shell_foreground(self) -> None:
        snap = _good_snapshot(exe_path="/bin/bash", exe_owner_uid=0)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_denylisted_basenames(self) -> None:
        for basename in ("bash", "vim", "tmux", "ssh", "sudo", "python3"):
            with self.subTest(basename=basename):
                snap = _good_snapshot(exe_path=f"/usr/bin/{basename}")
                with self.assertRaises(DispatchRefusedError):
                    _validate_foreground_snapshot(snap, _target())

    def test_refuses_exe_not_on_allowlist(self) -> None:
        snap = _good_snapshot(exe_path="/opt/other/claude.exe")
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_non_root_owned_exe(self) -> None:
        snap = _good_snapshot(exe_owner_uid=9999)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_target_owned_exe(self) -> None:
        snap = _good_snapshot(exe_owner_uid=RECOVERY_UID)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target(), deploy_owner_uids=frozenset({0, RECOVERY_UID}))

    def test_refuses_group_writable_exe(self) -> None:
        snap = _good_snapshot(exe_mode=0o775)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_world_writable_exe(self) -> None:
        snap = _good_snapshot(exe_mode=0o757)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_descendant_not_group_leader(self) -> None:
        snap = _good_snapshot(pid=556, pgid=555)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_session_id_mismatch(self) -> None:
        snap = _good_snapshot(sid=501, pane_sid=500)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())

    def test_refuses_tty_mismatch(self) -> None:
        snap = _good_snapshot(tty_matches_pane=False)
        with self.assertRaises(DispatchRefusedError):
            _validate_foreground_snapshot(snap, _target())


class TtyMatchTests(unittest.TestCase):
    def test_matching_device_numbers(self) -> None:
        self.assertTrue(_tty_match(os.makedev(136, 3), os.makedev(136, 3)))

    def test_mismatched_device_numbers(self) -> None:
        self.assertFalse(_tty_match(os.makedev(136, 3), os.makedev(136, 4)))


# ---------------------------------------------------------------------------
# unstick: control-keys-only argv, dispatch-level foreground gating.
# ---------------------------------------------------------------------------

class UnstickDispatchTests(DispatchFixtureMixin, unittest.TestCase):
    def test_unstick_argv_is_control_keys_only(self) -> None:
        argv = build_unstick_argv(_target(), "wake-drop-cancel", pwnam=_fake_pwnam)
        self.assertEqual(argv, [
            "/usr/bin/setpriv",
            "--reuid", str(RECOVERY_UID),
            "--regid", str(RECOVERY_GID),
            "--init-groups",
            "--reset-env",
            "--no-new-privs",
            "--",
            "/usr/bin/tmux",
            "send-keys",
            "-t", "claude-telegram-bot-fallback",
            "C-c",
        ])

    def test_spend_modal_dismiss_sends_escape(self) -> None:
        argv = build_unstick_argv(_target(), "spend-modal-dismiss", pwnam=_fake_pwnam)
        self.assertEqual(argv[-1], "Escape")

    def test_unknown_unstick_action_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            build_unstick_argv(_target(), "type-arbitrary-text", pwnam=_fake_pwnam)

    def test_dispatch_proceeds_when_foreground_allowlisted(self) -> None:
        probe = MagicMock(return_value=_good_snapshot())
        result = self.dispatch(verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "executed")
        self.runner.assert_called_once()
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[-1], "C-c")
        self.assertNotIn("shell", " ".join(argv).lower())

    def test_dispatch_refuses_when_foreground_is_shell(self) -> None:
        probe = MagicMock(return_value=_good_snapshot(exe_path="/bin/bash"))
        result = self.dispatch(verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_dispatch_refuses_cleanly_when_probe_raises_oserror(self) -> None:
        # A vanished pane / unreadable /proc entry raises OSError from the
        # real PaneForegroundProbe, not DispatchRefusedError — must still be
        # a clean, audited refusal, never an uncaught traceback out of
        # dispatch().
        probe = MagicMock(side_effect=OSError("pane vanished"))
        result = self.dispatch(verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("foreground probe failed", result.detail)
        self.runner.assert_not_called()

    def test_dispatch_refuses_cleanly_when_probe_raises_subprocess_error(self) -> None:
        probe = MagicMock(side_effect=subprocess.CalledProcessError(1, ["tmux"]))
        result = self.dispatch(verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("foreground probe failed", result.detail)
        self.runner.assert_not_called()


# ---------------------------------------------------------------------------
# Audit fail-closed.
# ---------------------------------------------------------------------------

class AuditFailClosedTests(DispatchFixtureMixin, unittest.TestCase):
    def test_no_action_fires_if_audit_sink_unwritable(self) -> None:
        broken_audit = MagicMock()
        broken_audit.write.side_effect = AuditWriteError("disk full")
        result = self.dispatch(audit=broken_audit)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_action_fires_when_audit_sink_healthy(self) -> None:
        result = self.dispatch()
        self.assertEqual(result.outcome, "executed")
        self.runner.assert_called_once()
        records = self.audit_records()
        self.assertTrue(any(r["event"] == "pre_action" for r in records))
        self.assertTrue(any(r["event"] == "post_action" for r in records))

    def test_dry_run_never_invokes_runner(self) -> None:
        result = self.dispatch(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.runner.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiting.
# ---------------------------------------------------------------------------

class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "rate"
        self.now = 1000.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def clock(self) -> float:
        return self.now

    def test_nth_action_in_window_refused(self) -> None:
        limiter = RateLimiter(self.state_dir, window_secs=300, max_actions=3, clock=self.clock)
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))
        self.assertFalse(limiter.check_and_record("kill-session__claude-fallback"))

    def test_window_expiry_allows_again(self) -> None:
        limiter = RateLimiter(self.state_dir, window_secs=300, max_actions=1, clock=self.clock)
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))
        self.assertFalse(limiter.check_and_record("kill-session__claude-fallback"))
        self.now += 301
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))

    def test_independent_keys_have_independent_budgets(self) -> None:
        limiter = RateLimiter(self.state_dir, window_secs=300, max_actions=1, clock=self.clock)
        self.assertTrue(limiter.check_and_record("kill-session__claude-fallback"))
        self.assertTrue(limiter.check_and_record("unstick__claude-fallback"))


class RateLimitDispatchIntegrationTests(DispatchFixtureMixin, unittest.TestCase):
    def test_dispatch_refuses_once_rate_limit_exceeded(self) -> None:
        self.rate_limiter = RateLimiter(self.rate_dir, max_actions=1, clock=lambda: 1000.0)
        first = self.dispatch()
        self.assertEqual(first.outcome, "executed")
        self.runner.reset_mock()
        second = self.dispatch()
        self.assertEqual(second.outcome, "refused")
        self.runner.assert_not_called()


# ---------------------------------------------------------------------------
# selftest.
# ---------------------------------------------------------------------------

class SelftestTests(ManifestFixtureMixin, unittest.TestCase):
    def test_selftest_reports_ok_for_a_valid_manifest(self) -> None:
        self.write_manifest(_manifest_payload())
        ok, report = cmd_selftest(
            manifest_path=self.manifest_path,
            pwnam=_fake_pwnam,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
        )
        self.assertTrue(ok)
        self.assertEqual(report["targets"], ["claude-fallback"])

    def test_selftest_reports_failure_for_an_invalid_manifest(self) -> None:
        self.write_manifest(_manifest_payload(), mode=0o646)
        ok, report = cmd_selftest(
            manifest_path=self.manifest_path,
            pwnam=_fake_pwnam,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
        )
        self.assertFalse(ok)
        self.assertIn("error", report)


# ---------------------------------------------------------------------------
# Audit sink never receives pane content.
# ---------------------------------------------------------------------------

class AuditSinkSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rejects_pane_text_field(self) -> None:
        sink = AuditSink(self.path, clock=lambda: 1.0)
        with self.assertRaises(ValueError):
            sink.write(event="x", pane_text="secret pane contents")

    def test_appends_and_flock_serializes(self) -> None:
        sink = AuditSink(self.path, clock=lambda: 1.0)
        sink.write(event="a")
        sink.write(event="b")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


# ---------------------------------------------------------------------------
# Residual-sudo audit script.
# ---------------------------------------------------------------------------

class ResidualAuditScriptTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent / "codex_recovery_audit.sh"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sudoers_dir = self.root / "sudoers.d"
        self.sudoers_dir.mkdir()
        self.scan_dir = self.root / "sbin"
        self.scan_dir.mkdir()
        self.scan_dir.chmod(0o755)  # mkdir()'s mode is umask-dependent; pin it not-group-writable
        self.owner = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def _env(self, **overrides) -> dict:
        env = dict(os.environ)
        env.update({
            "CODEX_RECOVERY_AUDIT_EXPECTED_OWNER": self.owner,
            "CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH": "/usr/local/sbin/codex-recovery",
            "CODEX_RECOVERY_AUDIT_SUDOERS_DIR": str(self.sudoers_dir),
            "CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE": str(self.root / "sudoers-main-absent"),
            "CODEX_RECOVERY_AUDIT_POLKIT_DIR": str(self.root / "polkit-absent"),
            "CODEX_RECOVERY_AUDIT_SCAN_DIRS": str(self.scan_dir),
        })
        env.update(overrides)
        return env

    def _run(self, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/usr/bin/env", "bash", str(self.SCRIPT)],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_exits_zero_for_a_clean_grant(self) -> None:
        id_file = self._write(self.root / "id.out", f"uid=1003(codex) gid=1004(codex) groups=1004(codex)\n")
        sudo_file = self._write(self.root / "sudo.out", "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        env = self._env(
            CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE=str(id_file),
            CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE=str(sudo_file),
        )
        result = self._run(env)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_exits_nonzero_for_synthetic_over_grant(self) -> None:
        id_file = self._write(self.root / "id.out", "uid=1003(codex) gid=1004(codex) groups=1004(codex),27(sudo)\n")
        sudo_file = self._write(self.root / "sudo.out", "(ALL : ALL) NOPASSWD: ALL\n")
        env = self._env(
            CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE=str(id_file),
            CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE=str(sudo_file),
        )
        result = self._run(env)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)

    def test_exits_nonzero_when_unexpected_sudoers_entry_present(self) -> None:
        id_file = self._write(self.root / "id.out", "uid=1003(codex) gid=1004(codex) groups=1004(codex)\n")
        sudo_file = self._write(self.root / "sudo.out", "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        self._write(self.sudoers_dir / "99-rogue", "codex ALL=(root) NOPASSWD: ALL\n")
        env = self._env(
            CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE=str(id_file),
            CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE=str(sudo_file),
        )
        result = self._run(env)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_exits_zero_with_the_correctly_pinned_sudoers_entry_present(self) -> None:
        # Regression guard for the '[^#].*codex' consumption bug: a line that
        # STARTS with "codex" (the exact shape every real entry has) must be
        # recognized as a codex reference and accepted when it matches the
        # pinned shape — not silently treated as "no codex references".
        id_file = self._write(self.root / "id.out", "uid=1003(codex) gid=1004(codex) groups=1004(codex)\n")
        sudo_file = self._write(self.root / "sudo.out", "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        self._write(
            self.sudoers_dir / "90-codex-recovery",
            "codex ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery\n",
        )
        env = self._env(
            CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE=str(id_file),
            CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE=str(sudo_file),
        )
        result = self._run(env)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("codex reference(s) match only the pinned broker line", result.stdout)


if __name__ == "__main__":
    unittest.main()
