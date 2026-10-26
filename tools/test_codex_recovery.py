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

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.codex_recovery import (
    AuditSink,
    AuditWriteError,
    BrokerContext,
    DENYLIST_FOREGROUND_BASENAMES,
    DispatchRefusedError,
    DispatchResult,
    ForegroundSnapshot,
    Invoker,
    LEASE_EXPORT_DIR_MODE,
    LEASE_EXPORT_FILE_MODE,
    LEASE_TTL_SECS,
    LeaseExportSink,
    LeaseStore,
    ManifestValidationError,
    PaneForegroundProbe,
    RateLimiter,
    SETPRIV,
    TargetSpec,
    _tty_match,
    _validate_foreground_snapshot,
    build_kill_session_argv,
    build_reset_failed_argv,
    build_restart_argv,
    build_unstick_argv,
    check_precondition,
    cmd_selftest,
    dispatch,
    dispatch_lease,
    dispatch_linger_enable,
    load_broker_manifest,
    load_manifest,
    main as broker_main,
    real_linger_probe,
    resolve_invoker,
)
from tools.codex_recovery_sudoers import (
    SudoersGenerationError,
    generate as generate_sudoers,
    load_for_generation,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers.
# ---------------------------------------------------------------------------

RECOVERY_ACCOUNT = "codex-recovery-test-target"
RECOVERY_UID = 4242
RECOVERY_GID = 4242
RECOVERY_ALLOWED_EXE = "/opt/deploy/claude-wrapper/claude.exe"

# Invokers (T-4884 B4): the recovery seat and Recovery. Fake uids, resolved only
# through the injected pwnam/pwuid seams.
WATCHMAN = "codex-fallback"
WATCHMAN_UID = 4300
RECOVERY = "claude-fallback"
RECOVERY_INVOKER_UID = 4301
STRANGER = "mason-test"
STRANGER_UID = 4302
ALL_VERBS = ["kill-session", "unstick", "selftest", "lease"]


# T-5124: extra fake accounts (the third invoker, evidence writers); tests extend it.
EXTRA_PW: dict = {}


def _fake_pwnam(name: str):
    if name in EXTRA_PW:
        return EXTRA_PW[name]
    table = {
        WATCHMAN: SimpleNamespace(pw_name=WATCHMAN, pw_uid=WATCHMAN_UID, pw_gid=WATCHMAN_UID,
                                  pw_dir=f"/home/{WATCHMAN}", pw_shell="/bin/bash"),
        RECOVERY: SimpleNamespace(pw_name=RECOVERY, pw_uid=RECOVERY_INVOKER_UID, pw_gid=RECOVERY_INVOKER_UID,
                              pw_dir=f"/home/{RECOVERY}", pw_shell="/bin/bash"),
        STRANGER: SimpleNamespace(pw_name=STRANGER, pw_uid=STRANGER_UID, pw_gid=STRANGER_UID,
                                  pw_dir=f"/home/{STRANGER}", pw_shell="/bin/bash"),
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


def _fake_pwuid(uid: int):
    for name in (*EXTRA_PW, WATCHMAN, RECOVERY, STRANGER, RECOVERY_ACCOUNT, "root", "codex", "daemon", "nobody"):
        entry = _fake_pwnam(name)
        if entry.pw_uid == uid:
            return entry
    raise KeyError(uid)


def _default_invokers() -> dict:
    return {WATCHMAN: {"verbs": list(ALL_VERBS)}, RECOVERY: {"verbs": ["lease"]}}


def _valid_target_payload() -> dict:
    return {
        "account": RECOVERY_ACCOUNT,
        "uid": RECOVERY_UID,
        "tmux_session": "claude-telegram-bot-fallback",
        "user_unit": "claude-telegram-fallback.service",
        "foreground_exe_allowlist": [RECOVERY_ALLOWED_EXE],
    }


def _manifest_payload(*, invokers: dict | None = None, **target_overrides) -> dict:
    target = _valid_target_payload()
    target.update(target_overrides)
    return {
        "targets": {"claude-fallback": target},
        "invokers": _default_invokers() if invokers is None else invokers,
    }


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
        raw = '{"invokers": {"codex-fallback": {"verbs": ["lease"]}}, "targets": {"claude-fallback": {"account": "a", "account": "b", "uid": 1, "tmux_session": "s", "user_unit": "u", "foreground_exe_allowlist": ["/a"]}}}'
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
        raw = '{"invokers": {"codex-fallback": {"verbs": ["lease"]}}, "targets": {"claude-fallback": "not-an-object"}}'
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
    # Most dispatch tests exercise the post-lease path, so the fixture takes
    # Watchman's lease on the target up front; lease tests switch this off.
    auto_lease = True

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_manifest_payload())
        self.now = 1000.0
        self.audit_path = self.root / "var" / "audit.jsonl"
        self.audit = AuditSink(self.audit_path, clock=lambda: 1000.0)
        (self.root / "run").mkdir()
        self.rate_dir = self.root / "run" / "codex-recovery"
        self.rate_limiter = RateLimiter(self.rate_dir, clock=lambda: 1000.0)
        self.lease_store = LeaseStore(self.rate_dir, expected_owner_uid=self.owner_uid, clock=lambda: self.now)
        self.watchman = Invoker(uid=WATCHMAN_UID, user=WATCHMAN)
        self.recovery = Invoker(uid=RECOVERY_INVOKER_UID, user=RECOVERY)
        self.runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        if self.auto_lease:
            self.assertEqual(self.lease(op="acquire").outcome, "executed")

    def lease(self, **overrides):
        kwargs = dict(
            op="acquire",
            target_key="claude-fallback",
            invoker=self.watchman,
            manifest_path=self.manifest_path,
            audit=self.audit,
            lease_store=self.lease_store,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
            pwnam=_fake_pwnam,
        )
        kwargs.update(overrides)
        return dispatch_lease(**kwargs)

    def audit_records(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]

    def dispatch(self, **overrides):
        kwargs = dict(
            verb="kill-session",
            target_key="claude-fallback",
            action=None,
            invoker=self.watchman,
            lease_store=self.lease_store,
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


class PaneProbeTargetSocketTests(unittest.TestCase):
    """T-5046: the broker runs as root, so the pane query must drop to the
    target uid or tmux reads root's server (/tmp/tmux-0), not the target's."""

    def _runner(self, *, returncode: int = 0, stdout: str = "4321 /dev/pts/7\n"):
        return MagicMock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""))

    def test_pane_query_runs_as_the_target_uid(self) -> None:
        runner = self._runner()
        probe = PaneForegroundProbe(runner=runner, pwnam=_fake_pwnam)
        self.assertEqual(probe._pane_facts(_target()), (4321, "/dev/pts/7"))
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], SETPRIV)
        self.assertEqual(argv[argv.index("--reuid") + 1], str(RECOVERY_UID))
        tmux_argv = argv[argv.index("--") + 1:]
        self.assertEqual(tmux_argv[1:5], ["display-message", "-p", "-t", "claude-telegram-bot-fallback"])
        self.assertEqual(runner.call_args.kwargs["env"]["XDG_RUNTIME_DIR"], f"/run/user/{RECOVERY_UID}")
        # display-message -p rewrites a tab to "_"; the format must use only
        # printable separators or the live split fails.
        self.assertNotIn("\t", tmux_argv[-1])

    def test_pane_query_refuses_when_account_uid_drifted(self) -> None:
        runner = self._runner()
        drifted = lambda name: SimpleNamespace(pw_uid=RECOVERY_UID + 1, pw_gid=RECOVERY_UID + 1, pw_dir="/x", pw_shell="/bin/sh")
        probe = PaneForegroundProbe(runner=runner, pwnam=drifted)
        with self.assertRaises(DispatchRefusedError):
            probe._pane_facts(_target())
        runner.assert_not_called()

    def test_pane_query_failure_raises_subprocess_error(self) -> None:
        probe = PaneForegroundProbe(runner=self._runner(returncode=1, stdout=""), pwnam=_fake_pwnam)
        with self.assertRaises(subprocess.CalledProcessError):
            probe._pane_facts(_target())


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
        # T-5046: the probe gets the whole target (uid + session), not a bare
        # session name it would resolve against the caller's tmux server.
        self.assertIsInstance(probe.call_args.args[0], TargetSpec)
        self.assertEqual(probe.call_args.args[0].uid, RECOVERY_UID)

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
        # Legacy `codex` data sources default to a CLEAN synthetic fixture so
        # no test ever reads the live host's `id codex` / `sudo -l -U codex`.
        legacy_id = self._write(self.root / "legacy-id.out", "uid=1003(codex) gid=1004(codex) groups=1004(codex)\n")
        legacy_sudo = self._write(self.root / "legacy-sudo.out", "User codex is not allowed to run sudo on host.\n")
        env.update({
            "CODEX_RECOVERY_AUDIT_LEGACY_ID_OUTPUT_FILE": str(legacy_id),
            "CODEX_RECOVERY_AUDIT_LEGACY_SUDO_L_OUTPUT_FILE": str(legacy_sudo),
            "CODEX_RECOVERY_AUDIT_EXPECTED_OWNER": self.owner,
            "CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH": "/usr/local/sbin/codex-recovery",
            # T-5142: inspect a fixture path, never the host's real (root-owned) broker. Absent by
            # default, as on CI; a test that checks the file creates it here.
            "CODEX_RECOVERY_AUDIT_BROKER_FILE": str(self.scan_dir / "codex-recovery"),
            "CODEX_RECOVERY_AUDIT_SUDOERS_DIR": str(self.sudoers_dir),
            "CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE": str(self.root / "sudoers-main-absent"),
            "CODEX_RECOVERY_AUDIT_POLKIT_DIR": str(self.root / "polkit-absent"),
            "CODEX_RECOVERY_AUDIT_SCAN_DIRS": str(self.scan_dir),
            # T-5036: fixed ids, so no test reads the live host's `id codex`.
            "CODEX_RECOVERY_AUDIT_CODEX_NUMERIC_IDS": "1003 1004 1011 1014",
        })
        env.update(overrides)
        # T-5142: a test that names its own broker path inspects that file, not the default fixture.
        if ("CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH" in overrides
                and "CODEX_RECOVERY_AUDIT_BROKER_FILE" not in overrides):
            env["CODEX_RECOVERY_AUDIT_BROKER_FILE"] = overrides["CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH"]
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
        # Re-homed (T-4884 B4): the pinned line now names codex-fallback.
        id_file = self._write(self.root / "id.out", "uid=1010(codex-fallback) gid=1010(codex-fallback) groups=1010(codex-fallback)\n")
        sudo_file = self._write(self.root / "sudo.out", "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        self._write(
            self.sudoers_dir / "90-codex-recovery",
            "codex-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery\n",
        )
        env = self._env(
            CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE=str(id_file),
            CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE=str(sudo_file),
        )
        result = self._run(env)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("codex reference(s) match only the pinned broker line", result.stdout)

    def _clean_seams(self) -> dict:
        id_file = self._write(self.root / "id.out", "uid=1010(codex-fallback) gid=1010(codex-fallback) groups=1010(codex-fallback)\n")
        sudo_file = self._write(self.root / "sudo.out", "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        return {
            "CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE": str(id_file),
            "CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE": str(sudo_file),
        }

    def test_legacy_codex_broker_line_now_fails(self) -> None:
        # Re-homing: the old `codex ALL=(root) ... codex-recovery` line is no
        # longer an allowed shape; codex gets no recovery grant (§10).
        self._write(self.sudoers_dir / "90-codex-recovery",
                    "codex ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery\n")
        result = self._run(self._env(**self._clean_seams()))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_legacy_codex_sudo_grant_fails(self) -> None:
        legacy_sudo = self._write(self.root / "legacy-sudo-bad.out", "    (ALL : ALL) ALL\n")
        result = self._run(self._env(**self._clean_seams(),
                                     CODEX_RECOVERY_AUDIT_LEGACY_SUDO_L_OUTPUT_FILE=str(legacy_sudo)))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("codex still holds sudo grant", result.stdout)

    def test_generated_fragment_passes_the_audit(self) -> None:
        sample = Path(__file__).resolve().parent / "codex_recovery_manifest.sample.json"
        self._write(self.sudoers_dir / "90-codex-recovery", generate_sudoers(load_for_generation(sample)))
        result = self._run(self._env(**self._clean_seams()))
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("no wildcard in any codex-recovery line", result.stdout)

    def test_wildcard_in_codex_recovery_line_fails(self) -> None:
        self._write(self.sudoers_dir / "90-codex-recovery",
                    "claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery lease *\n")
        result = self._run(self._env(**self._clean_seams()))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("wildcard in a codex-recovery line", result.stdout)

    def test_lease_account_extra_verb_line_fails(self) -> None:
        # Recovery may hold only exact lease lines — never the bare broker or kill-session.
        self._write(self.sudoers_dir / "90-codex-recovery",
                    "claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery kill-session claude-fallback\n")
        result = self._run(self._env(**self._clean_seams()))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    # --- T-4884 B7: what B7 grants by ratification (the operator TG 11705 mutual sudo;
    # design §4.4 lease audit needs systemd-journal) must PASS; near-misses must FAIL.
    _B7_SUDOERS = {
        "56-recovery-rescues-watchman": "# T-4884 B7\nclaude-fallback ALL=(codex-fallback) NOPASSWD: ALL\n",
        "56-watchman-rescues-recovery": "# T-4884 B7\ncodex-fallback ALL=(claude-fallback) NOPASSWD: ALL\n",
    }

    def _b7_seams(self, sudo_l: str = "(root) NOPASSWD: /usr/local/sbin/codex-recovery\n"
                                      "(claude-fallback) NOPASSWD: ALL\n",
                  groups: str = "1011(codex-fallback),1006(pantheon-mailbox),1013(pantheon-health),999(systemd-journal)") -> dict:
        id_file = self._write(self.root / "id-b7.out", f"uid=1011(codex-fallback) gid=1011(codex-fallback) groups={groups}\n")
        sudo_file = self._write(self.root / "sudo-b7.out", sudo_l)
        for name, text in self._B7_SUDOERS.items():
            self._write(self.sudoers_dir / name, text)
        sample = Path(__file__).resolve().parent / "codex_recovery_manifest.sample.json"
        self._write(self.sudoers_dir / "90-codex-recovery", generate_sudoers(load_for_generation(sample)))
        return {"CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE": str(id_file),
                "CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE": str(sudo_file)}

    def test_b7_mutual_sudo_and_journal_group_pass(self) -> None:
        result = self._run(self._env(**self._b7_seams()))
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_recovery_account_all_as_root_or_any_still_fails(self) -> None:
        for extra in ("(root) NOPASSWD: ALL\n", "(ALL) NOPASSWD: ALL\n", "(ALL : ALL) NOPASSWD: ALL\n",
                      "(claude) NOPASSWD: ALL\n", "(claude-fallback, root) NOPASSWD: ALL\n"):
            with self.subTest(extra=extra):
                seams = self._b7_seams(sudo_l="(root) NOPASSWD: /usr/local/sbin/codex-recovery\n" + extra)
                result = self._run(self._env(**seams))
                self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_recovery_account_missing_broker_line_fails(self) -> None:
        seams = self._b7_seams(sudo_l="(claude-fallback) NOPASSWD: ALL\n")
        self.assertNotEqual(self._run(self._env(**seams)).returncode, 0)

    def test_journal_group_still_dangerous_for_legacy_codex(self) -> None:
        legacy_id = self._write(self.root / "legacy-id-j.out",
                                "uid=1003(codex) gid=1004(codex) groups=1004(codex),999(systemd-journal)\n")
        result = self._run(self._env(**self._b7_seams(),
                                     CODEX_RECOVERY_AUDIT_LEGACY_ID_OUTPUT_FILE=str(legacy_id)))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_recovery_account_sudo_group_still_fails(self) -> None:
        seams = self._b7_seams(groups="1011(codex-fallback),27(sudo),999(systemd-journal)")
        self.assertNotEqual(self._run(self._env(**seams)).returncode, 0)

    def test_recovery_runas_line_mentioning_codex_passes_unless_it_includes_root(self) -> None:
        # Recovery's pre-existing asymmetric grant (ORG-063 r4) names codex as a RUN-AS
        # target: that's Recovery's privilege, not codex's. Allowed only without root/ALL.
        self._write(self.sudoers_dir / "60-fallback-fleet",
                    "claude-fallback ALL=(claude-research, codex, suborch1) NOPASSWD: ALL\n")
        self.assertEqual(self._run(self._env(**self._b7_seams())).returncode, 0)
        self._write(self.sudoers_dir / "60-fallback-fleet",
                    "claude-fallback ALL=(codex, root) NOPASSWD: ALL\n")
        self.assertNotEqual(self._run(self._env(**self._b7_seams())).returncode, 0)

    # --- T-5033: exact-line allowlist for the ratified host lines.
    _T5033_ALLOWED = (
        "claude-fallback ALL=(codex) NOPASSWD: ALL",
        "claude-fallback ALL=(codex-fallback) NOPASSWD: ALL",
        "codex-fallback ALL=(claude-fallback) NOPASSWD: ALL",
        "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux",
    )

    def _run_with_line(self, line: str) -> subprocess.CompletedProcess:
        seams = self._b7_seams()
        self._write(self.sudoers_dir / "70-t5033", line + "\n")
        return self._run(self._env(**seams))

    def test_t5033_each_ratified_line_passes(self) -> None:
        for line in self._T5033_ALLOWED:
            with self.subTest(line=line):
                r = self._run_with_line(line)
                self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5033_whitespace_variants_of_ratified_lines_pass(self) -> None:
        for line in ("claude   ALL = (codex)\tNOPASSWD : /usr/local/bin/pantheon-s3star-tmux",
                     "  claude-fallback\tALL=(codex)  NOPASSWD:  ALL  "):
            with self.subTest(line=line):
                r = self._run_with_line(line)
                self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5033_unlisted_and_near_miss_lines_fail(self) -> None:
        for line in ("claude ALL=(codex) NOPASSWD: ALL",
                     "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux *",
                     "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux2",
                     "claude ALL=(codex) NOPASSWD: /tmp/pantheon-s3star-tmux",
                     "claude ALL=(root) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux codex",
                     "claude-fallback ALL=(codex, root) NOPASSWD: ALL",
                     "claude-fallback ALL=(codex) NOPASSWD: /bin/sh",
                     "codex-fallback ALL=(claude) NOPASSWD: ALL",
                     "codex-fallback ALL=(root) NOPASSWD: ALL",
                     "codex-fallback ALL=(claude-fallback) NOPASSWD: /bin/sh"):
            with self.subTest(line=line):
                r = self._run_with_line(line)
                self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5033_quote_and_backslash_variants_fail(self) -> None:
        s3 = "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux"
        for line in (s3 + "\\",
                     s3 + " \\",
                     'claude ALL=(codex) NOPASSWD: "/usr/local/bin/pantheon-s3star-tmux"',
                     "claude ALL=(codex) NOPASSWD: '/usr/local/bin/pantheon-s3star-tmux'",
                     "claude-fallback ALL=(codex) NOPASSWD: ALL\\"):
            with self.subTest(line=line):
                r = self._run_with_line(line)
                self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5033_continuation_touching_codex_line_fails(self) -> None:
        s3 = "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux"
        cases = {
            "codex-then-tail": s3 + "\\\n, /bin/sh",
            "into-codex": "Cmnd_Alias FOO = /bin/true, \\\n" + s3,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                seams = self._b7_seams()
                self._write(self.sudoers_dir / "70-t5033", text + "\n")
                r = self._run(self._env(**seams))
                self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)
                self.assertIn("unexpected codex reference", r.stdout)

    def test_t5033_continuation_not_touching_codex_is_ignored(self) -> None:
        seams = self._b7_seams()
        self._write(self.sudoers_dir / "70-t5033",
                    "Cmnd_Alias FOO = /bin/true, \\\n/bin/false\n")
        r = self._run(self._env(**seams))
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    # T-5036: indirection the codex grep cannot see.
    def test_t5036_numeric_codex_id_fails_in_every_position(self) -> None:
        cases = {
            "uid-user-spec": "#1011 ALL=(root) NOPASSWD: ALL",
            "gid-user-spec": "%#1004 ALL=(ALL) ALL",
            "runas-uid": "web ALL=(#1003) NOPASSWD: ALL",
            "runas-alias": "Runas_Alias R = #1003\nweb ALL=(R) NOPASSWD: ALL",
            "user-alias": "User_Alias U = #1011, alice\nU ALL=(root) ALL",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self._write(self.sudoers_dir / "70-t5036", text + "\n")
                r = self._run(self._env(**self._b7_seams()))
                self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)
                self.assertIn("numeric id of a codex account", r.stdout)

    def test_t5036_named_codex_alias_still_fails(self) -> None:
        self._write(self.sudoers_dir / "70-t5036",
                    "Runas_Alias R = codex\nweb ALL=(R) NOPASSWD: ALL\n")
        r = self._run(self._env(**self._b7_seams()))
        self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5036_other_numeric_ids_and_comments_pass(self) -> None:
        self._write(self.sudoers_dir / "70-t5036",
                    "# uid 1011 is codex-fallback (a comment, not a spec)\n"
                    "#  #1011 ALL=(ALL) ALL   (commented out)\n"
                    "%#2000 ALL=(root) NOPASSWD: /usr/bin/true\n"
                    "web ALL=(#10110) NOPASSWD: /usr/bin/true\n")
        r = self._run(self._env(**self._b7_seams()))
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5036_any_include_in_sudoers_d_fails(self) -> None:
        for directive in ("@include /etc/extra", "#include /etc/extra",
                          "@includedir /etc/more.d", "#includedir /etc/more.d"):
            with self.subTest(directive=directive):
                self._write(self.sudoers_dir / "70-t5036", directive + "\n")
                r = self._run(self._env(**self._b7_seams()))
                self.assertNotEqual(r.returncode, 0, msg=r.stdout + r.stderr)
                self.assertIn("include outside the scanned set", r.stdout)

    def test_t5036_main_file_may_include_only_the_scanned_dir(self) -> None:
        main = self.root / "sudoers"
        for text, ok in ((f"Defaults env_reset\n@includedir {self.sudoers_dir}\n", True),
                         (f"#includedir {self.sudoers_dir}/\n", True),
                         ("@includedir /etc/elsewhere.d\n", False),
                         (f"@includedir {self.sudoers_dir}\n@include /etc/sudoers.local\n", False)):
            with self.subTest(text=text):
                self._write(main, text)
                r = self._run(self._env(CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE=str(main),
                                        **self._b7_seams()))
                self.assertEqual(r.returncode == 0, ok, msg=r.stdout + r.stderr)

    def test_t5033_long_non_codex_cmnd_alias_block_still_passes(self) -> None:
        # Mirrors the live /etc/sudoers.d/pantheon-pwa KINO_SVC block shape.
        seams = self._b7_seams()
        units = ["restart", "start", "stop", "reload", "status"]
        body = ", \\\n".join(f"/usr/bin/systemctl {u} kino" for u in units * 2)
        self._write(self.sudoers_dir / "pantheon-pwa",
                    "Cmnd_Alias KINO_SVC = " + body + "\nweb ALL=(root) NOPASSWD: KINO_SVC\n")
        r = self._run(self._env(**seams))
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_t5033_fail_message_names_only_disallowed_lines(self) -> None:
        rogue = "claude ALL=(codex) NOPASSWD: ALL"
        seams = self._b7_seams()
        self._write(self.sudoers_dir / "70-t5033",
                    "claude-fallback ALL=(codex) NOPASSWD: ALL\n" + rogue + "\n")
        r = self._run(self._env(**seams))
        self.assertNotEqual(r.returncode, 0)
        fail_lines = [l for l in r.stdout.splitlines() if "unexpected codex reference" in l]
        self.assertEqual(len(fail_lines), 1, msg=r.stdout)
        self.assertIn(rogue, fail_lines[0])
        self.assertNotIn("claude-fallback ALL=(codex) NOPASSWD: ALL", fail_lines[0])
        self.assertNotIn("codex-recovery", fail_lines[0])

    def test_t5059_linger_enable_exact_line_is_an_allowed_codex_reference(self) -> None:
        b = "/usr/local/sbin/codex-recovery"
        self._write(self.sudoers_dir / "70-t5059",
                    f"claude-fallback ALL=(root) NOPASSWD: {b} linger-enable claude-fallback\n")
        r = self._run(self._env(**self._b7_seams()))
        self.assertNotIn("unexpected codex reference", r.stdout)
        self._write(self.sudoers_dir / "71-t5059",
                    f"claude-fallback ALL=(root) NOPASSWD: {b} linger-enable claude-fallback extra\n")
        r = self._run(self._env(**self._b7_seams()))
        self.assertIn("unexpected codex reference", r.stdout)

    def _broker_with_first_line(self, first_line: str) -> Path:
        broker = self.scan_dir / "codex-recovery"
        broker.write_text(first_line + "\n\"\"\"broker\"\"\"\n", encoding="utf-8")
        broker.chmod(0o755)
        return broker

    def test_correct_interpreter_line_passes(self) -> None:
        broker = self._broker_with_first_line("#!/usr/bin/python3 -I")
        result = self._run(self._env(**self._clean_seams(),
                                     CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH=str(broker)))
        # (The overridden broker path also changes check 2's pinned line, so
        # assert on the interpreter check's own verdict only.)
        self.assertIn("[ok]   " + str(broker) + " interpreter line is '#!/usr/bin/python3 -I'", result.stdout)
        self.assertNotIn("expected '#!/usr/bin/python3 -I'", result.stdout)

    def test_wrong_interpreter_line_fails(self) -> None:
        broker = self._broker_with_first_line("#!/usr/bin/env python3")
        result = self._run(self._env(**self._clean_seams(),
                                     CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH=str(broker)))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("expected '#!/usr/bin/python3 -I'", result.stdout)

    def test_repo_broker_carries_the_isolated_interpreter_line(self) -> None:
        broker_src = Path(__file__).resolve().parent / "codex_recovery.py"
        first = broker_src.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -I")


