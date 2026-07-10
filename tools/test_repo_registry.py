"""test_repo_registry.py — unit tests for tools/repo_registry.py (T-1136).

Tests cover the five failure modes that load_repo_registry must handle:
  1. File absent → None
  2. Valid YAML dict → dict returned
  3. Malformed YAML → None
  4. Valid YAML but non-dict content → None
  5. PyYAML unavailable (ImportError) → None

All tests are hermetic (tmp dirs; no network; no real repos.yaml).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLoadRepoRegistry(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Import must not fail at import time even if yaml is absent (the
    # module uses a lazy import).
    # ------------------------------------------------------------------

    def test_import_succeeds(self) -> None:
        """Module imports cleanly."""
        from tools.repo_registry import load_repo_registry  # noqa: F401

    # ------------------------------------------------------------------
    # Case 1: file absent
    # ------------------------------------------------------------------

    def test_file_absent_returns_none(self) -> None:
        """repos.yaml does not exist → None."""
        from tools.repo_registry import load_repo_registry

        result = load_repo_registry(self.root / "no_such_dir")
        self.assertIsNone(result)

    def test_repos_yaml_absent_returns_none(self) -> None:
        """Root dir exists but repos.yaml not present → None."""
        from tools.repo_registry import load_repo_registry

        result = load_repo_registry(self.root)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Case 2: valid YAML dict
    # ------------------------------------------------------------------

    def test_valid_yaml_returns_dict(self) -> None:
        """Well-formed repos.yaml with dict content → dict returned."""
        import yaml
        from tools.repo_registry import load_repo_registry

        registry = {
            "schema_version": 1,
            "repos": [{"name": "pantheon-config", "grants": {"orchestrator": "rw"}}],
            "always_available": [],
            "ignored": [],
        }
        (self.root / "repos.yaml").write_text(yaml.dump(registry), encoding="utf-8")

        result = load_repo_registry(self.root)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)
        self.assertIn("repos", result)
        self.assertIn("always_available", result)
        self.assertIn("ignored", result)

    def test_valid_yaml_minimal_dict_returns_dict(self) -> None:
        """Minimal dict YAML (no required keys) → dict returned (caller validates schema)."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text("foo: bar\nbaz: 1\n", encoding="utf-8")
        result = load_repo_registry(self.root)
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"foo": "bar", "baz": 1})

    # ------------------------------------------------------------------
    # Case 3: malformed YAML → None
    # ------------------------------------------------------------------

    def test_malformed_yaml_returns_none(self) -> None:
        """YAML with a parse error → None (not a raised exception)."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text(
            ": broken: [\n  unclosed\n", encoding="utf-8"
        )
        result = load_repo_registry(self.root)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Case 4: valid YAML but non-dict content → None
    # ------------------------------------------------------------------

    def test_yaml_list_returns_none(self) -> None:
        """YAML that parses to a list → None."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        result = load_repo_registry(self.root)
        self.assertIsNone(result)

    def test_yaml_scalar_returns_none(self) -> None:
        """YAML that parses to a scalar string → None."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text("just a string\n", encoding="utf-8")
        result = load_repo_registry(self.root)
        self.assertIsNone(result)

    def test_yaml_null_returns_none(self) -> None:
        """Empty file (parses to None / null) → None."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text("", encoding="utf-8")
        result = load_repo_registry(self.root)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Case 5: PyYAML unavailable → None
    # ------------------------------------------------------------------

    def test_yaml_import_error_returns_none(self) -> None:
        """If PyYAML is not importable, return None (don't raise)."""
        from tools.repo_registry import load_repo_registry

        (self.root / "repos.yaml").write_text("key: value\n", encoding="utf-8")

        # Intercept the lazy 'import yaml' inside load_repo_registry by
        # temporarily injecting a broken module into sys.modules.
        real_yaml = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # type: ignore[assignment]
        try:
            result = load_repo_registry(self.root)
        finally:
            if real_yaml is not None:
                sys.modules["yaml"] = real_yaml
            else:
                sys.modules.pop("yaml", None)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
