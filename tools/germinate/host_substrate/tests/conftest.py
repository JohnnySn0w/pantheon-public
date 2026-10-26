"""host_substrate test guards (T-2918): these run on EVERY runner, CI included.

* session: the specific real-host paths these tests could touch fingerprint
  identically before and after, and no fixture account/group/id appears in the
  real account databases (review N5: narrow, so concurrent host changes do not
  false-fail);
* per test: the audit hook blocks any real-host write or mutator launch, and a
  test that tripped it (outside the guard's own negative tests) fails.

These are tripwires; OS permissions are the real barrier (sandbox/guards.py).
"""

from __future__ import annotations

import pytest

from .sandbox import guards


@pytest.fixture(scope="session", autouse=True)
def real_host_fingerprint_guard(tmp_path_factory):
    # The session's ACTUAL basetemp, from pytest itself (CI: --basetemp=../pytest-basetemp,
    # under /opt; under xdist this is the worker's own popen-gwN dir), not a /tmp guess.
    guards.allow_temp_root(tmp_path_factory.getbasetemp())
    before = guards.real_host_state()
    assert before[1] == (), f"fixture names already present in the real account databases: {before[1]}"
    yield
    assert guards.real_host_state() == before, "a host_substrate test changed the REAL host (T-2918 guard)"


@pytest.fixture(autouse=True)
def real_host_write_guard():
    with guards.active() as violations:
        yield
    assert violations == [], f"real-host writes were attempted: {violations}"
