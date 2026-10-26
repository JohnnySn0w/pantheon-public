"""Tests for tools/ace_curate.py (T-2819 PoC, hardened in T-5348).

Covers parse, byte-exact round-trip, ADD / ADD_SECTION / UPDATE / RETIRE /
TAG, the no-silent-drop invariant, Jaccard dedup, malformed-delta rejection
(all-or-nothing), the canonical-path write guard, and the T-5348 additions:
the terminal-id policy (ruling a), the locked-section replace path (ruling b),
the topic-section render, and the store-to-markdown reconcile.

Run: python3 -m pytest tools/test_ace_curate.py
Fixtures are inline (no seat-home or /tmp reads).
"""
import contextlib
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ace_curate as ac  # noqa: E402


@contextlib.contextmanager
def _chronicle_env(root):
    """Point the Chronicle root (tools/chronicle.py precedence) at ``root``."""
    with mock.patch.dict(os.environ, {"PANTHEON_CHRONICLE_ROOT": str(root)}):
        yield

FIXTURE = """\
# UPDATE 2026-10-21 ~01:30Z
- T-5037 PUSHED (agent/T-5037 @d6a24b0). MR mailed, await-merge.
- RULE: security/privacy findings must clear ORG-105.

# UPDATE 2026-10-20 ~08:00Z
- AWAITING MERGE: T-5251 PR1 (MR mailed).
- After the T-3571 session: grep for "text cut: moved=" as the PR2 trigger;
  run the reducer on the tap log.

Persona **artisan**, a paragraph with no list marker.
It runs over two lines.

## Basics
- **Mailbox send:** write the body to a FILE first.
- Gate command:
  - `nice -n 10 echo-cargo.sh fmt --all --check`

```bash
git -C ~/echo-prismatic ls-remote origin agent/T-5164
```
"""


def ids_by_text(store, needle):
    return [it["id"] for it in store["items"] if needle in it["text"]]


def delta(*ops, did="d1", **kw):
    d = {"delta_version": 1, "delta_id": did, "ops": list(ops)}
    d.update(kw)
    return d


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)

    def test_round_trip_is_byte_exact(self):
        self.assertEqual(ac.render(self.store), FIXTURE)

    def test_round_trip_through_json_and_empty_apply_free(self):
        import json
        again = json.loads(json.dumps(self.store))
        self.assertEqual(ac.render(again), FIXTURE)

    def test_block_kinds_and_sections(self):
        kinds = [it["kind"] for it in self.store["items"]]
        self.assertEqual(kinds.count("heading"), 3)
        self.assertEqual(kinds.count("code"), 1)
        self.assertEqual(kinds.count("para"), 1)
        # the continuation line and the indented sub-bullet stay in their parent item
        cont = [it for it in self.store["items"] if "reducer on the tap log" in it["text"]]
        self.assertEqual(len(cont), 1)
        self.assertTrue(cont[0]["text"].startswith("- After the T-3571"))
        sub = [it for it in self.store["items"] if "fmt --all --check" in it["text"]]
        self.assertTrue(sub[0]["text"].startswith("- Gate command:"))
        # every non-heading item points at the heading above it
        basics = ids_by_text(self.store, "## Basics")[0]
        code = [it for it in self.store["items"] if it["kind"] == "code"][0]
        self.assertEqual(code["section"], basics)

    def test_ids_are_stable_and_unique(self):
        again = ac.parse(FIXTURE)
        self.assertEqual([i["id"] for i in self.store["items"]], [i["id"] for i in again["items"]])
        ids = [i["id"] for i in self.store["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_prepended_snapshot_keeps_old_ids(self):
        newer = "# UPDATE 2026-10-22\n- new thing T-9999\n\n" + FIXTURE
        old_ids = {i["id"] for i in self.store["items"]}
        new_ids = {i["id"] for i in ac.parse(newer)["items"]}
        self.assertTrue(old_ids <= new_ids)

    def test_tickets_extracted(self):
        it = [i for i in self.store["items"] if "T-5037" in i["text"]][0]
        self.assertEqual(it["tickets"], ["T-5037"])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.sec = ids_by_text(self.store, "# UPDATE 2026-10-21")[0]
        self.t5251 = ids_by_text(self.store, "AWAITING MERGE: T-5251")[0]

    def test_add_to_existing_section(self):
        new, rep = ac.apply_delta(self.store, delta({"op": "ADD", "section": "UPDATE 2026-10-21 ~01:30Z",
                                                      "text": "T-5314 filed, open in the pool."}))
        self.assertEqual(len(rep["added"]), 1)
        out = ac.render(new)
        self.assertIn("- T-5314 filed, open in the pool.\n", out)
        # inserted inside its section, before the next heading
        self.assertLess(out.index("T-5314"), out.index("# UPDATE 2026-10-20"))
        self.assertIn("ORG-105.\n- T-5314", out)

    def test_add_section_top_then_add(self):
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD_SECTION", "heading": "# UPDATE 2026-10-22 ~06:00Z", "position": "top"},
            {"op": "ADD", "section": "# UPDATE 2026-10-22 ~06:00Z", "text": "T-2819 claimed."}))
        out = ac.render(new)
        self.assertTrue(out.startswith("# UPDATE 2026-10-22 ~06:00Z\n- T-2819 claimed.\n\n# UPDATE 2026-10-21"))
        self.assertEqual(len(rep["added_sections"]), 1)

    def test_update_keeps_history_and_marker(self):
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": self.t5251, "text": "T-5251 RESOLVED (#482).", "reason": "merged"}))
        it = [i for i in new["items"] if i["id"] == self.t5251][0]
        self.assertEqual(it["text"], "- T-5251 RESOLVED (#482).")
        self.assertEqual(it["history"][0]["old_text"], "- AWAITING MERGE: T-5251 PR1 (MR mailed).")
        self.assertEqual(it["history"][0]["reason"], "merged")

    def test_retire_tombstones_with_reason(self):
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "RETIRE", "id": self.t5251, "reason": "T-5251 resolved per tickets/done"}))
        it = [i for i in new["items"] if i["id"] == self.t5251][0]
        self.assertEqual(it["status"], "retired")
        self.assertEqual(it["retired"]["reason"], "T-5251 resolved per tickets/done")
        self.assertNotIn("T-5251", ac.render(new))
        self.assertEqual(rep["retired"][0]["tickets"], ["T-5251"])

    def test_retire_heading_with_live_children_rejected(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.sec, "reason": "old"}))

    def test_retire_whole_section_children_first(self):
        store = ac.parse("# A\n- one T-1\n- two\n\n# B\n- three\n")
        sec = ids_by_text(store, "# A")[0]
        kids = [i["id"] for i in store["items"] if i["section"] == sec and i["id"] != sec]
        ops = [{"op": "RETIRE", "id": k, "reason": "superseded"} for k in kids]
        ops.append({"op": "RETIRE", "id": sec, "reason": "empty section"})
        new, _ = ac.apply_delta(store, delta(*ops))
        self.assertEqual(ac.render(new), "# B\n- three\n")

    def test_retire_parent_with_live_nested_heading_refused(self):
        # LOW r2: '# UPDATE 2026-10-20' has '## Basics' nested under it
        sec = ids_by_text(self.store, "# UPDATE 2026-10-20")[0]
        kids = [i["id"] for i in self.store["items"] if i["section"] == sec and i["id"] != sec]
        ops = [{"op": "RETIRE", "id": k, "reason": "superseded"} for k in kids]
        ops.append({"op": "RETIRE", "id": sec, "reason": "empty section"})
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(self.store, delta(*ops))
        self.assertIn("subsections", str(cm.exception))

    def test_tag_counters(self):
        new, rep = ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5251, "helpful": 2, "harmful": 1}))
        it = [i for i in new["items"] if i["id"] == self.t5251][0]
        self.assertEqual((it["helpful"], it["harmful"]), (2, 1))

    def test_input_store_not_mutated(self):
        before = copy.deepcopy(self.store)
        ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "x"}))
        self.assertEqual(before, self.store)

    def test_same_delta_twice_rejected(self):
        new, _ = ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5251, "helpful": 1}))
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(new, delta({"op": "TAG", "id": self.t5251, "helpful": 1}))

    def test_base_digest_pins_the_store(self):
        good = ac.store_digest(self.store)
        ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5251, "helpful": 1}, base_digest=good))
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5251, "helpful": 1},
                                             base_digest="0" * 16))

    def test_apply_is_deterministic(self):
        d = delta({"op": "ADD", "section": self.sec, "text": "x y z T-1"},
                  {"op": "RETIRE", "id": self.t5251, "reason": "r"})
        a, _ = ac.apply_delta(self.store, d, ts="t")
        b, _ = ac.apply_delta(self.store, d, ts="t")
        self.assertEqual(a, b)


class NoSilentDropTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)

    def test_every_old_item_survives_any_valid_delta(self):
        new, _ = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": "Basics", "text": "a brand new rule"}))
        old_active = {i["id"] for i in self.store["items"]}
        new_active = {i["id"] for i in new["items"] if i["status"] == "active"}
        self.assertTrue(old_active <= new_active)

    def test_checker_catches_deleted_item(self):
        broken = copy.deepcopy(self.store)
        broken["items"].pop(1)
        with self.assertRaises(ac.InvariantError):
            ac.check_no_silent_drop(self.store, broken, set())

    def test_checker_catches_status_flip_without_retire(self):
        broken = copy.deepcopy(self.store)
        broken["items"][1]["status"] = "retired"
        with self.assertRaises(ac.InvariantError):
            ac.check_no_silent_drop(self.store, broken, set())

    def test_checker_catches_retire_without_reason(self):
        broken = copy.deepcopy(self.store)
        broken["items"][1]["status"] = "retired"
        broken["items"][1]["retired"] = {"reason": ""}
        with self.assertRaises(ac.InvariantError):
            ac.check_no_silent_drop(self.store, broken, {broken["items"][1]["id"]})


class LastOpenMentionGuardTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.t5251 = ids_by_text(self.store, "AWAITING MERGE: T-5251")[0]

    def test_warns_when_last_mention_of_open_ticket_retired(self):
        _, rep = ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "merged"}),
                                open_tickets={"T-5251"})
        self.assertEqual(rep["warnings"], [{"kind": "last_open_mention", "ticket": "T-5251",
                                            "by": [self.t5251]}])

    def test_strict_rejects(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "merged"}),
                           open_tickets={"T-5251"}, strict=True)

    def test_update_that_drops_the_id_is_caught(self):
        _, rep = ac.apply_delta(self.store, delta({"op": "UPDATE", "id": self.t5251, "text": "PR1 merged",
                                                  "reason": "merged"}),
                                open_tickets={"T-5251"})
        w = [x for x in rep["warnings"] if x["kind"] == "last_open_mention"]
        self.assertEqual(w[0]["by"], [self.t5251])

    def test_quiet_when_another_bullet_still_mentions_it(self):
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": "Basics", "text": "T-5251 follow-up lives on the ticket"},
            {"op": "RETIRE", "id": self.t5251, "reason": "merged"}), open_tickets={"T-5251"})
        self.assertEqual(rep["warnings"], [])

    def test_quiet_for_terminal_tickets_and_without_input(self):
        _, rep = ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "merged"}),
                                open_tickets={"T-9999"})
        self.assertEqual(rep["warnings"], [])
        _, rep = ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "merged"}))
        self.assertEqual(rep["warnings"], [])

    def test_add_then_retire_in_same_delta_is_caught(self):
        sec = ids_by_text(self.store, "# UPDATE 2026-10-20")[0]
        text = "- T-7070 follow-up filed to orchestrator"
        new_id = "b-" + ac._sha(sec + "\0" + text)
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": sec, "text": text},
            {"op": "RETIRE", "id": new_id, "reason": "parent done"}), open_tickets={"T-7070"})
        self.assertEqual([w["ticket"] for w in rep["warnings"]], ["T-7070"])


class UpdateGuardTest(unittest.TestCase):
    """r2 M1: UPDATE must not be a way around no-silent-drop."""

    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.t5037 = ids_by_text(self.store, "T-5037 PUSHED")[0]
        self.after = ids_by_text(self.store, "After the T-3571")[0]

    def test_update_needs_reason(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "UPDATE", "id": self.t5037, "text": "x y"}))
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "UPDATE", "id": self.t5037, "text": "x y", "reason": " "}))

    def test_large_shrink_flagged(self):
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": self.t5037, "text": "T-5037 merged.", "reason": "merged"}))
        kinds = {w["kind"] for w in rep["warnings"]}
        self.assertIn("update_shrink", kinds)

    def test_small_rewording_not_flagged(self):
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": self.t5037,
             "text": "T-5037 PUSHED (agent/T-5037 @d6a24b0). MR mailed; await-merge now.", "reason": "wording"}))
        self.assertEqual(rep["warnings"], [])

    def test_token_loss_flagged(self):
        # drops the quoted command and the T-3571 id while staying long
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": self.after, "reason": "trim",
             "text": "After the wet session: check the journal as the PR2 trigger;\n  run the reducer on the tap log."}))
        loss = [w for w in rep["warnings"] if w["kind"] == "update_token_loss"]
        self.assertEqual(len(loss), 1)
        self.assertIn("T-3571", loss[0]["lost"])
        _, rep = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": self.after, "reason": "trim",
             "text": "After the wet session: check the journal as the PR2 trigger;\n  run the reducer on the tap log."}),
            open_tickets={"T-3571"})
        loss = [w for w in rep["warnings"] if w["kind"] == "update_token_loss"]
        self.assertEqual(loss[0]["lost_open_tickets"], ["T-3571"])

    def test_path_and_code_tokens_tracked(self):
        toks = ac.load_bearing_tokens("run `ls -la` in ~/artisan-drafts/t1/x.py and tools/ticket.py")
        self.assertIn("`ls -la`", toks)
        self.assertIn("~/artisan-drafts/t1/x.py", toks)
        self.assertIn("ticket.py", toks)

    def test_strict_rejects_update_flags(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta(
                {"op": "UPDATE", "id": self.t5037, "text": "T-5037 merged.", "reason": "merged"}),
                open_tickets=set(), strict=True)

    def test_strict_needs_open_set(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5037, "helpful": 1}), strict=True)
        ac.apply_delta(self.store, delta({"op": "TAG", "id": self.t5037, "helpful": 1}),
                       open_tickets=set(), strict=True)


