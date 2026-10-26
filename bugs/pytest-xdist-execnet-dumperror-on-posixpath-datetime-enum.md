---
symptom: "Under pytest-xdist (`-n auto` or even `-n 1`), a test fails with `execnet.gateway_base.DumpError: can't serialize <class 'pathlib.PosixPath'>` (or `<class 'datetime.datetime'>`, or `<enum 'FooKind'>`) — regardless of whether the underlying assertion passed. Same test passes under bare pytest (no xdist). The failure surfaces as a test failure via xdist's marshaling of the failure/status stream (typically a subTest with the offending object in the frame)."
fix: "In a repo-level `conftest.py`, monkey-patch `execnet.gateway_base._Serializer` to register `save_PosixPath` / `save_datetime` / etc. as class methods that stringify via `save_str`. For Enum subclasses (dynamic __name__), wrap `_Serializer._save` with an isinstance(obj, enum.Enum) branch. Keep the type list specific — a generic fallback masks legitimate xdist-marshaling issues that would otherwise surface real regressions."
domain: "pytest, pytest-xdist, execnet, pathlib, datetime, enum, subTest, CI, marshaling"
---

## Symptom

```
E   execnet.gateway_base.DumpError: can't serialize <class 'pathlib.PosixPath'>
    (also seen for datetime.datetime, and Enum subclasses)

../.local/lib/python3.12/site-packages/execnet/gateway_base.py:1665: DumpError
----------------------------- Captured stdout call -----------------------------
uuuuuuuuuuuuuu   ← subTest 'u' markers; 17 subtests passed, 1 marshaling-failed
=========================== short test summary info ============================
FAILED tools/test_host_control_core.py::HostControlCase::test_effect_digest_...
1 failed, 17 subtests passed
```

Under bare `pytest` (no `-n`), the same test passes. Under `pytest -n 1` or `-n auto`, it fails with DumpError. The failure is order-independent (fires even solo under `-n 1`).

## Cause

pytest-xdist ships test parameters, subTest names, and failure exceptions between master and workers via execnet's `_Serializer`. That serializer's dispatch table only carries a fixed set of primitives (`str`, `int`, `bytes`, `bool`, `tuple`, `list`, `dict`, `frozenset`, `set`, `NoneType`, `complex`, `float`, `long`, `Channel`) — no MRO fallback, no `object` catch-all. Dispatch is by `type(obj)`:

```python
tp = type(obj)
try:
    dispatch = self._dispatch[tp]
except KeyError:
    methodname = "save_" + tp.__name__
    meth: Callable[...] | None = getattr(self.__class__, methodname, None)
    if meth is None:
        raise DumpError(f"can't serialize {tp}") from None
```

When a test's frame contains `Path("/some/path")`, `datetime.datetime(...)`, or an `Enum.MEMBER`, and xdist marshals that frame (a subTest failure, a captured local, an exception repr), the serializer raises DumpError. pytest reports this as a test failure — even when the underlying assertion passed.

Confirmed types the tools/ suite hit (2026-09-16): `PosixPath`, `datetime.datetime`, and an application-defined Enum (`AtlasAuthorityKind`).

## Fix

Repo-level `conftest.py` (imported by pytest before workers spin up), monkey-patch `_Serializer` at import time:

```python
try:
    import datetime as _dt
    import enum as _enum
    import pathlib as _pathlib
    from execnet.gateway_base import _Serializer as _ExecnetSerializer

    def _save_str_repr(self, obj):
        _ExecnetSerializer.save_str(self, str(obj))

    for _cls in (_pathlib.PosixPath, _pathlib.WindowsPath,
                 _pathlib.PurePosixPath, _pathlib.PureWindowsPath):
        _name = "save_" + _cls.__name__
        if not hasattr(_ExecnetSerializer, _name):
            setattr(_ExecnetSerializer, _name, _save_str_repr)

    for _cls in (_dt.datetime, _dt.date, _dt.time, _dt.timedelta):
        _name = "save_" + _cls.__name__
        if not hasattr(_ExecnetSerializer, _name):
            setattr(_ExecnetSerializer, _name, _save_str_repr)

    # Enum: dynamic per-subclass __name__, so hook _save itself.
    _original_save = _ExecnetSerializer._save
    def _save_with_enum_hook(self, obj):
        if isinstance(obj, _enum.Enum):
            _ExecnetSerializer.save_str(self, f"<{type(obj).__name__}.{obj.name}>")
            return
        _original_save(self, obj)
    _ExecnetSerializer._save = _save_with_enum_hook
except ImportError:
    pass
```

**Do not use a generic `except: fallback = str(obj)` at `_save`** — the tools/ suite's first attempt did exactly that and silenced a real subtest assertion in `test_persona_installer` pointer-delete mode. Specific type registration surfaces real regressions; generic fallback hides them.

Reference impl lives at `tools/conftest.py` in this repo.

## Verification

Repro (against tools/ suite before patch, one-liner):
```bash
cd /home/claude/pantheon-config && python3 -m pytest tools/test_host_control_core.py::HostControlCase::test_effect_digest_changes_for_every_execution_affecting_field -q -n 1 --tb=short 2>&1 | grep DumpError
```
Expected before fix: `E   execnet.gateway_base.DumpError: can't serialize <class 'pathlib.PosixPath'>`.

Confirm the fix by re-running the same command with the conftest patch present: expected `1 passed, 28 subtests passed`.

Full-suite validation post-patch (2026-09-16): `pytest tools/ -q -n auto` returned 0 stable failures (down from 22 baseline per T-3752 diagnostic; the last 3 xdist-marshaling failures fixed by this patch at commit `ae1fad2e3`).

## Discovered

- T-3753 investigation, commit `ae1fad2e3` (tools/conftest.py), 2026-09-16.
- Isolation approach that surfaced it: `pytest <one-file> -q -n 1 --tb=long` — the `-n 1` (single worker, still xdist marshaling) reproduces the failure while running the same test without `-n` passes. That solo-vs-`-n 1` divergence is the signature that separates xdist-marshaling issues from real assertion regressions.

## Keywords

execnet DumpError, can't serialize, pytest-xdist, xdist marshaling, subTest, PosixPath, datetime.datetime, enum.Enum, gateway_base _Serializer, save_ dispatch, _save method, pathlib serializer, worker crashed, conftest monkey-patch, save_str fallback, register save method, execnet 2.1, xdist 3.8, xdist 3.x
