"""T-4835 / ORG-050 gate 3: credential describe report contract, classification and
the staged installer gate. Fixtures only: fake report bytes, tempdir "stores"; no real
credential or secret path is ever named, opened or hashed."""

from __future__ import annotations

import builtins
import contextlib
import copy
import dataclasses
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import persona_installer as _pim
from tools import persona_installer_apply as pia
from tools import persona_installer_credential_report as cr
from tools import test_persona_installer as _t
from tools.persona_installer import ApplyReceipt, SimulatedHostBackend

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
CHECKED = "2026-11-03T11:30:00Z"
CANARY = "CANARY-7f3a91c2"
UID = 4242


def _ts(delta_seconds: int) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(credential_id="fixture/one", **over):
    value = {
        "credential_id": credential_id,
        "available": True,
        "owner_matches": True,
        "mode_ok": True,
        "kind": "deploy-key",
        "errors": [],
    }
    value.update(over)
    return value


def report_doc(entries=None, checked_at=CHECKED):
    return {
        "schema": cr.CREDENTIAL_REPORT_SCHEMA,
        "checked_at": checked_at,
        "entries": [entry()] if entries is None else entries,
    }


def raw(doc) -> bytes:
    return json.dumps(doc).encode()


class ReportContractTests(unittest.TestCase):
    def test_valid_report_loads_with_stable_digest(self) -> None:
        a = cr.load_report(raw(report_doc()))
        reordered = dict(reversed(list(report_doc().items())))
        b = cr.load_report(json.dumps(reordered, indent=4).encode())
        self.assertEqual(a.sha256, b.sha256)
        self.assertEqual(len(a.sha256), 64)
        self.assertEqual(a.entry("fixture/one").kind, "deploy-key")

    def test_expires_at_is_optional_and_validated(self) -> None:
        cr.load_report(raw(report_doc([entry(expires_at="2027-01-01T00:00:00Z")])))
        with self.assertRaises(cr.ReportInvalid):
            cr.load_report(raw(report_doc([entry(expires_at="tomorrow")])))

    def test_malformed_shapes_are_invalid(self) -> None:
        bad_docs = {
            "unknown top key": {**report_doc(), "extra": 1},
            "missing top key": {k: v for k, v in report_doc().items() if k != "entries"},
            "bad schema": {**report_doc(), "schema": "other/v9"},
            "bad checked_at": report_doc(checked_at="2026-11-03 11:30"),
            "entries not list": {**report_doc(), "entries": {}},
            "unknown entry key": report_doc([entry(secret="x")]),
            "missing entry key": report_doc([{k: v for k, v in entry().items() if k != "kind"}]),
            "non-bool flag": report_doc([entry(available="yes")]),
            "bad id": report_doc([entry("../etc/shadow")]),
            "duplicate id": report_doc([entry(), entry()]),
            "errors not list": report_doc([entry(errors="x")]),
            "unsafe error text": report_doc([entry(errors=["bad <text>\nline"])]),
        }
        for label, doc in bad_docs.items():
            with self.subTest(label), self.assertRaises(cr.ReportInvalid):
                cr.load_report(raw(doc))
        for junk in (b"", b"not json", b"[]", b"\xff\xfe"):
            with self.subTest(junk=junk), self.assertRaises(cr.ReportInvalid):
                cr.load_report(junk)

    def test_deeply_nested_input_is_invalid_not_a_crash(self) -> None:
        for junk in (b"[" * 100000, b'{"a":' * 100000):
            with self.subTest(len=len(junk)), self.assertRaises(cr.ReportInvalid):
                cr.load_report(junk)

    def test_dot_dot_ids_are_refused(self) -> None:
        for bad in ("a/../b", "a/..", "a..b"):
            with self.subTest(bad=bad), self.assertRaises(cr.ReportInvalid):
                cr.load_report(raw(report_doc([entry(bad)])))
        cr.load_report(raw(report_doc([entry("a/b.c")])))

    def test_invalid_message_never_echoes_the_offending_value(self) -> None:
        with self.assertRaises(cr.ReportInvalid) as ctx:
            cr.load_report(raw(report_doc([entry(errors=[f"{CANARY}<x>"])])))
        self.assertNotIn(CANARY, str(ctx.exception))


