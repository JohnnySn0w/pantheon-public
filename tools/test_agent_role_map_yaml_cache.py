"""T-4279: agent_role_map parses instance.yaml once per process, with libyaml's
CSafeLoader when available. These tests pin the two properties that make that
speed-up safe:

  PARITY    — on the REAL repo instance.yaml, CSafeLoader and SafeLoader produce
              the same Python object, compared TYPE-STRICTLY (plain == would let
              True == 1 or 1 == 1.0 slip through) and including mapping key
              order. The cached helper returns that same object.
  CACHE     — one parse per distinct file CONTENT (read every call); any edit
              (in-place, same-size, same-tick, or atomic rename) is observed (current_mailbox_hub()'s re-read contract, T-2693);
              callers of the public API cannot corrupt the shared cached document.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map as arm  # noqa: E402

REPO_INSTANCE_YAML = Path(__file__).resolve().parent.parent / "instance.yaml"


def _strict(obj):
    """Type-tagged canonical form: equal iff same values, same types, same key order."""
    if isinstance(obj, dict):
        return ("dict", tuple((_strict(k), _strict(v)) for k, v in obj.items()))
    if isinstance(obj, list):
        return ("list", tuple(_strict(v) for v in obj))
    return (type(obj).__name__, repr(obj))


class ParityOnRealInstanceYaml(unittest.TestCase):
    def setUp(self):
        self.text = REPO_INSTANCE_YAML.read_text(encoding="utf-8")
        self.py = yaml.load(self.text, Loader=yaml.SafeLoader)

    def test_csafeloader_matches_safeloader_on_live_manifest(self):
        if getattr(yaml, "CSafeLoader", None) is None:
            self.skipTest("PyYAML built without libyaml: no CSafeLoader")
        c = yaml.load(self.text, Loader=yaml.CSafeLoader)
        self.assertEqual(c, self.py)
        self.assertEqual(_strict(c), _strict(self.py))

    def test_cached_helper_matches_safeloader_parse(self):
        got = arm._parse_manifest(REPO_INSTANCE_YAML)
        self.assertEqual(got, self.py)
        self.assertEqual(_strict(got), _strict(self.py))

    def test_loader_selection_prefers_c_and_falls_back(self):
        expected = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        self.assertIs(arm._yaml_safe_loader_class(), expected)
        with mock.patch.object(yaml, "CSafeLoader", None, create=True):
            self.assertIs(arm._yaml_safe_loader_class(), yaml.SafeLoader)


class CacheBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "instance.yaml"
        self.path.write_text(
            "persona_installer:\n  mailbox:\n    hub: /hub/one\n    group: g1\n",
            encoding="utf-8",
        )

    def tearDown(self):
        arm._MANIFEST_CACHE.pop(os.path.abspath(self.path), None)
        self.tmp.cleanup()

    def _counting_load(self):
        calls = {"n": 0}
        real = yaml.load

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        return calls, mock.patch.object(yaml, "load", counting)

    def test_parses_once_per_unchanged_content(self):
        calls, patch = self._counting_load()
        with patch:
            a = arm._parse_manifest(self.path)
            b = arm._parse_manifest(self.path)
            arm._load_instance_mailbox(self.path)
            arm._load_instance_role_map(self.path)
            arm._load_instance_bindings(self.path)
        self.assertEqual(calls["n"], 1)
        self.assertIs(a, b)

    def test_in_place_edit_is_observed(self):
        self.assertEqual(arm._load_instance_mailbox(self.path)["hub"], "/hub/one")
        st = self.path.stat()
        self.path.write_text(
            "persona_installer:\n  mailbox:\n    hub: /hub/two\n    group: g1\n",
            encoding="utf-8",
        )
        # Force a distinct mtime even on coarse-timestamp filesystems (same size).
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertEqual(arm._load_instance_mailbox(self.path)["hub"], "/hub/two")

    def test_atomic_rename_is_observed(self):
        self.assertEqual(arm._load_instance_mailbox(self.path)["hub"], "/hub/one")
        st = self.path.stat()
        new = self.path.with_name("instance.yaml.new")
        new.write_text(
            "persona_installer:\n  mailbox:\n    hub: /hub/six\n    group: g1\n",
            encoding="utf-8",
        )
        # Same size, same mtime: only the inode differs.
        os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(new, self.path)
        self.assertEqual(arm._load_instance_mailbox(self.path)["hub"], "/hub/six")

    def test_current_mailbox_hub_sees_edit(self):
        with mock.patch.object(arm, "_INSTANCE_YAML", self.path):
            self.assertEqual(arm.current_mailbox_hub(), "/hub/one")
            self.path.write_text(
                "persona_installer:\n  mailbox:\n    hub: /hub/three/longer\n    group: g1\n",
                encoding="utf-8",
            )
            self.assertEqual(arm.current_mailbox_hub(), "/hub/three/longer")

    def test_same_size_rapid_rewrite_never_serves_stale(self):
        """T-4279 review blocker: a same-size in-place rewrite inside one mtime tick
        (no utime bump) must still be observed. A stat-keyed cache served stale hubs
        here; the content-keyed cache must serve ZERO stale reads."""
        hubs = ("/hub/AAAA", "/hub/BBBB")  # equal length -> equal file size
        stale = 0
        with mock.patch.object(arm, "_INSTANCE_YAML", self.path):
            for i in range(200):
                want = hubs[i % 2]
                self.path.write_text(
                    f"persona_installer:\n  mailbox:\n    hub: {want}\n    group: g1\n",
                    encoding="utf-8",
                )
                if arm.current_mailbox_hub() != want:
                    stale += 1
        self.assertEqual(stale, 0, f"{stale}/200 stale reads")

    def test_symlink_loop_raises_oserror_family(self):
        """Callers (seat_launch_config, persona_bringup) catch OSError; a symlink
        loop must not surface as RuntimeError (Path.resolve()'s loop error)."""
        loop = Path(self.tmp.name) / "loop.yaml"
        os.symlink(loop, loop)
        with self.assertRaises(OSError):
            arm._parse_manifest(loop)
        self.assertEqual(arm._load_instance_mailbox(loop), arm._FALLBACK_MAILBOX)

    def test_missing_file_still_falls_back(self):
        missing = Path(self.tmp.name) / "absent.yaml"
        self.assertEqual(arm._load_instance_mailbox(missing), arm._FALLBACK_MAILBOX)
        self.assertEqual(
            arm._load_instance_role_map(missing), arm._FALLBACK_PERSONA_TO_INSTANCE_ROLE
        )
        with self.assertRaises(arm.MembershipSourceMissing):
            arm._load_membership_projection(missing)

    def test_malformed_yaml_not_cached_and_falls_back(self):
        self.path.write_text("roles: [unclosed\n", encoding="utf-8")
        self.assertEqual(arm._load_instance_mailbox(self.path), arm._FALLBACK_MAILBOX)
        self.assertNotIn(os.path.abspath(self.path), arm._MANIFEST_CACHE)

    def test_malformed_after_good_parse_never_serves_old_document(self):
        self.assertEqual(arm._load_instance_mailbox(self.path)["hub"], "/hub/one")
        self.path.write_text("roles: [unclosed\n", encoding="utf-8")
        with self.assertRaises(yaml.YAMLError):
            arm._parse_manifest(self.path)
        self.assertEqual(arm._load_instance_mailbox(self.path), arm._FALLBACK_MAILBOX)


class CachedDocumentCannotBeCorruptedByCallers(unittest.TestCase):
    """The cached document is shared (no per-call deepcopy). Pin that nothing the
    module hands out aliases it: mutate everything the public/loader surface
    returns, then the cache must still equal a fresh SafeLoader parse."""

    def test_mutating_returned_values_leaves_cache_intact(self):
        fresh = yaml.load(REPO_INSTANCE_YAML.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
        outs = [
            arm._load_instance_role_map(REPO_INSTANCE_YAML),
            arm._load_instance_bindings(REPO_INSTANCE_YAML),
            arm._load_instance_mailbox(REPO_INSTANCE_YAML),
            arm._load_membership_projection(REPO_INSTANCE_YAML),
            arm.account_persona_map(),
        ]
        for persona in list(arm._PERSONA_TO_BINDING):
            outs.append(arm.instance_binding(persona))
        for out in outs:
            for k in list(out):
                v = out[k]
                if isinstance(v, dict):
                    v.clear()
                out[k] = "CLOBBERED"
            out["__injected__"] = "x"
        cached = arm._parse_manifest(REPO_INSTANCE_YAML)
        self.assertEqual(_strict(cached), _strict(fresh))

    def test_public_load_instance_document_is_a_mutable_deep_copy(self):
        fresh = yaml.load(REPO_INSTANCE_YAML.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
        doc = arm.load_instance_document(REPO_INSTANCE_YAML)
        self.assertEqual(_strict(doc), _strict(fresh))
        self.assertIsNot(doc, arm._parse_manifest(REPO_INSTANCE_YAML))
        for binding in doc["roles"].values():
            if isinstance(binding, dict):
                binding.clear()
        doc["roles"]["__injected__"] = {"persona": "x"}
        self.assertEqual(_strict(arm._parse_manifest(REPO_INSTANCE_YAML)), _strict(fresh))
        self.assertEqual(_strict(arm.load_instance_document(REPO_INSTANCE_YAML)), _strict(fresh))


if __name__ == "__main__":
    unittest.main()
