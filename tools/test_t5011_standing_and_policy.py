"""T-5011 PR-D: harness_standing_instructions, the hermes inputs builder, the
Hermes tool/skill policy, and the PR-C render LOWs.

Byte safety: the uppercase context-file name never appears; built by
concatenation where a test needs it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path, PurePath

import yaml

from tools import persona_bringup
from tools import persona_installer as pi
from tools.harness_adapter import hermes, hermes_render
from tools.harness_adapter.hermes_render import HermesRenderError, HermesRenderTarget
from tools.harness_adapter.types import HarnessLaunchConfig
from tools.persona_orientation import render
from tools.test_persona_orientation_sop_skills import ENTRY, FixtureRepo

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "instance.yaml"
DEFAULTS = REPO / "harness" / "hermes" / "config.defaults.yaml"
ORCHESTRATOR_CODEX_SHA = "36f0d5eb9ff0bcd6396bd82e658fa68fd49d46b1d7415186943a357a865cc688"
H0_LEAVES = REPO / "tools" / "fixtures" / "harness_adapter" / "hermes" / "default_config_leaves.txt"
UPPER = "HER" + "MES" + ".md"


def _converted(manifest: pi.Manifest) -> pi.Manifest:
    """The live legacy orchestrator-codex entry, moved verbatim into the new schema."""
    installer = dict(manifest.installer)
    legacy = installer.pop("codex_standing_instructions")
    installer["harness_standing_instructions"] = {
        "schema": pi.HARNESS_STANDING_SCHEMA,
        "personas": {persona: {"codex": entry} for persona, entry in legacy["personas"].items()},
    }
    return dataclasses.replace(manifest, installer=installer)


def _standing_ops(plan) -> list:
    return [op for op in plan.operations if op.operation_id.startswith("codex-standing-instructions.")]


class StandingInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.manifest = pi.Manifest.load(MANIFEST)

    def test_live_orchestrator_codex_plan_is_identical_under_the_new_schema(self):
        legacy_plan = pi.PersonaInstaller().plan(self.manifest, "orchestrator", harness_override="codex")
        new_plan = pi.PersonaInstaller().plan(_converted(self.manifest), "orchestrator", harness_override="codex")
        self.assertEqual(len(_standing_ops(legacy_plan)), 4)
        self.assertEqual(_standing_ops(legacy_plan), _standing_ops(new_plan))
        self.assertEqual(legacy_plan.operations, new_plan.operations)

    def test_install_bytes_still_hash_to_the_pinned_sha(self):
        plan = pi.PersonaInstaller().plan(self.manifest, "orchestrator", harness_override="codex")
        install = next(op for op in plan.operations if op.operation_id == "codex-standing-instructions.install")
        content = install.desired["content"]
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), ORCHESTRATOR_CODEX_SHA)
        self.assertEqual(install.target, "/home/claude/.codex/AGENTS.md")

    def test_wrapper_reference_is_identical_under_both_schemas(self):
        self.assertEqual(
            pi.codex_standing_instructions_reference(self.manifest, "orchestrator"),
            pi.codex_standing_instructions_reference(_converted(self.manifest), "orchestrator"),
        )

    def test_bringup_reference_is_identical_under_both_schemas(self):
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        legacy = persona_bringup._standing_instruction_reference(
            data, role="r", persona="orchestrator", selected_harness="codex", account_home=Path("/home/claude")
        )
        converted = dict(data)
        converted["persona_installer"] = dict(_converted(self.manifest).installer)
        new = persona_bringup._standing_instruction_reference(
            converted, role="r", persona="orchestrator", selected_harness="codex", account_home=Path("/home/claude")
        )
        self.assertEqual(legacy["source_sha256"], ORCHESTRATOR_CODEX_SHA)
        self.assertEqual({k: v for k, v in legacy.items() if k != "schema"}, {k: v for k, v in new.items() if k != "schema"})

    def test_both_keys_for_the_same_pair_is_an_error(self):
        installer = dict(self.manifest.installer)
        legacy = installer["codex_standing_instructions"]
        installer["harness_standing_instructions"] = {
            "schema": pi.HARNESS_STANDING_SCHEMA,
            "personas": {"orchestrator": {"codex": legacy["personas"]["orchestrator"]}},
        }
        with self.assertRaises(pi.ManifestError):
            pi.standing_instruction_entries(installer, "orchestrator")

    def test_entry_or_list_and_extract_shape(self):
        installer = {
            "harness_standing_instructions": {
                "schema": pi.HARNESS_STANDING_SCHEMA,
                "personas": {"q": {"hermes": [{"source": "a"}, {"source": "a", "extract": {"heading": "## Identity"}}],
                                   "codex": {"source": "b"}}},
            }
        }
        entries = pi.standing_instruction_entries(installer, "q")
        self.assertEqual(len(entries["hermes"]), 2)
        self.assertEqual(entries["codex"], ({"source": "b"},))
        for bad in ({"heading": ""}, {"heading": "x", "other": 1}, "## Identity"):
            installer["harness_standing_instructions"]["personas"]["q"]["hermes"] = {"source": "a", "extract": bad}
            with self.subTest(bad=bad), self.assertRaises(pi.ManifestError):
                pi.standing_instruction_entries(installer, "q")

    def test_a_harness_nothing_installs_is_refused(self):
        for harness in ("claude", "claude-code"):
            installer = {"harness_standing_instructions": {
                "schema": pi.HARNESS_STANDING_SCHEMA, "personas": {"q": {harness: {"source": "b"}}}}}
            with self.subTest(harness=harness), self.assertRaises(pi.ManifestError) as info:
                pi.standing_instruction_entries(installer, "q")
            self.assertIn("only codex, hermes", str(info.exception))

    def test_codex_entry_with_extract_is_refused(self):
        installer = dict(self.manifest.installer)
        legacy = dict(installer.pop("codex_standing_instructions")["personas"]["orchestrator"])
        legacy["extract"] = {"heading": "## Identity"}
        installer["harness_standing_instructions"] = {
            "schema": pi.HARNESS_STANDING_SCHEMA, "personas": {"orchestrator": {"codex": legacy}},
        }
        with self.assertRaises(pi.ManifestError):
            pi.codex_standing_instructions_reference(dataclasses.replace(self.manifest, installer=installer), "orchestrator")

    def test_live_manifest_is_not_migrated(self):
        self.assertNotIn("harness_standing_instructions", self.manifest.installer)


def _hermes_installer(**overrides) -> dict:
    main = {"source": "pinned-rules/qianliyan.md", "source_sha256": "a" * 64, "source_commit": "c" * 40,
            "target": "seat/.hermes.md"}
    soul = dict(main, target=".hermes/SOUL.md", extract={"heading": "## Who I am"})
    main.update(overrides.pop("main", {}))
    soul.update(overrides.pop("soul", {}))
    return {"harness_standing_instructions": {"schema": pi.HARNESS_STANDING_SCHEMA,
                                              "personas": {"qianliyan": {"hermes": [main, soul]}}}}


class HermesInputsBuilderTests(unittest.TestCase):
    def _build(self, installer):
        return pi.hermes_render_inputs(
            installer, "qianliyan", account_home=Path("/home/df"), repo_root=Path("/repo"), defaults_path=DEFAULTS
        )

    def test_builds_inputs_from_a_synthetic_manifest(self):
        inputs = self._build(_hermes_installer())
        self.assertIsInstance(inputs, hermes_render.HermesRenderInputs)
        self.assertEqual(inputs.source_path, Path("/repo/pinned-rules/qianliyan.md"))
        self.assertEqual(inputs.source_sha256, "a" * 64)
        self.assertEqual(inputs.identity_heading, "## Who I am")
        self.assertEqual(inputs.account_home, Path("/home/df"))

    def test_targets_match_the_render_target(self):
        target = HermesRenderTarget()
        home = Path("/home/df")
        self.assertEqual(home / pi.HERMES_PROJECT_CONTEXT_TARGET, target.install_path("project-context", account_home=home))
        self.assertEqual(home / pi.HERMES_IDENTITY_TARGET, target.install_path("identity", account_home=home))
        self.assertNotIn(UPPER, pi.HERMES_PROJECT_CONTEXT_TARGET)

    def test_absent_is_none(self):
        self.assertIsNone(self._build({}))

    def test_refusals(self):
        for overrides in ({"main": {"target": "seat/AGENTS.md"}}, {"soul": {"target": ".hermes/x.md"}},
                          {"soul": {"source_sha256": "b" * 64}}, {"main": {"source_sha256": "short"}},
                          {"main": {"source_sha256": "A" * 64}}, {"main": {"source_sha256": "g" * 64}},
                          {"soul": {"source_commit": "d" * 40}}):
            with self.subTest(overrides=overrides), self.assertRaises(pi.ManifestError):
                self._build(_hermes_installer(**overrides))


def _launch(**native):
    base = {"provider": "deepseek", "busy_input_mode": "queue"}
    base.update(native)
    return HarnessLaunchConfig(harness="hermes", profile="deepseek-direct", executable=Path("/opt/hermes"),
                               model="deepseek-v4-pro", mode="interactive", meter="api",
                               quota_source="provider-api", native=base, sources=("test",))


class HermesPolicyTests(unittest.TestCase):
    def setUp(self):
        self.defaults = hermes_render.load_defaults(DEFAULTS)
        self.overlay = self.defaults["overlay"]

    def test_cli_toolsets_are_the_narrow_lock(self):
        self.assertEqual(self.overlay["platform_toolsets"], {"cli": ["terminal", "file", "skills", "todo"]})
        self.assertNotIn("toolsets", self.overlay)
        for dangerous in ("web", "browser", "delegation", "code_execution", "memory", "connections", "kanban"):
            self.assertIn(dangerous, self.overlay["agent"]["disabled_toolsets"])
        self.assertEqual(self.overlay["agent"]["coding_context"], "auto")
        self.assertIn("context_engine", self.overlay["agent"]["disabled_toolsets"])
        self.assertEqual(self.overlay["context"]["engine"], "compressor")
        self.assertEqual(self.overlay["plugins"]["enabled"], [])
        self.assertIs(self.overlay["skills"]["write_approval"], True)

    def test_rendered_config_carries_the_lock_in_both_placements(self):
        for placement in ("merged", "overlay"):
            config = yaml.safe_load(HermesRenderTarget(placement=placement).render_runtime_config(
                _launch(), self.defaults, account_home=Path("/h")))
            with self.subTest(placement=placement):
                self.assertEqual(config["platform_toolsets"]["cli"], ["terminal", "file", "skills", "todo"])
                self.assertNotIn("toolsets", config)

    def test_render_fails_closed_without_the_exact_cli_list(self):
        for config in ({}, {"platform_toolsets": {}}, {"platform_toolsets": {"cli": ["terminal"]}},
                       {"platform_toolsets": {"cli": ["terminal", "file", "skills", "todo"]}, "toolsets": ["x"]}):
            with self.subTest(config=config), self.assertRaises(HermesRenderError) as info:
                hermes_render._check_cli_toolsets(config, self.overlay)
            self.assertEqual(info.exception.reason, "cli-toolsets")

    def test_defaults_without_a_cli_lock_or_with_bad_deny_are_refused(self):
        raw = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
        cases = []
        no_lock = yaml.safe_load(yaml.safe_dump(raw)); del no_lock["overlay"]["platform_toolsets"]; cases.append(no_lock)
        legacy = yaml.safe_load(yaml.safe_dump(raw)); legacy["overlay"]["toolsets"] = ["hermes-cli"]; cases.append(legacy)
        for deny in ("rm -rf *", [""], [1]):
            bad = yaml.safe_load(yaml.safe_dump(raw)); bad["overlay"]["approvals"]["deny"] = deny; cases.append(bad)
        for data in cases:
            with self.subTest(case=data["overlay"].get("toolsets") or data["overlay"]["approvals"]["deny"]), \
                    self.assertRaises(HermesRenderError):
                hermes_render.parse_defaults(yaml.safe_dump(data).encode("utf-8"))

    def test_approvals_deny_shape(self):
        deny = self.overlay["approvals"]["deny"]
        self.assertIsInstance(deny, list)
        self.assertTrue(all(isinstance(g, str) and g.strip() for g in deny))
        ok = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
        ok["overlay"]["approvals"]["deny"] = ["rm -rf /*", "*curl*"]
        hermes_render.parse_defaults(yaml.safe_dump(ok).encode("utf-8"))

    def test_toolsets_is_no_longer_a_native_key(self):
        self.assertNotIn("toolsets", hermes.HERMES_NATIVE_KEYS)
        adapter = hermes.HermesHarnessAdapter("system-orchestrator", account_home=Path("/h"),
                                              launch=_launch(toolsets=["hermes-cli"]))
        (obs,) = adapter.validate_launch_config()
        self.assertEqual(obs.status, "fail")
        self.assertIn("unknown-native-keys", obs.summary)

    def test_every_new_locked_path_is_a_captured_leaf(self):
        leaves = {line.split(" = ", 1)[0] for line in H0_LEAVES.read_text(encoding="utf-8").splitlines() if " = " in line}
        for path in ("agent.disabled_toolsets", "agent.coding_context", "skills.write_approval", "approvals.deny",
                     "context.engine"):
            self.assertIn(path, leaves)
        self.assertFalse(any(leaf.startswith("platform_toolsets") for leaf in leaves))  # justified inline
        self.assertNotIn("plugins.enabled", leaves)  # justified inline (read, not defaulted)

    def test_error_branch_is_kept_and_cited(self):
        source = Path(hermes.__file__).read_text(encoding="utf-8")
        self.assertIn('elif native_type == "error":', source)
        self.assertIn("hermes_cli/stream_json.py", source)


class HermesSeatFilesTests(unittest.TestCase):
    def _inputs(self, tmp: Path):
        source = tmp / "pin" / "q.md"
        source.parent.mkdir(parents=True)
        text = "# Q\n\n## Identity\n\nYou are a watcher.\n"
        source.write_text(text, encoding="utf-8")
        home = tmp / "home"
        (home / "seat").mkdir(parents=True)
        return hermes_render.HermesRenderInputs(
            persona="q", account_home=home, source_path=source,
            source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), source_commit="0" * 40,
            defaults_path=DEFAULTS,
        )

    def test_no_bundled_skills_marker_and_managed_are_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._inputs(Path(tmp))
            hermes_render.prepare_hermes_launch(inputs, _launch(), repo_probe=lambda p: False)
            hh = inputs.account_home / ".hermes"
            self.assertEqual((hh / ".no-bundled-skills").read_bytes(), b"")
            self.assertTrue((hh / ".managed").exists())

    def test_env_file_is_tightened_to_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._inputs(Path(tmp))
            env = inputs.account_home / ".hermes" / ".env"
            env.parent.mkdir(parents=True)
            env.write_text("DEEPSEEK_API_KEY=x\n", encoding="utf-8")
            env.chmod(0o644)
            result = hermes_render.prepare_hermes_launch(inputs, _launch(), repo_probe=lambda p: False)
            self.assertEqual(result.env_file_mode, "tightened")
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
            again = hermes_render.prepare_hermes_launch(inputs, _launch(), repo_probe=lambda p: False)
            self.assertEqual(again.env_file_mode, "0600")

    def test_absent_env_and_chmod_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            self.assertEqual(hermes_render.tighten_env_file(path), "absent")
            path.write_text("x\n")
            path.chmod(0o640)

            def refuse(fd, mode):
                raise PermissionError("no")

            with self.assertRaises(HermesRenderError) as info:
                hermes_render.tighten_env_file(path, fchmod=refuse)
            self.assertEqual(info.exception.reason, "env-file-mode")

    def test_symlink_to_a_seat_owned_file_is_refused_and_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "elsewhere"
            real.write_text("DEEPSEEK_API_KEY=x\n")
            real.chmod(0o644)
            link = Path(tmp) / ".env"
            link.symlink_to(real)
            with self.assertRaises(HermesRenderError) as info:
                hermes_render.tighten_env_file(link)
            self.assertEqual(info.exception.reason, "env-file-symlink")
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o644)

    def test_symlink_to_a_foreign_file_is_refused_without_a_chmod_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / ".env"
            link.symlink_to("/etc/passwd")
            calls = []
            with self.assertRaises(HermesRenderError) as info:
                hermes_render.tighten_env_file(link, fchmod=lambda fd, mode: calls.append(mode))
            self.assertEqual(info.exception.reason, "env-file-symlink")
            self.assertEqual(calls, [])

    def test_foreign_owner_regular_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("x\n")
            path.chmod(0o640)
            calls = []
            with unittest.mock.patch.object(hermes_render.os, "geteuid", return_value=os.geteuid() + 1):
                result = hermes_render.tighten_env_file(path, fchmod=lambda fd, mode: calls.append(mode))
            self.assertEqual(result, "foreign-owner")
            self.assertEqual(calls, [])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_non_regular_env_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            os.mkfifo(path)
            with self.assertRaises(HermesRenderError) as info:
                hermes_render.tighten_env_file(path)
            self.assertEqual(info.exception.reason, "env-file-not-regular")


class RenderLowTests(FixtureRepo):
    def test_all_write_never_creates_hermes_state(self):
        hermes_root = self.tmp / "hermes-skills"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = render.run_sop_skills(self.root, self.home, "write", render.expand_harness("all"), self.codex, hermes_root)
        self.assertEqual(rc, 0)
        self.assertFalse((self.home / ".local" / "state" / "pantheon" / "hermes-render").exists())
        self.assertFalse(hermes_root.exists())
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())

    def test_harnesses_on_a_pending_entry_is_refused(self):
        self.write_manifest([dict(ENTRY)], [{"slug": "later", "awaiting": "T-1", "harnesses": ["hermes"]}])
        with self.assertRaises(ValueError):
            render.load_sop_skills_manifest(self.root)

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_hermes_write_without_a_writable_root_stops_before_writing(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = render.run_sop_skills(self.root, self.home, "write", ("hermes",), self.codex, locked / "pin")
        self.assertEqual(rc, 1)
        self.assertIn("--hermes-skills-root", err.getvalue())
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
