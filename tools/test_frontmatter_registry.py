"""Tests for the ORG-021 frontmatter field registry (T-4751 S1).

Run from repo root:
    python3 -m pytest tools/test_frontmatter_registry.py -q
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _git_utils, frontmatter_registry as fr, ticket
from tools import ticket_purpose

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
DRIFT_HINT = (
    "add a registry entry (or value) to ORG-021 §Frontmatter field registry "
    "(architecture/adr/ORG-021-ticket-state-taxonomy-compress-direction.md) "
    "in the SAME PR as the code that writes it"
)


def _real_adr_text() -> str:
    # The REAL ADR, ignoring any PATH_ENV override a test already installed.
    matches = sorted(fr.ADR_DIR.glob(fr.ADR_GLOB))
    assert matches
    return matches[0].read_text(encoding="utf-8")


def _write_ticket(path: Path, tid: str, state: str, extra: list[str] | None = None) -> Path:
    lines = [
        "---",
        f"id: {tid}",
        "created: 2026-10-01T00:00:00Z",
        "requester: orchestrator",
        "target: recovery",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
        *(extra or []),
        "---",
        "",
        "# Example",
        "",
        "## Transition log",
        "",
        "- 2026-10-01T00:00:00Z [orchestrator] filed in inbox",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class _RegistryEnv(unittest.TestCase):
    """Isolate mode/path env + per-process caches for each test."""

    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(fr.MODE_ENV, None)
        os.environ.pop(fr.PATH_ENV, None)
        fr.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        fr.clear_cache()
        self._env.stop()
        self._tmp.cleanup()

    def use_registry_text(self, text: str) -> Path:
        p = self.tmp / "ORG-021-test.md"
        p.write_text(text, encoding="utf-8")
        os.environ[fr.PATH_ENV] = str(p)
        fr.clear_cache()
        return p

    def use_modified_real_registry(self, old: str, new: str) -> Path:
        text = _real_adr_text()
        self.assertIn(old, text)
        return self.use_registry_text(text.replace(old, new, 1))


class TestParseRealRegistry(_RegistryEnv):
    def test_real_org021_parses_with_57_fields(self) -> None:
        reg = fr.load_registry()
        self.assertIsNotNone(reg, "real ORG-021 registry must parse")
        self.assertEqual(len(reg.fields), 57)
        self.assertEqual(reg.get("state").values, frozenset({"inbox", "active", "done"}))
        self.assertEqual(reg.get("blocked_by").status, "legacy")

    def test_cached_per_process(self) -> None:
        self.assertIs(fr.load_registry(), fr.load_registry())


class TestValidateWrite(_RegistryEnv):
    def test_unknown_field_flagged(self) -> None:
        f = fr.validate_write(None, {"id": "T-1", "bogus_field": "x"})
        self.assertEqual([(x.kind, x.field) for x in f], [("unknown-field", "bogus_field")])

    def test_enum_check_substate(self) -> None:
        old = {"id": "T-1"}
        bad = fr.validate_write(old, {"id": "T-1", "substate": "bogus"})
        self.assertEqual([(x.kind, x.field, x.value) for x in bad],
                         [("bad-value", "substate", "bogus")])
        self.assertEqual(fr.validate_write(old, {"id": "T-1", "substate": "awaiting-merge"}), [])

    def test_list_fields_have_no_value_check(self) -> None:
        self.assertEqual(fr.validate_write(None, {"labels": ["anything-goes"]}), [])

    def test_int_and_bool_enums_compare_on_disk_strings(self) -> None:
        self.assertEqual(fr.validate_write(None, {"appetite": "5"}), [])
        self.assertEqual(fr.validate_write(None, {"appetite": 5}), [])
        self.assertEqual([x.kind for x in fr.validate_write(None, {"appetite": "11"})],
                         ["bad-value"])
        self.assertEqual(fr.validate_write(None, {"auto_delivered": "true"}), [])
        # values: [true] — a false write is a removal, not a value.
        self.assertEqual(fr.validate_write(None, {"auto_delivered": "false"}), [])

    def test_removal_always_allowed(self) -> None:
        old = {"id": "T-1", "blocked_by": "T-2", "weird_key": "x", "substate": "bogus"}
        self.assertEqual(fr.validate_write(old, {"id": "T-1"}), [])

    def test_unchanged_legacy_roundtrips_silently(self) -> None:
        old = {"id": "T-1", "blocked_by": "T-2", "weird_key": "x", "substate": "bogus"}
        self.assertEqual(fr.validate_write(old, dict(old, labels=["a"])), [])

    def test_new_legacy_write_flagged(self) -> None:
        f = fr.validate_write({"id": "T-1"}, {"id": "T-1", "blocked_by": "T-2"})
        self.assertEqual([(x.kind, x.field) for x in f], [("legacy-write", "blocked_by")])
        # Changing an existing legacy value is also a new write.
        f = fr.validate_write({"blocked_by": "T-2"}, {"blocked_by": "T-3"})
        self.assertEqual([x.kind for x in f], ["legacy-write"])


class TestModes(_RegistryEnv):
    def test_warn_mode_prints_and_returns(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            findings = fr.enforce_write(None, {"id": "T-1", "substate": "bogus", "zz": "1"})
        self.assertEqual(len(findings), 2)
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertTrue(ln.startswith("[frontmatter-registry] WARN"), ln)

    def test_default_mode_is_warn(self) -> None:
        self.assertEqual(fr.get_mode(), "warn")

    def test_refuse_mode_raises(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        with self.assertRaises(fr.RegistryRefusal) as ctx:
            fr.enforce_write(None, {"id": "T-1", "substate": "bogus"})
        self.assertIn("substate", str(ctx.exception))
        self.assertIn("ORG-021", str(ctx.exception))

    def test_off_mode_skips(self) -> None:
        os.environ[fr.MODE_ENV] = "off"
        self.assertEqual(fr.enforce_write(None, {"zz": "1"}), [])


class TestFailOpen(_RegistryEnv):
    """Orchestrator ruling (T-4751): a malformed registry → ONE WARN, no findings, no
    refusal, even in refuse mode."""

    def _assert_fail_open(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(fr.validate_write(None, {"zz": "1", "substate": "bogus"}), [])
            self.assertEqual(fr.enforce_write(None, {"zz": "1", "substate": "bogus"}), [])
            self.assertEqual(fr.enforce_write(None, {"yy": "2"}), [])
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(
            "[frontmatter-registry] WARN registry unavailable:"), lines[0])
        self.assertIn("validation skipped", lines[0])

    def test_unparseable_yaml(self) -> None:
        self.use_registry_text(
            f"# x\n\n{fr.SECTION_HEADING}\n\n```yaml\nfields: [ {{field: id, kind\n```\n")
        self._assert_fail_open()

    def test_missing_section(self) -> None:
        self.use_registry_text("# ORG-021\n\nno registry here\n")
        self._assert_fail_open()

    def test_missing_block(self) -> None:
        self.use_registry_text(f"# x\n\n{fr.SECTION_HEADING}\n\nprose only\n\n## Next\n")
        self._assert_fail_open()

    def test_missing_file(self) -> None:
        os.environ[fr.PATH_ENV] = str(self.tmp / "nope.md")
        self._assert_fail_open()

    def test_null_in_values_list(self) -> None:
        # r1 NIT-1: parses as YAML but `~` in values used to crash warn mode.
        self.use_modified_real_registry(
            "values: [appetite-overrun, dropped, awaiting-merge]",
            "values: [appetite-overrun, dropped, awaiting-merge, ~]")
        self._assert_fail_open()

    def test_nested_in_values_list(self) -> None:
        self.use_modified_real_registry("values: [inbox, active, done]",
                                        "values: [inbox, [active], done]")
        self._assert_fail_open()

    def test_malformed_entry(self) -> None:
        self.use_registry_text(
            f"{fr.SECTION_HEADING}\n\n```yaml\nfields:\n  - just-a-string\n```\n")
        self._assert_fail_open()


class _TicketTree(_RegistryEnv):
    """Temp ticket tree, patched like test_ticket_label's _LabelTestBase."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "tickets"
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._patches = [
            patch.object(ticket, "TICKETS_ROOT", self.root),
            patch.object(ticket, "REPO_ROOT", self.tmp),
            patch.object(_git_utils, "REPO_ROOT", self.tmp),
            patch.object(ticket, "_pull_if_needed"),
        ]
        for p in self._patches:
            p.start()
        commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = commit_patch.start()
        self._patches.append(commit_patch)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()