class ClassificationTests(unittest.TestCase):
    def classify(self, doc, kind="deploy-key", ref="fixture/one", now=NOW):
        report = None if doc is None else cr.load_report(raw(doc))
        return cr.classify_ref(report, ref, kind, now)

    def test_each_status(self) -> None:
        self.assertEqual(self.classify(report_doc()), "resolved")
        self.assertEqual(self.classify(None), "undescribed")
        self.assertEqual(self.classify(report_doc([entry("other/ref")])), "undescribed")
        for flag in ("available", "owner_matches", "mode_ok"):
            with self.subTest(flag=flag):
                self.assertEqual(self.classify(report_doc([entry(**{flag: False})])), "unresolved")
        self.assertEqual(self.classify(report_doc([entry(errors=["probe failed"])])), "unresolved")
        self.assertEqual(self.classify(report_doc(), kind="api-token"), "mismatch")
        self.assertEqual(
            self.classify(report_doc([entry(expires_at="2026-11-03T11:00:00Z")])), "expired"
        )
        # expiry is judged against the apply-time clock (NOW), not the report's checked_at
        self.assertEqual(
            self.classify(report_doc([entry(expires_at="2026-11-03T11:45:00Z")])), "expired"
        )
        self.assertEqual(
            self.classify(report_doc([entry(expires_at=_ts(0))])), "resolved"
        )
        self.assertEqual(
            self.classify(report_doc([entry(expires_at=_ts(-1))])), "expired"
        )
        self.assertEqual(
            self.classify(report_doc([entry(expires_at=_ts(1))])), "resolved"
        )

    def test_stale_and_future_skew(self) -> None:
        age = cr.CREDENTIAL_REPORT_MAX_AGE_SECONDS
        self.assertEqual(self.classify(report_doc(checked_at=_ts(-age))), "resolved")
        self.assertEqual(self.classify(report_doc(checked_at=_ts(-age - 1))), "stale")
        self.assertEqual(self.classify(report_doc(checked_at=_ts(300))), "resolved")
        self.assertEqual(self.classify(report_doc(checked_at=_ts(301))), "stale")
        # stale outranks a missing entry: an old report is not evidence either way
        self.assertEqual(
            self.classify(report_doc([entry("other/ref")], checked_at=_ts(-age - 1))), "stale"
        )

    def test_missing_manifest_kind_classifies_as_mismatch(self) -> None:
        self.assertEqual(self.classify(report_doc(), kind=""), "mismatch")

    def test_precedence_unresolved_before_mismatch_before_expired(self) -> None:
        both = report_doc([entry(available=False, kind="api-token")])
        self.assertEqual(self.classify(both), "unresolved")
        mism = report_doc([entry(kind="api-token", expires_at="2020-01-01T00:00:00Z")])
        self.assertEqual(self.classify(mism), "mismatch")


class GateCase(unittest.TestCase):
    # Borrow only the fixture helpers: subclassing InstallerCase would re-run its
    # whole test suite under this class.
    write_manifest = _t.InstallerCase.write_manifest
    plan = _t.InstallerCase.plan
    actor = _t.InstallerCase.actor

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        data = _t._manifest_dict(second_persona=True)
        data["roles"]["recovery"]["credential_refs"] = [
            {
                "id": "fixture/one",
                "kind": "deploy-key",
                "provider": "host-file",
                "owner": "claude-fallback",
                "scope": ["repo:read"],
                "rotation": {"strategy": "replace", "revoke": "remove old grant"},
                "location": "/nonexistent/fixture/one",
                "direct_use_justification": "fixture",
            }
        ]
        self.write_manifest(data)
        self.manifest = _pim.Manifest.load(self.manifest_path)
        self.source_bytes: bytes | None = None
        self.installer = self.make_installer()

    def make_installer(self, source="unset"):
        if source == "unset":
            source = lambda: self.source_bytes  # noqa: E731
        return _t.ManifestPinnedInstaller(
            _t.REPO_ROOT, credential_report_source=source, clock=lambda: NOW
        )

    def use_report(self, doc) -> None:
        self.source_bytes = raw(doc)

    def run_apply(self, plan, *, all_capabilities=True):
        backend = SimulatedHostBackend()
        return backend, self.installer.apply(
            plan, plan.digest, backend, self.actor(plan, all_capabilities=all_capabilities)
        )