class LockedRegionTest(unittest.TestCase):
    """r2 M2: ORG-074 amendment gate 1."""

    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.rule = ids_by_text(self.store, "RULE: security")[0]
        self.basics_item = ids_by_text(self.store, "**Mailbox send:**")[0]
        self.basics_head = ids_by_text(self.store, "## Basics")[0]

    def test_update_and_retire_of_locked_items_rejected(self):
        for iid in (self.rule, self.basics_item, self.basics_head):
            with self.assertRaises(ac.DeltaError, msg=iid) as cm:
                ac.apply_delta(self.store, delta({"op": "RETIRE", "id": iid, "reason": "old"}))
            self.assertIn("locked", str(cm.exception))
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "UPDATE", "id": self.rule, "text": "RULE: x", "reason": "r"}))

    def test_lesson_item_locked_and_add_into_locked_section_allowed(self):
        store = ac.parse("# U\n- Lesson: subagents cannot sudo.\n- plain T-1\n")
        lesson = ids_by_text(store, "Lesson:")[0]
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(store, delta({"op": "RETIRE", "id": lesson, "reason": "old"}))
        new, rep = ac.apply_delta(self.store, delta({"op": "ADD", "section": "Basics", "text": "new how-to"}))
        self.assertEqual(len(rep["added"]), 1)

    def test_heading_that_merely_mentions_rule_is_not_locked(self):
        store = ac.parse("# UPDATE: the weekend-off rule starts next week\n- T-1 parked\n")
        iid = ids_by_text(store, "T-1 parked")[0]
        ac.apply_delta(store, delta({"op": "RETIRE", "id": iid, "reason": "done"}))

    def test_locks_are_configurable(self):
        locks = {"sections": [], "items": [r"^- plain"]}
        store = ac.parse("# Basics\n- plain T-1\n- other\n")
        other = ids_by_text(store, "other")[0]
        ac.apply_delta(store, delta({"op": "RETIRE", "id": other, "reason": "x"}), locks=locks)
        plain = ids_by_text(store, "plain")[0]
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(store, delta({"op": "RETIRE", "id": plain, "reason": "x"}), locks=locks)


class LowFixesTest(unittest.TestCase):
    """r2 LOW items."""

    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.t5251 = ids_by_text(self.store, "AWAITING MERGE: T-5251")[0]

    def test_multiline_text_with_heading_rejected(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "ADD", "section": "Basics", "text": "x\n# Smuggled heading"}))
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "UPDATE", "id": self.t5251, "reason": "r",
                                              "text": "- AWAITING MERGE: T-5251 PR1\n## New section"}))
        heading = ids_by_text(self.store, "# UPDATE 2026-10-21")[0]
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "UPDATE", "id": heading, "reason": "r",
                                              "text": "# UPDATE 2026-10-21\n# second"}))

    def test_prepended_duplicate_heading_keeps_old_ids(self):
        base = "## Status\n- a T-1\n\n## Status\n- b T-2\n"
        newer = "## Status\n- c T-3\n\n" + base
        old_ids = [i["id"] for i in ac.parse(base)["items"]]
        new_ids = [i["id"] for i in ac.parse(newer)["items"]]
        self.assertEqual(new_ids[-len(old_ids):], old_ids)
        self.assertEqual(len(set(new_ids)), len(new_ids))
        self.assertEqual(ac.render(ac.parse(newer)), newer)

    def test_tag_on_retired_refused(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, delta({"op": "RETIRE", "id": self.t5251, "reason": "r"},
                                             {"op": "TAG", "id": self.t5251, "helpful": 1}))

    def test_path_allowlist(self):
        # T-5348: the allowlist is the explicit scratch roots plus the Chronicle
        # root; the tool's own dir (tools/) is no longer a write root
        repo = Path(ac.PKG_DIR).parent
        with tempfile.TemporaryDirectory() as td, _chronicle_env(Path(td, "chron")):
            ac.check_output_path(Path(td, "out", "x.json"), [td])
            refused = ["/home/someone/out.json", "/etc/x.json", str(Path(td).parent / "elsewhere.json"),
                       str(Path(ac.PKG_DIR, "replay", "x.json")),
                       str(repo / "tools" / "x.json"), str(repo / "ledger" / "x.jsonl"),
                       str(repo / "rulebook" / "x.md"), str(repo / "architecture" / "adr" / "ORG-1.md")]
            for bad in refused:
                with self.assertRaises(PermissionError, msg=bad):
                    ac.check_output_path(bad, [td])

    def test_denylist_applies_inside_scratch_root(self):
        # a pantheon-config checkout living under the scratch root is still protected
        with tempfile.TemporaryDirectory() as td:
            for rel in ("pc/ledger/2026/x.jsonl", "pc/rulebook/p.md", "pc/architecture/adr/ORG-9.md",
                        "pc/notes/n.md", "pc/sop/s.md", "h/.claude/projects/p/memory/m.md",
                        "pc/CLAUDE.md", "pc/tools/README.md", "HANDOFF.md", "MEMORY.md",
                        "pc/tools/ace_curate.py", "pc/tools/x.json", "pc/tickets/active/T-1.md",
                        "pc/tickets/done/x.json"):
                with self.assertRaises(PermissionError, msg=rel):
                    ac.check_output_path(Path(td, rel), [td])
            ac.check_output_path(Path(td, "pc", "knowledge", "x.json"), [td])

    def test_cli_malformed_json_and_strict_without_open_exit_2(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            sp, dp = Path(td, "s.json"), Path(td, "d.json")
            sp.write_text(json.dumps(self.store))
            dp.write_text("{not json")
            self.assertEqual(ac.main(["apply", "--scratch-root", str(td), "--store", str(sp), "--delta", str(dp),
                                      "--out", str(Path(td, "o.json"))]), 2)
            dp.write_text(json.dumps(delta({"op": "TAG", "id": self.t5251, "helpful": 1})))
            self.assertEqual(ac.main(["apply", "--scratch-root", str(td), "--store", str(sp), "--delta", str(dp), "--strict",
                                      "--out", str(Path(td, "o.json"))]), 2)
            ot = Path(td, "open.txt")
            ot.write_text("")
            self.assertEqual(ac.main(["apply", "--scratch-root", str(td), "--store", str(sp), "--delta", str(dp), "--strict",
                                      "--open-tickets", str(ot), "--out", str(Path(td, "o.json"))]), 0)


class ReemittedSectionTest(unittest.TestCase):
    """r3: a rewrite re-emitting a heading the store already holds merges."""

    def test_add_section_existing_heading_merges(self):
        store = ac.parse(FIXTURE)
        new, rep = ac.apply_delta(store, delta(
            {"op": "ADD_SECTION", "heading": "## Basics", "position": "top"},
            {"op": "ADD", "section": "Basics", "text": "- **New how-to:** run the thing."}))
        self.assertEqual(len(rep["merged_sections"]), 1)
        self.assertEqual(rep["added_sections"], [])
        out = ac.render(new)
        self.assertEqual(out.count("## Basics"), 1)
        self.assertIn("New how-to", out)
        self.assertLess(out.index("## Basics"), out.index("New how-to"))

    def test_lock_pattern_case_insensitive(self):
        store = ac.parse("# U\n- Rule: never --no-verify.\n- rules to remember: x\n- plain\n")
        for needle in ("Rule:", "rules to remember"):
            iid = ids_by_text(store, needle)[0]
            with self.assertRaises(ac.DeltaError, msg=needle):
                ac.apply_delta(store, delta({"op": "RETIRE", "id": iid, "reason": "x"}))


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)

    def test_jaccard_basics(self):
        self.assertEqual(ac.jaccard("A b c", "a, B; c!"), 1.0)
        self.assertLess(ac.jaccard("alpha beta", "gamma delta"), 0.01)
        self.assertIn("t-5037", ac.tokens("see T-5037."))

    def test_near_identical_add_is_deduped(self):
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": "Basics",
             "text": "RULE: security/privacy findings must clear ORG-105"}))
        self.assertEqual(rep["added"], [])
        self.assertEqual(len(rep["deduped"]), 1)
        matched = [i for i in new["items"] if i["id"] == rep["deduped"][0]["matched"]][0]
        self.assertEqual(matched["seen"], 2)
        self.assertEqual(ac.render(new), FIXTURE)

    def test_dedup_within_one_delta(self):
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": "Basics", "text": "Disk at 88 percent, run gc before push"},
            {"op": "ADD", "section": "Basics", "text": "disk at 88 percent: run gc before push!"}))
        self.assertEqual((len(rep["added"]), len(rep["deduped"])), (1, 1))

    def test_distinct_add_not_deduped_and_threshold_is_tunable(self):
        text = "RULE: security findings must clear ORG-105 first"
        _, rep = ac.apply_delta(self.store, delta({"op": "ADD", "section": "Basics", "text": text}))
        self.assertEqual(len(rep["added"]), 1)
        _, rep2 = ac.apply_delta(self.store, delta({"op": "ADD", "section": "Basics", "text": text}),
                                 dedup_threshold=0.5)
        self.assertEqual(len(rep2["deduped"]), 1)


