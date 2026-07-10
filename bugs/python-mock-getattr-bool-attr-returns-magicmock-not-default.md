---
symptom: "`getattr(mock, 'some_bool_attr', default)` on a `unittest.mock.MagicMock` returns a truthy `MagicMock` instance instead of the `default` — boolean checks silently flip behavior"
fix: "use `getattr(mock_instance, 'attr', default)` only on real-attribute presence, OR explicitly set the attr on the mock (`mock.some_bool_attr = False`), OR refactor to read attr via `mock.configure_mock` upfront"
domain: "python, unittest.mock, testing"
---

## Symptom

```python
from unittest.mock import MagicMock

mock = MagicMock()
flag = getattr(mock, "is_dry_run", False)   # expected False
if flag:
    raise SystemExit("dry-run path triggered")  # this fires!
```

A boolean-flavored attribute lookup via `getattr(..., default)` on a `MagicMock` returns a child `MagicMock`, which is truthy. Code that branches on `if flag:` takes the truthy path even though no caller has set the attribute. Discovered when adding a new code path that auto-injected a `bool` attribute on a pre-existing test mock — pre-existing unrelated tests started silently exercising the new path because their mocks now reported the new attr as truthy.

## Cause

`MagicMock.__getattr__` returns a new auto-generated `MagicMock` child for any attribute access — the lookup never falls through to `getattr`'s third-positional `default`. The `default` argument is only honored when the underlying object raises `AttributeError`; `MagicMock` never raises, so the default path is unreachable. Bool-coerced `MagicMock` is truthy by default.

## Fix

Three workable patterns, by preference:

1. **Set the attr explicitly on the mock** (best — locks the contract per test):
   ```python
   mock = MagicMock()
   mock.is_dry_run = False
   ```

2. **Use `spec=` when constructing the mock** so only declared attrs exist:
   ```python
   mock = MagicMock(spec=MyClass)   # AttributeError on undeclared attrs → default honored
   flag = getattr(mock, "is_dry_run", False)   # works as expected
   ```

3. **Refactor the production code** to require the attr via constructor / function-arg signature instead of polymorphic `getattr` — eliminates the lookup-with-default pattern entirely.

For new tests, prefer (2) — `spec=`. For patching pre-existing tests after introducing a new code path, (1) is the surgical fix.

## Discovered

- Surfaced 2026-05-11 by b35 Phase A implementer (T-0211); ticket.py added an `args.no_push` attribute, and pre-existing test fixtures via `MagicMock()` started silently taking the no-push branch because `getattr(args, "no_push", False)` returned a truthy `MagicMock` child.
- Pattern is broadly applicable to any Python test suite using bare `MagicMock()` for argparse-Namespace-shaped fixtures or for any kwargs-bag where new fields land later.
- Filed as bugs/ entry per HO directive 2026-05-11 — handoff priority #5.

## Keywords

python, unittest.mock, MagicMock, getattr, default-not-honored, truthy-mock, spec, configure_mock, argparse-namespace, test-fixture-bleed, polymorphic-getattr, new-attr-silent-branching, t0211, phase-a-implementer-a-reflection
