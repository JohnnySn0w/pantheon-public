"""Tests for tools/a2a/netcaps.py — the ORG-071 named-parameter set (T-2857 PR1).

Pure-local, no network, no sleeping. Covers: every ORG-071 taxonomy name is a
field, construction-time validation (negative / zero / non-finite / wrong-type /
cross-field), the ORG-047 D5 rotation ceiling on the positive TTLs, and that the
PASSPORT.md default-value table cannot drift from the dataclass defaults.
"""

from __future__ import annotations

import dataclasses
import math
import unittest
from pathlib import Path

from tools.a2a.netcaps import (
    GROUPS,
    ROTATION_CEILING_S,
    NetcapsParams,
    parameter_table,
)

# The full ORG-071 taxonomy (relocated to T-2857 by the operator's ratify-with-trim,
# TG 1784). Spelled out here, not derived from the module, so a dropped or
# renamed parameter fails loudly.
TAXONOMY = {
    "Caching": [
        "did_doc_positive_ttl_max", "status_list_positive_ttl_max",
        "negative_ttl_definitive_absent", "negative_ttl_transient_infra",
        "jitter_range_pct",
    ],
    "Timeouts": [
        "timeout_dns", "timeout_connect", "timeout_tls", "timeout_fetch_read",
        "timeout_handshake_roundtrip", "timeout_total_handshake",
    ],
    "Connections": [
        "max_conns_global", "max_conns_per_source",
        "max_concurrent_handshakes_global", "max_concurrent_handshakes_per_source",
        "max_concurrent_handshakes_per_counterparty",
        "rate_handshake_per_source", "rate_handshake_per_counterparty",
    ],
    "Budgets": [
        "budget_preauth", "budget_authenticated_ungranted", "budget_granted",
        "budget_unknown_source", "budget_known_degraded",
    ],
    "Amplification": [
        "resolution_rate_global", "resolution_rate_per_source_unresolved",
        "resolution_rate_per_anchor", "max_distinct_dids_per_source_window",
        "max_distinct_anchors_per_source_window", "circuit_breaker_threshold",
        "circuit_breaker_cooldown", "circuit_breaker_probe_interval",
    ],
    "Sizes": [
        "max_did_doc_bytes", "max_status_list_bytes",
        "max_status_list_decompressed_bytes", "max_compression_ratio",
        "max_negotiation_steps", "max_redirects", "retry_policy",
    ],
}
ALL_NAMES = [n for names in TAXONOMY.values() for n in names]
# Parameters where 0 is a meaningful setting (0 redirects, 0 retries, 0 jitter).
ZERO_OK = {"max_redirects", "retry_policy", "jitter_range_pct"}


def _fields():
    return {f.name: f for f in dataclasses.fields(NetcapsParams)}


class TestTaxonomyCoverage(unittest.TestCase):
    def test_every_taxonomy_name_is_a_field_and_nothing_else(self):
        self.assertEqual(set(_fields()), set(ALL_NAMES))

    def test_each_field_sits_in_its_taxonomy_group(self):
        fields = _fields()
        self.assertEqual(list(GROUPS), list(TAXONOMY))
        for group, names in TAXONOMY.items():
            for n in names:
                self.assertEqual(fields[n].metadata["group"], group, n)

    def test_each_field_carries_unit_and_rationale(self):
        for name, f in _fields().items():
            self.assertTrue(f.metadata.get("unit"), name)
            self.assertTrue(f.metadata.get("why"), name)

    def test_defaults_construct(self):
        NetcapsParams()  # the provisional defaults must satisfy their own rules

    def test_frozen(self):
        p = NetcapsParams()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.timeout_dns = 99  # type: ignore[misc]