class MalformedDeltaTest(unittest.TestCase):
    def setUp(self):
        self.store = ac.parse(FIXTURE)
        self.anyid = ids_by_text(self.store, "AWAITING MERGE")[0]

    def assertRejected(self, d):
        before = copy.deepcopy(self.store)
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, d)
        self.assertEqual(before, self.store)

    def test_shape_errors(self):
        self.assertRejected([])
        self.assertRejected({"delta_version": 2, "delta_id": "x", "ops": [{"op": "TAG"}]})
        self.assertRejected({"delta_version": 1, "ops": [{"op": "TAG", "id": self.anyid, "helpful": 1}]})
        self.assertRejected(delta())
        self.assertRejected(delta("ADD"))
        self.assertRejected(delta({"op": "DELETE", "id": self.anyid}))

    def test_retire_needs_reason(self):
        self.assertRejected(delta({"op": "RETIRE", "id": self.anyid}))
        self.assertRejected(delta({"op": "RETIRE", "id": self.anyid, "reason": "   "}))

    def test_unknown_or_retired_ids(self):
        self.assertRejected(delta({"op": "UPDATE", "id": "b-nope", "text": "x", "reason": "r"}))
        self.assertRejected(delta({"op": "RETIRE", "id": self.anyid, "reason": "a"},
                                  {"op": "RETIRE", "id": self.anyid, "reason": "b"}))
        self.assertRejected(delta({"op": "RETIRE", "id": self.anyid, "reason": "a"},
                                  {"op": "UPDATE", "id": self.anyid, "text": "zombie", "reason": "r"}))

    def test_bad_add_and_section_refs(self):
        self.assertRejected(delta({"op": "ADD", "section": "No such section", "text": "x"}))
        self.assertRejected(delta({"op": "ADD", "section": "Basics", "text": ""}))
        self.assertRejected(delta({"op": "ADD_SECTION", "heading": "not a heading"}))
        self.assertRejected(delta({"op": "ADD_SECTION", "heading": "# X", "position": "middle"}))

    def test_bad_tag(self):
        self.assertRejected(delta({"op": "TAG", "id": self.anyid}))
        self.assertRejected(delta({"op": "TAG", "id": self.anyid, "helpful": -1}))
        self.assertRejected(delta({"op": "TAG", "id": self.anyid, "helpful": True}))

    def test_all_or_nothing(self):
        # a valid first op followed by an invalid one leaves the store untouched
        self.assertRejected(delta({"op": "ADD", "section": "Basics", "text": "fine"},
                                  {"op": "RETIRE", "id": "b-missing", "reason": "r"}))

    def test_cli_rejects_with_exit_2(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            sp, dp = Path(td, "s.json"), Path(td, "d.json")
            sp.write_text(json.dumps(self.store))
            dp.write_text(json.dumps(delta({"op": "RETIRE", "id": self.anyid})))
            rc = ac.main(["apply", "--scratch-root", str(td), "--store", str(sp), "--delta", str(dp), "--out", str(Path(td, "o.json"))])
            self.assertEqual(rc, 2)
            self.assertFalse(Path(td, "o.json").exists())


class OutputGuardAndCliTest(unittest.TestCase):
    def test_canonical_paths_refused(self):
        for bad in ("/x/HANDOFF.md", "/x/handoff.md", "/x/MEMORY.md", "/x/CLAUDE.md", "/r/ledger/a.json",
                    "/r/rulebook/p.md", "/r/sop/x.md", "/r/notes/n.md", "/r/architecture/adr/x.md",
                    "/h/.claude/projects/p/memory/x.md"):
            with self.assertRaises(PermissionError, msg=bad):
                ac.check_output_path(bad, ["/x", "/r", "/h"])
        ac.check_output_path("/tmp/x/proposed-handoff.md", ["/tmp/x"])

    def test_cli_propose_end_to_end(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td, "input.md")
            hp.write_text(FIXTURE)
            sp = Path(td, "store.json")
            self.assertEqual(ac.main(["parse", "--scratch-root", str(td), str(hp), "--out", str(sp)]), 0)
            store = json.loads(sp.read_text())
            tid = ids_by_text(store, "AWAITING MERGE")[0]
            dp = Path(td, "delta.json")
            dp.write_text(json.dumps(delta(
                {"op": "ADD_SECTION", "heading": "# UPDATE 2026-10-22"},
                {"op": "ADD", "section": "UPDATE 2026-10-22", "text": "T-5251 merged #482"},
                {"op": "RETIRE", "id": tid, "reason": "merged, see the 10-22 bullet"})))
            out = Path(td, "out")
            out.mkdir()
            self.assertEqual(ac.main(["propose", "--scratch-root", str(td), "--store", str(sp), "--delta", str(dp),
                                      "--out-dir", str(out)]), 0)
            review = Path(out, "review.md").read_text()
            self.assertIn("REASON: merged, see the 10-22 bullet", review)
            self.assertIn("-- AWAITING MERGE: T-5251", review)
            proposed = Path(out, "proposed-handoff.md").read_text()
            self.assertTrue(proposed.startswith("# UPDATE 2026-10-22\n- T-5251 merged #482\n\n# UPDATE 2026-10-21"))
            # input untouched
            self.assertEqual(hp.read_text(), FIXTURE)
            # refuses to write a HANDOFF.md even inside a scratch dir
            self.assertEqual(ac.main(["render", "--scratch-root", str(td), "--store", str(sp), "--out", str(Path(td, "HANDOFF.md"))]), 2)
            self.assertFalse(Path(td, "HANDOFF.md").exists())
            # without --scratch-root, the temp dir is not an allowed destination
            self.assertEqual(ac.main(["render", "--store", str(sp), "--out", str(Path(td, "r.md"))]), 2)
            self.assertFalse(Path(td, "r.md").exists())


# --------------------------------------------------------------- T-5348 additions
TERMINAL_FIX = "# U\n- T-100 and T-200 both waiting on the merge queue for the echo push.\n"
# drops T-100, keeps the length (so update_shrink stays quiet) and every other token
TERMINAL_EDIT = "- T-200 still waiting on the merge queue for the echo push; other done."


def _tickets_root(td, done=(), active=()):
    root = Path(td, "tickets")
    for sub, ids in (("done", done), ("active", active)):
        (root / sub).mkdir(parents=True, exist_ok=True)
        for t in ids:
            (root / sub / f"{t}.md").write_text(f"---\nid: {t}\n---\n")
    (root / "done" / "README.md").write_text("not a ticket\n")
    return root


class TerminalIdPolicyTest(unittest.TestCase):
    """T-2819 ruling (a): update_token_loss exempts ids whose ticket is done."""

    def setUp(self):
        self.store = ac.parse(TERMINAL_FIX)
        self.bid = ids_by_text(self.store, "T-100 and")[0]
        self.d = delta({"op": "UPDATE", "id": self.bid, "text": TERMINAL_EDIT, "reason": "T-100 closed"})

    def test_dropping_done_id_passes_strict(self):
        new, rep = ac.apply_delta(self.store, self.d, open_tickets={"T-200"}, strict=True,
                                  done_tickets={"T-100"})
        self.assertEqual(rep["warnings"], [])
        self.assertEqual(rep["exempt_terminal"], [{"id": self.bid, "dropped": ["T-100"]}])
        self.assertNotIn("T-100", ac.render(new))

    def test_dropping_open_id_rejected(self):
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(self.store, self.d, open_tickets={"T-100", "T-200"}, strict=True,
                           done_tickets=set())
        self.assertIn("update_token_loss", str(cm.exception))

    def test_dropping_unknown_id_rejected(self):
        # T-100 is neither open nor done: unknown stays strict
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(self.store, self.d, open_tickets={"T-200"}, strict=True, done_tickets={"T-999"})
        self.assertIn("update_token_loss", str(cm.exception))

    def test_id_both_open_and_done_stays_strict(self):
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, self.d, open_tickets={"T-100"}, strict=True, done_tickets={"T-100"})

    def test_other_token_loss_still_rejected(self):
        # dropping a done id AND a path: the path loss alone still rejects
        store = ac.parse("# U\n- T-100 done; notes in tools/ticket.py for the merge queue run.\n")
        bid = ids_by_text(store, "T-100 done")[0]
        d = delta({"op": "UPDATE", "id": bid, "reason": "trim",
                   "text": "- closed; notes elsewhere now for the merge queue run, nothing left."})
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(store, d, open_tickets=set(), strict=True, done_tickets={"T-100"})
        self.assertIn("ticket.py", str(cm.exception))
        self.assertNotIn("'T-100'", str(cm.exception))

    def test_done_set_read_from_tickets_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tickets_root(td, done=["T-100"], active=["T-200"])
            self.assertEqual(ac.load_done_tickets(root), {"T-100"})
            _, rep = ac.apply_delta(self.store, self.d, open_tickets={"T-200"}, strict=True, tickets_root=root)
            self.assertEqual(rep["warnings"], [])
            # read at check time: the same root, T-100 moved back out of done -> strict again
            Path(root, "done", "T-100.md").unlink()
            with self.assertRaises(ac.DeltaError):
                ac.apply_delta(self.store, self.d, open_tickets={"T-200"}, strict=True, tickets_root=root)

    def test_without_done_set_behaviour_unchanged(self):
        _, rep = ac.apply_delta(self.store, self.d)
        self.assertEqual([w["kind"] for w in rep["warnings"]], ["update_token_loss"])
        self.assertEqual(rep["warnings"][0]["lost"], ["T-100"])

    def test_review_lists_exempt_drops(self):
        new, rep = ac.apply_delta(self.store, self.d, done_tickets={"T-100"})
        self.assertIn("T-100", ac.review_markdown(self.store, new, rep).split("## Terminal ids dropped")[1])

    def test_cli_tickets_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tickets_root(td, done=["T-100"])
            sp, dp, ot = Path(td, "s.json"), Path(td, "d.json"), Path(td, "open.txt")
            sp.write_text(json.dumps(self.store))
            dp.write_text(json.dumps(self.d))
            ot.write_text("T-200\n")
            base = ["apply", "--scratch-root", td, "--store", str(sp), "--delta", str(dp), "--strict",
                    "--open-tickets", str(ot), "--out", str(Path(td, "o.json"))]
            self.assertEqual(ac.main(base), 2)
            self.assertEqual(ac.main(base + ["--tickets-root", str(root)]), 0)
            df = Path(td, "done.txt")
            df.write_text("T-100\n")
            self.assertEqual(ac.main(base + ["--done-tickets", str(df)]), 0)