# ---------------------------------------------------------------------------
# T-4884 B4 — invoker identity.
# ---------------------------------------------------------------------------

def _sudo_env(user: str | None, uid: int | str | None) -> dict:
    env: dict = {}
    if user is not None:
        env["SUDO_USER"] = user
    if uid is not None:
        env["SUDO_UID"] = str(uid)
    return env


class InvokerIdentityTests(unittest.TestCase):
    def resolve(self, environ, *, real_uid: int = 0):
        return resolve_invoker(environ, getuid=lambda: real_uid, pwuid=_fake_pwuid)

    def test_valid_sudo_caller_resolves(self) -> None:
        inv = self.resolve(_sudo_env(WATCHMAN, WATCHMAN_UID))
        self.assertEqual(inv, Invoker(uid=WATCHMAN_UID, user=WATCHMAN))

    def test_missing_sudo_uid_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env(WATCHMAN, None))

    def test_missing_sudo_user_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env(None, WATCHMAN_UID))

    def test_mismatched_sudo_user_refused(self) -> None:
        # SUDO_USER claims Watchman but SUDO_UID is Recovery's.
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env(WATCHMAN, RECOVERY_INVOKER_UID))

    def test_non_numeric_sudo_uid_refused(self) -> None:
        for bad in ("4300x", "-1", " 4300", "", "1" * 11):
            with self.subTest(bad=bad):
                with self.assertRaises(DispatchRefusedError):
                    self.resolve({"SUDO_USER": WATCHMAN, "SUDO_UID": bad})

    def test_unresolvable_sudo_uid_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env(WATCHMAN, 9999))

    def test_real_uid_not_root_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env(WATCHMAN, WATCHMAN_UID), real_uid=WATCHMAN_UID)

    def test_codex_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env("codex", 1003))

    def test_root_refused(self) -> None:
        with self.assertRaises(DispatchRefusedError):
            self.resolve(_sudo_env("root", 0))


