"""The sole ``contract_digest`` oracle (ORG-080 §2 "Canonical contract_digest", ORG-081 D-9).

``compute_contract_digest(module_path)`` is the ONLY definition of a component
contract's digest. There is no equivalent-implementation carve-out: an auditor
recomputing a digest calls this function. ORG-082 §2a's expected-runtime-subset
wrapper is digested by the same function.

Procedure (ORG-080 §2, steps 1-5):

1. load the module with safe_load semantics (the registry's strict loader: a
   SafeLoader subclass that additionally refuses duplicate keys, so a second
   ``contract_digest`` key cannot hide behind the first);
2. find the single ``contract_digest`` field of the top-level ``component:``
   mapping (a module with more than one ``contract_digest`` key at any nesting
   level is refused, so the walk is unambiguous);
3. replace its value with the fixed sentinel ``SHA256_DIGEST_PLACEHOLDER``;
4. re-emit with ``_canonical_yaml_emit`` (pinned parameters below);
5. return ``sha256:<hex>`` of those bytes.

Version pin: the canonicalizer's bytes depend on the PyYAML emitter. Only the
versions in ``PINNED_PYYAML_VERSIONS`` are accepted; any other version is a
refusal (fail-closed), never a silent recompute. The golden vector in
``contract_digest_test.py`` was produced and verified locally under 6.0.1;
6.0.3 is the CI pin (.github/workflows/ci.yml) and CI is its proof. If the two
ever disagree on the golden fixture, the set narrows to one version.

The reference container image ORG-080 §2 names for non-Python auditors is not
published yet (deferred past T-5074).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from tools.germinate.registry.load import RegistryLoadError, load_file

SENTINEL = "SHA256_DIGEST_PLACEHOLDER"
assert len(SENTINEL) == 25
DIGEST_FIELD = "contract_digest"
TOP_KEY = "component"
PINNED_PYYAML_VERSIONS = frozenset({"6.0.1", "6.0.3"})


class ContractDigestError(ValueError):
    """The module cannot be digested (unreadable, malformed, ambiguous, or unpinned emitter)."""


def _count_digest_fields(value: Any) -> int:
    if isinstance(value, dict):
        return sum((1 if key == DIGEST_FIELD else 0) + _count_digest_fields(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(_count_digest_fields(item) for item in value)
    return 0


def _require_pinned_emitter() -> None:
    version = getattr(yaml, "__version__", None)
    if version not in PINNED_PYYAML_VERSIONS:
        raise ContractDigestError(
            f"PyYAML {version!r} is not a pinned contract_digest emitter "
            f"(pinned: {', '.join(sorted(PINNED_PYYAML_VERSIONS))}); refusing to digest"
        )


def _canonical_yaml_emit(document: Any) -> bytes:
    text = yaml.safe_dump(
        document,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        default_style=None,
        line_break="\n",
        explicit_end=False,
        explicit_start=False,
        indent=2,
        width=float("inf"),
    )
    lines = [line.rstrip() for line in text.split("\n")]
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def digest_document(document: Any, *, source: str = "<document>") -> str:
    """Steps 2-5 over an already-loaded document (used by the module entry point)."""

    _require_pinned_emitter()
    if not isinstance(document, dict) or set(document) != {TOP_KEY} or not isinstance(document[TOP_KEY], dict):
        raise ContractDigestError(f"{source}: top level must be exactly one `{TOP_KEY}:` mapping")
    count = _count_digest_fields(document)
    if count != 1 or DIGEST_FIELD not in document[TOP_KEY]:
        raise ContractDigestError(
            f"{source}: needs exactly one `{DIGEST_FIELD}` field, at the top of `{TOP_KEY}:` (found {count})"
        )
    body = dict(document[TOP_KEY])
    body[DIGEST_FIELD] = SENTINEL
    return "sha256:" + hashlib.sha256(_canonical_yaml_emit({TOP_KEY: body})).hexdigest()


def compute_contract_digest(module_path: Path) -> str:
    """The sole oracle: ``sha256:<hex>`` of the module's canonical serialization."""

    try:
        document = load_file(Path(module_path))
    except RegistryLoadError as exc:
        raise ContractDigestError(str(exc)) from None
    return digest_document(document, source=str(module_path))