class TestTicketPyChokePoint(_TicketTree):
    def test_refuse_label_add_leaves_file_untouched(self) -> None:
        # Registry without `labels` → adding labels is an unknown-field write.
        self.use_modified_real_registry(
            '  - {field: labels, kind: list, required: never, authority: '
            '"ORG-003 (free vocabulary); ORG-021 (interactive-required)", status: active}\n',
            "")
        os.environ[fr.MODE_ENV] = "refuse"
        path = _write_ticket(self.root / "active" / "T-9001.md", "T-9001", "active")
        before = path.read_bytes()
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            ticket.cmd_label_add(argparse.Namespace(ticket="T-9001", labels=["x"], no_push=True))
        self.assertIn("labels", str(ctx.exception))
        self.assertEqual(path.read_bytes(), before)
        self.mock_commit.assert_not_called()

    def test_warn_label_add_proceeds(self) -> None:
        self.use_modified_real_registry(
            '  - {field: labels, kind: list, required: never, authority: '
            '"ORG-003 (free vocabulary); ORG-021 (interactive-required)", status: active}\n',
            "")
        path = _write_ticket(self.root / "active" / "T-9002.md", "T-9002", "active")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ticket.cmd_label_add(argparse.Namespace(ticket="T-9002", labels=["x"], no_push=True))
        self.assertEqual(rc, 0)
        self.assertIn("[frontmatter-registry] WARN T-9002", err.getvalue())
        self.assertEqual(ticket.parse_front_matter(path.read_text())[0]["labels"], ["x"])
        self.mock_commit.assert_called_once()

    def test_legacy_field_on_disk_roundtrips_silently_through_a_verb(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        path = _write_ticket(self.root / "active" / "T-9003.md", "T-9003", "active",
                             ["blocked_by: T-0001", "resolved: 2026-05-01T00:00:00Z"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ticket.cmd_label_add(argparse.Namespace(ticket="T-9003", labels=["x"], no_push=True))
        self.assertEqual(rc, 0)
        self.assertNotIn("frontmatter-registry", err.getvalue())
        fields = ticket.parse_front_matter(path.read_text())[0]
        self.assertEqual(fields["blocked_by"], "T-0001")

    def _build_twice_and_capture(self) -> tuple[list[str], list[str]]:
        texts = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for tid in ("T-9201", "T-9202"):
                texts.append(ticket._build_ticket_text(
                    {"id": tid, "created": "x", "requester": "r", "target": "t",
                     "state": "inbox", "substate": "bogus"}, "# b\n", old_fields=None))
        return texts, [ln for ln in err.getvalue().splitlines() if ln]

    def test_null_value_registry_through_choke_point_fails_open(self) -> None:
        # r1 NIT-1: the real write path, refuse mode, registry with a null value.
        self.use_modified_real_registry(
            "values: [appetite-overrun, dropped, awaiting-merge]",
            "values: [appetite-overrun, dropped, awaiting-merge, ~]")
        os.environ[fr.MODE_ENV] = "refuse"
        texts, lines = self._build_twice_and_capture()
        self.assertEqual(len(texts), 2)
        self.assertIn("substate: bogus", texts[0])
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("registry unavailable", lines[0])
        self.assertIn("validation skipped", lines[0])

    def test_unexpected_registry_exception_fails_open(self) -> None:
        # r1 NIT-1 catch-all: any non-refusal error from the registry module →
        # one WARN per process, the write proceeds.
        os.environ[fr.MODE_ENV] = "refuse"
        with patch.object(ticket, "_FM_REGISTRY_ERROR_WARNED", False), \
                patch.object(fr, "enforce_write", side_effect=AttributeError("boom")):
            texts, lines = self._build_twice_and_capture()
        self.assertEqual(len(texts), 2)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("[frontmatter-registry] WARN registry check failed unexpectedly", lines[0])
        self.assertIn("AttributeError: boom", lines[0])
        self.assertIn("validation skipped", lines[0])

    def test_unexpected_exception_path_still_writes_via_verb(self) -> None:
        path = _write_ticket(self.root / "active" / "T-9204.md", "T-9204", "active")
        err = io.StringIO()
        with patch.object(ticket, "_FM_REGISTRY_ERROR_WARNED", False), \
                patch.object(fr, "enforce_write", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            rc = ticket.cmd_label_add(argparse.Namespace(ticket="T-9204", labels=["x"], no_push=True))
        self.assertEqual(rc, 0)
        self.assertEqual(ticket.parse_front_matter(path.read_text())[0]["labels"], ["x"])
        self.mock_commit.assert_called_once()
        self.assertEqual(err.getvalue().count("[frontmatter-registry] WARN"), 1)

    def test_module_missing_warns_once_and_write_proceeds(self) -> None:
        # r1 NIT-2: tools/frontmatter_registry.py not importable (_fm_registry None).
        path = _write_ticket(self.root / "active" / "T-9203.md", "T-9203", "active")
        err = io.StringIO()
        with patch.object(ticket, "_fm_registry", None), \
                patch.object(ticket, "_FM_REGISTRY_MISSING_WARNED", False), \
                contextlib.redirect_stderr(err):
            rc = ticket.cmd_label_add(argparse.Namespace(ticket="T-9203", labels=["x"], no_push=True))
            texts, lines = self._build_twice_and_capture()
        self.assertEqual(rc, 0)
        self.assertEqual(ticket.parse_front_matter(path.read_text())[0]["labels"], ["x"])
        self.mock_commit.assert_called_once()
        self.assertEqual(len(texts), 2)
        self.assertEqual(lines, [])  # already warned once above
        warns = [ln for ln in err.getvalue().splitlines() if "[frontmatter-registry]" in ln]
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("not importable", warns[0])
        self.assertIn("validation skipped", warns[0])

    def test_new_ticket_build_validates_everything(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ticket._build_ticket_text(
                {"id": "T-9999", "created": "x", "requester": "r", "target": "t",
                 "state": "inbox", "blocked_by": "T-1"}, "# b\n", old_fields=None)
        self.assertIn("legacy field", err.getvalue())


def _g(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


class TestRefuseBeforeGitMv(_RegistryEnv):
    """refuse mode on the _do_transition path (the one with a git mv): the
    refusal must land before the mv — file bytes, index, and HEAD unchanged."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _g("init", "-q", "-b", "main", cwd=self.repo)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _g("config", k, v, cwd=self.repo)
        self.root = self.repo / "tickets"
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
            (self.root / s / ".keep").write_text("")
        self.path = _write_ticket(self.root / "inbox" / "T-9101.md", "T-9101", "inbox")
        _g("add", "-A", cwd=self.repo)
        _g("commit", "-q", "-m", "base", cwd=self.repo)
        os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = str(self.tmp / "mut.lock")
        self._patches = [
            patch.object(ticket, "TICKETS_ROOT", self.root),
            patch.object(ticket, "REPO_ROOT", self.repo),
            patch.object(_git_utils, "REPO_ROOT", self.repo),
            patch.object(ticket, "_pull_if_needed"),
        ]
        for p in self._patches:
            p.start()
        # `_git`'s cwd default is bound at import to the real repo; pin it here.
        repo = self.repo
        git_patch = patch.object(ticket, "_git",
                                 lambda *a, **_k: _git_utils._git(*a, cwd=repo))
        git_patch.start()
        self._patches.append(git_patch)
        commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = commit_patch.start()
        self._patches.append(commit_patch)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def test_refused_claim_leaves_no_trace(self) -> None:
        # Registry where `active` is not a state value → claim's state write refused.
        self.use_modified_real_registry("values: [inbox, active, done]", "values: [inbox, done]")
        os.environ[fr.MODE_ENV] = "refuse"
        before = self.path.read_bytes()
        head = _g("rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            ticket._do_transition("T-9101", "inbox", "active", "claim", "x", no_push=True)
        # The refusal must be the registry's, not some unrelated gate's.
        self.assertIn("frontmatter-registry", str(ctx.exception))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "active" / "T-9101.md").exists())
        self.assertEqual(_g("status", "--porcelain", cwd=self.repo), "")
        self.assertEqual(_g("rev-parse", "HEAD", cwd=self.repo), head)
        self.mock_commit.assert_not_called()

    def test_claim_still_works_with_real_registry(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        rc = ticket._do_transition("T-9101", "inbox", "active", "claim", "x", no_push=True)
        self.assertEqual(rc, 0)
        moved = self.root / "active" / "T-9101.md"
        self.assertTrue(moved.exists())
        self.assertEqual(ticket.parse_front_matter(moved.read_text())[0]["state"], "active")


class TestDrift(_RegistryEnv):
    """Every field the code can write, and every enum value it accepts, must be
    in the ORG-021 registry."""

    # T-4760 (d): the writer modules are DISCOVERED (every non-test tools/*.py
    # that calls the single write choke point), not a fixed list; the literal
    # scan below runs over each.
    KNOWN_WRITERS = {"ticket.py", "inbox_consumer.py", "ticket_grooming.py",
                     "ticket_lint.py", "s3star_audit_runner.py"}
    # Keys the scan MUST find per module — keeps it non-vacuous (a scan that
    # silently matched nothing would pass every drift check).
    MUST_FIND = {
        "inbox_consumer.py": {"auto_delivered", "auto_delivered_at", "delivered_to"},
        "s3star_audit_runner.py": {"retest_status"},
        "ticket_grooming.py": {"substate", "labels", "state"},
        "ticket_lint.py": {"intent"},  # T-5239: lint no longer rewrites target (it flags persona targets)
        "ticket.py": {"id", "created", "requester", "target", "state", "depends_on",
                      "substate", "dropped_reason", "dropped_from_state", "appetite",
                      "awaiting_merge_ref", "recovered_before_repair", "labels",
                      "delivered_to", "project_id", "nearest_intent_ref", "serves"},
    }

    # A dict that holds ticket frontmatter: any identifier containing
    # fields / fm / field_updates (fields, new_fields, pre_fields, fm, ...).
    _NAME = r"\b\w*(?:fields|fm|field_updates)\w*"
    _SUBSCRIPT = re.compile(_NAME + r'\[\s*"([a-z_][a-z0-9_]*)"\s*\]\s*=(?!=)')
    _SETDEFAULT = re.compile(_NAME + r'\.setdefault\(\s*"([a-z_][a-z0-9_]*)"')
    _DICT_KEY = re.compile(r'"([a-z_][a-z0-9_]*)"\s*:')
    # Openers of a dict LITERAL that becomes frontmatter: assigned to / merged
    # into such a name, passed straight to the builder, or returned by a
    # `*_field_updates` / `*_fields` helper.
    _LITERAL_OPEN = re.compile(
        _NAME + r"\s*(?::\s*[\w\[\]|, ]+)?\s*(?:=|\.update\()\s*(?:dict\()?\{"
        r"|\bfield_updates\s*=\s*\{"
        r"|_build_ticket_text\(\s*\{")
    _HELPER_DEF = re.compile(r"^def (\w*(?:field_updates|_fields)\w*)\(", re.M)

    def setUp(self) -> None:
        super().setUp()
        self.reg = fr.load_registry()
        self.assertIsNotNone(self.reg)

    def _src(self, name: str) -> str:
        return (TOOLS / name).read_text(encoding="utf-8")

    @classmethod
    def writer_modules(cls) -> list[str]:
        return sorted(
            p.name for p in TOOLS.glob("*.py")
            if not p.name.startswith("test_") and p.name != "conftest.py"
            and "_build_ticket_text(" in p.read_text(encoding="utf-8"))

    @staticmethod
    def _balanced(src: str, i: int) -> str:
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[i:j + 1]
        return src[i:]

    @classmethod
    def scan_written_keys(cls, src: str) -> set:
        keys = set(cls._SUBSCRIPT.findall(src)) | set(cls._SETDEFAULT.findall(src))
        for m in cls._LITERAL_OPEN.finditer(src):
            keys |= set(cls._DICT_KEY.findall(cls._balanced(src, m.end() - 1)))
        for m in cls._HELPER_DEF.finditer(src):
            nxt = re.search(r"^def ", src[m.end():], re.M)
            body = src[m.end(): m.end() + nxt.start()] if nxt else src[m.end():]
            for r in re.finditer(r"return\s+\{", body):
                keys |= set(cls._DICT_KEY.findall(cls._balanced(body, r.end() - 1)))
        return keys

    def test_writer_discovery_finds_the_known_writers(self) -> None:
        found = set(self.writer_modules())
        self.assertTrue(self.KNOWN_WRITERS <= found,
                        f"writer discovery found only {sorted(found)}")

    def test_scan_is_non_vacuous(self) -> None:
        for mod, must in self.MUST_FIND.items():
            got = self.scan_written_keys(self._src(mod))
            self.assertTrue(must <= got, f"{mod}: scan missed {sorted(must - got)}")

    def test_scan_patterns_catch_each_shape(self) -> None:
        src = ('new_fields["a_sub"] = 1\nfm["b_fm"] = 2\nfields.setdefault("c_set", [])\n'
               'fields.update({"d_upd": 1})\nfield_updates = {\n  "e_lit": 1,\n}\n'
               'x = _build_ticket_text({"f_arg": 1}, body, old_fields=None)\n'
               'def _x_field_updates(t):\n    return {"g_ret": t}\n'
               'other["not_me"] = 1\nif fields["eq_only"] == 1: pass\n')
        self.assertEqual(self.scan_written_keys(src),
                         {"a_sub", "b_fm", "c_set", "d_upd", "e_lit", "f_arg", "g_ret"})

    def test_every_writable_field_is_registered(self) -> None:
        writable = set(ticket.FRONT_MATTER_FIELD_ORDER)
        writable |= set(ticket.REQUIRED_FRONT_MATTER)
        writable |= set(ticket.OPTIONAL_FRONT_MATTER)
        writable |= set(ticket.REPAIR_FIELD_SPEC)
        # T-4760 (d): literal writes in every DISCOVERED writer module (this is
        # also where auto_delivered / auto_delivered_at / delivered_to come from
        # now — no hard-coded list).
        by_module = {mod: self.scan_written_keys(self._src(mod))
                     for mod in self.writer_modules()}
        for keys in by_module.values():
            writable |= keys
        missing = sorted(k for k in writable if k not in self.reg)
        where = {k: sorted(m for m, ks in by_module.items() if k in ks) for k in missing}
        self.assertEqual(missing, [], f"code writes unregistered field(s) {where}: {DRIFT_HINT}")
        legacy = sorted(k for k in writable if self.reg.get(k).status == "legacy")
        self.assertEqual(legacy, [], f"code writes legacy field(s) {legacy}: mark them active "
                                     f"or stop writing them; {DRIFT_HINT}")

    def _assert_subset(self, field: str, accepted) -> None:
        entry = self.reg.get(field)
        self.assertIsNotNone(entry, f"`{field}` missing from registry: {DRIFT_HINT}")
        accepted = {fr.norm_value(v) for v in accepted}
        extra = sorted(accepted - set(entry.values or ()))
        self.assertEqual(extra, [], f"code accepts `{field}` value(s) {extra} the registry "
                                    f"does not list: {DRIFT_HINT}")

    def test_enums_are_subsets_of_registry_values(self) -> None:
        self._assert_subset("state", ticket.STATES)
        self._assert_subset("intent", ticket.INTENT_VOCAB)
        self._assert_subset("retest_status", ticket.RETEST_STATUS_VOCAB)
        self._assert_subset("appetite", range(ticket_purpose.APPETITE_MIN,
                                              ticket_purpose.APPETITE_MAX + 1))
        self._assert_subset("dropped_from_state", [s for s in ticket.STATES if s != "done"])
        self._assert_subset("auto_delivered", ["true"])
        for key, (kind, _req) in ticket.REPAIR_FIELD_SPEC.items():
            if kind.startswith("enum:"):
                self._assert_subset(key, kind[len("enum:"):].split(","))
            elif kind == "bool":
                self._assert_subset(key, ["true", "false"])

    def test_substate_literals_are_registered(self) -> None:
        written: set = set()
        for mod in self.writer_modules():
            src = self._src(mod)
            written |= set(re.findall(r'\["substate"\]\s*=\s*"([^"]+)"', src))
            written |= set(re.findall(r'"substate":\s*"([^"]+)"', src))
        self.assertTrue({"awaiting-merge", "dropped", "appetite-overrun"} <= written,
                        f"substate literal scan found only {sorted(written)}")
        self._assert_subset("substate", written)


class TestLintFrontmatter(_RegistryEnv):
    def test_runs_on_real_tree_exit_0(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ticket.cmd_lint_frontmatter(argparse.Namespace(strict=False))
        self.assertEqual(rc, 0)
        self.assertIn("lint-frontmatter: registry", out.getvalue())

    def test_reports_and_strict_on_temp_tree(self) -> None:
        root = self.tmp / "tickets"
        _write_ticket(root / "inbox" / "T-0001.md", "T-0001", "inbox", ["substate: bogus"])
        _write_ticket(root / "active" / "T-0002.md", "T-0002", "active",
                      ["blocked_by: T-0001", "weird: 1"])
        _write_ticket(root / "done" / "T-0003.md", "T-0003", "done")
        (root / "done" / "T-0004.md").write_text("no frontmatter\n")
        with patch.object(ticket, "TICKETS_ROOT", root):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ticket.cmd_lint_frontmatter(argparse.Namespace(strict=False))
            text = out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("scanned 3 tickets; 3 finding(s)", text)
            self.assertIn("[bad-value] substate: bogus — 1 ticket(s): T-0001", text)
            self.assertIn("[unknown-field] weird — 1 ticket(s): T-0002", text)
            self.assertIn("[legacy-field use] blocked_by — 1 ticket(s): T-0002", text)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    ticket.cmd_lint_frontmatter(argparse.Namespace(strict=True)), 1)


# ---------------------------------------------------------------------------
# T-4760: refuse-mode readiness (must-fix list from the #800 reviews)
# ---------------------------------------------------------------------------

_LABELS_ENTRY = ('  - {field: labels, kind: list, required: never, authority: '
                 '"ORG-003 (free vocabulary); ORG-021 (interactive-required)", status: active}\n')


def _parse(path: Path) -> tuple[dict, str]:
    return ticket.parse_front_matter(path.read_text(encoding="utf-8"))


class TestExplicitOldFields(_RegistryEnv):
    """(a) old-vs-new is explicit: the writer passes what IT parsed."""

    def test_no_process_wide_snapshot_store(self) -> None:
        for name in ("record_snapshot", "snapshot_for", "clear_snapshots", "_SNAPSHOTS"):
            self.assertFalse(hasattr(fr, name), name)
        # old_fields is required: forgetting it is a TypeError, never a silent
        # "everything is new" fallback.
        with self.assertRaises(TypeError):
            ticket._build_ticket_text({"id": "T-1"}, "# b\n")  # type: ignore[call-arg]

    def test_parse_has_no_registry_side_effect(self) -> None:
        a = _write_ticket(self.tmp / "a" / "T-9300.md", "T-9300", "inbox", ["weird: 1"])
        before = dict(fr._CACHE)
        _parse(a)
        self.assertEqual(fr._CACHE, before)

    def test_same_id_from_two_worktrees_does_not_cross_contaminate(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        a = _write_ticket(self.tmp / "wt-a" / "tickets" / "active" / "T-9300.md",
                          "T-9300", "active", ["blocked_by: T-0001"])
        b = _write_ticket(self.tmp / "wt-b" / "tickets" / "active" / "T-9300.md",
                          "T-9300", "active")
        # Writer on A: its own legacy value is unchanged -> no refusal, even
        # though B (same id, no blocked_by) was read in between.
        fa, ba = _parse(a)
        old_a = ticket._fm_snapshot(fa)
        _parse(b)
        fa["labels"] = ["x"]
        text = ticket._build_ticket_text(fa, ba, old_fields=old_a)
        self.assertIn("blocked_by: T-0001", text)
        # Writer on B ADDS blocked_by: T-0001 — a real legacy write. A's read
        # in between (which carries exactly that value) must not mask it.
        fb, bb = _parse(b)
        old_b = ticket._fm_snapshot(fb)
        _parse(a)
        fb["blocked_by"] = "T-0001"
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            ticket._build_ticket_text(fb, bb, old_fields=old_b)
        self.assertIn("blocked_by", str(ctx.exception))

    def test_stale_read_judged_against_this_writers_read(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        path = _write_ticket(self.tmp / "tickets" / "active" / "T-9301.md", "T-9301", "active")
        fields, body = _parse(path)
        old = ticket._fm_snapshot(fields)
        # Another writer rewrites the file after our read: adds an unregistered
        # key and a legacy key.
        _write_ticket(path, "T-9301", "active", ["weird: 1", "blocked_by: T-0001"])
        # Our unchanged values (+ a legit label) do not refuse...
        fields["labels"] = ["x"]
        ticket._build_ticket_text(dict(fields), body, old_fields=old)
        # ...and a real change of ours is caught.
        with self.assertRaises(ticket.TicketRefusal):
            ticket._build_ticket_text(dict(fields, substate="bogus"), body, old_fields=old)
        # Adding the very value the other writer put on disk is still OUR new
        # legacy write relative to what we read.
        with self.assertRaises(ticket.TicketRefusal):
            ticket._build_ticket_text(dict(fields, blocked_by="T-0001"), body, old_fields=old)

    def test_refused_or_unwritten_build_does_not_poison_later_checks(self) -> None:
        path = _write_ticket(self.tmp / "tickets" / "active" / "T-9302.md", "T-9302", "active")
        fields, body = _parse(path)
        old = ticket._fm_snapshot(fields)
        # 1. warn mode: a build carrying `weird` is made but NEVER written.
        with contextlib.redirect_stderr(io.StringIO()):
            ticket._build_ticket_text(dict(fields, weird="1"), body, old_fields=old)
        # 2. refuse mode: the same addition is still an addition.
        os.environ[fr.MODE_ENV] = "refuse"
        with self.assertRaises(ticket.TicketRefusal):
            ticket._build_ticket_text(dict(fields, weird="1"), body, old_fields=old)
        # 3. a refused build does not make the next identical attempt "unchanged".
        with self.assertRaises(ticket.TicketRefusal):
            ticket._build_ticket_text(dict(fields, weird="1"), body, old_fields=old)
        # 4. and an unrelated legit write of the same ticket is unaffected.
        ticket._build_ticket_text(dict(fields, labels=["ok"]), body, old_fields=old)


class TestGroomingOverrunFlag(_RegistryEnv):
    """(b) ticket_grooming._apply_overrun_flag: re-parse + loud refusal."""

    NOW = None

    def setUp(self) -> None:
        super().setUp()
        from datetime import datetime, timezone
        from tools import ticket_grooming as tg
        self.tg = tg
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.tickets = self.tmp / "tickets"
        self.path = _write_ticket(self.tickets / "active" / "T-9600.md", "T-9600", "active",
                                  ["appetite: 5"])

    def test_reparses_so_it_never_writes_stale_fields(self) -> None:
        stale = ticket.parse_ticket(self.path)
        _write_ticket(self.path, "T-9600", "active", ["appetite: 5", "labels: [added-later]"])
        self.assertEqual(self.tg._apply_overrun_flag(self.path, stale, self.now), "written")
        fields, body = _parse(self.path)
        self.assertEqual(fields["substate"], "appetite-overrun")
        self.assertEqual(fields["labels"], ["added-later"])  # not clobbered by the stale read
        self.assertIn("overrun-flag", body)

    def test_already_flagged_on_reread_is_skipped(self) -> None:
        stale = ticket.parse_ticket(self.path)
        _write_ticket(self.path, "T-9600", "active",
                      ["appetite: 5", "substate: appetite-overrun"])
        before = self.path.read_bytes()
        self.assertEqual(self.tg._apply_overrun_flag(self.path, stale, self.now), "skipped")
        self.assertEqual(self.path.read_bytes(), before)

    def _refusing_registry(self) -> None:
        self.use_modified_real_registry(
            "values: [appetite-overrun, dropped, awaiting-merge]",
            "values: [dropped, awaiting-merge]")
        os.environ[fr.MODE_ENV] = "refuse"

    def test_refusal_is_logged_loudly_not_swallowed(self) -> None:
        self._refusing_registry()
        before = self.path.read_bytes()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.tg._apply_overrun_flag(self.path, ticket.parse_ticket(self.path), self.now)
        self.assertEqual(out, "refused")
        self.assertEqual(self.path.read_bytes(), before)
        msg = err.getvalue()
        self.assertIn("REFUSED overrun-flag write for T-9600", msg)
        self.assertIn("substate: appetite-overrun", msg)  # the finding itself

    def test_refused_flag_sends_no_false_advisory(self) -> None:
        self._refusing_registry()
        policy = self.tmp / "appetite-time-budget.yaml"
        policy.write_text("mode: active\ncalibration_version: 1\nbands:\n  5: 3600\n",
                          encoding="utf-8")
        _write_ticket(self.path, "T-9600", "active", ["appetite: 5"])
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("- 2026-09-05T15:00:00Z [mason] claimed\n")
        sent: list = []
        with contextlib.redirect_stderr(io.StringIO()):
            found = self.tg._appetite_overrun_check(
                policy, self.tickets, self.now, apply=True,
                send_fn=lambda *a, **k: sent.append(a))
        self.assertEqual([f.tid for f in found], ["T-9600"])
        self.assertEqual(sent, [])
        self.assertNotIn("substate", _parse(self.path)[0])


class TestInboxStampRefusal(_RegistryEnv):
    """(b) inbox_consumer: the stamp's registry check, preflight + classified."""

    def setUp(self) -> None:
        super().setUp()
        from tools import inbox_consumer as ic
        self.ic = ic
        self.path = _write_ticket(self.tmp / "tickets" / "inbox" / "T-9700.md", "T-9700", "inbox")
        self.info = ic.TicketInfo(id="T-9700", title="Example", created_str="2026-10-01T00:00:00Z",
                                  depends_on=[], labels=[], body="", path=self.path)
        self.use_modified_real_registry(
            '  - {field: delivered_to, kind: scalar, required: "when a role:<R> ticket is '
            'delivered or claimed", authority: "T-4695; T-4698; ORG-088 rev-2", status: active}\n',
            "")

    def test_preflight_is_a_noop_in_warn_mode(self) -> None:
        self.assertIsNone(self.ic._preflight_stamp_registry(self.info, agent="orchestrator"))

    def test_preflight_reports_refusal_and_writes_nothing(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        before = self.path.read_bytes()
        msg = self.ic._preflight_stamp_registry(self.info, agent="orchestrator")
        self.assertIsNotNone(msg)
        self.assertIn("T-9700", msg)
        self.assertIn("delivered_to", msg)
        self.assertEqual(self.path.read_bytes(), before)
        # no agent -> no delivered_to -> nothing to refuse
        self.assertIsNone(self.ic._preflight_stamp_registry(self.info, agent=None))

    def test_stamp_classifies_refusal_distinctly(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        before = self.path.read_bytes()
        err = io.StringIO()
        with patch("subprocess.run") as run, contextlib.redirect_stderr(err):
            result = self.ic._stamp_auto_delivered(self.info, agent="orchestrator")
        self.assertIs(result, self.ic.StampResult.REGISTRY_REFUSED)
        run.assert_not_called()  # no git step after a refusal
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("REGISTRY REFUSED for T-9700", err.getvalue())


class TestCascadeAndChildWrites(_TicketTree):
    """(c) writes that run AFTER the parent write."""

    def test_cascade_on_legacy_and_unknown_fields_never_refuses(self) -> None:
        os.environ[fr.MODE_ENV] = "refuse"
        _write_ticket(self.root / "done" / "T-9800.md", "T-9800", "done")
        dep = _write_ticket(self.root / "inbox" / "T-9801.md", "T-9801", "inbox",
                            ["blocked_by: T-0001", "weird: 1", "auto_delivered: true",
                             "auto_delivered_at: 2026-10-01T00:00:00Z", "delivered_to: orchestrator"])
        dep.write_text(dep.read_text().replace("depends_on: []", "depends_on: [T-9800]"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            paths = ticket._cascade_unblock_dependants("T-9800")
        self.assertEqual(paths, [dep])
        fields, _ = _parse(dep)
        self.assertEqual(fields["depends_on"], [])
        self.assertEqual((fields["blocked_by"], fields["weird"]), ("T-0001", "1"))
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("frontmatter-registry", err.getvalue())

    def test_cascade_write_shape_assertion(self) -> None:
        old = {"id": "T-1", "depends_on": ["T-2", "T-3"], "auto_delivered": "true"}
        ticket._assert_cascade_write_shape(old, {"id": "T-1", "depends_on": ["T-3"]})
        for bad in ({"id": "T-1", "depends_on": [], "auto_delivered": "true", "new": "x"},
                    {"id": "T-1", "depends_on": [], "auto_delivered": "false"},
                    {"id": "T-1", "depends_on": ["T-2", "T-4"]}):
            with self.assertRaises(ticket.TicketError):
                ticket._assert_cascade_write_shape(old, bad)

    def test_every_child_spawn_shape_is_registered(self) -> None:
        # The three spawn shapes (ratify impl, close-noodle, close-action review).
        shapes = [("recovery", None, ["ratify-impl"], ["T-1"]),
                  ("recovery", None, ["spawned-from-noodle"], None),
                  ("recovery", None, ["review", "follow-up", "auto-spawned-from-action-close"], None),
                  ("operator", "action", ["review"], None)]
        for target, intent, labels, deps in shapes:
            fields = ticket._child_ticket_fields(
                tid="T-2", parent_tid="T-1",
                parent_fields={"project_id": "p", "serves": "s"}, actor="orchestrator",
                target=target, intent=intent, labels=labels, depends_on=deps)
            self.assertEqual(fr.validate_write(None, fields), [], fields)


class TestChildPreflightBeforeParentWrite(TestRefuseBeforeGitMv):
    """(c) close-action: the review child's fields are refused BEFORE the
    parent transition — no parent mv, no orphan child."""

    def test_refused_child_leaves_parent_untouched(self) -> None:
        _write_ticket(self.path, "T-9101", "inbox", ["intent: action"])
        self.path.write_text(self.path.read_text().replace("target: recovery", "target: operator"))
        _g("add", "-A", cwd=self.repo)
        from tools.test_ticket import with_ruling  # T-5015 register flags
        cargs = with_ruling(argparse.Namespace(
            ticket="T-9101", with_summary="done", no_push=True), self.repo)
        _g("add", "-A", cwd=self.repo)
        _g("commit", "-q", "-m", "operator action", cwd=self.repo)
        self.use_modified_real_registry(_LABELS_ENTRY, "")
        os.environ[fr.MODE_ENV] = "refuse"
        before = self.path.read_bytes()
        head = _g("rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            ticket.cmd_close_action(cargs)
        self.assertIn("labels", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "inbox").iterdir()),
                         [".keep", "T-9101.md"])
        self.assertEqual(_g("status", "--porcelain", cwd=self.repo), "")
        self.assertEqual(_g("rev-parse", "HEAD", cwd=self.repo), head)
        self.mock_commit.assert_not_called()


class TestLintFrontmatterRobustness(_RegistryEnv):
    """(e) fail-open catch-all + ASCII-only int values; (f) allowed off main."""

    def test_unexpected_error_report_mode_warns_once_exit_0(self) -> None:
        err, out = io.StringIO(), io.StringIO()
        with patch.object(fr, "check_fields", side_effect=RuntimeError("kaboom")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = ticket.cmd_lint_frontmatter(argparse.Namespace(strict=False))
        self.assertEqual(rc, 0)
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("WARN lint-frontmatter: registry unavailable", lines[0])
        self.assertIn("RuntimeError: kaboom", lines[0])
        self.assertNotIn("Traceback", err.getvalue())

    def test_unexpected_error_strict_exits_1_clearly(self) -> None:
        err = io.StringIO()
        with patch.object(fr, "load_registry", side_effect=KeyError("x")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = ticket.cmd_lint_frontmatter(argparse.Namespace(strict=True))
        self.assertEqual(rc, 1)
        self.assertIn("lint-frontmatter: registry unavailable", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_unavailable_registry_strict_says_so(self) -> None:
        os.environ[fr.PATH_ENV] = str(self.tmp / "nope.md")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ticket.cmd_lint_frontmatter(argparse.Namespace(strict=False)), 0)
            self.assertEqual(ticket.cmd_lint_frontmatter(argparse.Namespace(strict=True)), 1)
        self.assertIn("lint-frontmatter: registry unavailable", err.getvalue())

    def test_non_ascii_digit_int_values_fail_open(self) -> None:
        for bad in ("'\u00b2'", "'\u0663'", "true"):
            with self.subTest(bad=bad):
                self.use_modified_real_registry(
                    "values: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]",
                    f"values: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, {bad}]")
                fr._WARNED_UNAVAILABLE.clear()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertIsNone(fr.load_registry())
                self.assertIn("non-integer", err.getvalue())

    def test_ascii_int_values_still_load(self) -> None:
        self.use_modified_real_registry("values: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]",
                                        "values: [1, 2, '3', -4]")
        reg = fr.load_registry()
        self.assertIsNotNone(reg)
        self.assertEqual(reg.get("appetite").values, frozenset({"1", "2", "3", "-4"}))

    def test_lint_frontmatter_runs_off_main(self) -> None:
        with patch.object(ticket, "_current_branch", return_value="agent/T-4760"), \
                patch.object(ticket, "cmd_lint_frontmatter", return_value=0) as lint, \
                contextlib.redirect_stderr(io.StringIO()):
            # main() builds its parser per call, so it binds the patched verb.
            rc = ticket.main(["lint-frontmatter", "--strict"])
        self.assertEqual(rc, 0)
        lint.assert_called_once()

    def test_other_verbs_still_refused_off_main(self) -> None:
        err = io.StringIO()
        with patch.object(ticket, "_current_branch", return_value="agent/T-4760"), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = ticket.main(["list"])
        self.assertNotEqual(rc, 0)
        self.assertIn("refusing to run ticket.py from 'agent/T-4760'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
