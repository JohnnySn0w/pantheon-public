# WAL proof-of-concept

This package proves the minimum crash-safe write-ahead-log substrate tracked by
T-3336. It owns held-directory authority checks, immutable intent and receipt
artifacts, and typed reconciliation for modeled crash prefixes.

The package is intentionally not a harness-controller composition layer or a
general runtime adapter. Canonical scope and acceptance remain in T-3336; the
tests live in `../tests/test_wal_poc.py` and
`../tests/test_wal_crash_poc.py`.