LOCKED_FIX = """\
# UPDATE 2026-10-21
- T-5037 pushed.

## Basics
- **Mailbox send:** write the body to a FILE first.
- Gate: `nice -n 10 echo-cargo.sh fmt`

# UPDATE 2026-10-20
- older note T-4000
"""


class LockedSectionReplaceTest(unittest.TestCase):
    """T-2819 ruling (b): same-heading restatement of a locked section replaces it."""

    BODY = "- **Mailbox send:** write the body to a file, then send.\n- Disk: run gc after every merge.\n"

    def setUp(self):
        self.store = ac.parse(LOCKED_FIX)
        self.head = ids_by_text(self.store, "## Basics")[0]
        self.old_kids = [i["id"] for i in self.store["items"] if i["section"] == self.head and i["id"] != self.head]

    def replace(self, heading, body=None, store=None, did="d1", **kw):
        op = {"op": "ADD_SECTION", "heading": heading}
        if body is not None:
            op["body"] = body
        return ac.apply_delta(store or self.store, delta(op, did=did), **kw)

    def test_same_heading_replaces_and_logs_bytes(self):
        new, rep = self.replace("## Basics", self.BODY)
        out = ac.render(new)
        self.assertIn("run gc after every merge", out)
        self.assertNotIn("FILE first", out)
        self.assertNotIn("echo-cargo.sh fmt", out)
        self.assertEqual(out.count("## Basics"), 1)
        self.assertLess(out.index("## Basics"), out.index("run gc"))
        self.assertLess(out.index("run gc"), out.index("# UPDATE 2026-10-20"))
        # byte counts are of the section body (blank lines before the next heading excluded)
        old_bytes = len("- **Mailbox send:** write the body to a FILE first.\n"
                        "- Gate: `nice -n 10 echo-cargo.sh fmt`\n".encode())
        r = rep["replaced_sections"]
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["id"], r[0]["old_bytes"], r[0]["new_bytes"]),
                         (self.head, old_bytes, len(self.BODY.encode())))
        review = ac.review_markdown(self.store, new, rep)
        self.assertIn(f"old {old_bytes} B -> new {len(self.BODY.encode())} B", review)

    def test_old_items_tombstoned_with_reason_not_deleted(self):
        new, rep = self.replace("## Basics", self.BODY)
        by_id = {i["id"]: i for i in new["items"]}
        for k in self.old_kids:
            self.assertEqual(by_id[k]["status"], "retired")
            self.assertIn("ruling b", by_id[k]["retired"]["reason"])
        self.assertEqual(sorted(r["id"] for r in rep["retired"]), sorted(self.old_kids))

    def test_case_and_trailing_parenthetical_count_as_replace(self):
        for heading in ("## basics", "## BASICS", "## Basics (unchanged)", "### Basics (carried, see above)",
                        "## basics  (Unchanged) "):
            new, rep = self.replace(heading, self.BODY)
            self.assertEqual(len(rep["replaced_sections"]), 1, heading)
            self.assertEqual(rep["added_sections"], [], heading)
            out = ac.render(new)
            self.assertEqual(out.lower().count("basics"), 1, heading)
            self.assertIn("run gc after every merge", out, heading)

    def test_other_heading_is_a_new_section(self):
        for heading in ("## Basics extra", "## Basics (a) (b)", "## Old Basics", "## Base"):
            new, rep = self.replace(heading, "- new item here\n")
            self.assertEqual(rep["replaced_sections"], [], heading)
            self.assertEqual(len(rep["added_sections"]), 1, heading)
            out = ac.render(new)
            self.assertIn("FILE first", out, heading)
            self.assertIn("new item here", out, heading)

    def test_latest_wins(self):
        mid, _ = self.replace("## Basics", self.BODY, did="d1")
        new, rep = self.replace("## Basics (unchanged)", "- third version only.\n", store=mid, did="d2")
        out = ac.render(new)
        self.assertIn("third version only", out)
        self.assertNotIn("run gc after every merge", out)
        self.assertEqual(rep["replaced_sections"][0]["old_bytes"], len(self.BODY.encode()))

    def test_never_removed_by_omission(self):
        # a re-emitted locked heading with no body is a merge no-op
        new, rep = self.replace("## Basics")
        self.assertEqual(ac.render(new), LOCKED_FIX)
        self.assertEqual(rep["replaced_sections"], [])
        self.assertEqual(len(rep["merged_sections"]), 1)
        # an empty body does not count as a restatement either
        with self.assertRaises(ac.DeltaError):
            self.replace("## Basics", "   \n")
        # a delta that never mentions the locked section leaves it in place
        other = ids_by_text(self.store, "older note")[0]
        new, _ = ac.apply_delta(self.store, delta({"op": "RETIRE", "id": other, "reason": "done"}))
        self.assertIn("## Basics\n- **Mailbox send:** write the body to a FILE first.", ac.render(new))

    def test_identical_restatement_is_noop(self):
        body = "- **Mailbox send:** write the body to a FILE first.\n- Gate: `nice -n 10 echo-cargo.sh fmt`\n"
        new, rep = self.replace("## Basics (unchanged)", body)
        self.assertEqual(ac.render(new), LOCKED_FIX)
        self.assertEqual(rep["retired"], [])
        self.assertEqual(len(rep["merged_sections"]), 1)

    def test_replace_dropping_last_open_mention_rejected_in_strict(self):
        store = ac.parse("# Rules\n- RULE: T-777 stays parked until merge.\n")
        with self.assertRaises(ac.DeltaError) as cm:
            self.replace("# Rules", "- RULE: no parking.\n", store=store, open_tickets={"T-777"}, strict=True)
        self.assertIn("last_open_mention", str(cm.exception))

    def test_body_with_heading_rejected(self):
        with self.assertRaises(ac.DeltaError):
            self.replace("## Basics", "- a\n## Smuggled\n- b\n")

    def test_new_unlocked_section_with_body(self):
        new, rep = self.replace("# UPDATE 2026-10-22", "- T-5348 claimed.\n- second line.\n")
        out = ac.render(new)
        self.assertTrue(out.startswith("# UPDATE 2026-10-22\n- T-5348 claimed.\n- second line.\n\n# UPDATE 2026-10-21"))

    def test_add_naming_the_restated_heading_lands_in_merged_section(self):
        # replay regression (agent-state-history): ADD_SECTION 'Basics (unchanged)' merges
        # into the locked 'Basics', then ADD section='Basics (unchanged)' must resolve to it.
        # ADDs append (grow); only an ADD_SECTION body replaces.
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD_SECTION", "heading": "## Basics (unchanged)", "position": "top"},
            {"op": "ADD", "section": "Basics (unchanged)", "text": "- Disk: run gc after every merge.",
             "as_list": False},
            {"op": "ADD", "section": "## basics", "text": "- Another basic."}))
        self.assertEqual(len(rep["merged_sections"]), 1)
        self.assertEqual([r["section"] for r in rep["added"]], [self.head, self.head])
        out = ac.render(new)
        self.assertEqual(out.lower().count("basics"), 1)
        self.assertIn("FILE first", out)  # appended, nothing replaced or dropped
        self.assertLess(out.index("echo-cargo.sh fmt"), out.index("run gc after every merge"))
        self.assertLess(out.index("Another basic"), out.index("# UPDATE 2026-10-20"))

    def test_section_ref_fallback_keeps_exact_one_rule(self):
        store = ac.parse("# Rules (a)\n- x\n\n# rules (b)\n- y\n")
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(store, delta({"op": "ADD", "section": "Rules", "text": "gamma three"}))
        self.assertIn("matches 2", str(cm.exception))
        # an exact match still wins over the normalised fallback
        store = ac.parse("# Status\n- alpha one\n\n# Status (old)\n- beta two\n")
        new, rep = ac.apply_delta(store, delta({"op": "ADD", "section": "Status", "text": "gamma three"}))
        exact = [i["id"] for i in store["items"] if i["text"] == "# Status"]
        self.assertEqual(rep["added"][0]["section"], exact[0])

    def test_section_ref_fallback_is_locked_only(self):
        # r2 L2: an ADD naming 'Status (v2)' before its ADD_SECTION must not land in 'Status'
        store = ac.parse("## Status\n- alpha one\n")
        with self.assertRaises(ac.DeltaError) as cm:
            ac.apply_delta(store, delta({"op": "ADD", "section": "Status (v2)", "text": "beta two"},
                                        {"op": "ADD_SECTION", "heading": "## Status (v2)"}))
        self.assertIn("matches 0", str(cm.exception))
        # in the right order it lands in the new section
        new, rep = ac.apply_delta(store, delta({"op": "ADD_SECTION", "heading": "## Status (v2)"},
                                               {"op": "ADD", "section": "Status (v2)", "text": "beta two"}))
        self.assertEqual(rep["added"][0]["section"], rep["added_sections"][0]["id"])

    def test_replace_advisory_never_rejects(self):
        # r2 L1: a replace that drops a non-done ticket id, or shrinks below 50%, is flagged
        # (review + warnings) but never rejected, even under strict
        store = ac.parse("# Rules\n- RULE: T-777 and T-778 both follow the long merge discipline here.\n"
                         "- RULE: second long rule about pushing through the evidence wrapper always.\n")
        new, rep = ac.apply_delta(store, delta({"op": "ADD_SECTION", "heading": "# Rules",
                                                "body": "- RULE: T-778 short.\n"}),
                                  open_tickets=set(), strict=True, done_tickets=set())
        adv = [w for w in rep["warnings"] if w["kind"] == "locked_replace_advisory"]
        self.assertEqual(len(adv), 1)
        self.assertEqual(adv[0]["dropped_tickets"], ["T-777"])
        self.assertLess(adv[0]["ratio"], 0.5)
        self.assertIn("locked_replace_advisory", ac.review_markdown(store, new, rep))
        self.assertIn("T-778 short", ac.render(new))
        # dropping only a done id at a similar size: no advisory
        body = ("- RULE: T-778 both follow the long merge discipline here, unchanged.\n"
                "- RULE: second long rule about pushing through the evidence wrapper always.\n")
        _, rep = ac.apply_delta(store, delta({"op": "ADD_SECTION", "heading": "# Rules", "body": body}),
                                done_tickets={"T-777"})
        self.assertEqual(rep["warnings"], [])

    def test_locked_items_outside_locked_sections_still_locked(self):
        store = ac.parse("# U\n- RULE: x stays.\n")
        rid = ids_by_text(store, "RULE: x")[0]
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(store, delta({"op": "RETIRE", "id": rid, "reason": "r"}))