class InvokerManifestTests(ManifestFixtureMixin, unittest.TestCase):
    def test_invokers_parse(self) -> None:
        self.write_manifest(_manifest_payload())
        manifest = load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid, pwnam=_fake_pwnam)
        self.assertEqual(manifest.invokers[WATCHMAN].uid, WATCHMAN_UID)
        self.assertEqual(manifest.invokers[WATCHMAN].verbs, frozenset(ALL_VERBS))
        self.assertEqual(manifest.invokers[RECOVERY].verbs, frozenset({"lease"}))

    def _rejects(self, invokers) -> None:
        self.write_manifest(_manifest_payload(invokers=invokers))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_missing_invokers_rejected(self) -> None:
        payload = _manifest_payload()
        del payload["invokers"]
        self.write_manifest(payload)
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_empty_invokers_rejected(self) -> None:
        self._rejects({})

    def test_codex_invoker_rejected(self) -> None:
        self._rejects({"codex": {"verbs": ["lease"]}})

    def test_root_invoker_rejected(self) -> None:
        self._rejects({"root": {"verbs": ["lease"]}})

    def test_system_account_invoker_rejected(self) -> None:
        self._rejects({"daemon": {"verbs": ["lease"]}})

    def test_unresolvable_invoker_rejected(self) -> None:
        self._rejects({"no-such-account": {"verbs": ["lease"]}})

    def test_unknown_invoker_field_rejected(self) -> None:
        self._rejects({WATCHMAN: {"verbs": ["lease"], "ttl": 99999}})

    def test_unknown_verb_rejected(self) -> None:
        self._rejects({WATCHMAN: {"verbs": ["lease", "restore"]}})

    def test_duplicate_verbs_rejected(self) -> None:
        self._rejects({WATCHMAN: {"verbs": ["lease", "lease"]}})

    def test_empty_verbs_rejected(self) -> None:
        self._rejects({WATCHMAN: {"verbs": []}})

    def test_duplicate_invoker_key_rejected(self) -> None:
        raw = json.dumps(_manifest_payload())
        raw = raw.replace('"invokers": {', '"invokers": {"codex-fallback": {"verbs": ["lease"]}, ', 1)
        self.write_manifest_raw(raw)
        with self.assertRaises(ManifestValidationError):
            self.load()


