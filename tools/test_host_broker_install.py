"""T-2920 U1: the pure planning core of the ORG-052 host-broker installer.

Intent (T-2920): a clean box converges to the whole authority boundary from a
reviewed root-run plan, reproducibly. Pinned here:

* the closed ``pantheon-host-broker-install/v1`` manifest and the seat-registry
  input refuse every out-of-schema shape, inline operator-only values included;
* the renderer resolves every ``@VAR@`` in tools/host_control/dist/*, refuses an
  unknown, unused or missing variable, an unsafe value, and ANY leftover '@'
  outside a template's allowlisted literal contexts; per-seat and per-approver
  renders; the operator-only ingress allowlist stays deferred (Q-F) and the
  typed partial render parses with the ingress config parser;
* the render-staging seam: ``families.load_manifest(fragment, repo_root=staging)``
  accepts every family-routed entry with NO change to FAMILIES/PARAMS, and the
  sha pins bite;
* determinism: two fresh processes with different env/cwd/hash seed give
  byte-identical plan bytes and a valid plan digest;
* a structural golden of the ordered entries; dependency order; coverage of
  every dist/ file and every "T-2920 ... install" marker in the repo;
* the AST pin: LIVE_ACTUATION_ENABLED is the literal False, no subprocess, no
  apply verb; the dark CLI (exclusive --out, names --check).
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import host_broker_install as H
from tools.germinate.host_substrate import families as F

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_IDS = [
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/systemd/pantheon-persona-consume@.timer",
    "tools/systemd/pantheon-persona-quota@.service",
    "tools/systemd/pantheon-persona-quota@.timer",
    "tools/systemd/pantheon-mcp-allowlist-audit.service",
    "tools/systemd/pantheon-mcp-allowlist-audit.timer",
]


def registry_doc() -> dict:
    return {
        "schema": H.REGISTRY_SCHEMA,
        "seats": [
            {"seat_id": "recovery", "uid": 1002, "persona": "recovery", "role": "head-system-orchestrator",
             "instance": "pantheon", "account": "claude-fallback", "harness": "claude", "install_root": "/"},
            {"seat_id": "watchman", "uid": 1011, "persona": "watchman", "role": "system-orchestrator-1",
             "instance": "pantheon", "account": "codex-fallback", "harness": "codex", "install_root": "/"},
        ],
        "manifests": [{
            "revision": "m1", "path": "/etc/pantheon/host-broker/manifests/m1.yaml", "sha256": "a" * 64,
            "framework_revision": "runtime",
            "framework_sha256": H.framework_artifact_digest(REPO_ROOT, TEMPLATE_IDS),
            "template_ids": list(TEMPLATE_IDS),
        }],
    }


def manifest_doc() -> dict:
    return json.loads(H.EXAMPLE_MANIFEST.read_text(encoding="utf-8"))


def with_approver(doc: dict, seat_id: str = "recovery") -> dict:
    doc = copy.deepcopy(doc)
    doc["approvers"]["seat_ids"] = [seat_id]
    return doc


def raw(doc: dict) -> bytes:
    return json.dumps(doc).encode("utf-8")


_CLOSURE: H.Closure | None = None
_TRACKED: frozenset[str] | None = None


def tracked_bytes() -> bytes:
    return subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True,
                          timeout=120).stdout


def tracked() -> frozenset[str]:
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = H.parse_tracked_files(tracked_bytes())
    return _TRACKED


def closure() -> H.Closure:
    global _CLOSURE
    if _CLOSURE is None:
        _CLOSURE = H.derive_closure(REPO_ROOT, tracked=tracked(), template_ids=TEMPLATE_IDS)
    return _CLOSURE


def build(manifest: dict | None = None, registry: dict | None = None):
    m = H.parse_manifest(raw(manifest or with_approver(manifest_doc())))
    r = H.parse_registry(raw(registry or registry_doc()))
    return H.build_plan(m, r, REPO_ROOT, closure=closure())


_PLAN = None


def plan():
    global _PLAN
    if _PLAN is None:
        _PLAN = build()
    return _PLAN


class ManifestSchemaTest(unittest.TestCase):
    def test_example_manifest_parses(self) -> None:
        m = H.parse_manifest(H.EXAMPLE_MANIFEST.read_bytes())
        self.assertEqual(m.account_name("monitor"), "pantheon-hostmonitor")  # Q5
        self.assertEqual(m.home("broker"), "/var/lib/pantheon/home/pantheon-hostbroker")  # Q-B

    def refused(self, doc: dict, message: str) -> None:
        with self.assertRaisesRegex(H.InstallPlanError, message):
            H.parse_manifest(raw(doc))

    def test_unknown_and_missing_sections_are_refused(self) -> None:
        doc = manifest_doc()
        doc["extra"] = 1
        self.refused(doc, "unknown fields")
        doc = manifest_doc()
        del doc["refs"]
        self.refused(doc, "missing fields")
        doc = manifest_doc()
        doc["accounts"]["broker"]["shell"] = "/bin/bash"
        self.refused(doc, "unknown fields")

    def test_duplicate_json_keys_are_refused(self) -> None:
        text = H.EXAMPLE_MANIFEST.read_text(encoding="utf-8").replace(
            '"revision": "provisional-2026-10-27",', '"revision": "a", "revision": "b",')
        with self.assertRaisesRegex(H.InstallPlanError, "duplicate JSON key"):
            H.parse_manifest(text.encode())

    def test_operator_only_values_are_references_never_inline(self) -> None:
        for path, value in ((("refs", "telegram_human", "source_ref"), 123456789),
                            (("refs", "telegram_human", "source_ref"), "123456789"),
                            (("refs", "telegram_bot_token", "source_ref"), "000000:FAKE-TOKEN-NOT-A-REAL-BOT-TOKEN"),
                            (("approvers", "policy_ref"), "/etc/pantheon/host-approval/approver-policy.json")):
            doc = manifest_doc()
            node = doc
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            self.refused(doc, "reference id")
        doc = manifest_doc()
        doc["refs"]["telegram_human"]["user_id"] = 1
        self.refused(doc, "unknown fields")

    def test_account_shapes(self) -> None:
        for field, value, message in (("uid", 999, "1000"), ("uid", True, "1000"), ("name", "Bad Name", "account name")):
            doc = manifest_doc()
            doc["accounts"]["broker"][field] = value
            self.refused(doc, message)
        doc = manifest_doc()
        doc["accounts"]["monitor"]["uid"] = doc["accounts"]["broker"]["uid"]
        self.refused(doc, "duplicate")
        doc = manifest_doc()
        doc["account_defaults"]["locked"] = False
        self.refused(doc, "locked")

    def test_closed_decisions_are_refused_when_changed(self) -> None:
        cases = [
            (("groups", "entries"), ["pantheon-mailbox"], "user-private"),
            (("packages_verify",), ["python3"], "verify-only"),
            (("producer", "user"), "pantheon-hostbroker", "nobody"),
            (("framework", "keep_previous"), 2, "previous rev"),
            (("paths", "ingress_credentials", "mode"), "0750", "0700"),
            (("paths", "broker_etc", "mode"), "755", "octal"),
            (("paths", "broker_etc", "owner"), "nobody", "owner/group"),
            (("mailbox_hub",), "relative/hub", "absolute"),
            (("files", "broker_config", "placeholder"), "later", "U2 config schema"),
        ]
        for path, value, message in cases:
            doc = manifest_doc()
            node = doc
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path):
                self.refused(doc, message)

    def test_manifest_drift_from_staged_artifacts_is_refused_at_plan_time(self) -> None:
        for path, value, message in (
                (("executables", "broker"), "/usr/local/libexec/other-broker", "executables.broker"),
                (("accounts", "broker", "name"), "pantheon-broker", "accounts.broker"),
                (("files", "executor_config"), "/etc/pantheon/host-executor/other.json", "files.executor_config"),
                (("runtimes", "host", "launcher"), "/usr/local/libexec/other", "runtimes.host.launcher")):
            doc = with_approver(manifest_doc())
            node = doc
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaisesRegex(H.InstallPlanError, message):
                build(manifest=doc)


class RegistryInputTest(unittest.TestCase):
    def refused(self, doc: dict, message: str, manifest: dict | None = None) -> None:
        with self.assertRaisesRegex(H.InstallPlanError, message):
            build(manifest=manifest, registry=doc)

    def test_plans_must_be_empty_before_registration(self) -> None:
        doc = registry_doc()
        doc["plans"] = []
        H.parse_registry(raw(doc))
        doc["plans"] = [{"seat_id": "recovery"}]
        with self.assertRaisesRegex(H.InstallPlanError, "plans must be empty"):
            H.parse_registry(raw(doc))

    def test_seat_shapes(self) -> None:
        for field, value, message in (("seat_id", "recovery:1", "cannot name a unit"), ("uid", 0, ">= 1000"),
                                      ("harness", "gemini", "claude or codex"), ("install_root", "/a/../b", "normalized")):
            doc = registry_doc()
            doc["seats"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(H.InstallPlanError, message):
                H.parse_registry(raw(doc))
        doc = registry_doc()
        doc["seats"][1]["uid"] = doc["seats"][0]["uid"]
        with self.assertRaisesRegex(H.InstallPlanError, "duplicate seat"):
            H.parse_registry(raw(doc))

    def test_stale_framework_digest_is_refused(self) -> None:
        doc = registry_doc()
        doc["manifests"][0]["framework_sha256"] = "f" * 64
        self.refused(doc, "framework_sha256 is stale")

    def test_registered_manifest_must_sit_in_the_manifests_dir(self) -> None:
        doc = registry_doc()
        doc["manifests"][0]["path"] = "/etc/pantheon/m1.yaml"
        self.refused(doc, "broker_manifests")

    def test_seat_colliding_with_a_service_account_is_refused(self) -> None:
        doc = registry_doc()
        doc["seats"][0]["uid"] = manifest_doc()["accounts"]["broker"]["uid"]
        self.refused(doc, "collides with a service account")

    def test_approver_must_be_a_registered_seat(self) -> None:
        self.refused(registry_doc(), "not in the seat registry", manifest=with_approver(manifest_doc(), "nobody-seat"))


class RenderTest(unittest.TestCase):
    def tpl(self, name: str) -> str:
        return H.dist_bytes(REPO_ROOT, name).decode("utf-8")

    def test_every_dist_template_is_declared_and_renders(self) -> None:
        templates = {p.name for p in (REPO_ROOT / H.DIST_REL).iterdir() if _TOKEN.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(templates, set(H.TEMPLATES))
        values = {"SEAT_ACCOUNT": "claude-fallback", "SEAT_UID": 1002, "SEAT_ID": "recovery", "SEAT_ROOT": "/home/claude-fallback",
                  "MAILBOX_HUB": "/home/post-office/mailbox", "APPROVER_ACCOUNT": "claude-fallback", "INGRESS_UID": 1903,
                  "APPROVAL_UID": 1902, "INGRESS_GID": 1903, "ALLOWLISTED_TELEGRAM_USER_ID": 11, "ALLOWLISTED_TELEGRAM_CHAT_ID": 11}
        for name, spec in H.TEMPLATES.items():
            out = H.render_template(name, self.tpl(name), {k: values[k] for k in spec.vars})
            self.assertIsNone(_TOKEN.search(out), name)

    def test_unknown_unused_and_missing_variables_fail(self) -> None:
        name = "pantheon-host-broker-seat.conf.template"
        with self.assertRaisesRegex(H.RenderError, "unknown variables"):
            H.render_template(name, self.tpl(name) + "<!-- @SEAT_UID@ -->", {"SEAT_ACCOUNT": "a"})
        with self.assertRaisesRegex(H.RenderError, "declared but unused"):
            H.render_template(name, self.tpl(name).replace("@SEAT_ACCOUNT@", "x"), {"SEAT_ACCOUNT": "a"})
        with self.assertRaisesRegex(H.RenderError, "no value"):
            H.render_template(name, self.tpl(name), {})
        with self.assertRaisesRegex(H.RenderError, "not a known template"):
            H.render_template("other.template", "x", {})

    def test_a_leftover_at_sign_fails_outside_allowlisted_contexts(self) -> None:
        name = "pantheon-host-broker-seat.conf.template"
        with self.assertRaisesRegex(H.RenderError, "leftover"):
            H.render_template(name, self.tpl(name) + "<!-- mail@host -->", {"SEAT_ACCOUNT": "a"})
        unit = "pantheon-host-executor@.service"
        values = {"SEAT_ACCOUNT": "a", "SEAT_UID": 1002, "SEAT_ROOT": "/home/a", "MAILBOX_HUB": "/hub"}
        out = H.render_template(unit, self.tpl(unit), values)
        self.assertIn("After=dbus.service user@1002.service", out)
        self.assertIn("SystemCallFilter=@system-service", out)
        with self.assertRaisesRegex(H.RenderError, "leftover"):
            H.render_template(unit, self.tpl(unit) + "\nSystemCallFilter=@mount\n", values)

    def test_unsafe_values_are_refused(self) -> None:
        name = "pantheon-host-broker-seat.conf.template"
        for value in ("a\nb", "a b", 'a"><allow own="x"', "a%i", "a@b", True, 1.5, ""):
            with self.subTest(value=value), self.assertRaises(H.RenderError):
                H.render_template(name, self.tpl(name), {"SEAT_ACCOUNT": value})

    def test_operator_only_ingress_values_stay_deferred(self) -> None:
        self.assertEqual(H.DEFERRED_VARS, {"ALLOWLISTED_TELEGRAM_USER_ID", "ALLOWLISTED_TELEGRAM_CHAT_ID"})
        self.assertEqual([n for n, s in H.TEMPLATES.items() if s.deferred],
                         ["pantheon-host-approval-telegram-ingress.json.template"])
        name = "pantheon-host-approval-telegram-ingress.json.template"
        values = {"INGRESS_UID": 1903, "APPROVAL_UID": 1902, "INGRESS_GID": 1903}
        with self.assertRaisesRegex(H.RenderError, "operator-only"):
            H.render_template(name, self.tpl(name), values)
        partial = H.render_template(name, self.tpl(name), values, allow_deferred=True)
        self.assertEqual(sorted(_TOKEN.findall(partial)), sorted(H.DEFERRED_VARS))

    def test_typed_json_render_parses_with_the_ingress_config_parser(self) -> None:
        from tools.host_control.telegram_ingress.config import parse_ingress_config

        name = "pantheon-host-approval-telegram-ingress.json.template"
        out = H.render_template(name, self.tpl(name), {
            "INGRESS_UID": 1903, "APPROVAL_UID": 1902, "INGRESS_GID": 1903,
            "ALLOWLISTED_TELEGRAM_USER_ID": 424242, "ALLOWLISTED_TELEGRAM_CHAT_ID": 424242})
        config = parse_ingress_config(json.loads(out))
        self.assertEqual((config.ingress_uid, config.allow_user_id), (1903, 424242))

    def test_per_seat_and_per_approver_renders(self) -> None:
        doc, _ = plan()
        ids = {e["id"]: e for e in doc["entries"]}
        for seat_id, uid, account in (("recovery", 1002, "claude-fallback"), ("watchman", 1011, "codex-fallback")):
            unit = ids[f"unit:pantheon-host-executor@{seat_id}.service"]
            self.assertEqual(unit["path"], f"/etc/systemd/system/pantheon-host-executor@{seat_id}.service")
            self.assertIn(f"dbus_policy:org.pantheon.HostBroker1.seat-{seat_id}.conf", ids)
            self.assertIn(f"dbus_policy:org.pantheon.HostExecutor1.seat-{seat_id}.conf", ids)
            act = ids[f"activation:org.pantheon.HostExecutor1.U{uid}.service"]
            self.assertEqual(act["source"]["route"], "tool")
            self.assertTrue(act["path"].startswith("/usr/share/dbus-1/system-services/"))
        self.assertIn("dbus_policy:org.pantheon.HostApproval1.approver-recovery.conf", ids)
        self.assertNotIn("dbus_policy:org.pantheon.HostApproval1.approver-watchman.conf", ids)
        _, staging = plan()
        text = staging["units/pantheon-host-executor@recovery.service"].decode()
        self.assertIn("ReadWritePaths=/home/claude-fallback /home/post-office/mailbox", text)
        self.assertIn("BusName=org.pantheon.HostExecutor1.U1002", text)
        self.assertIn("User=claude-fallback", text)
        self.assertNotIn("[Install]", text)


_TOKEN = re.compile(r"@([A-Z][A-Z0-9_]*)@")


class StagingSeamTest(unittest.TestCase):
    def test_families_load_the_fragment_from_the_staging_root_unchanged(self) -> None:
        doc, staging = plan()
        m = H.parse_manifest(raw(with_approver(manifest_doc())))
        fragment = H.substrate_manifest_fragment(m, doc["entries"])
        self.assertEqual(doc["substrate_fragment_sha256"], H.sha256_bytes(H.canonical(fragment).encode("ascii")))
        with tempfile.TemporaryDirectory() as tmp:
            H.write_staging(staging, Path(tmp))
            loaded = F.load_manifest(fragment, repo_root=Path(tmp))
            family_entries = [e for e in doc["entries"] if e["source"]["route"] == "family"]
            self.assertTrue(family_entries)
            for entry in family_entries:
                src = entry["source"]
                self.assertIn(src["family"], F.FAMILIES)
                resolved = F.resolve(src["family"], src["params"], loaded)
                self.assertTrue(resolved.subject.endswith(entry["path"]) or resolved.subject.endswith(
                    entry["path"].rsplit("/", 1)[-1]) or resolved.subject.startswith("passwd:"), entry["id"])
            # the pins bite: one changed staged byte is refused
            victim = next(iter(staging))
            (Path(tmp) / victim).write_bytes(staging[victim] + b"\n")
            with self.assertRaisesRegex(F.ManifestError, "pinned sha256"):
                F.load_manifest(fragment, repo_root=Path(tmp))

    def test_every_family_path_passes_the_ancestor_walk_on_the_planned_host(self) -> None:
        """T-5657 (Recovery D15) audit: every family-routed path's ancestors, as THIS plan
        installs them (owner/group/mode of the plan's own earlier entries; / and /etc as a
        correctly installed host), are root-owned, non-g/o-writable directories, and each
        is planned BEFORE the family op that walks it. T-5658: the fragment (no ACLs)
        loads under the base-entry rule."""
        from tools.germinate.host_substrate import helper as Hp
        from tools.germinate.host_substrate import ports as P

        doc, _staging = plan()
        m = H.parse_manifest(raw(with_approver(manifest_doc())))
        loaded = F.load_manifest(H.substrate_manifest_fragment(m, doc["entries"]))
        entries = doc["entries"]
        first: dict[str, int] = {}
        port = P.SimulatedSubstratePort()
        for index, e in enumerate(entries):
            if e.get("path") and e.get("mode") and e["source"].get("route") != "family":
                first.setdefault(e["path"], index)
                port.state["path_stats"][e["path"]] = ("directory", e["owner"], e["group"], e["mode"])
        helper = Hp.RootHelper(port, loaded, Hp.BOOTSTRAP)
        walked = 0
        for index, e in enumerate(entries):
            src = e["source"]
            if src.get("route") != "family" or src["family"] not in ("owner_mode.set", "acl.converge"):
                continue
            step = helper.admit(src["family"], src["params"], Hp.ACTIVATE)
            self.assertIsNone(helper.ancestor_walk(step.resolved.subject), e["path"])
            for anc in Hp._ancestors(e["path"]):
                if anc not in ("/", "/etc"):
                    self.assertLess(first[anc], index, (anc, e["path"]))
                    walked += 1
        self.assertGreater(walked, 0)
        self.assertFalse(loaded.section("mailbox_dirs"))  # no dir: subject is planned today

    def test_every_family_file_is_staged_and_every_tool_entry_is_not(self) -> None:
        doc, staging = plan()
        staged = {e["source"]["staging"] for e in doc["entries"] if "staging" in e["source"]}
        self.assertEqual(staged, set(staging))
        for e in doc["entries"]:
            if e["source"]["route"] == "tool":
                self.assertNotIn("family", e["source"], e["id"])
        self.assertEqual({e["source"]["route"] for e in doc["entries"]}, {"family", "tool"})


class PlanShapeTest(unittest.TestCase):
    def test_digest_and_dark_flag(self) -> None:
        doc, _ = plan()
        self.assertTrue(H.verify_plan_digest(doc))
        self.assertIs(doc["live_actuation_enabled"], False)
        tampered = copy.deepcopy(doc)
        tampered["entries"][0]["mode"] = "0777"
        self.assertFalse(H.verify_plan_digest(tampered))

    def test_entry_fields_order_and_dependencies(self) -> None:
        doc, _ = plan()
        position = {e["id"]: e["seq"] for e in doc["entries"]}
        self.assertEqual([e["seq"] for e in doc["entries"]], list(range(len(doc["entries"]))))
        for e in doc["entries"]:
            self.assertLessEqual({"id", "phase", "kind", "path", "owner", "group", "mode", "content_sha256", "source",
                                  "gate", "after", "seq"}, set(e))
            self.assertIn("E35", e["gate"])
            for dep in e["after"]:
                self.assertLess(position[dep], e["seq"], e["id"])
            if e["content_sha256"] is None and e["kind"] in ("config", "registry", "launcher", "credential",
                                                             "plan_registration", "registry_post_registration"):
                self.assertIn("deferred", e, e["id"])

    def test_inventory_lines_none_lines_exclusions_and_residuals(self) -> None:
        doc, _ = plan()
        ids = {e["id"] for e in doc["entries"]}
        for wanted in ("dir:/etc/pantheon", "dir:/etc/pantheon/host-approval-telegram/credentials",
                       "credential-ref:telegram-approval-bot", "journal-schema:approval", "precondition:mailbox-hub",
                       "account:pantheon-hostmonitor", "package:python3-dbus", "package:python3-gi", "package:python3",
                       "package:python3-yaml"):
            self.assertIn(wanted, ids)
        self.assertEqual({n["id"] for n in doc["none"]}, {"polkit", "sockets", "tmpfiles", "logrotate"})
        self.assertIn("no-cgroup-limits", {r["id"] for r in doc["residuals"]})
        self.assertIn("active-switch-at-restart", {r["id"] for r in doc["residuals"]})
        self.assertEqual([x["id"] for x in doc["exclusions"]], ["excl:healthsig"])
        self.assertFalse(any("healthsig" in e["id"] or "health-signal" in (e["path"] or "") for e in doc["entries"]))
        creds = next(e for e in doc["entries"] if e["id"] == "dir:/etc/pantheon/host-approval-telegram/credentials")
        self.assertEqual((creds["mode"], creds["owner"]), ("0700", "pantheon-hostapproval-ingress"))

    def test_packages_are_verify_only_and_executor_activation_is_tool_owned(self) -> None:
        doc, _ = plan()
        for e in doc["entries"]:
            if e["kind"] == "package_verify":
                self.assertEqual(e["source"], {"route": "tool", "action": "dpkg-verify-and-record", "never": "install"})
            if e["kind"] == "activation":
                self.assertEqual(e["source"]["route"], "tool")
                self.assertNotIn(F.DBUS_DIR, e["path"])

    def test_configs_name_the_concrete_framework_rev_never_active(self) -> None:
        from tools.host_control.executor.config import parse_executor_config

        doc, _ = plan()
        root = doc["framework"]["root"]
        self.assertEqual(root, f"/usr/local/lib/pantheon/framework/{closure().rev}")
        _, staging = plan()
        entry = next(e for e in doc["entries"] if e["id"] == "config:/etc/pantheon/host-executor/config.json")
        m = H.parse_manifest(raw(with_approver(manifest_doc())))
        r = H.parse_registry(raw(registry_doc()))
        body = {"schema": H.EXECUTOR_CONFIG_SCHEMA, "registry_path": m.file("seat_registry"),
                "trusted_roots": [m.path("broker_etc"), root], "broker_uid": 1901, "framework_root": root,
                "mailbox_hub": "/home/post-office/mailbox"}
        self.assertEqual(entry["content_sha256"], H.sha256_bytes((H.canonical(body) + "\n").encode()))
        self.assertEqual(parse_executor_config(body).framework_root, Path(root))
        self.assertNotIn("/active", json.dumps([e.get("inputs") for e in doc["entries"] if e["kind"] in ("config", "launcher")]))
        for e in doc["entries"]:
            if e["kind"] == "plan_registration":
                argv = e["source"]["producer_argv"]
                self.assertEqual(argv[:8], [H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups",
                                            "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", "--"])
                self.assertIn("-I", argv)
                self.assertEqual(argv[argv.index("--framework-root") + 1], root)
        del r

    def test_no_operator_only_value_reaches_the_plan(self) -> None:
        doc, _ = plan()
        text = H.canonical(doc)
        self.assertIn("operator-batch:1", text)
        self.assertIn("operator-batch:2", text)
        ingress = next(e for e in doc["entries"] if e["id"] == "config:/etc/pantheon/host-approval-telegram/ingress.json")
        self.assertIsNone(ingress["content_sha256"])
        self.assertEqual(ingress["inputs"]["deferred_vars"], sorted(H.DEFERRED_VARS))

    def test_journal_baseline_and_allowlist_precondition(self) -> None:
        doc, _ = plan()
        ids = {e["id"]: e for e in doc["entries"]}
        self.assertEqual(ids["journal-schema:approval"]["inputs"]["schema_digest"], H.APPROVAL_JOURNAL_SCHEMA_DIGEST_V1)
        self.assertIn(ids["precondition:mcp-allowlist"]["inputs"]["observed"], ("met", "unmet"))

    def test_structural_golden(self) -> None:
        doc, _ = plan()
        rows = [(e["phase"], e["kind"], e["id"], e["source"]["route"]) for e in doc["entries"]
                if e["kind"] != "framework_file"]
        rows = [(p, k, i.replace(closure().rev, "<rev>"), r) for p, k, i, r in rows]
        self.assertEqual(rows, GOLDEN)
        files = [e for e in doc["entries"] if e["kind"] == "framework_file"]
        self.assertEqual(len(files), len(closure().files))


GOLDEN = [
    ("P0", "package_verify", "package:python3", "tool"),
    ("P0", "package_verify", "package:python3-dbus", "tool"),
    ("P0", "package_verify", "package:python3-gi", "tool"),
    ("P0", "package_verify", "package:python3-yaml", "tool"),
    ("P0", "precondition", "precondition:mailbox-hub", "tool"),
    ("P0", "precondition", "precondition:mcp-allowlist", "tool"),
    ("P0", "journal_schema_check", "journal-schema:approval", "tool"),
    ("P0", "journal_schema_check", "journal-schema:broker", "tool"),
    ("P2", "dir", "dir:/var/lib/pantheon", "tool"),
    ("P2", "dir", "dir:/var/lib/pantheon/home", "tool"),
    ("P2", "account", "account:pantheon-hostapproval", "family"),
    ("P2", "account", "account:pantheon-hostapproval-ingress", "family"),
    ("P2", "account", "account:pantheon-hostbroker", "family"),
    ("P2", "account", "account:pantheon-hostmonitor", "family"),
    ("P4", "dir", "dir:/usr/local/lib/pantheon", "tool"),
    ("P4", "dir", "dir:/usr/local/lib/pantheon/framework", "tool"),
    ("P4", "framework_tree", "framework:<rev>", "tool"),
    ("P4", "framework_active_link", "framework-active:/usr/local/lib/pantheon/framework/active", "tool"),
    ("P4", "runtime", "runtime:approval", "tool"),
    ("P4", "runtime", "runtime:host", "tool"),
    ("P5", "dir", "dir:/etc/pantheon", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-approval", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-approval-telegram", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-approval-telegram/credentials", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-broker", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-broker/manifests", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-broker/plans", "tool"),
    ("P5", "dir", "dir:/etc/pantheon/host-executor", "tool"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-approval", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-approval-telegram", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-approval-telegram/credentials", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-broker", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-broker/manifests", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-broker/plans", "family"),
    ("P5", "owner_mode", "owner-mode:/etc/pantheon/host-executor", "family"),
    ("P5", "launcher", "launcher:/usr/local/libexec/pantheon-host-approval", "tool"),
    ("P5", "launcher", "launcher:/usr/local/libexec/pantheon-host-approval-telegram-ingress", "tool"),
    ("P5", "launcher", "launcher:/usr/local/libexec/pantheon-host-broker", "tool"),
    ("P5", "launcher", "launcher:/usr/local/libexec/pantheon-host-executor", "tool"),
    ("P5", "config", "config:/etc/pantheon/host-approval-telegram/ingress.json", "tool"),
    ("P5", "config", "config:/etc/pantheon/host-approval/config.json", "tool"),
    ("P5", "config", "config:/etc/pantheon/host-broker/config.json", "tool"),
    ("P5", "config", "config:/etc/pantheon/host-executor/config.json", "tool"),
    ("P5", "registry", "registry:/etc/pantheon/host-approval/approver-policy.json", "tool"),
    ("P5", "registry", "registry:/etc/pantheon/host-broker/host-policy.json", "tool"),
    ("P5", "registry", "registry:/etc/pantheon/host-broker/seat-registry.json", "tool"),
    ("P5", "registered_manifest", "registered-manifest:m1", "tool"),
    ("P5", "credential_ref_registration", "credential-ref:telegram-approval-bot", "tool"),
    ("P6", "dir", "dir:/run/pantheon-host-install", "tool"),
    ("P6", "dir", "dir:/run/pantheon-host-install/staging", "tool"),
    ("P6", "plan_registration", "plan-registration:watchman:m1", "tool"),
    ("P6", "plan_registration", "plan-registration:recovery:m1", "tool"),
    ("P6", "registry_post_registration", "registry-post:/etc/pantheon/host-broker/seat-registry.json", "tool"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostApproval1.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostApprovalTelegram1.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostBroker1.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostExecutor1.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostApproval1.approver-recovery.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostBroker1.seat-watchman.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostBroker1.seat-recovery.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostExecutor1.seat-watchman.conf", "family"),
    ("P7", "dbus_policy", "dbus_policy:org.pantheon.HostExecutor1.seat-recovery.conf", "family"),
    ("P7", "unit", "unit:pantheon-host-approval-telegram-ingress.service", "family"),
    ("P7", "unit", "unit:pantheon-host-approval.service", "family"),
    ("P7", "unit", "unit:pantheon-host-broker.service", "family"),
    ("P7", "unit", "unit:pantheon-host-executor@watchman.service", "family"),
    ("P7", "unit", "unit:pantheon-host-executor@recovery.service", "family"),
    ("P7", "activation", "activation:org.pantheon.HostExecutor1.U1002.service", "tool"),
    ("P7", "activation", "activation:org.pantheon.HostExecutor1.U1011.service", "tool"),
    ("P8", "credential", "credential:/etc/pantheon/host-approval-telegram/credentials/bot-token", "tool"),
    ("P9", "unit_enable", "unit_enable:pantheon-host-approval.service", "family"),
    ("P9", "unit_enable", "unit_enable:pantheon-host-broker.service", "family"),
    ("P9", "unit_enable", "unit_enable:pantheon-host-approval-telegram-ingress.service", "family"),
    ("P9", "journal_root", "journal-root:/var/lib/pantheon-host-approval", "tool"),
    ("P9", "journal_root", "journal-root:/var/lib/pantheon-host-approval-telegram", "tool"),
    ("P9", "journal_root", "journal-root:/var/lib/pantheon-host-broker", "tool"),
]


class DeterminismTest(unittest.TestCase):
    def test_two_fresh_processes_give_byte_identical_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "manifest.json"
            registry = tmp_path / "registry.json"
            manifest.write_bytes(raw(with_approver(manifest_doc())))
            registry.write_bytes(raw(registry_doc()))
            tracked_path = tmp_path / "tracked"
            tracked_path.write_bytes(tracked_bytes())
            outputs = []
            for index, (seed, cwd) in enumerate((("1", tmp), ("4242", str(REPO_ROOT)))):
                env = {"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed, "LC_ALL": "C" if index else "C.UTF-8",
                       "TZ": "UTC" if index else "Pacific/Auckland"}
                result = subprocess.run(
                    [sys.executable, "-B", str(REPO_ROOT / "tools" / "host_broker_install.py"), "plan",
                     "--manifest", str(manifest), "--registry", str(registry), "--tracked-files", str(tracked_path)],
                    cwd=cwd, env=env, capture_output=True, timeout=300, check=False)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                outputs.append(result.stdout)
            self.assertEqual(outputs[0], outputs[1])
            doc = json.loads(outputs[0])
            self.assertTrue(H.verify_plan_digest(doc))
            self.assertEqual(outputs[0], H.plan_bytes(plan()[0]))


#: Every file in tools/host_control/dist/ -> plan entry id patterns or an exclusion id.
COVERAGE_DIST = {
    "org.pantheon.HostApproval1.conf": ["dbus_policy:org.pantheon.HostApproval1.conf"],
    "org.pantheon.HostApprovalTelegram1.conf": ["dbus_policy:org.pantheon.HostApprovalTelegram1.conf"],
    "org.pantheon.HostBroker1.conf": ["dbus_policy:org.pantheon.HostBroker1.conf"],
    "org.pantheon.HostExecutor1.conf": ["dbus_policy:org.pantheon.HostExecutor1.conf"],
    "org.pantheon.HostExecutor1.service.template": ["activation:org.pantheon.HostExecutor1.U*.service"],
    "pantheon-health-signal-writer.service": ["excl:healthsig"],
    "pantheon-host-approval-approver.conf.template": ["dbus_policy:org.pantheon.HostApproval1.approver-*.conf"],
    "pantheon-host-approval-telegram-ingress.json.template": ["config:/etc/pantheon/host-approval-telegram/ingress.json"],
    "pantheon-host-approval-telegram-ingress.service": ["unit:pantheon-host-approval-telegram-ingress.service",
                                                        "unit_enable:pantheon-host-approval-telegram-ingress.service"],
    "pantheon-host-approval.service": ["unit:pantheon-host-approval.service", "unit_enable:pantheon-host-approval.service"],
    "pantheon-host-broker-seat.conf.template": ["dbus_policy:org.pantheon.HostBroker1.seat-*.conf"],
    "pantheon-host-broker.service": ["unit:pantheon-host-broker.service", "unit_enable:pantheon-host-broker.service"],
    "pantheon-host-executor-seat.conf.template": ["dbus_policy:org.pantheon.HostExecutor1.seat-*.conf"],
    "pantheon-host-executor@.service": ["unit:pantheon-host-executor@*.service"],
}
#: Every non-dist file carrying a "T-2920 ... install" marker -> what covers it.
COVERAGE_MARKERS = {
    "tools/approval_mcp_adapter/__init__.py": ["runtime:approval", "registry:/etc/pantheon/host-approval/approver-policy.json"],
    "tools/harness_adapter/host_mcp_registration.py": ["registry:/etc/pantheon/host-broker/seat-registry.json",
                                                       "registry:/etc/pantheon/host-approval/approver-policy.json"],
    "tools/host_control/README.md": ["unit:*", "dbus_policy:*", "activation:*",
                                     "config:/etc/pantheon/host-executor/config.json",
                                     "launcher:/usr/local/libexec/pantheon-host-executor"],
    "tools/host_control/dbus_service.py": ["launcher:/usr/local/libexec/pantheon-host-broker",
                                           "config:/etc/pantheon/host-broker/config.json"],
    "tools/host_control/approval/dbus_service.py": ["launcher:/usr/local/libexec/pantheon-host-approval",
                                                    "config:/etc/pantheon/host-approval/config.json",
                                                    "registry:/etc/pantheon/host-approval/approver-policy.json"],
    "tools/host_control/executor/__main__.py": ["launcher:/usr/local/libexec/pantheon-host-executor",
                                                "config:/etc/pantheon/host-executor/config.json",
                                                "unit:pantheon-host-executor@*.service"],
    "tools/host_control/executor/config.py": ["config:/etc/pantheon/host-executor/config.json"],
    "tools/host_mcp_adapter/README.md": ["runtime:host", "runtime:approval"],
    "tools/host_mcp_adapter/dist/build_runtime.py": ["runtime:host", "runtime:approval"],
    "tools/host_mcp_adapter/dist/verify_artifact.py": ["runtime:host", "runtime:approval"],
    "tools/approval_mcp_adapter/README.md": ["runtime:approval", "registry:/etc/pantheon/host-approval/approver-policy.json"],
}
MARKER_RE = re.compile(r"T-2920(?:'s)? (?:to install|install leg|install receipt|owns|must)|owned by the T-2920")
MARKER_ROOTS = ("tools", "registry", "schemas", "harness")


def marker_files() -> set[str]:
    out = set()
    for root in MARKER_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if (not path.is_file() or "__pycache__" in rel or "/tests/" in rel or path.name.startswith("test_")
                    or path.name.startswith("host_broker_install")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if MARKER_RE.search(re.sub(r"\s+", " ", text)):
                out.add(rel)
    return out


class CoverageTest(unittest.TestCase):
    def covered(self, patterns: list[str]) -> None:
        doc, _ = plan()
        ids = [e["id"] for e in doc["entries"]] + [x["id"] for x in doc["exclusions"]]
        for pattern in patterns:
            self.assertTrue(fnmatch.filter(ids, pattern), f"{pattern!r} matches no plan entry or exclusion")

    def test_every_dist_file_maps_to_a_plan_entry_or_exclusion(self) -> None:
        dist = {p.name for p in (REPO_ROOT / H.DIST_REL).iterdir() if p.is_file()}
        self.assertEqual(dist, set(COVERAGE_DIST), "unmapped or stale dist/ coverage rows")
        for name, patterns in COVERAGE_DIST.items():
            with self.subTest(name=name):
                self.covered(patterns)

    def test_every_install_marker_in_the_repo_is_covered(self) -> None:
        found = marker_files()
        dist_markers = {f for f in found if f.startswith(H.DIST_REL + "/")}
        self.assertLessEqual({f.rsplit("/", 1)[1] for f in dist_markers}, set(COVERAGE_DIST))
        self.assertEqual(found - dist_markers, set(COVERAGE_MARKERS), "unmapped or stale marker coverage rows")
        for name, patterns in COVERAGE_MARKERS.items():
            with self.subTest(name=name):
                self.covered(patterns)


class ClosureStaticTest(unittest.TestCase):
    def test_required_members_and_external_imports(self) -> None:
        c = closure()
        for rel in (*H.FRAMEWORK_CODE_ARTIFACTS, "tools/runtime_invocation_receipt.py", "tools/host_plan_producer.py",
                    "registry/systems.yaml", "instance.yaml", *TEMPLATE_IDS):
            self.assertIn(rel, c.files)
        self.assertLessEqual(c.external_imports, set(H.EXTERNAL_PACKAGES))
        packages = set(json.loads(H.EXAMPLE_MANIFEST.read_text())["packages_verify"])
        self.assertLessEqual({H.EXTERNAL_PACKAGES[m] for m in c.external_imports}, packages)
        self.assertFalse([f for f in c.files if "/tests/" in f or f.rsplit("/", 1)[-1].startswith("test_")])
        self.assertEqual(c.rev, c.digest[:16])

    def test_closure_derivation_is_deterministic(self) -> None:
        self.assertEqual(H.derive_closure(REPO_ROOT, tracked=tracked(), template_ids=TEMPLATE_IDS), closure())

    def test_an_unresolvable_tools_import_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools" / "__init__.py").write_text("")
            (root / "tools" / "a.py").write_text("def f():\n    from tools.missing import x\n")
            with self.assertRaisesRegex(H.ClosureError, "tools.missing"):
                H.import_closure(root, ("tools.a",))


class DarkPinTest(unittest.TestCase):
    SOURCE = Path(H.__file__).read_text(encoding="utf-8")

    def test_live_actuation_is_the_literal_false(self) -> None:
        tree = ast.parse(self.SOURCE)
        assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                   and any(isinstance(t, ast.Name) and t.id == "LIVE_ACTUATION_ENABLED"
                           for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        self.assertEqual(len(assigns), 1)
        node = assigns[0]
        self.assertIsInstance(node, ast.AnnAssign)
        self.assertIn(node, tree.body)
        self.assertEqual(ast.unparse(node.annotation), "Final")
        self.assertIsInstance(node.value, ast.Constant)
        self.assertIs(node.value.value, False)
        self.assertIs(H.LIVE_ACTUATION_ENABLED, False)
        globals_writes = [n for n in ast.walk(tree) if isinstance(n, ast.Global)]
        self.assertFalse(globals_writes)

    def test_no_subprocess_no_network_no_apply_verb(self) -> None:
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"subprocess", "socket", "urllib", "http", "shutil", "pty", "multiprocessing"})
        self.assertNotIn("os.system", self.SOURCE)
        self.assertNotIn("os.exec", self.SOURCE)
        verbs = re.findall(r'add_parser\("([a-z-]+)"', self.SOURCE)
        self.assertEqual(sorted(verbs), ["names", "plan"])


class ReviewFixesTest(unittest.TestCase):
    """The T-2920 U1 review MEDs/LOWs (2026-10-27)."""

    def test_med1_the_producer_writes_only_into_a_dir_the_producer_user_owns(self) -> None:
        doc, _ = plan()
        by_id = {e["id"]: e for e in doc["entries"]}
        regs = [e for e in doc["entries"] if e["kind"] == "plan_registration"]
        self.assertTrue(regs)
        for e in regs:
            argv = e["source"]["producer_argv"]
            reuid = next(a.split("=", 1)[1] for a in argv if a.startswith("--reuid="))
            regid = next(a.split("=", 1)[1] for a in argv if a.startswith("--regid="))
            out = argv[argv.index("--out") + 1]
            self.assertEqual(e["inputs"]["producer_writes"], out)
            parent = by_id[f"dir:{out.rsplit('/', 1)[0]}"]
            # the only dir the producer writes into is owned by it, 0700, and created before
            self.assertEqual((parent["owner"], parent["group"], parent["mode"]), (reuid, regid, "0700"))
            self.assertLess(parent["seq"], e["seq"])
            self.assertTrue(parent["source"]["must_not_exist"])
            run_root = by_id[f"dir:{parent['path'].rsplit('/', 1)[0]}"]
            self.assertEqual((run_root["owner"], run_root["mode"]), ("root", "0755"))
            self.assertLess(run_root["seq"], parent["seq"])
            checks = " ".join(e["inputs"]["root_side_checks"])
            for needle in ("O_NOFOLLOW", "validate_digest", "sha256", "SAME bytes"):
                self.assertIn(needle, checks)
            self.assertEqual(e["inputs"]["expected_writer"], f"{reuid}:{regid}")
            self.assertNotEqual(e["path"].rsplit("/", 1)[0], parent["path"])  # installed elsewhere, by root
        # no other plan dir is producer-owned
        producer_dirs = [e["id"] for e in doc["entries"] if e["kind"] == "dir" and e["owner"] == "nobody"]
        self.assertEqual(producer_dirs, ["dir:/run/pantheon-host-install/staging"])

    def test_med1_schema_expresses_the_producer_owner_honestly(self) -> None:
        doc = manifest_doc()
        doc["paths"]["producer_staging"].update(owner="root", group="root", mode="0755")
        with self.assertRaisesRegex(H.InstallPlanError, "producer_staging"):
            H.parse_manifest(raw(doc))
        doc = manifest_doc()
        doc["paths"]["broker_plans"]["owner"] = "producer"
        with self.assertRaisesRegex(H.InstallPlanError, "only producer_staging"):
            H.parse_manifest(raw(doc))
        doc = manifest_doc()
        doc["paths"]["producer_staging"]["path"] = "/run/other/staging"
        with self.assertRaisesRegex(H.InstallPlanError, "producer_run_root"):
            H.parse_manifest(raw(doc))

    def fixture_tree(self, root: Path) -> frozenset[str]:
        """A tree holding exactly the closure plus the non-closure inputs
        build_plan reads (dist/, MCP build inputs)."""

        extra = [p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / H.DIST_REL).iterdir() if p.is_file()]
        extra += ["tools/host_mcp_adapter/dist/build_runtime.py", "tools/host_mcp_adapter/dist/requirements.lock"]
        for pkg in ("tools/host_mcp_adapter", "tools/approval_mcp_adapter"):
            extra += [p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / pkg).glob("*.py")]
        names = sorted(set(closure().files) | set(extra))
        for rel in names:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPO_ROOT / rel).read_bytes())
        return frozenset(names)

    def test_med2_untracked_strays_change_neither_rev_nor_plan_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = self.fixture_tree(root)
            base_closure = H.derive_closure(root, tracked=listing, template_ids=TEMPLATE_IDS)
            self.assertEqual(base_closure.digest, closure().digest)  # the tree re-derives the same rev
            m = H.parse_manifest(raw(with_approver(manifest_doc())))
            r = H.parse_registry(raw(registry_doc()))
            before = H.plan_bytes(H.build_plan(m, r, root, tracked=listing)[0])
            strays = ["pinned-rules/stray.md", "sop/leaked.env", "rulebook/id.key", "tools/systemd/notes.swp",
                      "architecture/adr/.ORG-999.md.swp"]
            for rel in strays:
                (root / rel).write_bytes(b"SECRET=not-for-the-copy\n")
            after_closure = H.derive_closure(root, tracked=listing, template_ids=TEMPLATE_IDS)
            self.assertEqual(after_closure, base_closure)
            self.assertFalse(set(strays) & set(after_closure.files))
            self.assertEqual(H.plan_bytes(H.build_plan(m, r, root, tracked=listing)[0]), before)
            with tempfile.TemporaryDirectory() as dest:
                H.materialize_closure(after_closure, root, Path(dest))
                self.assertFalse([s for s in strays if (Path(dest) / s).exists()])

    def test_med2_an_untracked_required_file_is_refused_by_name(self) -> None:
        for rel in ("tools/host_plan_producer.py", "registry/systems.yaml", TEMPLATE_IDS[0]):
            with self.subTest(rel=rel), self.assertRaisesRegex(H.ClosureError, "not tracked by git"):
                H.derive_closure(REPO_ROOT, tracked=tracked() - {rel}, template_ids=TEMPLATE_IDS)
        with self.assertRaisesRegex(H.InstallPlanError, "tracked-file list is required"):
            H.build_plan(H.parse_manifest(raw(with_approver(manifest_doc()))), H.parse_registry(raw(registry_doc())),
                         REPO_ROOT)
        for bad in (b"", b"/etc/passwd\0", b"a/../b\0"):
            with self.subTest(bad=bad), self.assertRaises(H.InstallPlanError):
                H.parse_tracked_files(bad)

    def test_low2_blocking_preconditions_name_every_unmet_precondition(self) -> None:
        doc, _ = plan()
        unmet = sorted(e["id"] for e in doc["entries"]
                       if e["kind"] == "precondition" and e.get("inputs", {}).get("observed") == "unmet")
        self.assertEqual(doc["blocking_preconditions"], unmet)
        allowlist = next(e for e in doc["entries"] if e["id"] == "precondition:mcp-allowlist")
        if allowlist["inputs"]["observed"] == "unmet":
            self.assertIn("precondition:mcp-allowlist", doc["blocking_preconditions"])

    def test_u5_mcp_allowlist_rows_landed_so_nothing_blocks(self) -> None:
        # T-2920 U5: _CLAUDE_MCP_ALLOWLIST carries both in-house adapter rows, so the
        # REAL plan's mcp-allowlist precondition is met and blocking_preconditions is
        # empty (key presence only; seat membership is checked live, CF-U5-1)
        doc, _ = plan()
        allowlist = next(e for e in doc["entries"] if e["id"] == "precondition:mcp-allowlist")
        self.assertEqual(allowlist["inputs"]["servers"], ["pantheon-approval", "pantheon-host"])
        self.assertEqual(allowlist["inputs"]["observed"], "met")
        self.assertNotIn("precondition:mcp-allowlist", doc["blocking_preconditions"])
        self.assertEqual(doc["blocking_preconditions"], [])

    def test_python3_yaml_is_a_verify_time_refusal_with_the_pinned_versions(self) -> None:
        from tools.germinate.registry import contract_digest as CD

        doc, _ = plan()
        entry = next(e for e in doc["entries"] if e["id"] == "package:python3-yaml")
        self.assertEqual(entry["inputs"]["accepted_versions"], sorted(CD.PINNED_PYYAML_VERSIONS))
        self.assertEqual(entry["inputs"]["probe"], [H.PYTHON3, "-I", "-c", "import yaml; print(yaml.__version__)"])
        self.assertIn("not in accepted_versions", entry["inputs"]["refuse_at_verify"])
        self.assertEqual(doc["verify_time_refusals"], ["package:python3-yaml"])
        self.assertNotIn("package:python3-yaml", doc["blocking_preconditions"])  # a host fact, not a plan-time one
        self.assertEqual(entry["source"], {"route": "tool", "action": "dpkg-verify-and-record", "never": "install"})
        # taken by import: a pin change moves the plan, never a copied list
        old = CD.PINNED_PYYAML_VERSIONS
        try:
            H.PINNED_PYYAML_VERSIONS = frozenset({"9.9.9"})
            patched, _ = build()
        finally:
            H.PINNED_PYYAML_VERSIONS = old
        patched_entry = next(e for e in patched["entries"] if e["id"] == "package:python3-yaml")
        self.assertEqual(patched_entry["inputs"]["accepted_versions"], ["9.9.9"])

    def test_low3_typed_json_render_must_parse(self) -> None:
        name = "pantheon-host-approval-telegram-ingress.json.template"
        text = H.dist_bytes(REPO_ROOT, name).decode("utf-8") + "}"
        values = {"INGRESS_UID": 1903, "APPROVAL_UID": 1902, "INGRESS_GID": 1903}
        with self.assertRaisesRegex(H.RenderError, "not valid JSON"):
            H.render_template(name, text, values, allow_deferred=True)

    def test_low4_status_and_none_reason_are_bounded(self) -> None:
        for field, value in (("status", "anything goes"), ("status", "424242")):
            doc = manifest_doc()
            doc[field] = value
            with self.subTest(value=value), self.assertRaisesRegex(H.InstallPlanError, "status"):
                H.parse_manifest(raw(doc))
        for value in ("123456789", "short", "user id 1234567890 is the human",
                      "token 000000:FAKEFAKEFAKEFAKEFAKE0 here ok", "x" * 301):
            doc = manifest_doc()
            doc["groups"]["none_reason"] = value
            with self.subTest(value=value), self.assertRaisesRegex(H.InstallPlanError, "none_reason"):
                H.parse_manifest(raw(doc))

    def test_low1_account_lock_provenance_is_recorded(self) -> None:
        doc, _ = plan()
        for e in doc["entries"]:
            if e["kind"] == "account":
                self.assertIn("useradd default", e["inputs"]["lock"])
                self.assertIn("'!' or '*'", e["inputs"]["lock"])

    def test_u2_reconcile_placeholders_name_schemas_paths_and_launchers(self) -> None:
        doc, _ = plan()
        by_id = {e["id"]: e for e in doc["entries"]}
        broker = by_id["config:/etc/pantheon/host-broker/config.json"]
        approval = by_id["config:/etc/pantheon/host-approval/config.json"]
        self.assertEqual(broker["inputs"]["schema"], "pantheon-host-broker-config/v1")
        self.assertEqual(approval["inputs"]["schema"], "pantheon-host-approval-config/v1")
        for e in (broker, approval):
            self.assertIn("render in U3 after U2 merges", e["deferred"])
            self.assertEqual(e["inputs"]["known_values"]["monitor_uids"], [1904])
            self.assertTrue(any("LOW-3" in n for n in e["inputs"]["notes"]))
            self.assertIsNone(e["content_sha256"])
        self.assertEqual(broker["inputs"]["known_values"]["framework_root"], doc["framework"]["root"])
        self.assertEqual(set(approval["inputs"]["refs"].values()), {"operator-batch:2"})
        launchers = {e["path"]: e["inputs"] for e in doc["entries"] if e["kind"] == "launcher"}
        self.assertEqual(launchers["/usr/local/libexec/pantheon-host-broker"]["exec"],
                         [H.PYTHON3, "-I", "-m", "tools.host_control"])
        self.assertEqual(launchers["/usr/local/libexec/pantheon-host-approval"]["exec"],
                         [H.PYTHON3, "-I", "-m", "tools.host_control.approval"])
        for inputs in launchers.values():
            self.assertEqual(inputs["cwd"], doc["framework"]["root"])


class CliTest(unittest.TestCase):
    def test_out_is_exclusive_and_0644(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "m.json"
            registry = tmp_path / "r.json"
            manifest.write_bytes(raw(with_approver(manifest_doc())))
            registry.write_bytes(raw(registry_doc()))
            listing = tmp_path / "tracked"
            listing.write_bytes(tracked_bytes())
            out = tmp_path / "plan.json"
            old = os.umask(0o077)
            try:
                self.assertEqual(H.main(["plan", "--manifest", str(manifest), "--registry", str(registry),
                                         "--tracked-files", str(listing), "--out", str(out)]), 0)
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o644)
            self.assertTrue(H.verify_plan_digest(json.loads(out.read_bytes())))
            self.assertEqual(H.main(["plan", "--manifest", str(manifest), "--registry", str(registry),
                                     "--tracked-files", str(listing), "--out", str(out)]), 2)

    def test_refusal_is_rc_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "m.json"
            manifest.write_text("{}")
            self.assertEqual(H.main(["plan", "--manifest", str(manifest), "--registry", str(manifest),
                                     "--tracked-files", str(manifest)]), 2)

    def test_name_table_is_current(self) -> None:
        self.assertEqual(H.main(["names", "--check"]), 0)
        doc = json.loads(H.NAMES_JSON.read_text())
        items = {r["provisional"] for r in doc["rows"]}
        for wanted in ("pantheon-hostbroker", "pantheon-hostapproval", "pantheon-hostapproval-ingress",
                       "pantheon-hostmonitor", "org.pantheon.HostExecutor1.U<uid>", "/var/lib/pantheon/home/pantheon-hostbroker",
                       "/usr/local/lib/pantheon/framework/<rev>", "nobody:nogroup via setpriv"):
            self.assertIn(wanted, items)
        uid_rows = [r for r in doc["rows"] if r["kind"] == "uid"]
        self.assertEqual(len(uid_rows), 4)
        for r in uid_rows:
            self.assertIn("uid >= 1000", r["rename_later_is_migration"])


if __name__ == "__main__":
    unittest.main()