TOPIC_FIX = """\
Preamble line.

# UPDATE 1
- a T-1

## Lessons
- lesson one

# UPDATE 0
- b

# gotchas
- g

## Rules
- r"""


class TopicRenderTest(unittest.TestCase):
    def test_default_render_unchanged(self):
        self.assertEqual(ac.render(ac.parse(TOPIC_FIX)), TOPIC_FIX)
        self.assertEqual(ac.render(ac.parse(FIXTURE)), FIXTURE)

    def test_durable_sections_pinned_first_rest_in_order(self):
        out = ac.render(ac.parse(TOPIC_FIX), topic=True)
        self.assertEqual(out, "Preamble line.\n\n## Lessons\n- lesson one\n\n# gotchas\n- g\n\n## Rules\n- r\n\n"
                              "# UPDATE 1\n- a T-1\n\n# UPDATE 0\n- b")

    def test_nested_basics_pulled_out(self):
        out = ac.render(ac.parse(FIXTURE), topic=True)
        self.assertTrue(out.startswith("## Basics\n"), out[:40])
        self.assertIn("```bash", out.split("# UPDATE 2026-10-21")[0])
        self.assertEqual(sorted(out.splitlines()), sorted(FIXTURE.splitlines()))

    def test_deterministic_and_idempotent(self):
        store = ac.parse(TOPIC_FIX)
        once = ac.render(store, topic=True)
        self.assertEqual(once, ac.render(copy.deepcopy(store), topic=True))
        self.assertEqual(ac.render(ac.parse(once), topic=True), once)
        self.assertEqual(ac.render(ac.parse(once)), once)

    def test_already_pinned_or_no_locks_is_identity(self):
        doc = "# Basics\n- x\n\n# UPDATE\n- y\n"
        self.assertEqual(ac.render(ac.parse(doc), topic=True), doc)
        doc = "# UPDATE: the rule change\n- y\n# U2\n- z\n"
        self.assertEqual(ac.render(ac.parse(doc), topic=True), doc)

    def test_topic_uses_configured_locks(self):
        out = ac.render(ac.parse("# A\n- a\n\n# Pinned\n- p\n"), topic=True,
                        locks={"sections": [r"^Pinned"], "items": []})
        self.assertEqual(out, "# Pinned\n- p\n\n# A\n- a\n")

    def test_cli_render_topic(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td, "s.json")
            sp.write_text(json.dumps(ac.parse(TOPIC_FIX)))
            self.assertEqual(ac.main(["render", "--scratch-root", td, "--store", str(sp),
                                      "--out", str(Path(td, "d.md"))]), 0)
            self.assertEqual(Path(td, "d.md").read_text(), TOPIC_FIX)
            self.assertEqual(ac.main(["render", "--scratch-root", td, "--topic", "--store", str(sp),
                                      "--out", str(Path(td, "t.md"))]), 0)
            self.assertTrue(Path(td, "t.md").read_text().startswith("Preamble line.\n\n## Lessons"))