class InvokerAuthorizationTests(DispatchFixtureMixin, unittest.TestCase):
    def test_invoker_not_in_manifest_refused(self) -> None:
        stranger = Invoker(uid=STRANGER_UID, user=STRANGER)
        result = self.dispatch(invoker=stranger)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("not listed", result.detail)
        self.runner.assert_not_called()
        self.assertEqual(self.lease(invoker=stranger).outcome, "refused")

    def test_invoker_uid_mismatch_refused(self) -> None:
        result = self.dispatch(invoker=Invoker(uid=RECOVERY_INVOKER_UID, user=WATCHMAN))
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_recovery_kill_session_refused_even_holding_lease(self) -> None:
        # Recovery's grant is lease-only: even with Recovery holding the lease,
        # kill-session is refused at the verb-authorization step.
        self.now += LEASE_TTL_SECS  # Watchman's fixture lease expires
        self.assertEqual(self.lease(invoker=self.recovery).outcome, "executed")
        result = self.dispatch(invoker=self.recovery)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("not allowed verb", result.detail)
        self.runner.assert_not_called()

    def test_recovery_unstick_refused(self) -> None:
        probe = MagicMock(return_value=_good_snapshot())
        result = self.dispatch(invoker=self.recovery, verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()
        probe.assert_not_called()

    def test_refusals_are_audited_with_invoker(self) -> None:
        self.dispatch(invoker=self.recovery)
        refusals = [r for r in self.audit_records() if r["event"] == "refuse"]
        self.assertTrue(refusals)
        self.assertEqual(refusals[-1]["invoker_user"], RECOVERY)
        self.assertEqual(refusals[-1]["invoker_uid"], RECOVERY_INVOKER_UID)


# ---------------------------------------------------------------------------
# T-4884 B4 — the lease verb.
# ---------------------------------------------------------------------------

class LeaseVerbTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False

    def lease_dir(self) -> Path:
        return self.rate_dir / "leases"

    def record(self) -> dict | None:
        path = self.lease_dir() / "claude-fallback.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def test_acquire_free_lease(self) -> None:
        result = self.lease()
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(self.record(), {
            "holder_uid": WATCHMAN_UID, "holder_user": WATCHMAN,
            "acquired_at": 1000, "expires_at": 1000 + LEASE_TTL_SECS,
        })
        self.assertEqual(stat.S_IMODE(self.lease_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.lease_dir() / "claude-fallback.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.lease_dir() / "claude-fallback.lock").stat().st_mode), 0o600)

    def test_ttl_is_fifteen_minutes(self) -> None:
        self.assertEqual(LEASE_TTL_SECS, 900)

    def test_same_uid_renews(self) -> None:
        self.lease()
        self.now += 600
        result = self.lease()
        self.assertEqual(result.outcome, "executed")
        self.assertIn("renewed", result.detail)
        self.assertEqual(self.record()["expires_at"], 1600 + LEASE_TTL_SECS)

    def test_conflict_names_holder_and_expiry(self) -> None:
        self.assertEqual(self.lease(invoker=self.recovery).outcome, "executed")
        before = self.record()
        result = self.lease(invoker=self.watchman)
        self.assertEqual(result.outcome, "refused")
        self.assertIn(RECOVERY, result.detail)
        self.assertIn("1970-01-01T00:31:40Z", result.detail)  # 1000 + 900
        self.assertEqual(self.record(), before)

    def test_expired_lease_can_be_taken(self) -> None:
        self.lease(invoker=self.recovery)
        self.now += LEASE_TTL_SECS
        result = self.lease(invoker=self.watchman)
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(self.record()["holder_user"], WATCHMAN)

    def test_release_by_non_holder_refused(self) -> None:
        self.lease(invoker=self.recovery)
        before = self.record()
        result = self.lease(op="release", invoker=self.watchman)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(self.record(), before)

    def test_release_by_holder(self) -> None:
        self.lease(invoker=self.recovery)
        result = self.lease(op="release", invoker=self.recovery)
        self.assertEqual(result.outcome, "executed")
        self.assertIsNone(self.record())

    def test_expired_lease_release_by_anyone(self) -> None:
        self.lease(invoker=self.recovery)
        self.now += LEASE_TTL_SECS + 1
        result = self.lease(op="release", invoker=self.watchman)
        self.assertEqual(result.outcome, "executed")
        self.assertIsNone(self.record())

    def test_status_lines_fixed_format(self) -> None:
        self.assertEqual(
            self.lease(op="status").detail,
            "lease target=claude-fallback state=free holder=- holder_uid=- expires_at=- remaining_secs=-",
        )
        self.lease(invoker=self.recovery)
        self.now += 100
        self.assertEqual(
            self.lease(op="status").detail,
            f"lease target=claude-fallback state=held holder={RECOVERY} holder_uid={RECOVERY_INVOKER_UID} "
            "expires_at=1970-01-01T00:31:40Z remaining_secs=800",
        )
        self.now += 800
        self.assertEqual(
            self.lease(op="status").detail,
            f"lease target=claude-fallback state=expired holder={RECOVERY} holder_uid={RECOVERY_INVOKER_UID} "
            "expires_at=1970-01-01T00:31:40Z remaining_secs=0",
        )

    def test_all_lease_ops_audited(self) -> None:
        self.lease()
        self.lease(op="status")
        self.lease(op="release")
        events = [r["event"] for r in self.audit_records()]
        self.assertEqual(events, ["lease_acquire", "lease_status", "lease_release"])

    def test_option_like_and_unknown_targets_refused_before_state(self) -> None:
        for bad in ("-x", "--all", "-", "no-such-target", "../etc", "a b"):
            with self.subTest(target=bad):
                result = self.lease(target_key=bad)
                self.assertEqual(result.outcome, "refused")
                self.assertFalse(self.rate_dir.exists(), "state dir must not be created by a refused target")

    def test_unknown_op_refused(self) -> None:
        self.assertEqual(self.lease(op="steal").outcome, "refused")
        self.assertFalse(self.rate_dir.exists())

    def test_symlinked_lease_dir_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.rate_dir.mkdir(mode=0o700)
        (self.rate_dir / "leases").symlink_to(elsewhere)
        result = self.lease()
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_symlinked_lease_file_refused(self) -> None:
        self.lease()  # creates the dir with good modes
        target = self.root / "planted.json"
        target.write_text(json.dumps({"holder_uid": STRANGER_UID, "holder_user": STRANGER,
                                      "acquired_at": 1000, "expires_at": 99999}), encoding="utf-8")
        target.chmod(0o600)  # otherwise-valid record: only the symlink check can refuse it
        record = self.lease_dir() / "claude-fallback.json"
        record.unlink()
        record.symlink_to(target)
        self.assertEqual(self.lease(op="status").outcome, "refused")
        self.assertEqual(self.lease().outcome, "refused")

    def test_symlinked_lock_file_refused(self) -> None:
        self.rate_dir.mkdir(mode=0o700)
        self.lease_dir().mkdir(mode=0o700)
        (self.lease_dir() / "claude-fallback.lock").symlink_to(self.root / "elsewhere.lock")
        self.assertEqual(self.lease().outcome, "refused")
        self.assertFalse((self.root / "elsewhere.lock").exists())

    def test_wrong_mode_lease_dir_refused(self) -> None:
        self.rate_dir.mkdir(mode=0o700)
        self.lease_dir().mkdir()
        self.lease_dir().chmod(0o755)
        self.assertEqual(self.lease().outcome, "refused")

    def test_wrong_owner_lease_state_refused(self) -> None:
        store = LeaseStore(self.rate_dir, expected_owner_uid=self.owner_uid + 12345, clock=lambda: self.now)
        self.assertEqual(self.lease(lease_store=store).outcome, "refused")

    def test_corrupt_record_refused(self) -> None:
        self.lease()
        (self.lease_dir() / "claude-fallback.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.lease(invoker=self.recovery).outcome, "refused")

    def test_audit_failure_on_acquire_changes_nothing(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("disk full")
        result = self.lease(audit=broken)
        self.assertEqual(result.outcome, "refused")
        self.assertIsNone(self.record())

    def test_audit_failure_on_release_changes_nothing(self) -> None:
        self.lease()
        before = self.record()
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("disk full")
        result = self.lease(op="release", audit=broken)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(self.record(), before)

    def test_audit_failure_on_status_refuses(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("disk full")
        self.assertEqual(self.lease(op="status", audit=broken).outcome, "refused")


# ---------------------------------------------------------------------------
# T-4884 B4 — destructive verbs require the invoker's live lease.
# ---------------------------------------------------------------------------

class DestructiveVerbLeaseTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False

    def test_kill_session_without_lease_refused(self) -> None:
        result = self.dispatch()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("lease", result.detail)
        self.runner.assert_not_called()

    def test_kill_session_with_lease_allowed(self) -> None:
        self.assertEqual(self.lease().outcome, "executed")
        result = self.dispatch()
        self.assertEqual(result.outcome, "executed")
        self.runner.assert_called_once()

    def test_kill_session_with_expired_lease_refused(self) -> None:
        self.lease()
        self.now += LEASE_TTL_SECS
        result = self.dispatch()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("expired", result.detail)
        self.runner.assert_not_called()

    def test_kill_session_with_someone_elses_lease_refused(self) -> None:
        self.lease(invoker=self.recovery)
        result = self.dispatch()
        self.assertEqual(result.outcome, "refused")
        self.assertIn(RECOVERY, result.detail)
        self.runner.assert_not_called()

    def test_kill_session_after_release_refused(self) -> None:
        self.lease()
        self.lease(op="release")
        self.assertEqual(self.dispatch().outcome, "refused")
        self.runner.assert_not_called()

    def test_unstick_without_lease_refused(self) -> None:
        probe = MagicMock(return_value=_good_snapshot())
        result = self.dispatch(verb="unstick", action="wake-drop-cancel", foreground_probe=probe)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_dry_run_without_lease_refused(self) -> None:
        self.assertEqual(self.dispatch(dry_run=True).outcome, "refused")

    def test_option_like_target_refused_before_state(self) -> None:
        for bad in ("-t", "--all"):
            with self.subTest(target=bad):
                self.assertEqual(self.dispatch(target_key=bad).outcome, "refused")
                self.assertFalse(self.rate_dir.exists())
        self.runner.assert_not_called()

    def test_lease_lock_is_held_through_the_action(self) -> None:
        # No TOCTOU: while the subprocess runs, a competing lease operation
        # cannot take the target's lock (a separate open file description
        # trying LOCK_NB must fail).
        import fcntl as _fcntl
        self.lease()
        observed: list[bool] = []

        def runner(argv, *, env, timeout):
            fd = os.open(self.rate_dir / "leases" / "claude-fallback.lock", os.O_RDWR)
            try:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    observed.append(False)
                except BlockingIOError:
                    observed.append(True)
            finally:
                os.close(fd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertEqual(self.dispatch(runner=runner).outcome, "executed")
        self.assertEqual(observed, [True])


# ---------------------------------------------------------------------------
# T-4884 B4 — main(): sudo identity end to end, path overrides, argparse.
# ---------------------------------------------------------------------------

class BrokerMainTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False

    def ctx(self, *, user=WATCHMAN, uid=WATCHMAN_UID, real_uid=0) -> BrokerContext:
        return BrokerContext(
            environ=_sudo_env(user, uid),
            getuid=lambda: real_uid,
            geteuid=lambda: real_uid,
            pwuid=_fake_pwuid,
            pwnam=_fake_pwnam,
            manifest_path=self.manifest_path,
            audit_log_path=self.audit_path,
            state_dir=self.rate_dir,
            state_owner_uid=self.owner_uid,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
            runner=self.runner,
            clock=lambda: self.now,
        )

    def run_main(self, argv, **ctx_kwargs) -> tuple[int, str]:
        import contextlib as _contextlib
        import io as _io
        out = _io.StringIO()
        with _contextlib.redirect_stdout(out):
            rc = broker_main(argv, ctx=self.ctx(**ctx_kwargs))
        return rc, out.getvalue()

    def test_watchman_lease_then_kill_session(self) -> None:
        rc, _ = self.run_main(["lease", "acquire", "claude-fallback"])
        self.assertEqual(rc, 0)
        rc, out = self.run_main(["kill-session", "claude-fallback"])
        self.assertEqual(rc, 0, msg=out)
        self.runner.assert_called_once()

    def test_kill_session_without_lease_via_main_refused(self) -> None:
        rc, _ = self.run_main(["kill-session", "claude-fallback"])
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_recovery_lease_ok_kill_session_refused(self) -> None:
        rc, _ = self.run_main(["lease", "acquire", "claude-fallback"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 0)
        rc, _ = self.run_main(["kill-session", "claude-fallback"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_codex_refused(self) -> None:
        rc, out = self.run_main(["lease", "acquire", "claude-fallback"], user="codex", uid=1003)
        self.assertEqual(rc, 1)
        self.assertIn("invoker refused", out)
        self.assertFalse(self.rate_dir.exists())

    def test_missing_sudo_env_refused(self) -> None:
        rc, _ = self.run_main(["lease", "acquire", "claude-fallback"], user=None, uid=None)
        self.assertEqual(rc, 1)

    def test_mismatched_sudo_env_refused(self) -> None:
        rc, _ = self.run_main(["lease", "acquire", "claude-fallback"], user=WATCHMAN, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 1)

    def test_not_root_refused(self) -> None:
        rc, _ = self.run_main(["lease", "acquire", "claude-fallback"], real_uid=WATCHMAN_UID)
        self.assertEqual(rc, 1)
        self.assertFalse(self.rate_dir.exists())

    def test_path_overrides_refused_under_privilege(self) -> None:
        other = self.root / "other-state"
        for flag, value in (("--rate-limit-dir", other), ("--manifest", self.manifest_path),
                            ("--audit-log", self.root / "other-audit.jsonl")):
            with self.subTest(flag=flag):
                rc, out = self.run_main([flag, str(value), "lease", "acquire", "claude-fallback"])
                self.assertEqual(rc, 1)
                self.assertIn("path overrides are refused", out)
        self.assertFalse(other.exists())
        self.assertFalse(self.rate_dir.exists())

    def test_option_like_targets_never_reach_state(self) -> None:
        for argv in (["lease", "acquire", "-x"], ["lease", "acquire", "--all"], ["kill-session", "--all"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    self.run_main(argv)
        for argv in (["lease", "acquire", "--", "-x"], ["kill-session", "--", "--all"]):
            with self.subTest(argv=argv):
                rc, _ = self.run_main(argv)
                self.assertEqual(rc, 1)
        self.assertFalse(self.rate_dir.exists())
        self.runner.assert_not_called()

    def test_ttl_cannot_be_set_from_argv(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main(["lease", "acquire", "claude-fallback", "--ttl", "99999"])
        with self.assertRaises(SystemExit):
            self.run_main(["--ttl", "99999", "lease", "acquire", "claude-fallback"])

    def test_status_prints_fixed_line(self) -> None:
        rc, out = self.run_main(["lease", "status", "claude-fallback"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            out, "lease target=claude-fallback state=free holder=- holder_uid=- expires_at=- remaining_secs=-\n"
        )

    def test_privileged_selftest_requires_listed_invoker(self) -> None:
        rc, _ = self.run_main(["selftest"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 1)  # Recovery's verbs are lease-only


# ---------------------------------------------------------------------------
# T-4884 B4 — the sudoers generator.
# ---------------------------------------------------------------------------

class SudoersGeneratorTests(ManifestFixtureMixin, unittest.TestCase):
    def two_target_manifest(self) -> Path:
        payload = _manifest_payload()
        second = dict(_valid_target_payload())
        payload["targets"]["suborch2"] = second
        path = self.root / "two.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def rule_lines(self, text: str) -> list[str]:
        return [l for l in text.splitlines() if l and not l.startswith("#")]

    def test_no_wildcard_anywhere(self) -> None:
        text = generate_sudoers(load_for_generation(self.two_target_manifest()))
        self.assertNotIn("*", text)
        for line in self.rule_lines(text):
            for ch in "?[]\\":
                self.assertNotIn(ch, line)

    def test_exact_lines_for_every_target(self) -> None:
        text = generate_sudoers(load_for_generation(self.two_target_manifest()))
        lines = self.rule_lines(text)
        expected = ["Defaults!/usr/local/sbin/codex-recovery env_reset"]
        for target in ("claude-fallback", "suborch2"):
            for op in ("acquire", "release", "status"):
                expected.append(
                    f"claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery lease {op} {target}"
                )
        expected.append("codex-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery")
        self.assertEqual(lines, expected)

    def test_env_reset_and_secure_path_note(self) -> None:
        text = generate_sudoers(load_for_generation(self.two_target_manifest()))
        self.assertIn("Defaults!/usr/local/sbin/codex-recovery env_reset\n", text)
        self.assertIn("secure_path", text)

    def test_lease_only_invoker_never_gets_bare_broker_line(self) -> None:
        text = generate_sudoers(load_for_generation(self.two_target_manifest()))
        self.assertNotIn("claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery\n", text)

    def test_codex_invoker_manifest_refused(self) -> None:
        payload = _manifest_payload(invokers={"codex": {"verbs": ALL_VERBS}})
        path = self.root / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ManifestValidationError):
            load_for_generation(path)

    def test_sample_manifest_generates(self) -> None:
        sample = Path(__file__).resolve().parent / "codex_recovery_manifest.sample.json"
        text = generate_sudoers(load_for_generation(sample))
        self.assertIn("codex-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery\n", text)
        self.assertIn(
            "claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery lease acquire claude-fallback\n",
            text,
        )

    def test_visudo_accepts_generated_fragment(self) -> None:
        visudo = "/usr/sbin/visudo"
        if not os.access(visudo, os.X_OK):
            self.skipTest("visudo not installed; shape is covered structurally above")
        out = self.root / "fragment.sudoers"
        out.write_text(generate_sudoers(load_for_generation(self.two_target_manifest())), encoding="utf-8")
        result = subprocess.run([visudo, "-c", "-f", str(out)], capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and "parsed OK" not in result.stdout and "syntax error" not in (result.stdout + result.stderr):
            self.skipTest(f"visudo not runnable unprivileged here: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# T-4884 B4b — readable lease-event export (best-effort, never gates).
# ---------------------------------------------------------------------------

EXPORT_KEYS = {"op", "target", "holder_user", "holder_uid", "ts_wall", "ts_mono", "expires_at_wall"}


class LeaseExportTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.pub = self.root / "pub"
        self.pub.mkdir()
        os.chmod(self.pub, LEASE_EXPORT_DIR_MODE)
        self.export_path = self.pub / "lease-events.jsonl"
        self.export = self.sink(self.export_path)
        self.stderr = io.StringIO()
        ctx = contextlib.redirect_stderr(self.stderr)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def sink(self, path: Path) -> LeaseExportSink:
        return LeaseExportSink(path, expected_owner_uid=self.owner_uid, group_gid=os.getegid(),
                               mono=lambda: 77.9)

    def events(self) -> list[dict]:
        if not self.export_path.exists():
            return []
        return [json.loads(ln) for ln in self.export_path.read_text(encoding="utf-8").splitlines()]

    def test_acquire_renew_release_each_exported(self) -> None:
        self.assertEqual(self.lease(export=self.export).outcome, "executed")
        self.now += 100
        self.assertEqual(self.lease(export=self.export).outcome, "executed")
        self.now += 50
        self.assertEqual(self.lease(op="release", export=self.export).outcome, "executed")
        self.assertEqual(self.lease(op="status", export=self.export).outcome, "executed")  # free: no event
        ev = self.events()
        self.assertEqual([e["op"] for e in ev], ["acquire", "renew", "release"])
        self.assertEqual(ev[0], {"op": "acquire", "target": "claude-fallback", "holder_user": WATCHMAN,
                                 "holder_uid": WATCHMAN_UID, "ts_wall": 1000, "ts_mono": 77,
                                 "expires_at_wall": 1000 + LEASE_TTL_SECS})
        self.assertEqual((ev[1]["ts_wall"], ev[1]["expires_at_wall"]), (1100, 1100 + LEASE_TTL_SECS))
        self.assertEqual((ev[2]["ts_wall"], ev[2]["holder_user"]), (1150, WATCHMAN))
        for e in ev:
            self.assertEqual(set(e), EXPORT_KEYS)
        st = self.export_path.stat()
        self.assertEqual(stat.S_IMODE(st.st_mode), LEASE_EXPORT_FILE_MODE)
        self.assertEqual(st.st_gid, os.getegid())

    def test_expiry_observed_by_lease_ops_and_verbs(self) -> None:
        self.lease(invoker=self.recovery, export=self.export)
        self.now += LEASE_TTL_SECS + 1
        self.assertEqual(self.dispatch(export=self.export).outcome, "refused")
        self.lease(invoker=self.watchman, export=self.export)
        ops = [(e["op"], e["holder_user"]) for e in self.events()]
        self.assertEqual(ops, [("acquire", RECOVERY), ("expire_observed", RECOVERY),
                               ("expire_observed", RECOVERY), ("acquire", WATCHMAN)])

    def test_export_never_contains_command_text(self) -> None:
        self.lease(export=self.export)
        self.assertEqual(self.dispatch(export=self.export).outcome, "executed")
        self.lease(op="release", export=self.export)
        raw = self.export_path.read_text(encoding="utf-8")
        for needle in ("tmux", "kill-session", "argv", "/usr/bin", "setpriv", "reason"):
            self.assertNotIn(needle, raw)
        for e in self.events():
            self.assertEqual(set(e), EXPORT_KEYS)
        # the authoritative audit log still carries the argv, unchanged
        self.assertTrue(any(r.get("argv") for r in self.audit_records()))

    def test_unwritable_export_never_blocks(self) -> None:
        bad_sinks = {
            "missing dir": self.sink(self.root / "nope" / "lease-events.jsonl"),
            "wrong group": LeaseExportSink(self.export_path, expected_owner_uid=self.owner_uid,
                                           group_gid=os.getegid() + 1),
            "wrong owner": LeaseExportSink(self.export_path, expected_owner_uid=self.owner_uid + 1,
                                           group_gid=os.getegid()),
        }
        for label, sink in bad_sinks.items():
            with self.subTest(label):
                self.assertEqual(self.lease(export=sink).outcome, "executed")
                self.assertEqual(self.dispatch(export=sink).outcome, "executed")
                self.assertEqual(self.lease(op="release", export=sink).outcome, "executed")
        self.assertEqual(self.events(), [])
        self.assertIn("lease export write failed", self.stderr.getvalue())
        self.assertTrue(any(r.get("event") == "lease_acquire" for r in self.audit_records()))

    def test_wrong_dir_mode_and_symlink_refused(self) -> None:
        os.chmod(self.pub, 0o2755)
        self.assertFalse(self.export.write(op="acquire", target="t", holder_user="u", holder_uid=1,
                                           ts_wall=1, expires_at_wall=2))
        os.chmod(self.pub, LEASE_EXPORT_DIR_MODE)
        elsewhere = self.root / "elsewhere.jsonl"
        elsewhere.write_text("")
        os.symlink(elsewhere, self.export_path)
        self.assertEqual(self.lease(export=self.export).outcome, "executed")
        self.assertEqual(elsewhere.read_text(), "")

    def test_wrong_file_mode_refused(self) -> None:
        self.export_path.write_text('{"op":"acquire"}\n')
        os.chmod(self.export_path, 0o644)
        self.assertFalse(self.export.write(op="acquire", target="t", holder_user="u", holder_uid=1,
                                           ts_wall=1, expires_at_wall=2))
        self.assertEqual(len(self.export_path.read_text().splitlines()), 1)

    def test_bad_fields_rejected(self) -> None:
        self.assertFalse(self.export.write(op="kill", target="t", holder_user="u", holder_uid=1,
                                           ts_wall=1, expires_at_wall=2))
        self.assertFalse(self.export.write(op="acquire", target="t; rm -rf /", holder_user="u",
                                           holder_uid=1, ts_wall=1, expires_at_wall=2))
        self.assertEqual(self.events(), [])

    def test_main_writes_export_and_refuses_override_under_privilege(self) -> None:
        ctx = BrokerContext(
            environ=_sudo_env(WATCHMAN, WATCHMAN_UID), getuid=lambda: 0, geteuid=lambda: 0,
            pwuid=_fake_pwuid, pwnam=_fake_pwnam, manifest_path=self.manifest_path,
            audit_log_path=self.audit_path, state_dir=self.rate_dir, state_owner_uid=self.owner_uid,
            manifest_kwargs={"expected_owner_uid": self.owner_uid}, runner=self.runner,
            clock=lambda: self.now, lease_export_path=self.export_path, lease_export_gid=os.getegid(),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(broker_main(["lease", "acquire", "claude-fallback"], ctx=ctx), 0)
            self.assertEqual(broker_main(["--lease-export", str(self.root / "x.jsonl"),
                                          "lease", "release", "claude-fallback"], ctx=ctx), 1)
        self.assertIn("path overrides are refused under privilege", out.getvalue())
        self.assertEqual([e["op"] for e in self.events()], ["acquire"])



class UserUnitInstanceTests(unittest.TestCase):
    """T-5039: user_unit accepts exactly one '@instance' (recovery runs under
    pantheon-runtime-controller@recovery.service); everything else stays strict."""

    def _target(self, unit: object) -> TargetSpec:
        return TargetSpec(
            key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
            tmux_session="claude-telegram-bot-fallback", user_unit=unit,
            foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
        )

    def test_plain_and_single_instance_units_are_accepted(self) -> None:
        for unit in ("claude-telegram-fallback.service", "pantheon-runtime-controller@recovery.service",
                     "a@b"):
            with self.subTest(unit=unit):
                self.assertEqual(self._target(unit).user_unit, unit)

    def test_bad_unit_names_are_refused(self) -> None:
        for unit in ("a@b@c.service", "@recovery.service", "pantheon-runtime-controller@",
                     "x@.service", "a@-b.service", "a@b;rm.service", "a@b c", "a@@b",
                     "a" * 40 + "@" + "b" * 24, "", 7, None):
            with self.subTest(unit=unit), self.assertRaises(ManifestValidationError):
                self._target(unit)

    def test_other_identifiers_still_refuse_an_at_sign(self) -> None:
        with self.assertRaises(ManifestValidationError):
            TargetSpec(
                key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
                tmux_session="sess@x", user_unit="u.service",
                foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
            )

    def test_the_sample_manifest_names_recoveries_live_unit(self) -> None:
        sample = json.loads((Path(__file__).parent / "codex_recovery_manifest.sample.json").read_text())
        self.assertEqual(sample["targets"]["claude-fallback"]["user_unit"],
                         "pantheon-runtime-controller@recovery.service")


# ---------------------------------------------------------------------------
# T-5059 — linger-enable (root, no lease, manifest account only).
# ---------------------------------------------------------------------------

LINGER_INVOKERS = {
    WATCHMAN: {"verbs": ["kill-session", "unstick", "selftest", "lease", "linger-enable"]},
    RECOVERY: {"verbs": ["lease", "linger-enable"]},
}


class FakeLinger:
    """Fake linger probe + runner pair: the runner 'creates' the linger file."""

    def __init__(self, *, present: bool = False, returncode: int = 0, creates: bool = True):
        self.present = present
        self.returncode = returncode
        self.creates = creates
        self.probe_calls: list[str] = []
        self.runner = MagicMock(side_effect=self._run)

    def probe(self, account: str) -> bool:
        self.probe_calls.append(account)
        return self.present

    def _run(self, argv, *, env, timeout):
        if self.returncode == 0 and self.creates:
            self.present = True
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


class LingerEnableTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_manifest_payload(invokers=LINGER_INVOKERS))
        self.fake = FakeLinger()

    def linger(self, **overrides):
        kwargs = dict(
            target_key="claude-fallback",
            invoker=self.watchman,
            manifest_path=self.manifest_path,
            audit=self.audit,
            rate_limiter=self.rate_limiter,
            runner=self.fake.runner,
            linger_probe=self.fake.probe,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
            pwnam=_fake_pwnam,
        )
        kwargs.update(overrides)
        return dispatch_linger_enable(**kwargs)

    def test_success_runs_exact_root_argv_and_audits_before_after(self) -> None:
        result = self.linger()
        self.assertEqual(result.outcome, "executed")
        self.fake.runner.assert_called_once()
        argv = self.fake.runner.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/loginctl", "enable-linger", RECOVERY_ACCOUNT])
        self.assertNotIn(SETPRIV, argv)  # root, no drop to the target
        self.assertNotIn("shell", self.fake.runner.call_args.kwargs)
        self.assertEqual(self.fake.runner.call_args.kwargs["timeout"], 10)
        records = self.audit_records()
        pre = [r for r in records if r["event"] == "pre_action"][0]
        post = [r for r in records if r["event"] == "post_action"][0]
        self.assertIs(pre["before_linger"], False)
        self.assertEqual(pre["argv"], argv)
        self.assertEqual((post["exit_code"], post["before_linger"], post["after_linger"]), (0, False, True))
        self.assertEqual(post["verb"], "linger-enable")

    def test_already_lingering_is_success_and_runs_nothing(self) -> None:
        self.fake.present = True
        result = self.linger()
        self.assertEqual(result.outcome, "executed")
        self.fake.runner.assert_not_called()
        rec = [r for r in self.audit_records() if r["event"] == "linger_already"][0]
        self.assertEqual((rec["before_linger"], rec["after_linger"]), (True, True))

    def test_idempotent_second_call(self) -> None:
        self.assertEqual(self.linger().outcome, "executed")
        self.assertEqual(self.linger().outcome, "executed")
        self.fake.runner.assert_called_once()

    def test_runner_failure_refused_with_audit_record(self) -> None:
        self.fake = FakeLinger(returncode=1)
        result = self.linger(runner=self.fake.runner, linger_probe=self.fake.probe)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(result.exit_code, 1)
        post = [r for r in self.audit_records() if r["event"] == "post_action"][0]
        self.assertEqual((post["exit_code"], post["after_linger"]), (1, False))

    def test_runner_exception_refused_with_audit_record(self) -> None:
        runner = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10))
        result = self.linger(runner=runner)
        self.assertEqual(result.outcome, "refused")
        post = [r for r in self.audit_records() if r["event"] == "post_action"][0]
        self.assertTrue(post["runner_error"])

    def test_zero_exit_without_linger_file_is_not_success(self) -> None:
        self.fake = FakeLinger(creates=False)
        result = self.linger(runner=self.fake.runner, linger_probe=self.fake.probe)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("not verified", result.detail)

    def test_unauthorized_invoker_refused(self) -> None:
        stranger = Invoker(uid=STRANGER_UID, user=STRANGER)
        result = self.linger(invoker=stranger)
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()
        self.assertTrue(any(r["event"] == "refuse" for r in self.audit_records()))

    def test_invoker_without_verb_refused(self) -> None:
        self.write_manifest(_manifest_payload(invokers={WATCHMAN: {"verbs": ["lease"]}}))
        result = self.linger()
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_unknown_target_refused(self) -> None:
        result = self.linger(target_key="no-such-target")
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_account_in_target_slot_refused(self) -> None:
        # the manifest KEY is claude-fallback; the account name is not a key
        result = self.linger(target_key=RECOVERY_ACCOUNT)
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_option_like_target_refused(self) -> None:
        for bad in ("-x", "--all", "a b", "a;b", ""):
            with self.subTest(bad=bad):
                self.assertEqual(self.linger(target_key=bad).outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_uid_drift_refused(self) -> None:
        def drifted(name):
            pw = _fake_pwnam(name)
            return SimpleNamespace(**{**vars(pw), "pw_uid": pw.pw_uid + 1}) if name == RECOVERY_ACCOUNT else pw
        result = self.linger(pwnam=drifted)
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_audit_unwritable_fails_closed(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("disk full")
        result = self.linger(audit=broken)
        self.assertEqual(result.outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_dry_run_never_runs(self) -> None:
        result = self.linger(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.fake.runner.assert_not_called()

    def test_rate_limited(self) -> None:
        limiter = RateLimiter(self.rate_dir, max_actions=1, clock=lambda: 1000.0)
        self.fake = FakeLinger(creates=False)
        kw = dict(rate_limiter=limiter, runner=self.fake.runner, linger_probe=self.fake.probe)
        self.linger(**kw)
        self.fake.runner.reset_mock()
        self.assertEqual(self.linger(**kw).outcome, "refused")
        self.fake.runner.assert_not_called()

    def test_needs_no_lease_and_is_not_a_dispatch_verb(self) -> None:
        self.assertEqual(self.linger().outcome, "executed")  # no lease was taken
        self.assertFalse((self.rate_dir / "leases").exists())
        result = self.dispatch(verb="linger-enable")
        self.assertEqual(result.outcome, "refused")


class RealLingerProbeTests(unittest.TestCase):
    def test_regular_file_symlink_and_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "present").write_text("")
            os.symlink("present", root / "linked")
            self.assertTrue(real_linger_probe("present", state_dir=root))
            self.assertFalse(real_linger_probe("linked", state_dir=root))
            self.assertFalse(real_linger_probe("absent", state_dir=root))


class LingerEnableMainTests(DispatchFixtureMixin, unittest.TestCase):
    """linger-enable driven through main() with a fake sudo context."""
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_manifest_payload(invokers=LINGER_INVOKERS))
        self.fake = FakeLinger()
        self.runner = self.fake.runner

    def ctx(self, *, user=WATCHMAN, uid=WATCHMAN_UID, real_uid=0) -> BrokerContext:
        return BrokerContext(
            environ=_sudo_env(user, uid), getuid=lambda: real_uid, geteuid=lambda: real_uid,
            pwuid=_fake_pwuid, pwnam=_fake_pwnam, manifest_path=self.manifest_path,
            audit_log_path=self.audit_path, state_dir=self.rate_dir, state_owner_uid=self.owner_uid,
            manifest_kwargs={"expected_owner_uid": self.owner_uid}, runner=self.runner,
            linger_probe=self.fake.probe, clock=lambda: self.now,
        )

    def run_main(self, argv, **ctx_kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = broker_main(argv, ctx=self.ctx(**ctx_kwargs))
        return rc, out.getvalue()

    def test_main_success_for_watchman_and_recovery(self) -> None:
        rc, out = self.run_main(["linger-enable", "claude-fallback"])
        self.assertEqual(rc, 0, msg=out)
        self.assertEqual(self.runner.call_args.args[0],
                         ["/usr/bin/loginctl", "enable-linger", RECOVERY_ACCOUNT])
        self.fake.present = False
        rc, out = self.run_main(["linger-enable", "claude-fallback"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 0, msg=out)

    def test_main_runner_failure_nonzero_with_audit(self) -> None:
        self.fake.returncode = 1
        rc, _ = self.run_main(["linger-enable", "claude-fallback"])
        self.assertEqual(rc, 1)
        self.assertTrue(any(r["event"] == "post_action" for r in self.audit_records()))

    def test_main_unlisted_invoker_refused(self) -> None:
        rc, _ = self.run_main(["linger-enable", "claude-fallback"], user=STRANGER, uid=STRANGER_UID)
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_account_or_extra_args_in_argv_rejected(self) -> None:
        for argv in (["linger-enable", "claude-fallback", RECOVERY_ACCOUNT],
                     ["linger-enable", "claude-fallback", "--account", "root"],
                     ["linger-enable"],
                     ["--account", "root", "linger-enable", "claude-fallback"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    self.run_main(argv)
        self.runner.assert_not_called()

    def test_main_unknown_target_and_account_name_refused(self) -> None:
        for target in ("no-such-target", RECOVERY_ACCOUNT, "root"):
            with self.subTest(target=target):
                rc, _ = self.run_main(["linger-enable", target])
                self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_path_override_refused_under_privilege(self) -> None:
        rc, out = self.run_main(["--manifest", str(self.manifest_path), "linger-enable", "claude-fallback"])
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()


class LingerEnableSudoersAndSelftestTests(ManifestFixtureMixin, unittest.TestCase):
    def manifest(self, invokers: dict) -> Path:
        payload = _manifest_payload(invokers=invokers)
        payload["targets"]["suborch2"] = dict(_valid_target_payload())
        path = self.root / "m.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def rules(self, invokers: dict) -> list[str]:
        text = generate_sudoers(load_for_generation(self.manifest(invokers)))
        return [l for l in text.splitlines() if l and not l.startswith("#")]

    def test_exact_line_per_target_for_lease_and_linger_invoker(self) -> None:
        lines = self.rules({RECOVERY: {"verbs": ["lease", "linger-enable"]}})
        b = "/usr/local/sbin/codex-recovery"
        expected = [f"Defaults!{b} env_reset"]
        for t in ("claude-fallback", "suborch2"):
            expected += [f"{RECOVERY} ALL=(root) NOPASSWD: {b} lease {op} {t}" for op in ("acquire", "release", "status")]
        expected += [f"{RECOVERY} ALL=(root) NOPASSWD: {b} linger-enable {t}" for t in ("claude-fallback", "suborch2")]
        self.assertEqual(lines, expected)
        self.assertFalse(any("*" in l for l in lines))
        self.assertNotIn(f"{RECOVERY} ALL=(root) NOPASSWD: {b}", lines)  # never the bare broker line

    def test_t5130_lease_only_target_gets_lease_lines_but_no_linger_line(self) -> None:
        payload = _manifest_payload(invokers={RECOVERY: {"verbs": ["lease", "linger-enable"]}})
        payload["targets"]["deepseek-fallback"] = {"account": "deepseek-fallback", "uid": 4303,
                                                   "lease_only": True}
        path = self.root / "m-lease-only.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        lines = generate_sudoers(load_for_generation(path)).splitlines()
        b = "/usr/local/sbin/codex-recovery"
        self.assertIn(f"{RECOVERY} ALL=(root) NOPASSWD: {b} lease acquire deepseek-fallback", lines)
        self.assertNotIn(f"{RECOVERY} ALL=(root) NOPASSWD: {b} linger-enable deepseek-fallback", lines)
        self.assertIn(f"{RECOVERY} ALL=(root) NOPASSWD: {b} linger-enable claude-fallback", lines)

    def test_linger_only_invoker_gets_only_linger_lines(self) -> None:
        lines = self.rules({RECOVERY: {"verbs": ["linger-enable"]}})
        self.assertEqual([l for l in lines if l.startswith(RECOVERY)], [
            f"{RECOVERY} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery linger-enable claude-fallback",
            f"{RECOVERY} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery linger-enable suborch2",
        ])

    def test_full_broker_invoker_unchanged(self) -> None:
        lines = self.rules(LINGER_INVOKERS)
        self.assertIn(f"{WATCHMAN} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery", lines)
        self.assertFalse(any(l.startswith(WATCHMAN) and "linger-enable" in l for l in lines))

    def test_sample_manifest_lists_linger_for_both_seats(self) -> None:
        sample = Path(__file__).resolve().parent / "codex_recovery_manifest.sample.json"
        data = json.loads(sample.read_text())
        for seat in (WATCHMAN, RECOVERY):
            self.assertIn("linger-enable", data["invokers"][seat]["verbs"])
        text = generate_sudoers(load_for_generation(sample))
        self.assertIn(
            "claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery linger-enable claude-fallback\n",
            text,
        )

    def test_selftest_preflights_manifest_with_linger_verb(self) -> None:
        self.write_manifest(_manifest_payload(invokers=LINGER_INVOKERS))
        ok, report = cmd_selftest(
            manifest_path=self.manifest_path, pwnam=_fake_pwnam,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
        )
        self.assertTrue(ok)
        self.assertTrue(report["binaries_present"])  # includes /usr/bin/loginctl

    def test_selftest_rejects_a_misspelled_linger_verb(self) -> None:
        self.write_manifest(_manifest_payload(invokers={WATCHMAN: {"verbs": ["linger_enable"]}}))
        ok, report = cmd_selftest(
            manifest_path=self.manifest_path, pwnam=_fake_pwnam,
            manifest_kwargs={"expected_owner_uid": self.owner_uid},
        )
        self.assertFalse(ok)
        self.assertIn("unknown verbs", report["error"])


# ---------------------------------------------------------------------------
# T-5099 (B14 S1): reset-failed <target> <unit>.
# ---------------------------------------------------------------------------

RESET_UNIT = "pantheon-persona-consume@recovery.service"


def _reset_manifest(**target_overrides) -> dict:
    target_overrides.setdefault("reset_failed_units", [RESET_UNIT])
    return _manifest_payload(
        invokers={WATCHMAN: {"verbs": ALL_VERBS + ["reset-failed"]}, RECOVERY: {"verbs": ["lease"]}},
        **target_overrides)


class ResetFailedManifestTests(ManifestFixtureMixin, unittest.TestCase):
    def test_optional_field_absent_means_empty(self) -> None:
        self.write_manifest(_manifest_payload())
        self.assertEqual(self.load()["claude-fallback"].reset_failed_units, ())

    def test_listed_units_parse(self) -> None:
        self.write_manifest(_reset_manifest())
        self.assertEqual(self.load()["claude-fallback"].reset_failed_units, (RESET_UNIT,))

    def test_bad_entries_refused(self) -> None:
        for bad in (["pantheon-*.service"], ["x.timer"], [RESET_UNIT, RESET_UNIT],
                    ["a@b@c.service"], ["../x.service"], "not-a-list", [7]):
            with self.subTest(units=bad):
                self.write_manifest(_reset_manifest(reset_failed_units=bad))
                with self.assertRaises(ManifestValidationError):
                    self.load()


class ResetFailedDispatchTests(DispatchFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_reset_manifest())

    def test_argv_is_exact(self) -> None:
        target = TargetSpec(key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
                            tmux_session="s", user_unit="u.service",
                            foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
                            reset_failed_units=(RESET_UNIT,))
        self.assertEqual(build_reset_failed_argv(target, RESET_UNIT, pwnam=_fake_pwnam), [
            "/usr/bin/setpriv", "--reuid", str(RECOVERY_UID), "--regid", str(RECOVERY_GID),
            "--init-groups", "--reset-env", "--no-new-privs", "--",
            "/usr/bin/env", f"XDG_RUNTIME_DIR=/run/user/{RECOVERY_UID}",
            "/usr/bin/systemctl", "--user", "reset-failed", "--", RESET_UNIT,
        ])

    def test_listed_unit_executes_under_lease(self) -> None:
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT)
        self.assertEqual(result.outcome, "executed", result.detail)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0][-1], RESET_UNIT)
        events = [r["event"] for r in self.audit_records()]
        self.assertIn("pre_action", events)
        self.assertIn("post_action", events)

    def test_unlisted_unit_refused(self) -> None:
        for unit in ("pantheon-runtime-controller@recovery.service", "x.service", "", None,
                     RESET_UNIT + " ; id", "pantheon-persona-consume@*.service"):
            with self.subTest(unit=unit):
                result = self.dispatch(verb="reset-failed", action=unit)
                self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_empty_allowlist_refuses_everything(self) -> None:
        self.write_manifest(_reset_manifest(reset_failed_units=[]))
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_no_lease_refused(self) -> None:
        self.assertEqual(self.lease(op="release").outcome, "executed")
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("no lease", result.detail)
        self.runner.assert_not_called()

    def test_audit_unwritable_refused(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("immutable")
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT, audit=broken)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_dry_run_inert(self) -> None:
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT, dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.runner.assert_not_called()

    def test_rate_limited(self) -> None:
        outcomes = [self.dispatch(verb="reset-failed", action=RESET_UNIT, dry_run=True).outcome
                    for _ in range(4)]
        self.assertEqual(outcomes, ["dry_run"] * 3 + ["refused"])

    def test_invoker_without_verb_refused(self) -> None:
        self.write_manifest(_manifest_payload(reset_failed_units=[RESET_UNIT]))  # default invokers
        result = self.dispatch(verb="reset-failed", action=RESET_UNIT)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()


class ResetFailedMainTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False
    ctx = BrokerMainTests.ctx
    run_main = BrokerMainTests.run_main

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_reset_manifest())

    def test_main_parses_and_dispatches(self) -> None:
        self.assertEqual(self.run_main(["lease", "acquire", "claude-fallback"])[0], 0)
        rc, out = self.run_main(["reset-failed", "claude-fallback", RESET_UNIT])
        self.assertEqual(rc, 0, msg=out)
        self.assertEqual(self.runner.call_args.args[0][-1], RESET_UNIT)

    def test_recovery_cannot_reset_failed(self) -> None:
        self.run_main(["lease", "acquire", "claude-fallback"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        rc, _ = self.run_main(["reset-failed", "claude-fallback", RESET_UNIT], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_sudoers_unchanged_for_full_invoker(self) -> None:
        # reset-failed is not an exact-line verb: the full invoker keeps its single
        # broker line and Recovery's lease lines are untouched (no new argv shapes).
        text = generate_sudoers(load_for_generation(self.manifest_path))
        rules = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", "Defaults"))]
        self.assertIn(f"{WATCHMAN} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery", rules)
        self.assertFalse([ln for ln in rules if "reset-failed" in ln], rules)

# ---------------------------------------------------------------------------
# T-5100 (B14 S2): restart <target> <unit>.
# ---------------------------------------------------------------------------

RESTART_UNIT = "pantheon-inbox-consumer@recovery.service"


def _restart_manifest(**target_overrides) -> dict:
    target_overrides.setdefault("restart_units", [RESTART_UNIT])
    return _manifest_payload(
        invokers={WATCHMAN: {"verbs": ALL_VERBS + ["restart"]}, RECOVERY: {"verbs": ["lease"]}},
        **target_overrides)


class RestartManifestTests(ManifestFixtureMixin, unittest.TestCase):
    def test_absent_means_empty(self) -> None:
        self.write_manifest(_manifest_payload())
        self.assertEqual(self.load()["claude-fallback"].restart_units, ())

    def test_listed_units_parse(self) -> None:
        self.write_manifest(_restart_manifest())
        self.assertEqual(self.load()["claude-fallback"].restart_units, (RESTART_UNIT,))

    def test_session_owning_units_rejected_at_parse(self) -> None:
        for unit in ("pantheon-runtime-controller@recovery.service", "pantheon-persona-consume@recovery.service",
                     "claude-telegram-fallback.service", "claude-telegram.service"):
            with self.subTest(unit=unit):
                self.write_manifest(_restart_manifest(restart_units=[unit]))
                with self.assertRaises(ManifestValidationError):
                    self.load()

    def test_target_user_unit_rejected_at_parse(self) -> None:
        self.write_manifest(_restart_manifest(user_unit="pantheon-mailbox-wake@recovery.service",
                                              restart_units=["pantheon-mailbox-wake@recovery.service"]))
        with self.assertRaises(ManifestValidationError):
            self.load()

    def test_bad_shapes_rejected(self) -> None:
        for bad in (["x.timer"], [RESTART_UNIT, RESTART_UNIT], ["*.service"], "nope", [1]):
            with self.subTest(units=bad):
                self.write_manifest(_restart_manifest(restart_units=bad))
                with self.assertRaises(ManifestValidationError):
                    self.load()


class RestartDispatchTests(DispatchFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_restart_manifest())

    def test_argv_is_exact(self) -> None:
        target = TargetSpec(key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID,
                            tmux_session="s", user_unit="u.service",
                            foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,), restart_units=(RESTART_UNIT,))
        self.assertEqual(build_restart_argv(target, RESTART_UNIT, pwnam=_fake_pwnam), [
            "/usr/bin/setpriv", "--reuid", str(RECOVERY_UID), "--regid", str(RECOVERY_GID),
            "--init-groups", "--reset-env", "--no-new-privs", "--",
            "/usr/bin/env", f"XDG_RUNTIME_DIR=/run/user/{RECOVERY_UID}",
            "/usr/bin/systemctl", "--user", "restart", "--", RESTART_UNIT,
        ])

    def test_denylist_rechecked_at_dispatch(self) -> None:
        # A TargetSpec built without parse-time validation still cannot restart a
        # session owner (defence in depth).
        target = TargetSpec.__new__(TargetSpec)
        for k, v in dict(key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID, tmux_session="s",
                         user_unit="u.service", foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
                         reset_failed_units=(),
                         restart_units=("pantheon-runtime-controller@recovery.service",)).items():
            object.__setattr__(target, k, v)
        with self.assertRaises(DispatchRefusedError):
            build_restart_argv(target, "pantheon-runtime-controller@recovery.service", pwnam=_fake_pwnam)

    def test_listed_unit_executes_under_lease(self) -> None:
        result = self.dispatch(verb="restart", action=RESTART_UNIT)
        self.assertEqual(result.outcome, "executed", result.detail)
        self.assertEqual(self.runner.call_args.args[0][-3:], ["restart", "--", RESTART_UNIT])

    def test_reset_failed_list_does_not_authorize_restart(self) -> None:
        self.write_manifest(_restart_manifest(restart_units=[], reset_failed_units=[RESTART_UNIT]))
        result = self.dispatch(verb="restart", action=RESTART_UNIT)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_unlisted_and_hostile_units_refused(self) -> None:
        for unit in ("pantheon-runtime-controller@recovery.service", "pantheon-persona-consume@recovery.service",
                     "x.service", "", None, RESTART_UNIT + " ; id"):
            with self.subTest(unit=unit):
                self.assertEqual(self.dispatch(verb="restart", action=unit).outcome, "refused")
        self.runner.assert_not_called()

    def test_no_lease_refused(self) -> None:
        self.assertEqual(self.lease(op="release").outcome, "executed")
        result = self.dispatch(verb="restart", action=RESTART_UNIT)
        self.assertEqual(result.outcome, "refused")
        self.runner.assert_not_called()

    def test_audit_unwritable_refused(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = AuditWriteError("immutable")
        self.assertEqual(self.dispatch(verb="restart", action=RESTART_UNIT, audit=broken).outcome, "refused")
        self.runner.assert_not_called()

    def test_dry_run_inert_and_rate_limited(self) -> None:
        outcomes = [self.dispatch(verb="restart", action=RESTART_UNIT, dry_run=True).outcome for _ in range(4)]
        self.assertEqual(outcomes, ["dry_run"] * 3 + ["refused"])
        self.runner.assert_not_called()


class RestartMainTests(DispatchFixtureMixin, unittest.TestCase):
    auto_lease = False
    ctx = BrokerMainTests.ctx
    run_main = BrokerMainTests.run_main

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(_restart_manifest())

    def test_main_parses_and_dispatches(self) -> None:
        self.assertEqual(self.run_main(["lease", "acquire", "claude-fallback"])[0], 0)
        rc, out = self.run_main(["restart", "claude-fallback", RESTART_UNIT])
        self.assertEqual(rc, 0, msg=out)

    def test_recovery_cannot_restart(self) -> None:
        self.run_main(["lease", "acquire", "claude-fallback"], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        rc, _ = self.run_main(["restart", "claude-fallback", RESTART_UNIT], user=RECOVERY, uid=RECOVERY_INVOKER_UID)
        self.assertEqual(rc, 1)
        self.runner.assert_not_called()

    def test_sudoers_unchanged(self) -> None:
        rules = [ln for ln in generate_sudoers(load_for_generation(self.manifest_path)).splitlines()
                 if ln and not ln.startswith(("#", "Defaults"))]
        self.assertFalse([ln for ln in rules if " restart " in ln], rules)


class RestartDenylistWideningTests(ManifestFixtureMixin, unittest.TestCase):
    """T-5112: session-ending/resuming units are denied at parse, whatever the
    manifest lists; a suffixless user_unit still matches its .service form."""

    NEW_DENIED = (
        "pantheon-restart-drain.service", "pantheon-ho-resumer.service",
        "pantheon-acp-bridge@specialist.service", "pantheon-context-fill-monitor@recovery.service",
        "recovery-telegram-child-watchdog.service",
    )

    def test_new_prefixes_rejected_at_parse(self) -> None:
        for unit in self.NEW_DENIED:
            with self.subTest(unit=unit):
                self.write_manifest(_restart_manifest(restart_units=[unit]))
                with self.assertRaises(ManifestValidationError):
                    self.load()

    def test_new_prefixes_rejected_at_dispatch(self) -> None:
        target = TargetSpec.__new__(TargetSpec)
        for k, v in dict(key="claude-fallback", account=RECOVERY_ACCOUNT, uid=RECOVERY_UID, tmux_session="s",
                         user_unit="u.service", foreground_exe_allowlist=(RECOVERY_ALLOWED_EXE,),
                         reset_failed_units=(), restart_units=self.NEW_DENIED).items():
            object.__setattr__(target, k, v)
        for unit in self.NEW_DENIED:
            with self.subTest(unit=unit), self.assertRaises(DispatchRefusedError):
                build_restart_argv(target, unit, pwnam=_fake_pwnam)

    def test_lookalike_not_overmatched(self) -> None:
        # acp-bridge-backlog@ is a backlog poller, not the bridge itself.
        self.write_manifest(_restart_manifest(restart_units=["pantheon-acp-bridge-backlog@specialist.service"]))
        self.assertEqual(self.load()["claude-fallback"].restart_units,
                         ("pantheon-acp-bridge-backlog@specialist.service",))

    def test_suffixless_user_unit_still_denied(self) -> None:
        self.write_manifest(_restart_manifest(user_unit="pantheon-mailbox-wake@recovery",
                                              restart_units=["pantheon-mailbox-wake@recovery.service"]))
        with self.assertRaises(ManifestValidationError):
            self.load()


# ---------------------------------------------------------------------------
# T-5124 (T-4924 H3a): lease_only targets + invoker precedence precondition.
# ---------------------------------------------------------------------------

DSF = "deepseek-fallback"
DSF_UID = 4303
EV_RECOVERY = "evidence-writer-recovery"
EV_HEIM = "evidence-writer-watchman"


def _pw(name, uid):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid, pw_dir=f"/home/{name}", pw_shell="/bin/bash")


class _ThirdSeatMixin(DispatchFixtureMixin):
    def setUp(self) -> None:
        EXTRA_PW.clear()
        EXTRA_PW[DSF] = _pw(DSF, DSF_UID)
        me = os.geteuid()
        EXTRA_PW[EV_RECOVERY] = _pw(EV_RECOVERY, me)
        EXTRA_PW[EV_HEIM] = _pw(EV_HEIM, me)
        self.addCleanup(EXTRA_PW.clear)
        super().setUp()
        self.health = self.root / "pantheon-health"
        self.health.mkdir()
        self.hb_dir = self.root / "recovery-seat-health"
        self.hb_dir.mkdir()
        self.dsf = Invoker(uid=DSF_UID, user=DSF)
        self.evnow = 1_800_000_000
        self.write_health(recovery_ok=True)
        self.write_heartbeat(self_state="ok")

    def precondition(self) -> dict:
        return {"kind": "higher-precedence-bad", "repairers": [
            {"kind": "export-record", "path": str(self.health), "writer": EV_RECOVERY, "stale_s": 600,
             "persona": "recovery", "activity_stale_s": 1800},
            {"kind": "watcher-heartbeat", "path": str(self.hb_dir / "watcher.json"), "writer": EV_HEIM,
             "stale_s": 600},
        ]}

    def third_manifest(self, **extra_targets) -> dict:
        payload = _manifest_payload(invokers={
            WATCHMAN: {"verbs": list(ALL_VERBS) + ["reset-failed", "restart"]}, RECOVERY: {"verbs": ["lease"]},
            DSF: {"verbs": ["lease", "kill-session", "unstick"], "precondition": self.precondition()},
        })
        payload["targets"][DSF] = {"account": DSF, "uid": DSF_UID, "lease_only": True}
        payload["targets"].update(extra_targets)
        return payload

    def _write(self, path: Path, obj, *, mode=0o640) -> None:
        path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
        path.chmod(mode)

    def write_health(self, *, recovery_ok=True, exported_at=None, record_at=None, **rec_over) -> None:
        t = self.evnow if exported_at is None else exported_at
        self._write(self.health / "_exporter.json", {"schema_version": 1, "exported_at": t, "persona_count": 1})
        rec = {"schema_version": 1, "persona": "recovery", "account": RECOVERY, "exported_at": record_at or t,
               "activity_age_s": 30, "pane_state": "idle", "consume_unit": "active"}
        if not recovery_ok:
            rec.update(activity_age_s=5000, pane_state="modal")
        rec.update(rec_over)
        self._write(self.health / "recovery.json", rec)

    def write_heartbeat(self, *, self_state="ok", written_at=None) -> None:
        self._write(self.hb_dir / "watcher.json", {
            "schema_version": 1, "written_at": self.evnow if written_at is None else written_at,
            "self_state": self_state, "cap_state": "unknown"})

    def check(self):
        pre = load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid,
                                   pwnam=_fake_pwnam).invokers[DSF].precondition
        return check_precondition(pre, now=self.evnow, pwnam=_fake_pwnam)

    def confirm_bad(self, heim="capped") -> None:
        """T-5129: make every repairer read BAD on two consecutive debounced
        reads of two snapshots, so the seat's NEXT gated call is the confirming
        read. Needs self.now == self.evnow (the lease clock is the evidence clock)."""
        from tools.codex_recovery import PRECONDITION_DEBOUNCE_MIN_SECS, check_precondition_debounced
        pre = load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid,
                                   pwnam=_fake_pwnam).invokers[DSF].precondition
        self.write_health(recovery_ok=False)
        self.write_heartbeat(self_state=heim)
        check_precondition_debounced(   # the first read (it may already confirm an unbroken streak)
            pre, invoker_user=DSF, now=self.now, pwnam=_fake_pwnam, lease_store=self.lease_store)
        self.now += PRECONDITION_DEBOUNCE_MIN_SECS
        self.evnow += PRECONDITION_DEBOUNCE_MIN_SECS
        self.write_health(recovery_ok=False)
        self.write_heartbeat(self_state=heim)


class LeaseOnlyTargetTests(_ThirdSeatMixin, unittest.TestCase):
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(self.third_manifest())

    def test_parses_with_only_account_and_uid(self) -> None:
        t = self.load()[DSF]
        self.assertTrue(t.lease_only)
        self.assertEqual((t.tmux_session, t.user_unit, t.foreground_exe_allowlist), ("", "", ()))

    def test_lease_only_with_action_fields_rejected(self) -> None:
        for extra in ({"tmux_session": "s"}, {"user_unit": "u.service"}, {"foreground_exe_allowlist": ["/x"]},
                      {"restart_units": ["a.service"]}, {"lease_only": "yes"}):
            with self.subTest(extra=extra):
                p = self.third_manifest()
                p["targets"][DSF].update(extra)
                self.write_manifest(p)
                with self.assertRaises(ManifestValidationError):
                    self.load()

    def test_every_invoker_may_lease_it(self) -> None:
        # deepseek-fallback carries a precondition, so since T-5128 it may lease
        # only while that holds (PreconditionLeaseAcquireTests); Recovery and
        # Watchman lease it unconditionally.
        for inv in (self.watchman, self.recovery):
            with self.subTest(invoker=inv.user):
                self.assertEqual(self.lease(op="acquire", target_key=DSF, invoker=inv).outcome, "executed")
                self.assertEqual(self.lease(op="release", target_key=DSF, invoker=inv).outcome, "executed")

    def test_no_verb_may_act_on_it_for_any_invoker(self) -> None:
        self.assertEqual(self.lease(op="acquire", target_key=DSF, invoker=self.watchman).outcome, "executed")
        for verb, action in (("kill-session", None), ("unstick", "spend-modal-dismiss"),
                             ("reset-failed", "x.service"), ("restart", "x.service")):
            with self.subTest(verb=verb):
                r = self.dispatch(verb=verb, action=action, target_key=DSF,
                                  foreground_probe=MagicMock(return_value=_good_snapshot()))
                self.assertEqual(r.outcome, "refused")
                self.assertIn("lease-only", r.detail)
        r = dispatch_linger_enable(target_key=DSF, invoker=self.watchman, manifest_path=self.manifest_path,
                                   audit=self.audit, rate_limiter=self.rate_limiter, runner=self.runner,
                                   linger_probe=lambda a: False,
                                   manifest_kwargs={"expected_owner_uid": self.owner_uid}, pwnam=_fake_pwnam)
        self.assertEqual(r.outcome, "refused")
        self.runner.assert_not_called()

    def test_sudoers_gets_recovery_lease_lines_for_the_new_target(self) -> None:
        text = generate_sudoers(load_for_generation(self.manifest_path))
        for op in ("acquire", "release", "status"):
            self.assertIn(f"{RECOVERY} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery lease {op} {DSF}", text)
        self.assertIn(f"{DSF} ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery", text)


class PreconditionStateTests(_ThirdSeatMixin, unittest.TestCase):
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(self.third_manifest())

    def test_both_healthy_refuses(self) -> None:
        ok, states = self.check()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=healthy", f"{EV_HEIM}=healthy"])

    def test_t5143_recursion_error_in_evidence_parse_is_unknown_not_a_crash(self) -> None:
        # Deep nesting can raise RecursionError (pure-Python decoder or a lower C limit);
        # it must read as unknown evidence, which refuses, never escape the broker.
        from unittest import mock
        import tools.codex_recovery as cr
        pre = load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid,
                                   pwnam=_fake_pwnam).invokers[DSF].precondition
        with mock.patch.object(cr.json, "loads", side_effect=RecursionError("too deep")):
            ok, states = check_precondition(pre, now=self.evnow, pwnam=_fake_pwnam)
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=unknown", f"{EV_HEIM}=unknown"])

    def test_t5143_deepest_nesting_under_the_cap_is_read_safely(self) -> None:
        n = (cr_cap := 8 * 1024) // 2 - 40
        payload = '{"schema_version": 1, "x": ' + "[" * n + "]" * n + "}"
        self.assertLess(len(payload), cr_cap)
        (self.hb_dir / "watcher.json").write_text(payload)
        ok, states = self.check()          # no exception, whatever the parser depth
        self.assertFalse(ok)
        self.assertIn(states[1], (f"{EV_HEIM}=unknown", f"{EV_HEIM}=healthy", f"{EV_HEIM}=bad"))

    def test_recovery_bad_alone_refuses(self) -> None:
        self.write_health(recovery_ok=False)
        ok, states = self.check()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad", f"{EV_HEIM}=healthy"])

    def test_both_bad_allows(self) -> None:
        for heim in ("capped", "dark", "stale"):
            with self.subTest(watchman=heim):
                self.write_health(recovery_ok=False)
                if heim == "stale":
                    self.write_heartbeat(self_state="ok", written_at=self.evnow - 601)
                else:
                    self.write_heartbeat(self_state=heim)
                self.assertTrue(self.check()[0])

    def test_stale_export_is_recovery_bad(self) -> None:
        self.write_health(recovery_ok=True, exported_at=self.evnow - 601)
        self.write_heartbeat(self_state="dark")
        self.assertTrue(self.check()[0])

    def test_single_recovery_signal_is_healthy(self) -> None:
        self.write_health(recovery_ok=True, pane_state="modal")
        self.assertEqual(self.check()[1][0], f"{EV_RECOVERY}=healthy")

    def test_unknown_refuses(self) -> None:
        cases = {
            "heartbeat self_state unknown": lambda: self.write_heartbeat(self_state="unknown"),
            "heartbeat missing": lambda: (self.hb_dir / "watcher.json").unlink(),
            "heartbeat future-dated": lambda: self.write_heartbeat(self_state="dark", written_at=self.evnow + 3600),
            "heartbeat garbage": lambda: self._write(self.hb_dir / "watcher.json", "{not json"),
            "heartbeat oversize": lambda: self._write(self.hb_dir / "watcher.json", "x" * 9000),
            "heartbeat group-writable": lambda: (self.write_heartbeat(self_state="dark"),
                                                 (self.hb_dir / "watcher.json").chmod(0o660)),
            "heartbeat symlink": lambda: ((self.hb_dir / "watcher.json").unlink(),
                                          (self.hb_dir / "real.json").write_text(
                                              json.dumps({"schema_version": 1, "written_at": self.evnow,
                                                          "self_state": "dark"})),
                                          (self.hb_dir / "watcher.json").symlink_to(self.hb_dir / "real.json")),
            "heartbeat wrong schema": lambda: self._write(self.hb_dir / "watcher.json",
                                                           {"schema_version": 2, "written_at": self.evnow,
                                                            "self_state": "dark"}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.write_health(recovery_ok=False)
                self.write_heartbeat(self_state="dark")
                for f in self.hb_dir.iterdir():
                    if f.name != "watcher.json":
                        f.unlink()
                mutate()
                ok, states = self.check()
                self.assertFalse(ok, states)
                self.assertEqual(states[1], f"{EV_HEIM}=unknown")

    def test_wrong_owner_is_unknown(self) -> None:
        EXTRA_PW[EV_HEIM] = _pw(EV_HEIM, os.geteuid() + 1)
        self.write_health(recovery_ok=False)
        self.write_heartbeat(self_state="dark")
        self.assertEqual(self.check()[1][1], f"{EV_HEIM}=unknown")

    def test_recovery_record_missing_or_mismatched_is_unknown(self) -> None:
        self.write_heartbeat(self_state="dark")
        (self.health / "recovery.json").unlink()
        self.assertEqual(self.check()[1][0], f"{EV_RECOVERY}=unknown")
        self.write_health(recovery_ok=False, persona="orchestrator")
        self.assertEqual(self.check()[1][0], f"{EV_RECOVERY}=unknown")

    def test_bad_precondition_shapes_rejected(self) -> None:
        bads = [
            {"kind": "nope", "repairers": []},
            {"kind": "higher-precedence-bad", "repairers": []},
            {"kind": "higher-precedence-bad", "repairers": [{"kind": "watcher-heartbeat", "path": "rel",
                                                             "writer": EV_HEIM, "stale_s": 600}]},
            {"kind": "higher-precedence-bad", "repairers": [{"kind": "watcher-heartbeat", "path": "/x",
                                                             "writer": DSF, "stale_s": 600}]},
            {"kind": "higher-precedence-bad", "repairers": [{"kind": "watcher-heartbeat", "path": "/x",
                                                             "writer": EV_HEIM, "stale_s": 0}]},
            {"kind": "higher-precedence-bad", "repairers": [{"kind": "export-record", "path": "/x",
                                                             "writer": EV_RECOVERY, "stale_s": 600}]},
            {"kind": "higher-precedence-bad", "repairers": [{"kind": "watcher-heartbeat", "path": "/x",
                                                             "writer": EV_HEIM, "stale_s": 600, "extra": 1}]},
        ]
        for bad in bads:
            with self.subTest(pre=bad):
                p = self.third_manifest()
                p["invokers"][DSF]["precondition"] = bad
                self.write_manifest(p)
                with self.assertRaises(ManifestValidationError):
                    load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid, pwnam=_fake_pwnam)


class PreconditionDispatchTests(_ThirdSeatMixin, unittest.TestCase):
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(self.third_manifest())
        self.now = float(self.evnow)   # the lease store's clock is the evidence clock
        # T-5128: the lease is taken while both repairers read bad (acquire is
        # gated too); they then recover, so the dispatch-time check is what
        # each test below exercises. T-5129: confirmed over two snapshots.
        self.confirm_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")
        self.write_health(recovery_ok=True)
        self.write_heartbeat(self_state="ok")

    def test_healthy_repairers_refuse_before_any_action(self) -> None:
        r = self.dispatch(verb="kill-session", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("precedence precondition not met", r.detail)
        self.assertIn(f"{EV_HEIM}=healthy", r.detail)
        self.runner.assert_not_called()

    def test_both_bad_executes(self) -> None:
        self.confirm_bad()
        r = self.dispatch(verb="kill-session", invoker=self.dsf)
        self.assertEqual(r.outcome, "executed", r.detail)
        self.runner.assert_called_once()

    def test_precondition_refusal_is_audited_and_spends_no_rate_slot(self) -> None:
        for _ in range(4):
            self.dispatch(verb="kill-session", invoker=self.dsf, dry_run=True)
        self.confirm_bad(heim="dark")
        self.assertEqual(self.dispatch(verb="kill-session", invoker=self.dsf, dry_run=True).outcome, "dry_run")
        refusals = [r for r in self.audit_records() if r.get("event") == "refuse"]
        self.assertTrue(any("precedence precondition" in r.get("reason", "") for r in refusals))

    def test_watchman_without_precondition_is_unaffected(self) -> None:
        self.assertEqual(self.lease(op="release", invoker=self.dsf).outcome, "executed")
        self.assertEqual(self.lease(op="acquire", invoker=self.watchman).outcome, "executed")
        self.assertEqual(self.dispatch(verb="kill-session").outcome, "executed")

    def test_lease_audit_sees_the_lease_only_target(self) -> None:
        from tools.lease_audit import load_target_keys
        self.assertEqual(load_target_keys(self.manifest_path).get(DSF), DSF)



class PreconditionLeaseAcquireTests(_ThirdSeatMixin, unittest.TestCase):
    """T-5128: a precondition-bearing invoker cannot take or renew a lease
    while its precondition fails, so it cannot squat a lease that a higher-
    precedence repairer needs. Release and status stay ungated; invokers with
    no precondition (Recovery, Watchman) behave exactly as before."""
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        self.write_manifest(self.third_manifest())
        self.now = float(self.evnow)   # the lease store's clock is the evidence clock

    def make_bad(self) -> None:
        self.confirm_bad()   # T-5129: a single BAD snapshot no longer suffices

    def status(self, target="claude-fallback") -> str:
        return self.lease(op="status", target_key=target, invoker=self.watchman).detail

    def test_acquire_refused_while_repairers_healthy(self) -> None:
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("precedence precondition not met", r.detail)
        self.assertIn(f"{EV_RECOVERY}=healthy", r.detail)
        self.assertIn("state=free", self.status())
        self.assertFalse(any(rec.get("event") == "lease_acquire" for rec in self.audit_records()))
        self.assertTrue(any(rec.get("event") == "refuse" and "precedence precondition" in rec.get("reason", "")
                            for rec in self.audit_records()))

    def test_acquire_refused_when_one_repairer_bad(self) -> None:
        self.write_health(recovery_ok=False)
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn(f"{EV_HEIM}=healthy", r.detail)

    def test_acquire_refused_on_unknown_evidence(self) -> None:
        # Fail-closed: a seat that cannot prove its precondition must not
        # hold a lease that blocks a higher-precedence repairer (D-42).
        self.write_health(recovery_ok=False)
        (self.hb_dir / "watcher.json").unlink()
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn(f"{EV_HEIM}=unknown", r.detail)
        self.assertIn("state=free", self.status())

    def test_acquire_allowed_when_every_repairer_bad(self) -> None:
        self.make_bad()
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "executed", r.detail)
        self.assertIn(f"holder={DSF}", self.status())

    def test_renew_refused_once_a_repairer_recovers(self) -> None:
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")
        before = self.status()
        self.now += 60
        self.write_health(recovery_ok=True, exported_at=self.evnow + 60)
        self.evnow += 60
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("precedence precondition not met", r.detail)
        # The record was not extended: same holder, same expiry.
        self.assertEqual(before.split("expires_at=")[1].split()[0],
                         self.status().split("expires_at=")[1].split()[0])

    def test_release_and_status_stay_ungated(self) -> None:
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")
        self.write_health(recovery_ok=True)
        self.write_heartbeat(self_state="ok")
        self.assertEqual(self.lease(op="status", invoker=self.dsf).outcome, "executed")
        self.assertEqual(self.lease(op="release", invoker=self.dsf).outcome, "executed")
        self.assertIn("state=free", self.status())

    def test_own_lease_only_target_also_gated(self) -> None:
        # Squatting the lease_only target would block Recovery's and Watchman's
        # lease on it, so their legitimate repairs would read as unleased.
        r = self.lease(op="acquire", target_key=DSF, invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("precedence precondition not met", r.detail)
        self.assertEqual(self.lease(op="acquire", target_key=DSF, invoker=self.recovery).outcome, "executed")

    def test_own_lease_only_target_allowed_when_every_repairer_bad(self) -> None:
        # The positive half (it replaces the deepseek-fallback leg of T-5124's
        # every-invoker loop): with the precondition met, the seat can still
        # take and give back its own lease_only target.
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", target_key=DSF, invoker=self.dsf).outcome, "executed")
        self.assertEqual(self.lease(op="release", target_key=DSF, invoker=self.dsf).outcome, "executed")

    def test_invokers_without_precondition_unaffected(self) -> None:
        # Healthy, bad and missing evidence alike: Recovery and Watchman lease as before.
        for setup in ("healthy", "bad", "missing"):
            with self.subTest(evidence=setup):
                if setup == "bad":
                    self.make_bad()
                elif setup == "missing":
                    for f in list(self.health.iterdir()) + list(self.hb_dir.iterdir()):
                        f.unlink()
                for inv, target in ((self.watchman, "claude-fallback"), (self.recovery, DSF)):
                    self.assertEqual(self.lease(op="acquire", target_key=target, invoker=inv).outcome, "executed")
                    self.assertEqual(self.lease(op="acquire", target_key=target, invoker=inv).outcome, "executed")
                    self.assertEqual(self.lease(op="release", target_key=target, invoker=inv).outcome, "executed")



class PreconditionDebounceTests(_ThirdSeatMixin, unittest.TestCase):
    """T-5129: one BAD health snapshot must not satisfy the precondition.
    Every higher-precedence repairer must read BAD on two consecutive broker
    reads that see two different evidence snapshots (or, for stale evidence,
    reads at least the minimum debounce age apart), matching the
    recovery_watcher DEBOUNCE_RUNS=2. HEALTHY or UNKNOWN breaks the streak.
    Invokers with no precondition read and write no debounce state."""
    auto_lease = False

    def setUp(self) -> None:
        super().setUp()
        import tools.codex_recovery as cr
        self.cr = cr
        self.write_manifest(self.third_manifest())
        self.now = float(self.evnow)   # the lease store's clock is the evidence clock
        self.min_s = getattr(cr, "PRECONDITION_DEBOUNCE_MIN_SECS", 60)
        self.confirm_gap_s = getattr(cr, "PRECONDITION_DEBOUNCE_CONFIRM_GAP_SECS", 180)
        self.held_gap_s = getattr(cr, "PRECONDITION_DEBOUNCE_CONFIRMED_GAP_SECS", LEASE_TTL_SECS)
        self.state_dir = self.rate_dir / "precondition"

    def pre(self):
        return load_broker_manifest(self.manifest_path, expected_owner_uid=self.owner_uid,
                                    pwnam=_fake_pwnam).invokers[DSF].precondition

    def dcheck(self):
        return self.cr.check_precondition_debounced(
            self.pre(), invoker_user=DSF, now=self.now, pwnam=_fake_pwnam, lease_store=self.lease_store)

    def bump(self, secs: int) -> None:
        self.now += secs
        self.evnow += secs

    def make_bad(self, heim="capped") -> None:
        self.write_health(recovery_ok=False)
        self.write_heartbeat(self_state=heim)

    def test_single_bad_snapshot_refuses(self) -> None:
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad-unconfirmed", f"{EV_HEIM}=bad-unconfirmed"])

    def test_same_snapshot_reread_later_still_refuses(self) -> None:
        self.make_bad()
        self.assertFalse(self.dcheck()[0])
        self.now += 2 * self.min_s          # broker time moves, the evidence does not
        ok, states = self.dcheck()
        self.assertFalse(ok, states)

    def test_two_bad_snapshots_confirm(self) -> None:
        self.make_bad()
        self.assertFalse(self.dcheck()[0])
        self.bump(self.min_s)
        self.make_bad()
        ok, states = self.dcheck()
        self.assertTrue(ok, states)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad", f"{EV_HEIM}=bad"])
        self.bump(self.min_s)               # a sustained BAD keeps confirming
        self.make_bad(heim="dark")
        self.assertTrue(self.dcheck()[0])

    def test_two_snapshots_back_to_back_refuse(self) -> None:
        self.make_bad()
        self.assertFalse(self.dcheck()[0])
        self.bump(1)
        self.make_bad()
        self.assertFalse(self.dcheck()[0])

    def test_healthy_read_resets_the_streak(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(self.min_s)
        self.write_heartbeat(self_state="ok")
        self.assertFalse(self.dcheck()[0])
        self.bump(self.min_s)
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states[1], f"{EV_HEIM}=bad-unconfirmed")
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])

    def test_unknown_read_refuses_and_resets_the_streak(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(self.min_s)
        self.write_health(recovery_ok=False)
        (self.hb_dir / "watcher.json").unlink()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states[1], f"{EV_HEIM}=unknown")
        self.bump(self.min_s)
        self.make_bad()
        self.assertFalse(self.dcheck()[0])

    def test_600s_gap_without_confirmation_refuses(self) -> None:
        # The reviewer's split: an UNCONFIRMED run may only be confirmed within
        # CONFIRM_GAP of its first read; the repairers may have recovered and
        # gone bad again on one fresh snapshot in between.
        self.make_bad()
        self.dcheck()
        self.bump(600)
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad-unconfirmed", f"{EV_HEIM}=bad-unconfirmed"])

    def test_120s_gap_confirms(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(120)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])

    def test_unconfirmed_run_older_than_confirm_gap_resets(self) -> None:
        self.make_bad()
        self.dcheck()
        self.now += 100                       # same snapshot re-read: still unconfirmed
        self.assertFalse(self.dcheck()[0])
        self.now += 100
        self.evnow = int(self.now)            # new snapshot, but the run began 200s ago
        self.make_bad()
        self.assertFalse(self.dcheck()[0])
        self.bump(self.min_s)                 # the reset run confirms on its own clock
        self.make_bad()
        self.assertTrue(self.dcheck()[0])

    def test_confirmed_run_survives_a_600s_renewal_gap(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])
        self.bump(600)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])
        self.bump(self.held_gap_s + 1)        # past the held window: start over
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states[0], f"{EV_RECOVERY}=bad-unconfirmed")

    def test_confirmed_run_still_resets_on_healthy(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])
        self.bump(self.min_s)
        self.write_heartbeat(self_state="ok")
        self.dcheck()
        self.bump(self.min_s)
        self.make_bad()
        self.assertFalse(self.dcheck()[0])

    def test_clock_going_backwards_breaks_the_streak(self) -> None:
        # Confirm a run (T0, T0+150), then step the broker clock back 70s
        # (more than the evidence skew) to T0+80 with a NEW fresh snapshot
        # stamped at that clock, so both repairers read bad, not unknown.
        # Only the seen_at <= now check stops this read confirming.
        t0 = self.now
        self.make_bad()
        self.dcheck()
        self.bump(150)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])
        self.now = t0 + 80
        self.evnow = int(t0) + 80
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok, states)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad-unconfirmed", f"{EV_HEIM}=bad-unconfirmed"])

    def test_stale_evidence_confirms_after_min_age(self) -> None:
        # Both writers dead (D-36 x D-42): the files stop changing, so the
        # snapshot cannot advance; stale is itself an evidence-age verdict, and
        # two reads at least the debounce age apart confirm it.
        self.write_health(recovery_ok=True, exported_at=self.evnow - 601)
        self.write_heartbeat(self_state="ok", written_at=self.evnow - 601)
        self.assertFalse(self.dcheck()[0])
        self.now += self.min_s - 1
        self.assertFalse(self.dcheck()[0])
        self.now += 1
        ok, states = self.dcheck()
        self.assertTrue(ok, states)

    def test_each_repairer_is_debounced_independently(self) -> None:
        # Recovery has read bad for a while; Watchman flips bad on one snapshot.
        self.write_health(recovery_ok=False)
        self.dcheck()
        self.bump(self.min_s)
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad", f"{EV_HEIM}=bad-unconfirmed"])

    def test_corrupt_state_is_no_streak(self) -> None:
        self.make_bad()
        self.dcheck()
        state_file = self.state_dir / f"{DSF}.json"
        state_file.write_text("{not json")
        self.bump(self.min_s)
        self.make_bad()
        self.assertFalse(self.dcheck()[0])
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])

    def test_streak_with_first_after_seen_is_invalid(self) -> None:
        self.make_bad()
        self.dcheck()
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])
        state_file = self.state_dir / f"{DSF}.json"
        obj = json.loads(state_file.read_text())
        for entry in obj["streaks"].values():
            entry["first_at"] = entry["seen_at"] + 1000   # out of order: forged or corrupt
        state_file.write_text(json.dumps(obj))
        self.bump(self.min_s)
        self.make_bad()
        ok, states = self.dcheck()
        self.assertFalse(ok)
        self.assertEqual(states, [f"{EV_RECOVERY}=bad-unconfirmed", f"{EV_HEIM}=bad-unconfirmed"])

    def test_deeply_nested_state_is_no_streak(self) -> None:
        self.make_bad()
        self.dcheck()
        state_file = self.state_dir / f"{DSF}.json"
        payload = "[" * 2000 + "]" * 2000              # under the 4 KiB cap, past the recursion limit
        self.assertLess(len(payload), 4096)
        state_file.write_text(payload)
        self.bump(self.min_s)
        self.make_bad()
        # CPython's C scanner tolerates this depth; the pure-Python scanner (or
        # a smaller C stack) raises RecursionError on it. Simulate that for the
        # state payload only, so the broker's handling is what is under test.
        real_loads = json.loads

        def loads(raw, *a, **kw):
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if text.startswith("[[[["):
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(raw, *a, **kw)

        with unittest.mock.patch.object(self.cr.json, "loads", loads):
            ok, states = self.dcheck()                  # no RecursionError out of the broker
        self.assertFalse(ok)
        self.assertEqual(states[0], f"{EV_RECOVERY}=bad-unconfirmed")
        self.bump(self.min_s)
        self.make_bad()
        self.assertTrue(self.dcheck()[0])

    def test_state_is_owner_only_and_symlink_refused(self) -> None:
        self.make_bad()
        self.dcheck()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        st = (self.state_dir / f"{DSF}.json").stat()
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (self.owner_uid, 0o600))
        (self.state_dir / f"{DSF}.json").unlink()
        (self.root / "elsewhere.json").write_text("{}")
        (self.state_dir / f"{DSF}.json").symlink_to(self.root / "elsewhere.json")
        with self.assertRaises(self.cr.LeaseStateError):
            self.dcheck()

    def test_no_precondition_touches_no_state(self) -> None:
        self.assertEqual(self.cr.check_precondition_debounced(
            None, invoker_user=WATCHMAN, now=self.now, pwnam=_fake_pwnam, lease_store=self.lease_store),
            (True, []))
        self.make_bad()
        for inv in (self.watchman, self.recovery):
            self.assertEqual(self.lease(op="acquire", invoker=inv).outcome, "executed")
            if inv is self.watchman:
                self.assertEqual(self.dispatch(verb="kill-session").outcome, "executed")
            self.assertEqual(self.lease(op="release", invoker=inv).outcome, "executed")
        self.assertFalse(self.state_dir.exists())

    def test_acquire_gate_refuses_a_single_bad_snapshot(self) -> None:
        self.make_bad()
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn(f"{EV_HEIM}=bad-unconfirmed", r.detail)
        self.bump(self.min_s)
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")

    def test_dispatch_gate_refuses_a_single_bad_snapshot(self) -> None:
        self.make_bad()
        self.lease(op="acquire", invoker=self.dsf)
        self.bump(self.min_s)
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")
        # Watchman recovers, then flips bad on one snapshot: the held lease
        # does not carry the old streak through the HEALTHY read.
        self.bump(self.min_s)
        self.write_heartbeat(self_state="ok")
        self.assertEqual(self.dispatch(verb="kill-session", invoker=self.dsf).outcome, "refused")
        self.bump(self.min_s)
        self.make_bad()
        r = self.dispatch(verb="kill-session", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn(f"{EV_HEIM}=bad-unconfirmed", r.detail)
        self.runner.assert_not_called()
        self.bump(self.min_s)
        self.make_bad()
        self.assertEqual(self.dispatch(verb="kill-session", invoker=self.dsf).outcome, "executed")
        self.runner.assert_called_once()

    def test_unreadable_state_refuses_at_both_gates(self) -> None:
        self.make_bad()
        self.lease(op="acquire", invoker=self.dsf)
        self.bump(self.min_s)
        self.make_bad()
        self.assertEqual(self.lease(op="acquire", invoker=self.dsf).outcome, "executed")
        (self.state_dir / f"{DSF}.json").chmod(0o644)   # wrong mode: integrity doubt
        r = self.lease(op="acquire", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("state error", r.detail)
        r = self.dispatch(verb="kill-session", invoker=self.dsf)
        self.assertEqual(r.outcome, "refused")
        self.assertIn("state error", r.detail)
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