class TestPerFieldValidation(unittest.TestCase):
    def test_negative_rejected_for_every_field(self):
        for name in ALL_NAMES:
            with self.subTest(name=name), self.assertRaises(ValueError):
                NetcapsParams(**{name: -1})

    def test_zero_rejected_except_where_meaningful(self):
        for name in ALL_NAMES:
            with self.subTest(name=name):
                if name in ZERO_OK:
                    NetcapsParams(**{name: 0})
                else:
                    with self.assertRaises(ValueError):
                        NetcapsParams(**{name: 0})

    def test_non_finite_rejected(self):
        for bad in (math.nan, math.inf):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NetcapsParams(timeout_total_handshake=bad)

    def test_bool_and_str_rejected(self):
        for bad in (True, "5", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NetcapsParams(max_conns_global=bad)  # type: ignore[arg-type]

    def test_integer_fields_reject_fractions(self):
        with self.assertRaises(ValueError):
            NetcapsParams(max_conns_global=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            NetcapsParams(max_redirects=0.5)  # type: ignore[arg-type]

    def test_jitter_upper_bound(self):
        NetcapsParams(jitter_range_pct=50)
        with self.assertRaises(ValueError):
            NetcapsParams(jitter_range_pct=51)


class TestRotationCeiling(unittest.TestCase):
    """ORG-047 D5: a rotation must propagate in minutes, so neither positive TTL
    may exceed the documented rotation ceiling."""

    def test_ceiling_is_minutes(self):
        self.assertGreaterEqual(ROTATION_CEILING_S, 60)
        self.assertLess(ROTATION_CEILING_S, 3600)

    def test_positive_ttls_at_ceiling_ok_above_rejected(self):
        for name in ("did_doc_positive_ttl_max", "status_list_positive_ttl_max"):
            with self.subTest(name=name):
                NetcapsParams(**{name: ROTATION_CEILING_S})
                with self.assertRaises(ValueError):
                    NetcapsParams(**{name: ROTATION_CEILING_S + 1})

    def test_negative_ttls_bounded_by_ceiling_too(self):
        for name in ("negative_ttl_definitive_absent", "negative_ttl_transient_infra"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                NetcapsParams(**{name: ROTATION_CEILING_S + 1})


class TestCrossFieldValidation(unittest.TestCase):
    def test_step_timeout_cannot_exceed_total_deadline(self):
        for name in ("timeout_dns", "timeout_connect", "timeout_tls",
                     "timeout_fetch_read", "timeout_handshake_roundtrip"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                NetcapsParams(**{name: 100, "timeout_total_handshake": 50})

    def test_per_source_and_per_counterparty_cannot_exceed_global(self):
        with self.assertRaises(ValueError):
            NetcapsParams(max_conns_global=4, max_conns_per_source=8)
        with self.assertRaises(ValueError):
            NetcapsParams(max_concurrent_handshakes_global=2,
                          max_concurrent_handshakes_per_source=3)
        with self.assertRaises(ValueError):
            NetcapsParams(max_concurrent_handshakes_global=2,
                          max_concurrent_handshakes_per_counterparty=3)

    def test_per_source_and_per_anchor_resolution_cannot_exceed_global(self):
        # T-2857 PR1 review (LOW), added in PR2.
        with self.assertRaises(ValueError):
            NetcapsParams(resolution_rate_global=10,
                          resolution_rate_per_source_unresolved=11)
        with self.assertRaises(ValueError):
            NetcapsParams(resolution_rate_global=10, resolution_rate_per_anchor=11)
        NetcapsParams(resolution_rate_global=10,
                      resolution_rate_per_source_unresolved=10,
                      resolution_rate_per_anchor=10)

    def test_preauth_budget_is_the_tightest_tier(self):
        # ORG-071 D1(b): pre-auth is the primary DoS target, tightest budget.
        with self.assertRaises(ValueError):
            NetcapsParams(budget_preauth=100, budget_authenticated_ungranted=50)
        with self.assertRaises(ValueError):
            NetcapsParams(budget_authenticated_ungranted=500, budget_granted=200)

    def test_decompressed_cap_not_below_compressed_cap(self):
        with self.assertRaises(ValueError):
            NetcapsParams(max_status_list_bytes=2048,
                          max_status_list_decompressed_bytes=1024)

    def test_compression_ratio_at_least_one(self):
        with self.assertRaises(ValueError):
            NetcapsParams(max_compression_ratio=0.5)
        NetcapsParams(max_compression_ratio=1)


class TestParameterTable(unittest.TestCase):
    def test_table_lists_every_field_with_its_default(self):
        table = parameter_table()
        p = NetcapsParams()
        for name in ALL_NAMES:
            row = next(line for line in table.splitlines()
                       if line.startswith(f"| `{name}` "))
            self.assertIn(f"| {getattr(p, name)} |", row)

    def test_passport_md_carries_the_rendered_table_verbatim(self):
        # Drift guard: the surfaced table must be the one the code renders.
        md = (Path(__file__).resolve().parent / "a2a" / "PASSPORT.md").read_text()
        self.assertIn(parameter_table(), md)


if __name__ == "__main__":
    unittest.main()