RECON_FIX = """\
# UPDATE 2026-10-21
- T-5037 PUSHED, await-merge.
- T-5251 PR1 awaiting merge (MR mailed).
- RULE: findings must clear ORG-105.

## Basics
- **Mailbox send:** write the body to a FILE first.

# UPDATE 2026-10-20
- older note T-4000
"""


class ReconcileTest(unittest.TestCase):
    REASON = "hand-edit, needs review"

    def setUp(self):
        self.store = ac.parse(RECON_FIX)

    def ops(self, text, **kw):
        d, notes = ac.reconcile(self.store, text, **kw)
        return d, notes

    def test_in_sync_gives_no_ops(self):
        d, _ = self.ops(RECON_FIX)
        self.assertEqual(d["ops"], [])
        # also after the store has moved on through deltas (UPDATEd text, ADDed items)
        t = ids_by_text(self.store, "T-5251 PR1")[0]
        new, _ = ac.apply_delta(self.store, delta(
            {"op": "UPDATE", "id": t, "text": "T-5251 PR1 merged (#482).", "reason": "merged"},
            {"op": "ADD", "section": "UPDATE 2026-10-20", "text": "T-6000 filed."}))
        d, _ = ac.reconcile(new, ac.render(new))
        self.assertEqual(d["ops"], [])

    def test_update_add_retire_each_marked_for_review(self):
        edited = RECON_FIX.replace("- T-5037 PUSHED, await-merge.", "- T-5037 PUSHED, merged as #500.") \
                          .replace("- older note T-4000\n", "- new hand note T-4100\n") \
                          .replace("- T-5251 PR1 awaiting merge (MR mailed).\n", "")
        before = copy.deepcopy(self.store)
        d, _ = self.ops(edited)
        self.assertEqual(before, self.store)  # never applied
        kinds = sorted(op["op"] for op in d["ops"])
        self.assertEqual(kinds, ["ADD", "RETIRE", "RETIRE", "UPDATE"])
        for op in d["ops"]:
            self.assertEqual(op["reason"], self.REASON)
        self.assertEqual(d["base_digest"], ac.store_digest(self.store))
        new, rep = ac.apply_delta(self.store, d)
        out = ac.render(new)
        self.assertIn("merged as #500", out)
        self.assertIn("new hand note T-4100", out)
        self.assertNotIn("T-5251", out)
        self.assertNotIn("older note", out)

    def test_locked_section_omitted_by_hand_is_kept(self):
        edited = RECON_FIX.replace("## Basics\n- **Mailbox send:** write the body to a FILE first.\n\n", "")
        d, notes = self.ops(edited)
        self.assertEqual(d["ops"], [])
        self.assertTrue(any("never removed by omission" in n for n in notes["locked"]), notes)
        edited = RECON_FIX.replace("- RULE: findings must clear ORG-105.\n", "")
        d, notes = self.ops(edited)
        self.assertEqual(d["ops"], [])
        self.assertTrue(notes["locked"])

    def test_locked_section_hand_edit_goes_through_replace_path(self):
        edited = RECON_FIX.replace("## Basics\n- **Mailbox send:** write the body to a FILE first.",
                                   "## Basics (unchanged)\n- **Mailbox send:** write it to a file.\n- extra basic.")
        d, _ = self.ops(edited)
        self.assertEqual([op["op"] for op in d["ops"]], ["ADD_SECTION"])
        self.assertEqual(d["ops"][0]["reason"], self.REASON)
        new, rep = ac.apply_delta(self.store, d)
        self.assertEqual(len(rep["replaced_sections"]), 1)
        out = ac.render(new)
        self.assertIn("write it to a file", out)
        self.assertNotIn("FILE first", out)

    def test_new_section_and_heading_retire(self):
        edited = "# UPDATE 2026-10-22\n- T-5348 claimed.\n\n" + RECON_FIX.replace(
            "# UPDATE 2026-10-20\n- older note T-4000\n", "")
        d, _ = self.ops(edited)
        kinds = [op["op"] for op in d["ops"]]
        # r2 M1 order: sections, item RETIREs, ADD/UPDATE, heading RETIREs last
        self.assertEqual(kinds, ["ADD_SECTION", "RETIRE", "ADD", "RETIRE"])
        self.assertEqual(d["ops"][-1]["id"], ids_by_text(self.store, "# UPDATE 2026-10-20")[0])
        new, _ = ac.apply_delta(self.store, d)
        self.assertTrue(ac.render(new).startswith("# UPDATE 2026-10-22\n- T-5348 claimed.\n\n# UPDATE 2026-10-21"))

    def test_removed_parent_heading_with_surviving_subsection_is_kept_and_applies(self):
        # found on the agent-state-history corpus: a rewrite drops '# UPDATE x' but keeps
        # the locked '## Basics' that was nested under it; apply refuses orphaning
        store = ac.parse("# UPDATE x\n- old T-1\n\n## Basics\n- b\n")
        d, notes = ac.reconcile(store, "## Basics\n- b\n")
        self.assertEqual([o["op"] for o in d["ops"]], ["RETIRE"])
        self.assertTrue(any("heading kept" in n for n in notes["locked"]), notes)
        new, _ = ac.apply_delta(store, d)
        self.assertEqual(ac.render(new), "# UPDATE x\n## Basics\n- b\n")

    def test_cross_section_move_survives_apply(self):
        # r2 M1: the moved copy was deduped against its own soon-retired original
        line = "- T-5037 PUSHED, await-merge."
        edited = RECON_FIX.replace(line + "\n", "").replace("- older note T-4000\n",
                                                            "- older note T-4000\n" + line + "\n")
        d, _ = self.ops(edited)
        kinds = [o["op"] for o in d["ops"]]
        self.assertLess(kinds.index("RETIRE"), kinds.index("ADD"))
        new, rep = ac.apply_delta(self.store, d)
        self.assertEqual(rep["deduped"], [])
        out = ac.render(new)
        self.assertEqual(out.count("T-5037 PUSHED"), 1)
        self.assertGreater(out.index("T-5037 PUSHED"), out.index("# UPDATE 2026-10-20"))

    def test_unlocked_rename_under_threshold_keeps_content(self):
        edited = RECON_FIX.replace("# UPDATE 2026-10-20\n- older note T-4000\n",
                                   "# Archive of earlier work\n- older note T-4000\n- plain ticketless line kept\n")
        store = ac.parse(RECON_FIX.replace("- older note T-4000\n",
                                           "- older note T-4000\n- plain ticketless line kept\n"))
        d, _ = ac.reconcile(store, edited)
        self.assertNotIn("UPDATE", [o["op"] for o in d["ops"]])  # below the pairing threshold
        new, rep = ac.apply_delta(store, d)
        self.assertEqual(rep["deduped"], [])
        out = ac.render(new)
        self.assertNotIn("# UPDATE 2026-10-20", out)
        tail = out.split("# Archive of earlier work")[1]
        self.assertIn("older note T-4000", tail)
        self.assertIn("plain ticketless line kept", tail)

    def test_add_never_dedups_against_item_retired_by_same_delta(self):
        # belt-and-braces: a hand-written ADD-then-RETIRE move keeps the content too
        bid = ids_by_text(self.store, "older note")[0]
        new, rep = ac.apply_delta(self.store, delta(
            {"op": "ADD", "section": "UPDATE 2026-10-21", "text": "- older note T-4000", "as_list": False},
            {"op": "RETIRE", "id": bid, "reason": "moved"}))
        self.assertEqual((len(rep["added"]), rep["deduped"]), (1, []))
        self.assertIn("older note T-4000", ac.render(new))

    def test_strict_guards_apply_when_proposed(self):
        edited = RECON_FIX.replace("- T-5251 PR1 awaiting merge (MR mailed).\n", "")
        d, _ = self.ops(edited)
        with self.assertRaises(ac.DeltaError):
            ac.apply_delta(self.store, d, open_tickets={"T-5251"}, strict=True)
        ac.apply_delta(self.store, d, open_tickets=set(), strict=True)

    def test_cli_reconcile_writes_delta_only(self):
        with tempfile.TemporaryDirectory() as td:
            sp, hp, out = Path(td, "s.json"), Path(td, "edited.md"), Path(td, "recon.json")
            sp.write_text(json.dumps(self.store))
            hp.write_text(RECON_FIX.replace("older note", "older hand-edited note"))
            store_before = sp.read_text()
            self.assertEqual(ac.main(["reconcile", "--scratch-root", td, "--store", str(sp),
                                      "--handoff", str(hp), "--out", str(out)]), 0)
            d = json.loads(out.read_text())
            self.assertEqual([op["op"] for op in d["ops"]], ["UPDATE"])
            self.assertEqual(sp.read_text(), store_before)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["edited.md", "recon.json", "s.json"])
            # the delta still goes through apply/propose (and its guards) like any other
            self.assertEqual(ac.main(["apply", "--scratch-root", td, "--store", str(sp), "--delta", str(out),
                                      "--out", str(Path(td, "o.json"))]), 0)
            self.assertEqual(ac.main(["reconcile", "--scratch-root", td, "--store", str(sp),
                                      "--handoff", str(hp), "--out", str(Path(td, "tools", "r.json"))]), 2)


