"""T-5449: the seat-local ``apply`` / ``rollback`` verbs.

SimulatedHostBackend, tmp roots and mocks only. Nothing here runs against a real
home, user manager, tmux, claude or codex.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import persona_installer as _pim
from tools import persona_installer_apply as pia
from tools.persona_installer import (
    FileReceiptStore,
    LocalHostBackend,
    PreflightResult,
    SimulatedHostBackend,
)
from tools.test_persona_installer_periodics import PeriodicCase

UID = 4242
SAME_UID = {"uid_of": lambda account: UID, "euid": UID}


class ApplyVerbCase(PeriodicCase):
    def kwargs(self, **extra):
        return {
            "repo_root": pia.REPO_ROOT,
            "root": self.root / "projection",
            **SAME_UID,
            **extra,
        }


class AccountGuardTests(ApplyVerbCase):
    def test_refuses_unless_effective_uid_is_the_target_accounts(self) -> None:
        with self.assertRaisesRegex(pia.ApplyRefused, "must run as"):
            pia.dry_run(
                self.manifest_path, "recovery", backend=SimulatedHostBackend(),
                **{**self.kwargs(), "euid": UID + 1},
            )

    def test_refuses_an_account_that_does_not_exist(self) -> None:
        with self.assertRaisesRegex(pia.ApplyRefused, "does not exist"):
            pia.dry_run(
                self.manifest_path, "recovery", backend=SimulatedHostBackend(),
                **{**self.kwargs(), "uid_of": lambda account: None},
            )

    def test_execute_and_rollback_carry_the_same_guard(self) -> None:
        bad = {**self.kwargs(), "euid": UID + 1}
        with self.assertRaises(pia.ApplyRefused):
            pia.execute(self.manifest_path, "recovery", plan_digest="x", backend=SimulatedHostBackend(), **bad)
        with self.assertRaises(pia.ApplyRefused):
            pia.rollback_receipt(self.manifest_path, "recovery", "r1", backend=SimulatedHostBackend(), **bad)


class DryRunTests(ApplyVerbCase):
    def test_dry_run_prints_digest_and_one_verdict_per_operation_and_writes_nothing(self) -> None:
        backend = SimulatedHostBackend()
        code, report = pia.dry_run(self.manifest_path, "recovery", backend=backend, **self.kwargs())
        plan = self.plan()
        self.assertEqual(code, 0, report["problems"])
        self.assertEqual(report["plan_digest"], plan.digest)
        # T-5472: all-in-one preflights only "all"-scope ops, like the real run; the
        # staged-cutover absence check is reported under stage-prefix / staged-activate.
        in_scope = [o.operation_id for o in plan.operations if o.execution_scope == "all"]
        self.assertEqual([v["operation_id"] for v in report["operations"]], in_scope)
        self.assertTrue(report["ok"])
        self.assertIn(plan.digest, report["next"])
        self.assertEqual(backend.entries, {})
        self.assertEqual(backend.events, [])

    def test_dry_run_collects_every_refusal_and_exits_nonzero(self) -> None:
        class Refusing(SimulatedHostBackend):
            def preflight(self, operation):
                if operation.kind in {"seat-periodic-unit-install", "harness-check"}:
                    return PreflightResult(False, "conflicting", "nope")
                return super().preflight(operation)

        code, report = pia.dry_run(self.manifest_path, "recovery", backend=Refusing(), **self.kwargs())
        self.assertEqual(code, 1)
        self.assertGreater(len(report["problems"]), 1)  # all of them, not just the first
        self.assertFalse(report["ok"])
        self.assertIn("nothing was changed", report["next"])

    def test_dry_run_prepends_the_account_npm_global_bin_to_path(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        pia.ensure_account_path(Path("/home/acct"), env)
        self.assertEqual(env["PATH"], "/home/acct/.npm-global/bin:/usr/bin")
        pia.ensure_account_path(Path("/home/acct"), env)  # idempotent
        self.assertEqual(env["PATH"].count(".npm-global"), 1)


class ExecuteTests(ApplyVerbCase):
    def run_execute(self, backend, **extra):
        plan = self.plan()
        digest = extra.pop("plan_digest", plan.digest)
        return plan, pia.execute(
            self.manifest_path, "recovery", plan_digest=digest, backend=backend, **self.kwargs(**extra)
        )

    def test_execute_requires_the_digest_the_dry_run_printed(self) -> None:
        backend = SimulatedHostBackend()
        with self.assertRaisesRegex(pia.ApplyRefused, "does not match"):
            self.run_execute(backend, plan_digest="0" * 64)
        self.assertEqual(backend.entries, {})

    def test_unknown_workflow_is_refused(self) -> None:
        with self.assertRaisesRegex(pia.ApplyRefused, "unknown workflow"):
            self.run_execute(SimulatedHostBackend(), workflow="yolo")

    def test_all_in_one_applies_writes_receipt_and_a_plan_sidecar(self) -> None:
        backend = SimulatedHostBackend()
        plan, (code, summary) = self.run_execute(backend)
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "applied")
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)
        sidecar = Path(summary["plan_sidecar"])
        self.assertEqual(sidecar.name, f"{summary['receipt_id']}.plan.json")
        self.assertEqual(oct(sidecar.stat().st_mode & 0o777), "0o600")
        stored = json.loads(sidecar.read_text())
        self.assertEqual(stored["plan"]["digest"], plan.digest)

    def test_failed_preflight_applies_nothing_and_exits_nonzero(self) -> None:
        class Refusing(SimulatedHostBackend):
            def preflight(self, operation):
                if operation.operation_id == "harness.direct-fire":
                    return PreflightResult(False, "conflicting", "nope")
                return super().preflight(operation)

        backend = Refusing()
        _plan, (code, summary) = self.run_execute(backend)
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "refused")
        self.assertEqual(backend.entries, {})

    def test_stage_prefix_then_staged_activate_then_rollback(self) -> None:
        backend = SimulatedHostBackend()
        plan, (code, staged) = self.run_execute(backend, workflow="stage-prefix")
        self.assertEqual(code, 0, staged)
        self.assertNotIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)
        # persist the staged receipt the way the local sink would
        FileReceiptStore().store_receipt(plan, _pim.ApplyReceipt.from_dict(backend.receipts[staged["receipt_id"]]))
        code, activated = pia.execute(
            self.manifest_path, "recovery", plan_digest=plan.digest, workflow="staged-activate",
            receipt_id=staged["receipt_id"], backend=backend, **self.kwargs(),
        )
        self.assertEqual(code, 0, activated)
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_staged_activate_needs_a_staged_receipt(self) -> None:
        with self.assertRaisesRegex(pia.ApplyRefused, "requires --receipt"):
            self.run_execute(SimulatedHostBackend(), workflow="staged-activate")
        with self.assertRaisesRegex(pia.ApplyRefused, "unreadable"):
            self.run_execute(SimulatedHostBackend(), workflow="staged-activate", receipt_id="nope")


class RollbackTests(ApplyVerbCase):
    def applied(self):
        backend = SimulatedHostBackend()
        plan = self.plan()
        code, summary = pia.execute(
            self.manifest_path, "recovery", plan_digest=plan.digest, backend=backend, **self.kwargs()
        )
        self.assertEqual(code, 0, summary)
        FileReceiptStore().store_receipt(
            plan, _pim.ApplyReceipt.from_dict(backend.receipts[summary["receipt_id"]])
        )
        return backend, summary["receipt_id"]

    def test_rollback_by_receipt_id_restores_the_host(self) -> None:
        backend, receipt_id = self.applied()
        code, summary = pia.rollback_receipt(
            self.manifest_path, "recovery", receipt_id, backend=backend, **self.kwargs()
        )
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "rolled-back")
        self.assertNotIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)
        self.assert_host_restored(backend)

    def test_rollback_rejects_a_path_shaped_receipt_id_and_a_missing_receipt(self) -> None:
        for bad in ("../x", "a/b", "", ".hidden"):
            with self.subTest(bad=bad), self.assertRaisesRegex(pia.ApplyRefused, "receipt id"):
                pia.rollback_receipt(
                    self.manifest_path, "recovery", bad, backend=SimulatedHostBackend(), **self.kwargs()
                )
        with self.assertRaisesRegex(pia.ApplyRefused, "unreadable"):
            pia.rollback_receipt(
                self.manifest_path, "recovery", "absent-1", backend=SimulatedHostBackend(), **self.kwargs()
            )

    def test_rollback_is_pinned_to_the_manifest_unless_break_glass(self) -> None:
        backend, receipt_id = self.applied()
        plan_now = self.plan()
        tampered = json.loads(
            (Path(plan_now.provenance["receipt_dir"]) / f"{receipt_id}.plan.json").read_text()
        )
        self.assertEqual(tampered["schema"], pia.PLAN_SIDECAR_SCHEMA)
        # break-glass skips ONLY the manifest re-derivation; still succeeds on a clean sidecar
        code, summary = pia.rollback_receipt(
            self.manifest_path, "recovery", receipt_id, break_glass=True, backend=backend,
            **self.kwargs(account_of_euid=lambda euid: (
                plan_now.account,
                Path("/") / pia._account_home(plan_now).relative_to(self.root / "projection"))),
        )
        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["break_glass"])


class BreakGlassNoManifestTests(ApplyVerbCase):
    """T-5453: break-glass rollback must work when instance.yaml cannot load."""

    ACCOUNT = "recovery"

    def applied(self):
        backend = SimulatedHostBackend()
        plan = self.plan()
        code, summary = pia.execute(
            self.manifest_path, "recovery", plan_digest=plan.digest, backend=backend, **self.kwargs()
        )
        self.assertEqual(code, 0, summary)
        FileReceiptStore().store_receipt(
            plan, _pim.ApplyReceipt.from_dict(backend.receipts[summary["receipt_id"]])
        )
        self.receipt_dir = Path(plan.provenance["receipt_dir"])
        self.home = pia._account_home(plan)  # rooted under the tmp projection
        self.passwd_home = Path("/") / self.home.relative_to(self.root / "projection")
        self.account = plan.account
        return backend, summary["receipt_id"]

    def bg(self, backend, receipt_id, manifest=None, **extra):
        kw = self.kwargs(account_of_euid=lambda euid: (self.account, self.passwd_home), **extra)
        return pia.rollback_receipt(
            manifest or self.root / "missing.yaml", "recovery", receipt_id,
            break_glass=True, backend=backend, **kw,
        )

    def test_default_state_dir_matches_the_manifest(self) -> None:
        self.assertEqual(
            self.manifest.installer["paths"]["state_dir"], pia.DEFAULT_STATE_DIR_RELATIVE
        )

    def test_succeeds_with_a_nonexistent_manifest(self) -> None:
        backend, rid = self.applied()
        code, summary = self.bg(backend, rid)
        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["break_glass"])
        self.assert_host_restored(backend)

    def test_break_glass_rollback_emits_nothing_on_stdout_or_stderr(self) -> None:
        # T-6167 PR-6: recovery stays quiet; the fallback notice must not leak into it.
        backend, rid = self.applied()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), contextlib.redirect_stderr(err):
            code, summary = self.bg(backend, rid)
        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["break_glass"])
        # break-glass already prints its own BREAK-GLASS warning on stderr (persona_installer);
        # the invariant is that the fallback notice is not added to it.
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("hand-run fallback", err.getvalue())
        self.assertNotIn(pia.FALLBACK_NOTICE, err.getvalue())
        self.assertNotIn("driver", summary)

    def test_succeeds_with_a_garbage_manifest(self) -> None:
        backend, rid = self.applied()
        self.manifest_path.write_text(": : [unparseable", encoding="utf-8")
        code, summary = self.bg(backend, rid, manifest=self.manifest_path)
        self.assertEqual(code, 0, summary)
        self.assert_host_restored(backend)

    def test_explicit_receipt_dir_override_works(self) -> None:
        backend, rid = self.applied()
        code, summary = self.bg(backend, rid, receipt_dir=self.receipt_dir)
        self.assertEqual(code, 0, summary)

    def test_stale_digest_after_payload_edit_is_refused(self) -> None:
        backend, rid = self.applied()
        path = self.receipt_dir / f"{rid}.plan.json"
        data = json.loads(path.read_text())
        data["plan"]["provenance"]["source_revision"] = "tampered"
        path.write_text(json.dumps(data))
        code, summary = self.bg(backend, rid)
        self.assertEqual(code, 1)
        self.assertIn("embedded digest", " ".join(summary["errors"]))
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_receipt_plan_digest_mismatch_is_refused(self) -> None:
        backend, rid = self.applied()
        path = self.receipt_dir / f"{rid}.json"
        data = json.loads(path.read_text())
        data["plan_digest"] = "0" * 64
        path.write_text(json.dumps(data))
        code, summary = self.bg(backend, rid)
        self.assertEqual(code, 1)
        self.assertIn("plan digest", " ".join(summary["errors"]))

    def test_euid_mismatch_is_refused_before_any_mutation(self) -> None:
        backend, rid = self.applied()
        with self.assertRaisesRegex(pia.ApplyRefused, "must run as"):
            self.bg(backend, rid, euid=UID + 1)
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_pwd_account_must_match_the_sidecar_account(self) -> None:
        backend, rid = self.applied()
        self.account = "someone-else"
        with self.assertRaisesRegex(pia.ApplyRefused, "account"):
            self.bg(backend, rid)
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_non_break_glass_with_unloadable_manifest_still_refuses(self) -> None:
        backend, rid = self.applied()
        with self.assertRaises((_pim.InstallerError, FileNotFoundError, OSError)):
            pia.rollback_receipt(
                self.root / "missing.yaml", "recovery", rid, backend=backend, **self.kwargs()
            )
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_receipt_dir_override_outside_the_home_is_refused(self) -> None:
        backend, rid = self.applied()
        outside = self.root / "elsewhere"
        outside.mkdir()
        for name in (f"{rid}.json", f"{rid}.plan.json"):
            (outside / name).write_text((self.receipt_dir / name).read_text())
        with self.assertRaisesRegex(pia.ApplyRefused, "outside allowed roots"):
            self.bg(backend, rid, receipt_dir=outside)
        self.assertIn("pantheon-runtime-controller@recovery.service", backend.enabled_services)

    def test_receipt_dir_symlink_escape_is_refused(self) -> None:
        backend, rid = self.applied()
        outside = self.root / "elsewhere"
        outside.mkdir()
        for name in (f"{rid}.json", f"{rid}.plan.json"):
            (outside / name).write_text((self.receipt_dir / name).read_text())
        link = self.home / "link"
        link.symlink_to(outside)
        with self.assertRaisesRegex(pia.ApplyRefused, "outside allowed roots"):
            self.bg(backend, rid, receipt_dir=link)

    def test_cli_receipt_dir_requires_break_glass(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(pia, "rollback_receipt") as rb:
            with self.assertRaises(SystemExit):
                _pim.main(["rollback", "--persona", "recovery", "--receipt", "r1",
                           "--receipt-dir", str(self.root)])
            rb.assert_not_called()
            rb.return_value = (0, {"ok": True})
            _pim.main(["rollback", "--persona", "recovery", "--receipt", "r1",
                       "--break-glass-no-manifest", "--receipt-dir", str(self.root)])
        self.assertEqual(rb.call_args.kwargs["receipt_dir"], self.root)


class HandRunFallbackLabelTests(ApplyVerbCase):
    """T-6167 PR-6: hand-run apply is a LABELLED fallback; the germinate engine is the
    default seat-install path. Nothing is refused; the label is stderr + JSON only."""

    def run_quiet(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, out.getvalue(), err.getvalue()

    def execute(self, backend, **extra):
        plan = self.plan()
        return plan, self.run_quiet(
            pia.execute, self.manifest_path, "recovery", plan_digest=plan.digest,
            backend=backend, **self.kwargs(**extra),
        )

    def test_notice_text_names_the_engine_path_and_the_live_converge_exit(self) -> None:
        self.assertIn("germinate install --seat", pia.FALLBACK_NOTICE)
        self.assertIn("T-5930", pia.FALLBACK_NOTICE)
        self.assertEqual(pia.FALLBACK_DRIVER, "hand-run-fallback")

    def test_execute_summary_and_sidecar_carry_the_driver_and_notice_is_stderr_only(self) -> None:
        _plan, ((code, summary), out, err) = self.execute(SimulatedHostBackend())
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["driver"], "hand-run-fallback")
        stored = json.loads(Path(summary["plan_sidecar"]).read_text())
        self.assertEqual(stored["driver"], "hand-run-fallback")
        self.assertIn(pia.FALLBACK_NOTICE, err)
        self.assertEqual(out, "")

    def test_stage_prefix_and_staged_activate_summaries_carry_the_driver(self) -> None:
        backend = SimulatedHostBackend()
        plan, ((code, staged), _out, _err) = self.execute(backend, workflow="stage-prefix")
        self.assertEqual(code, 0, staged)
        self.assertEqual(staged["driver"], "hand-run-fallback")
        FileReceiptStore().store_receipt(plan, _pim.ApplyReceipt.from_dict(backend.receipts[staged["receipt_id"]]))
        (code, activated), _out, err = self.run_quiet(
            pia.execute, self.manifest_path, "recovery", plan_digest=plan.digest,
            workflow="staged-activate", receipt_id=staged["receipt_id"], backend=backend,
            **self.kwargs(),
        )
        self.assertEqual(code, 0, activated)
        self.assertEqual(activated["driver"], "hand-run-fallback")
        self.assertIn(pia.FALLBACK_NOTICE, err)

    def test_a_refused_run_still_carries_the_notice_and_still_refuses(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaisesRegex(pia.ApplyRefused, "does not match"):
            pia.execute(
                self.manifest_path, "recovery", plan_digest="0" * 64,
                backend=SimulatedHostBackend(), **self.kwargs(),
            )
        self.assertIn(pia.FALLBACK_NOTICE, err.getvalue())

    def test_dry_run_report_carries_the_driver_and_the_notice(self) -> None:
        (code, report), out, err = self.run_quiet(
            pia.dry_run, self.manifest_path, "recovery", backend=SimulatedHostBackend(), **self.kwargs()
        )
        self.assertEqual(code, 0, report["problems"])
        self.assertEqual(report["driver"], "hand-run-fallback")
        self.assertIn(pia.FALLBACK_NOTICE, err)
        self.assertEqual(out, "")

    def test_rollback_and_break_glass_stay_quiet(self) -> None:
        backend = SimulatedHostBackend()
        plan, ((code, summary), _out, _err) = self.execute(backend)
        FileReceiptStore().store_receipt(
            plan, _pim.ApplyReceipt.from_dict(backend.receipts[summary["receipt_id"]])
        )
        (code, rolled), out, err = self.run_quiet(
            pia.rollback_receipt, self.manifest_path, "recovery", summary["receipt_id"],
            backend=backend, **self.kwargs(),
        )
        self.assertEqual(code, 0, rolled)
        self.assertEqual((out, err), ("", ""))
        self.assertNotIn("driver", rolled)

    def test_an_old_sidecar_without_the_driver_key_still_loads(self) -> None:
        backend = SimulatedHostBackend()
        plan, ((code, summary), _out, _err) = self.execute(backend)
        FileReceiptStore().store_receipt(
            plan, _pim.ApplyReceipt.from_dict(backend.receipts[summary["receipt_id"]])
        )
        path = Path(summary["plan_sidecar"])
        data = json.loads(path.read_text())
        del data["driver"]
        path.write_text(json.dumps(data))
        receipt, loaded = pia._load_receipt_and_plan(plan, summary["receipt_id"])
        self.assertEqual(loaded.digest, plan.digest)


class CliWiringTests(ApplyVerbCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = _pim.main(list(argv))
            except SystemExit as exc:  # argparse error
                code = exc.code
        return code, out.getvalue()

    def test_help_recommends_staged_for_a_live_seat(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            _pim.main(["apply", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("--workflow", text)
        self.assertIn("stage-prefix then staged-activate", text)
        self.assertIn("--plan-digest", text)

    def test_apply_is_dry_by_default_and_execute_needs_a_digest(self) -> None:
        with mock.patch.object(pia, "dry_run", return_value=(0, {"ok": True})) as dry, \
                mock.patch.object(pia, "execute") as run:
            code, out = self.run_main("apply", "--manifest", str(self.manifest_path), "--persona", "recovery")
        self.assertEqual(code, 0)
        dry.assert_called_once()
        run.assert_not_called()
        self.assertEqual(json.loads(out), {"ok": True})
        code, _ = self.run_main(
            "apply", "--manifest", str(self.manifest_path), "--persona", "recovery", "--execute"
        )
        self.assertNotEqual(code, 0)  # --execute without --plan-digest

    def test_a_refusal_is_a_nonzero_exit_not_a_traceback(self) -> None:
        with mock.patch.object(pia, "dry_run", side_effect=pia.ApplyRefused("must run as x")):
            code, _ = self.run_main("apply", "--manifest", str(self.manifest_path), "--persona", "recovery")
        self.assertNotEqual(code, 0)


# ---------------------------------------------------------------------------
# T-5449 (HO ruling 4): the dry run is READ-ONLY, proven not inherited.
# ---------------------------------------------------------------------------

# Closed set of commands the dry run may issue (argv prefixes).
READ_ONLY_COMMANDS = (
    ("systemctl", "--user", "show"),
    ("systemctl", "--user", "is-enabled"),
    ("systemctl", "--user", "is-active"),
    ("systemctl", "--user", "cat"),
    ("systemctl", "--user", "show-environment"),
    ("loginctl", "show-user"),
    ("tmux", "display-message"),
    ("git", "-C"),
)


def _is_read_only(argv) -> bool:
    """The closed read-only set. T-5612: plus the seat pane probe, `tmux -S <socket> has-session`
    (read-only, never starts a server), matched on its exact shape."""
    argv = tuple(argv)
    if len(argv) == 6 and argv[:2] == ("tmux", "-S") and argv[3:5] == ("has-session", "-t"):
        return True
    return any(argv[: len(prefix)] == prefix for prefix in READ_ONLY_COMMANDS)


def _tree_digest(root: Path) -> dict[str, str]:
    seen = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            seen[rel] = "L:" + os.readlink(path)
        elif path.is_file():
            seen[rel] = "F:" + hashlib.sha256(path.read_bytes()).hexdigest() + f":{path.stat().st_mtime_ns}"
        else:
            seen[rel] = "D"
    return seen


class DryRunIsReadOnlyTests(ApplyVerbCase):
    """Drives the real ``dry_run`` path with a real ``LocalHostBackend`` over a tmp
    root. ``_run`` and the user manager record every call. Only the plan's
    ``install_root`` marker is flipped to "/" (targets stay under the tmp root) so
    the local backend's preflight bodies actually execute."""

    def local_dry_run(self, manifest_path=None, persona="recovery", dry=True):
        if manifest_path is None:
            plan = self.plan(persona)
        else:
            plan = _pim.PersonaInstaller(pia.REPO_ROOT).plan(
                _pim.Manifest.load(manifest_path), persona, root=self.root / "real"
            )
        def isolate(index, op):
            fields = {"provenance": {**op.provenance, "install_root": "/"}}
            if op.kind == "mailbox-writable-root-check":
                # a real-instance plan names the REAL mailbox hub (root="/" at plan
                # time); point it at a tmp maildir so the test can never touch it
                maildir = self.root / f"maildir-{index}"
                for sub in ("tmp", "new", "cur"):
                    (maildir / sub).mkdir(parents=True, exist_ok=True)
                fields["target"] = str(maildir)
            return dataclasses.replace(op, **fields)

        rooted = dataclasses.replace(
            plan, operations=tuple(isolate(i, op) for i, op in enumerate(plan.operations))
        )
        backend = LocalHostBackend.__new__(LocalHostBackend)
        backend.command_timeout = 30
        backend.timed_fire_timeout = 240
        backend.events = {}
        backend._baseline_invocation = ""
        commands: list[tuple[str, ...]] = []

        def run(argv, *, timeout=None):
            commands.append(tuple(argv))
            if argv[0] == "loginctl":
                return subprocess.CompletedProcess(argv, 0, "yes\n", "")
            if tuple(argv[:3]) == ("systemctl", "--user", "show-environment"):
                return subprocess.CompletedProcess(argv, 0, "PATH=/usr/bin\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "Unit not found")

        # the REAL SystemctlUserManager (lazily built over backend._run), so every
        # user-manager call surfaces as a recorded argv, not as a mock method name
        backend._run = run

        before = _tree_digest(self.root)
        probes: list[str] = []

        def spy_mkstemp(*args, **kwargs):
            # record, and BLOCK: a real-instance plan names the real mailbox hub
            # (root="/" at plan time); the test must never write there
            probes.append(str(kwargs.get("prefix", "")))
            raise OSError("blocked by test")

        with mock.patch.object(pia, "build_plan", return_value=(None, rooted, None)), \
                mock.patch("tools.mailbox_writable_roots.tempfile.mkstemp", spy_mkstemp), \
                mock.patch.object(_pim.LocalHostBackend, "_current_account_only",
                                  return_value=PreflightResult(True, "current-account", "uid")), \
                mock.patch.object(_pim.LocalHostBackend, "_identity_ready",
                                  return_value=PreflightResult(True, "ready", "stub")):
            if dry:
                code, report = pia.dry_run(
                    manifest_path or self.manifest_path, persona, backend=backend, **self.kwargs(),
                )
            else:  # the execute-time preflight loop: dry_run defaults to False
                verdicts = pia.preflight_verdicts(
                    rooted, backend, pia.actor_for_plan(rooted, uid=UID))
                code, report = 0, {"operations": verdicts, "plan_digest": rooted.digest}
        return before, _tree_digest(self.root), commands, probes, report, code

    def test_every_issued_command_is_in_the_closed_read_only_set(self) -> None:
        _b, _a, commands, _p, report, _c = self.local_dry_run()
        self.assertGreater(len(report["operations"]), 20)  # the full plan really ran
        for argv in commands:
            self.assertTrue(
                _is_read_only(argv),
                f"dry run issued a command outside the read-only set: {argv}",
            )
        for argv in commands:  # belt and braces: no mutating systemctl verb anywhere
            self.assertFalse(
                {"start", "stop", "enable", "disable", "restart", "daemon-reload", "reset-failed", "kill"}
                & set(argv),
                argv,
            )

    def test_nothing_is_written_under_the_root(self) -> None:
        before, after, *_ = self.local_dry_run()
        self.assertEqual(before, after)

    def test_shell_box_settings_op_is_preflighted_read_only_against_a_populated_file(self) -> None:
        # T-5454: a populated settings.json and an executable wrapper in the tmp seat
        # home; the dry run must pass the op and leave both byte- and mtime-identical.
        operation = self.op(self.plan("recovery"), "harness.claude-shell-box")
        settings = Path(operation.target)
        script = Path(operation.desired["env_value"])
        settings.parent.mkdir(parents=True)
        settings.write_text('{"zeta": 1, "env": {"OTHER": "x"}}\n')
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        bytes_before = settings.read_bytes()
        before, after, commands, probes, report, _code = self.local_dry_run()
        self.assertEqual(before, after)
        self.assertEqual(settings.read_bytes(), bytes_before)
        verdict = next(v for v in report["operations"] if v["operation_id"] == "harness.claude-shell-box")
        self.assertEqual(verdict["state"], "pass", verdict)
        self.assertEqual(verdict["kind"], "seat-claude-settings-env")

    def test_every_real_claude_persona_dry_run_reports_the_shell_box_verdict_and_codex_only_does_not(self) -> None:
        real = pia.REPO_ROOT / "instance.yaml"
        manifest = _pim.Manifest.load(real)
        for persona in self.real_personas():
            with self.subTest(persona=persona):
                *_x, report, _code = self.local_dry_run(real, persona)
                plan = _pim.PersonaInstaller(pia.REPO_ROOT).plan(manifest, persona, root=self.root / "real")
                claude = "claude" in _pim._runtime_candidate_harnesses_for_install(
                    manifest, manifest.binding(persona), plan.selected_harness)
                kinds = [v["kind"] for v in report["operations"]]
                self.assertEqual(kinds.count("seat-claude-settings-env"), 1 if claude else 0)

    def real_personas(self):
        manifest = _pim.Manifest.load(pia.REPO_ROOT / "instance.yaml")
        return sorted({raw["persona"] for raw in manifest.roles.values()
                       if isinstance(raw, dict) and raw.get("persona")})

    def test_every_real_instance_persona_dry_run_has_zero_writes_and_only_read_commands(self) -> None:
        real = pia.REPO_ROOT / "instance.yaml"
        personas = self.real_personas()
        self.assertIn("mason", personas)
        self.assertIn("orchestrator", personas)
        for persona in personas:
            with self.subTest(persona=persona):
                before, after, commands, probes, report, code = self.local_dry_run(real, persona)
                self.assertGreater(len(report["operations"]), 40)
                self.assertEqual(before, after)  # zero writes
                self.assertEqual(probes, [], "dry run attempted a mailbox write probe")
                for argv in commands:
                    self.assertTrue(
                        _is_read_only(argv), argv)
                # the approve flow is intact: the printed digest IS the plan's digest
                plan = _pim.PersonaInstaller(pia.REPO_ROOT).plan(
                    _pim.Manifest.load(real), persona, root=self.root / "real")
                self.assertEqual(report["plan_digest"], plan.digest)

    def test_deferred_probe_verdict_is_exact_distinct_and_only_on_planned_probe_ops(self) -> None:
        real = pia.REPO_ROOT / "instance.yaml"
        for persona, expected in (("mason", 0), ("orchestrator", 2)):
            with self.subTest(persona=persona):
                *_x, report, _code = self.local_dry_run(real, persona)
                deferred = [v for v in report["operations"] if v["state"] == "deferred"]
                self.assertEqual(len(deferred), expected)
                for v in deferred:
                    self.assertEqual(v["kind"], "mailbox-writable-root-check")
                    self.assertEqual(v["detail"], "probe deferred to execute (would write)")
                    self.assertFalse(v["ok"])  # never shown as passed
                    self.assertNotEqual(v["state"], "refused")
                    self.assertNotIn(v["operation_id"], " ".join(report["problems"]))
                self.assertEqual(len(report["deferred"]), expected)

    def test_a_deferred_probe_alone_does_not_make_the_dry_run_fail(self) -> None:
        real = pia.REPO_ROOT / "instance.yaml"
        *_x, report, code = self.local_dry_run(real, "orchestrator")
        refused = [v for v in report["operations"] if v["state"] == "refused"]
        # orchestrator's stubbed-host refusals are unrelated (no real units here); the
        # point is that deferred ops contribute nothing to problems or the exit code
        self.assertEqual(len(report["problems"]), len(refused))
        self.assertEqual(code, 0 if not refused else 1)
        self.assertTrue(any(v["state"] == "deferred" for v in report["operations"]))

    def test_deferral_alone_exits_zero_and_still_offers_the_digest(self) -> None:
        class OnlyProbeOps(SimulatedHostBackend):
            pass

        code, report = pia.dry_run(self.manifest_path, "recovery", backend=OnlyProbeOps(), **self.kwargs())
        self.assertEqual(code, 0)
        plan = self.plan()
        probe_op = dataclasses.replace(
            next(o for o in plan.operations if o.kind == "harness-check"),
            operation_id="mailbox-writable-root.codex.9", kind="mailbox-writable-root-check")
        verdicts = pia.preflight_verdicts(
            dataclasses.replace(plan, operations=(probe_op,)), SimulatedHostBackend(),
            pia.actor_for_plan(plan, uid=UID), dry_run=True)
        self.assertEqual([v["state"] for v in verdicts], ["deferred"])

    def test_the_execute_time_preflight_still_runs_the_probe_unchanged(self) -> None:
        real = pia.REPO_ROOT / "instance.yaml"
        *_x, probes, report, _code = self.local_dry_run(real, "orchestrator", dry=False)
        self.assertTrue(probes)
        self.assertTrue(all(p == ".pantheon-rw-probe-" for p in probes), probes)
        self.assertNotIn("deferred", {v["state"] for v in report["operations"]})


if __name__ == "__main__":
    unittest.main()