class PlanBindingTests(GateCase):
    def test_no_report_means_no_provenance_and_undescribed(self) -> None:
        plan = self.plan()
        self.assertNotIn(_pim.CREDENTIAL_REPORT_SHA256_KEY, plan.provenance)
        res = self.installer.credential_resolution(plan)
        self.assertIsNone(res.refusal)
        self.assertEqual([e["status"] for e in res.entries], ["undescribed"])
        self.assertEqual(res.entries[0]["report_sha256"], None)

    def test_default_installer_has_no_report_source(self) -> None:
        self.assertIsNone(_pim.PersonaInstaller(_t.REPO_ROOT)._credential_report_source)

    def test_report_digest_is_bound_into_the_plan(self) -> None:
        self.use_report(report_doc())
        plan = self.plan()
        self.assertEqual(
            plan.provenance[_pim.CREDENTIAL_REPORT_SHA256_KEY],
            cr.load_report(self.source_bytes).sha256,
        )
        self.assertTrue(plan.validate_digest())
        self.use_report(report_doc([entry(kind="deploy-key", expires_at="2030-01-01T00:00:00Z")]))
        self.assertNotEqual(self.plan().digest, plan.digest)

    def test_report_changed_after_planning_is_refused(self) -> None:
        self.use_report(report_doc())
        plan = self.plan()
        self.use_report(report_doc([entry(expires_at="2030-01-01T00:00:00Z")]))
        _backend, result = self.run_apply(plan)
        self.assertEqual(result.status, "refused")
        self.assertIn("evidence the plan was approved against", result.errors[0])

    def test_bound_plan_with_vanished_or_failing_source_is_refused_unbound(self) -> None:
        self.use_report(report_doc())
        plan = self.plan()

        def boom():
            raise OSError(f"{CANARY} gone")

        for label, source in (("none", lambda: None), ("raises", boom)):
            with self.subTest(label):
                self.installer = self.make_installer(source)
                self.installer._last_manifest = self.manifest  # the plan came from the other installer
                res = self.installer.credential_resolution(plan)
                self.assertIsNotNone(res.refusal)
                backend, result = self.run_apply(plan)
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.receipt.phase, "failed")
                self.assertEqual(result.receipt.mutations, ())
                status = result.receipt.credential_resolution[0]["status"]
                self.assertEqual(status, "unbound" if label == "none" else "malformed")
                self.assertNotIn(CANARY, json.dumps(result.errors))
                self.assertIn(result.receipt.receipt_id, backend.receipts)

    def test_report_without_plan_kinds_classifies_mismatch(self) -> None:
        self.use_report(report_doc())
        plan = self.plan()
        provenance = {
            k: v for k, v in plan.provenance.items() if k != _pim.CREDENTIAL_REF_KINDS_KEY
        }
        stripped = _pim.InstallPlan.create(
            **{
                f.name: getattr(plan, f.name)
                for f in dataclasses.fields(plan)
                if f.name not in {"digest", "provenance"}
            },
            provenance=provenance,
        )
        res = self.installer.credential_resolution(stripped)
        self.assertEqual(res.entries[0]["status"], "mismatch")
        self.assertIn("mismatch", res.refusal)

    def test_credential_expiring_between_check_and_apply_is_refused(self) -> None:
        self.use_report(report_doc([entry(expires_at="2026-11-03T11:45:00Z")]))
        plan = self.plan()
        _backend, result = self.run_apply(plan)
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.errors[0], "credential fixture/one: expired")

    def test_receipt_without_credential_refs_omits_the_key(self) -> None:
        data = _t._manifest_dict(second_persona=True)
        self.write_manifest(data)
        self.manifest = _pim.Manifest.load(self.manifest_path)
        plan = self.plan()
        self.assertFalse([a for a in plan.operator_actions if "credential" in a.action_id])
        _backend, result = self.run_apply(plan)
        self.assertIn(result.status, {"applied", "noop"}, result.errors)
        self.assertEqual(result.receipt.credential_resolution, ())
        self.assertNotIn("credential_resolution", result.receipt.to_dict())

    def test_report_appearing_after_an_unbound_plan_is_refused(self) -> None:
        plan = self.plan()
        self.use_report(report_doc())
        _backend, result = self.run_apply(plan)
        self.assertEqual(result.status, "refused")