class WriteRootsTest(unittest.TestCase):
    """T-5348 path guard: explicit roots plus the Chronicle companion dir."""

    def test_chronicle_root_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            chron = Path(td, "chron")
            with _chronicle_env(chron):
                ac.check_output_path(chron / "artisan" / "2026-10-22" / "x.store.json")
                with self.assertRaises(PermissionError):
                    ac.check_output_path(Path(td, "elsewhere", "x.json"))
                with self.assertRaises(PermissionError):
                    ac.check_output_path(chron / "artisan" / "HANDOFF.md")

    def test_chronicle_files_never_overwritten(self):
        # r2 L3: new files under the Chronicle root are fine, existing ones are never overwritten
        with tempfile.TemporaryDirectory() as td:
            chron = Path(td, "chron")
            src = Path(td, "src")
            src.mkdir()
            hp, sp = src / "in.md", src / "s.json"
            hp.write_text(FIXTURE)
            sp.write_text(json.dumps(ac.parse(FIXTURE)))
            dp = src / "d.json"
            dp.write_text(json.dumps(delta({"op": "TAG", "id": ids_by_text(ac.parse(FIXTURE), "AWAITING")[0],
                                            "helpful": 1})))
            with _chronicle_env(chron):
                for argv in (["parse", str(hp)], ["apply", "--store", str(sp), "--delta", str(dp)],
                             ["render", "--store", str(sp)]):
                    out = chron / "artisan" / "2026-10-22" / f"{argv[0]}.out"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    self.assertEqual(ac.main(argv + ["--out", str(out)]), 0, argv[0])
                    first = out.read_bytes()
                    out.write_bytes(b"sentinel")
                    self.assertEqual(ac.main(argv + ["--out", str(out)]), 2, argv[0])
                    self.assertEqual(out.read_bytes(), b"sentinel", argv[0])
                    self.assertTrue(first)
                # outside the Chronicle root, an explicit scratch root may still overwrite
                o = src / "o.md"
                o.write_text("x")
                self.assertEqual(ac.main(["render", "--scratch-root", str(src), "--store", str(sp),
                                          "--out", str(o)]), 0)

    def test_repo_root_as_scratch_root_keeps_tickets_and_tools_refused(self):
        # T-2819 r3 LOW: a scratch root set to the repo root made tickets/ and tools/ writable
        repo = Path(ac.PKG_DIR).parent
        for rel in ("tools/x.json", "tickets/active/T-1.json", "tickets/done/x.md", "ledger/x.jsonl"):
            with self.assertRaises(PermissionError, msg=rel):
                ac.check_output_path(repo / rel, [repo])
        ac.check_output_path(repo / "knowledge" / "x.json", [repo])

    def test_propose_out_dir_is_an_explicit_root(self):
        store = ac.parse(FIXTURE)
        tid = ids_by_text(store, "AWAITING MERGE")[0]
        with tempfile.TemporaryDirectory() as td:
            sp, dp = Path(td, "in", "s.json"), Path(td, "in", "d.json")
            sp.parent.mkdir()
            sp.write_text(json.dumps(store))
            dp.write_text(json.dumps(delta({"op": "RETIRE", "id": tid, "reason": "merged"})))
            out = Path(td, "out")
            out.mkdir()
            self.assertEqual(ac.main(["propose", "--store", str(sp), "--delta", str(dp),
                                      "--out-dir", str(out)]), 0)
            self.assertTrue(Path(out, "review.md").is_file())
            bad = Path(td, "pc", "tools")
            bad.mkdir(parents=True)
            self.assertEqual(ac.main(["propose", "--store", str(sp), "--delta", str(dp),
                                      "--out-dir", str(bad)]), 2)
            self.assertEqual(list(bad.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
