"""Tests for tools/priority_list_parse.py (T-3020 BT1, ORG-078 §2)."""
import unittest
from pathlib import Path

from tools.priority_list_parse import (
    REPO_ROOT,
    PriorityListParseError,
    parse,
    parse_file,
)


def _wrap(yaml_body: str) -> str:
    """Build a sentinel + ```yaml``` fence-wrapped synthetic markdown text,
    matching the shape used by rulebook/priority-list.md today."""
    return (
        "# preamble\n\n"
        "<!-- PRIORITY-LIST-START -->\n"
        "```yaml\n"
        f"{yaml_body}\n"
        "```\n"
        "<!-- PRIORITY-LIST-END -->\n\n"
        "# trailer\n"
    )


class ParseValid(unittest.TestCase):
    def test_valid_parse_three_ranks_multiple_labels(self):
        text = _wrap(
            "priority-1:\n"
            "  - alpha\n"
            "  - beta\n"
            "priority-2:\n"
            "  - gamma\n"
            "priority-3:\n"
            "  - delta\n"
            "  - epsilon\n"
        )
        self.assertEqual(
            parse(text),
            {"alpha": 1, "beta": 1, "gamma": 2, "delta": 3, "epsilon": 3},
        )

    def test_live_priority_list_md_parses(self):
        live_path = REPO_ROOT / "rulebook" / "priority-list.md"
        result = parse(live_path.read_text(encoding="utf-8"))
        self.assertTrue(result)
        self.assertEqual(result.get("persona-orientation"), 1)
        self.assertEqual(result.get("harness-controller"), 2)
        self.assertEqual(result.get("ticket-flow"), 3)
        self.assertEqual(result.get("concordance"), 4)

    def test_duplicate_label_ceiling_lowest_rank_wins(self):
        text = _wrap(
            "priority-2:\n"
            "  - shared\n"
            "priority-3:\n"
            "  - shared\n"
            "  - other\n"
        )
        result = parse(text)
        self.assertEqual(result["shared"], 2)
        self.assertEqual(result["other"], 3)

    def test_empty_ranked_labels_is_legal_empty_state(self):
        text = _wrap("# no priorities defined yet")
        self.assertEqual(parse(text), {})

    def test_empty_fence_body_is_legal_empty_state(self):
        text = _wrap("")
        self.assertEqual(parse(text), {})


class ParseFailures(unittest.TestCase):
    def test_missing_sentinels_raises(self):
        text = "```yaml\npriority-1:\n  - foo\n```\n"
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("sentinel-missing", str(ctx.exception))

    def test_out_of_order_sentinels_raises_sentinel_missing(self):
        text = (
            "<!-- PRIORITY-LIST-END -->\n"
            "```yaml\npriority-1:\n  - foo\n```\n"
            "<!-- PRIORITY-LIST-START -->\n"
        )
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("sentinel-missing", str(ctx.exception))

    def test_missing_fence_raises_fence_missing(self):
        text = (
            "<!-- PRIORITY-LIST-START -->\n"
            "priority-1:\n  - foo\n"
            "<!-- PRIORITY-LIST-END -->\n"
        )
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("fence-missing", str(ctx.exception))

    def test_unparseable_yaml_raises_yaml_parse_fail(self):
        text = _wrap("priority-1: [unclosed")
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("yaml-parse-fail", str(ctx.exception))

    def test_malformed_top_level_key_raises_schema(self):
        text = _wrap("not-a-priority:\n  - foo\n")
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        msg = str(ctx.exception)
        self.assertTrue("schema" in msg or "priority-N key required" in msg)

    def test_non_list_rank_value_raises(self):
        text = _wrap('priority-1: "not-a-list"')
        with self.assertRaises(PriorityListParseError):
            parse(text)

    def test_top_level_not_a_mapping_raises_schema(self):
        text = _wrap("- foo\n- bar\n")
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("schema", str(ctx.exception))

    def test_non_string_label_entry_raises_schema(self):
        text = _wrap("priority-1:\n  - foo\n  - 42\n")
        with self.assertRaises(PriorityListParseError) as ctx:
            parse(text)
        self.assertIn("schema", str(ctx.exception))


class ParseFile(unittest.TestCase):
    def test_default_path_matches_direct_read(self):
        # Independently derived (not reusing the module's own
        # DEFAULT_PRIORITY_LIST_PATH constant) so this doesn't validate the
        # module's default against itself.
        repo_root = Path(__file__).resolve().parent.parent
        live_path = repo_root / "rulebook" / "priority-list.md"
        expected = parse(live_path.read_text(encoding="utf-8"))
        self.assertEqual(parse_file(), expected)

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(PriorityListParseError) as ctx:
            parse_file("/nonexistent/path/priority-list.md")
        self.assertIn("file-not-found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