class GateTests(GateCase):
    def test_resolved_applies_and_records_resolution(self) -> None:
        self.use_report(report_doc())
        plan = self.plan()
        backend, result = self.run_apply(plan)
        self.assertIn(result.status, {"applied", "noop"}, result.errors)
        (item,) = result.receipt.credential_resolution
        self.assertEqual(item["ref_id"], "fixture/one")
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["checked_at"], CHECKED)
        self.assertEqual(item["report_sha256"], plan.provenance[_pim.CREDENTIAL_REPORT_SHA256_KEY])
        stored = next(iter(backend.receipts.values()))
        self.assertEqual(stored["credential_resolution"][0]["status"], "resolved")

    def test_each_refusing_class_refuses_with_a_value_free_message(self) -> None:
        age = cr.CREDENTIAL_REPORT_MAX_AGE_SECONDS
        cases = {
            "unresolved": report_doc([entry(mode_ok=False, errors=[CANARY])]),
            "mismatch": report_doc([entry(kind="api-token")]),
            "expired": report_doc([entry(expires_at="2026-11-03T11:00:00Z")]),
            "stale": report_doc(checked_at=_ts(-age - 5)),
        }
        for status, doc in cases.items():
            with self.subTest(status=status):
                self.use_report(doc)
                plan = self.plan()
                backend, result = self.run_apply(plan)
                self.assertEqual(result.status, "refused")
                self.assertEqual(result.errors[0], f"credential fixture/one: {status}")
                self.assertNotIn(CANARY, json.dumps(result.errors))
                # refused path records a failed, no-mutation receipt
                self.assertEqual(result.receipt.phase, "failed")
                self.assertEqual(result.receipt.mutations, ())
                self.assertEqual(result.receipt.credential_resolution[0]["status"], status)
                stored = backend.receipts[result.receipt.receipt_id]
                self.assertEqual(stored["credential_resolution"][0]["status"], status)
                self.assertEqual(backend.entries, {})

    def test_malformed_report_refuses_and_is_not_undescribed(self) -> None:
        self.source_bytes = b"{not json"
        plan = self.plan()  # planning never refuses on a bad report
        backend, result = self.run_apply(plan)
        self.assertEqual(result.status, "refused")
        self.assertIn("credential report malformed", result.errors[0])
        self.assertEqual(result.receipt.credential_resolution[0]["status"], "malformed")

    def test_unreadable_source_refuses_without_echoing_the_exception(self) -> None:
        def boom():
            raise OSError(f"{CANARY} permission denied")

        self.installer = self.make_installer(boom)
        plan = self.plan()
        _backend, result = self.run_apply(plan)
        self.assertEqual(result.status, "refused")
        self.assertNotIn(CANARY, json.dumps(result.errors))

    def test_undescribed_matches_todays_capability_behaviour(self) -> None:
        for label, source in (("no source", None), ("no entry", "entry")):
            with self.subTest(label):
                if source is None:
                    self.installer = self.make_installer(None)
                else:
                    self.installer = self.make_installer()
                    self.use_report(report_doc([entry("some/other-ref")]))
                plan = self.plan()
                self.assertEqual(
                    [a.required_capability for a in plan.operator_actions if "credential" in a.action_id],
                    ["credential.check:fixture/one"],
                )
                held = self.actor(plan)
                lacking = dataclasses.replace(
                    held, capabilities=held.capabilities - {"credential.check:fixture/one"}
                )
                backend = SimulatedHostBackend()
                refused = self.installer.apply(plan, plan.digest, backend, lacking)
                self.assertEqual(refused.status, "refused")
                self.assertIn("credential.check:fixture/one", refused.errors[0])
                self.assertIsNone(refused.receipt)  # non-credential refusal: receipt stays None
                ok = self.installer.apply(plan, plan.digest, SimulatedHostBackend(), held)
                self.assertIn(ok.status, {"applied", "noop"}, ok.errors)
                self.assertEqual(ok.receipt.credential_resolution[0]["status"], "undescribed")

    def test_non_credential_refusal_still_returns_no_receipt(self) -> None:
        plan = self.plan()
        result = self.installer.apply(
            plan, "0" * 64, SimulatedHostBackend(), self.actor(plan)
        )
        self.assertEqual(result.status, "refused")
        self.assertIsNone(result.receipt)

    def test_old_receipt_without_the_field_loads(self) -> None:
        self.use_report(report_doc())
        _backend, result = self.run_apply(self.plan())
        old = dict(result.receipt.to_dict())
        old.pop("credential_resolution")
        loaded = ApplyReceipt.from_dict(old)
        self.assertEqual(loaded.credential_resolution, ())
        again = ApplyReceipt.from_dict(result.receipt.to_dict())
        self.assertEqual(again.credential_resolution, result.receipt.credential_resolution)

    def test_operator_action_reason_does_not_overclaim(self) -> None:
        action = next(a for a in self.plan().operator_actions if "credential" in a.action_id)
        self.assertIn("operator attestation", action.reason)
        self.assertIn("describe report", action.reason)
        self.assertNotIn("resolve provider metadata", action.reason)


