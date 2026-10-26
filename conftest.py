"""Generator-emitted: T-1677/T-1688 expected-failures skip consumer.

Reads bugs/seed-own-suite-expected-failures.manifest.json (sibling to this
file, since this conftest.py sits at the seed root) and SKIPS (not xfail —
xfail still executes the test body, which would ERROR on missing CLEAR-set
data rather than being cleanly skipped) every collected test item matching a
manifest target, at module, module+class or (T-5486) single-test granularity. Silently a no-op if
the manifest is absent, unreadable, or malformed — including valid JSON with
the WRONG SHAPE (e.g. a top-level array instead of an object, or a
"classes": null) — a fork that deletes, never regenerates, or somehow ships
a broken manifest gets the full un-skipped failure set back, never a crash.
Every manifest-processing step is defensively shape-checked, and the whole
pass is additionally wrapped so ANY unexpected exception degrades to a
no-op rather than a pytest INTERNALERROR (which would be worse than the
failures this file exists to paper over — this runs unattended in forks).

NOTE: module matching uses item.nodeid's first "::"-segment, which pytest
reports relative to its ROOTDIR. This assumes pytest is invoked from the seed
root (`pytest tools -q`) — invoking pytest from INSIDE tools/ changes rootdir
and drops the "tools/" prefix from nodeids, so the manifest's
"tools/test_....py"-shaped targets won't match and nothing will be skipped
(fails open silently, same posture as an absent manifest, not an error).

SECOND NOTE, and read it before relying on this file (T-1981): this hook binds
under pytest ONLY, because unittest never reads a conftest.py — and the seed
documents `python3 -m unittest discover tools` as its runner and ships no
.github/ at all. So in a stock seed there is no invocation that reaches this
file unless a user reaches for pytest themselves. It is not dead code (a fork
that adds CI, or any pytest user, gets the curated view), but it is NOT the
path most seed users take, and it must never be assumed to be running. The
honest baseline is stated in tools/README.md instead. An earlier revision of
this docstring asserted "as CI does" — there is no seed CI; that claim was
inherited from the upstream repo, where `python -m pytest tools/ -q` genuinely
does run in .github/workflows/ci.yml.

Regenerated verbatim on every seed build (tools/neutralize_seed.py in the
upstream pantheon-config repo); do not hand-edit — changes are overwritten on
the next refresh.
"""
import json
from pathlib import Path

import pytest

_MANIFEST_REL = "bugs/seed-own-suite-expected-failures.manifest.json"


def _load_manifest():
    manifest_path = Path(__file__).parent / _MANIFEST_REL
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _build_module_targets(manifest):
    module_targets = {}
    classes = manifest.get("classes")
    if not isinstance(classes, list):
        return module_targets
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        class_id = cls.get("id", "?")
        targets = cls.get("targets")
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            module = target.get("module")
            if not module:
                continue
            module_targets.setdefault(module, []).append(
                (target.get("test_class"), target.get("test"), class_id))
    return module_targets


def _skip_expected_failures(items):
    manifest = _load_manifest()
    if not manifest:
        return

    module_targets = _build_module_targets(manifest)
    if not module_targets:
        return

    for item in items:
        module_part = item.nodeid.split("::", 1)[0]
        targets = module_targets.get(module_part)
        if not targets:
            continue
        item_cls = getattr(item, "cls", None)
        cls_name = item_cls.__name__ if item_cls is not None else None
        test_name = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        for test_class, test, class_id in targets:
            if test_class is not None and test_class != cls_name:
                continue
            if test is not None and test != test_name:
                continue
            reason = (
                f"expected failure per {_MANIFEST_REL} (T-1677): class {class_id}"
                + (f", {test_class}" if test_class else "")
                + (f"::{test}" if test else "")
            )
            item.add_marker(pytest.mark.skip(reason=reason))
            break


def pytest_collection_modifyitems(config, items):
    # Belt+braces: shape-checking in _load_manifest/_build_module_targets
    # already covers the known malformed-JSON cases; this outer guard is the
    # backstop for anything unforeseen. A manifest-processing crash must
    # never escalate to a pytest INTERNALERROR (zero tests run) — that is
    # strictly worse than leaving the un-skipped failures un-skipped.
    try:
        _skip_expected_failures(items)
    except Exception:
        return
