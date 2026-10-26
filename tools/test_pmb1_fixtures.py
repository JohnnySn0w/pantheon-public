#!/usr/bin/env python3
"""PMB-1 synthetic fixture generator (T-2518): seeded, deterministic, spec-derived."""
from __future__ import annotations

import unittest

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import offline

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class TestFixtures(unittest.TestCase):
    def test_deterministic_for_a_seed_and_distinct_across_seeds(self) -> None:
        a, b, c = fxm.generate(1, "tiny"), fxm.generate(1, "tiny"), fxm.generate(2, "tiny")
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, c.sha256)

    def test_role_counts_follow_the_track_definitions(self) -> None:
        f = fxm.generate(3, "small")
        n = fxm.SIZES["small"]
        self.assertEqual((len(f.stable), len(f.updates), len(f.forgets)), (3 * n, n, n))
        self.assertGreaterEqual(len(f.unseen), 5)
        self.assertEqual(len(f.base_facts), 5 * n)

    def test_every_token_is_distinct_and_no_value_is_a_substring_of_another(self) -> None:
        f = fxm.generate(4, "medium")
        toks = [x.entity.lower() for x in f.base_facts] + [x.value for x in f.base_facts]
        toks += [u.new.value for u in f.updates] + [u.entity.lower() for u in f.unseen]
        entities = {x.entity.lower() for x in f.base_facts}
        values = [x.value for x in f.base_facts] + [u.new.value for u in f.updates]
        self.assertEqual(len(values), len(set(values)))
        self.assertFalse(entities & set(values))
        self.assertFalse(entities & {u.entity.lower() for u in f.unseen})
        self.assertEqual({len(t) for t in toks}, {6})  # equal length + distinct => never a substring

    def test_updates_carry_gold_evidence_keys_and_new_values(self) -> None:
        f = fxm.generate(1, "tiny")
        for u in f.updates:
            self.assertEqual((u.old.entity, u.old.attr), (u.new.entity, u.new.attr))
            self.assertNotEqual(u.old.value, u.new.value)
            self.assertEqual(u.new.key, u.old.key + "-u")

    def test_spec_parse_and_unknown_size(self) -> None:
        self.assertEqual(fxm.parse_spec("seed=7,size=small"), (7, "small"))
        with self.assertRaises(ValueError):
            fxm.parse_spec("seed=1,size=huge")


if __name__ == "__main__":
    unittest.main()