class DryRunTests(GateCase):
    def dry_run(self):
        installer_source = self.make_installer().__dict__["_credential_report_source"]

        class Injected(_pim.PersonaInstaller):
            def __init__(inner, repo_root=_t.REPO_ROOT):
                super().__init__(
                    repo_root, credential_report_source=installer_source, clock=lambda: NOW
                )

        with patch.object(pia, "PersonaInstaller", Injected):
            return pia.dry_run(
                self.manifest_path,
                "recovery",
                backend=SimulatedHostBackend(),
                repo_root=_t.REPO_ROOT,
                root=self.root / "projection",
                uid_of=lambda account: UID,
                euid=UID,
            )

    def test_undescribed_only_dry_run_stays_ok(self) -> None:
        code, report = self.dry_run()
        self.assertEqual(code, 0, report["problems"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["credential_warnings"], [])
        self.assertEqual([e["status"] for e in report["credential_resolution"]], ["undescribed"])

    def test_resolved_dry_run_is_ok(self) -> None:
        self.use_report(report_doc())
        code, report = self.dry_run()
        self.assertEqual(code, 0, report["problems"])
        self.assertEqual(report["credential_resolution"][0]["status"], "resolved")

    def test_refusing_class_makes_dry_run_not_ok_and_lists_a_warning(self) -> None:
        self.use_report(report_doc([entry(owner_matches=False)]))
        code, report = self.dry_run()
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["credential_warnings"], ["credential fixture/one: unresolved"])
        self.assertIn("nothing was changed", report["next"])

    def test_plan_cli_surfaces_classification(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _pim.main(
                ["plan", "--manifest", str(self.manifest_path), "--persona", "recovery",
                 "--root", str(self.root / "projection")]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["credential_resolution"][0]["status"], "undescribed")


class RedactionAndNoOpenTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.root / "fake-store"
        self.store.mkdir()
        (self.store / "fixture-one").write_text(f"secret={CANARY}-STORE\n", encoding="utf-8")

    def outputs(self, plan, result, dry_stdout):
        parts = [
            json.dumps(plan.to_dict()),
            json.dumps(_pim._jsonable(result)),
            json.dumps(result.errors),
            dry_stdout,
        ]
        if result.receipt is not None:
            parts.append(json.dumps(result.receipt.to_dict()))
        return "\n".join(parts)

    def test_canary_never_surfaces(self) -> None:
        for label, doc in (
            ("accepted diagnostic", report_doc([entry(mode_ok=False, errors=[CANARY])])),
            ("rejected diagnostic", report_doc([entry(errors=[f"{CANARY}<x>"])])),
            ("good", report_doc()),
        ):
            with self.subTest(label):
                self.use_report(doc)
                err, out = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    plan = self.plan()
                    backend, result = self.run_apply(plan)
                    code = _pim.main(
                        ["plan", "--manifest", str(self.manifest_path), "--persona", "recovery",
                         "--root", str(self.root / "projection")]
                    )
                text = self.outputs(plan, result, out.getvalue() + err.getvalue())
                text += json.dumps(list(backend.receipts.values()))
                self.assertNotIn(CANARY, text)

    # The os-level calls that read or probe a path. On this Python pathlib's exists /
    # is_file / is_dir / stat / lstat all reduce to os.stat or os.lstat, and os.path.*
    # predicates call os.stat, so patching these on `os` covers them (asserted below).
    _PROBES = ("open", "stat", "lstat", "access", "scandir", "listdir", "readlink")

    def test_installer_never_touches_the_declared_credential_location(self) -> None:
        # The manifest ref's `location` points at a fake store under a tempdir. This
        # records the path argument of builtins.open, io.open and os.open/stat/lstat/
        # access/scandir/listdir/readlink (pathlib probes included) during plan, apply
        # and credential_resolution, and asserts none is the store or inside it.
        # Limit: it sees Python-level calls only, not subprocess children.
        store = self.root / "run" / "pantheon" / "fixture-one"
        store.mkdir(parents=True)
        token = store / "token"
        token.write_text(f"secret={CANARY}-STORE\n", encoding="utf-8")
        data = _t._manifest_dict(second_persona=True)
        data["roles"]["recovery"]["credential_refs"] = [
            {
                "id": "fixture/one",
                "kind": "deploy-key",
                "provider": "host-file",
                "owner": "claude-fallback",
                "scope": ["repo:read"],
                "rotation": {"strategy": "replace", "revoke": "remove old grant"},
                "location": str(token),
                "direct_use_justification": "fixture",
            }
        ]
        self.write_manifest(data)
        self.manifest = _pim.Manifest.load(self.manifest_path)
        seen: list[str] = []

        def record(path):
            if isinstance(path, int) or path is None:
                return
            seen.append(os.path.abspath(os.fsdecode(path)))

        def wrap(real):
            def probe(path=None, *a, **k):
                record(path)
                return real(path, *a, **k) if path is not None else real(*a, **k)

            return probe

        real_open = builtins.open
        patches = [
            patch.object(builtins, "open", wrap(real_open)),
            patch("io.open", wrap(real_open)),
        ] + [
            patch.object(os, name, wrap(getattr(os, name)))
            for name in self._PROBES
            if name != "open"
        ] + [patch.object(os, "open", wrap(os.open))]
        self.use_report(report_doc())
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            plan = self.plan()
            _backend, result = self.run_apply(plan)
            self.installer.credential_resolution(plan)
            # the recorder really sees pathlib probes (so a Path.exists on the store
            # would be caught too); this probe is outside the store
            calls = len(seen)
            (self.root / "recorder-self-check").exists()
            self.assertEqual(len(seen), calls + 1)
        self.assertIn(result.status, {"applied", "noop"}, result.errors)
        self.assertGreater(calls, 0, "recorder saw no calls: the test would be vacuous")
        root = str(store)
        inside = [p for p in seen if p == root or p.startswith(root + os.sep)]
        self.assertEqual(inside, [])

    def test_resolution_leakage_is_scanned(self) -> None:
        token = "sk-" + "A" * 24
        self.use_report(report_doc([entry(credential_id="fixture/one", kind="deploy-key")]))
        plan = self.plan()
        poisoned = dataclasses.replace(
            plan,
            operator_actions=tuple(
                dataclasses.replace(a, action_id=f"operator.credential.{token}", target=token)
                if "credential" in a.action_id
                else a
                for a in plan.operator_actions
            ),
        )
        res = self.installer.credential_resolution(poisoned)
        self.assertIsNotNone(res.refusal)


if __name__ == "__main__":
    unittest.main()
