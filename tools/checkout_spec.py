"""tools/checkout_spec.py — T-2951: the declarative checkout-lifecycle surface.

WHAT THIS OWNS. The declarative source of truth for repository *lifecycle policy*
— where each managed checkout lives, what revision it should hold, how it may
advance, and (guarded-inert) what service its advance may restart. This is a NEW
modular `checkouts/` surface, deliberately NOT a `repos.yaml` extension:
`repos.yaml` is the access/grants + ORG-046 remotes gate (fail-open readers);
lifecycle policy is a distinct concern, so it lives in its own directory exactly
as ORG-064's `schedules/` lives outside `instance.yaml` (DECISION, plan §DECISIONS,
justifies-via P1/D10/ORG-064). This module loads + validates that directory into a
`CheckoutSpec`; it renders nothing, touches no checkout, and resolves no remote —
planning/apply is `repo_reconciler.py`.

ENTRY SCHEMA (v1, plan §"Declarative checkout surface"). Every entry carries:
  `checkout_id` — a stable id, unique across the spec; NEVER derived from a URL or
      path (a path/URL can change identity; the id is the stable handle).
  `logical_repo` — a `repos.yaml` registry name (the registry/grants axis, ORG-055
      logical identity). A raw remote URL is refused here; remotes resolve through
      ORG-046.
  `remote_ref` — v1 accepts exactly the literal `manifest`: the remote is resolved
      through the ORG-046 seam (`remote_resolve.resolve_remote`), never a raw URL
      baked into lifecycle policy.
  `target_account` — a `role:<role-key>` reference into instance.yaml's role→account
      binding, or a bare account name. Cross-reference validation against
      instance.yaml is an injectable seam (`known_accounts`), off by default so the
      loader stays hermetic.
  `target_path` — the normalised absolute on-disk path of the checkout (no `..`,
      `.`, `//` or trailing `/`; T-5071) (read-only handle here;
      NEVER touched at load).
  `mode` — EXACTLY ONE of `pinned: <sha>` (desired = an exact commit) or
      `tracking: <branch>` (desired = ff-only track a branch). Both, or neither, is
      a load error.
  `update_policy` — `ff-only` | `availability-only` | `never`. `availability-only`
      is the `provision_repos` posture: clone/fetch so the repo is PRESENT, but
      never advance an existing checkout.
  `health_contract` (optional, default `[]`) — probe refs; an evidence surface. NOT
      live-run at load; the reconciler's injected health seam runs them.
  `restart_contract` (optional, default absent) — a guarded-inert brokered INTENT
      against the ORG-052 op-family contract shape (`operation_family`, `target_unit`).
      It is emitted into the plan/receipt and NEVER dispatched in phase-1 (the broker
      D-Bus/MCP exposure is unbuilt, T-2652).

`logical_repo` cross-reference (does the name exist in repos.yaml?) is an injectable
seam too (`known_repos`), off by default. The loader's own job is STRUCTURE
(strict keys, exactly-one mode, no raw URLs, stable ids); the registry/instance
cross-references are the caller's to wire, so the loader is testable without either
file present.

DEDUP + FILE LAYOUT. Mirrors `schedules/`: a `checkouts/global.yaml` plus zero or
more additional `checkouts/<scope>.yaml`, each with a top-level `checkouts:` list;
`checkout_id` is unique across every file (a duplicate is a load error naming both
source files). This is fail-fast config validation, not a partial-success API.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from tools.component_catalogue import (
    STABLE_ID_PATTERN,
    ComponentCatalogueError,
    _array_schema,
    _exact_mapping,
    _json_bytes,
    _load_yaml,
    _nullable_string_schema,
    _object_schema,
    _stable_id,
    _string,
    _string_tuple,
)

SCHEMA_VERSION = 1
GLOBAL_FILE = "global.yaml"

# v1 accepts exactly one remote_ref form: the ORG-046 manifest seam. A raw URL is
# never baked into lifecycle policy (it belongs to ORG-046 / repos.yaml).
REMOTE_REF_MANIFEST = "manifest"

UPDATE_POLICY_FF_ONLY = "ff-only"
UPDATE_POLICY_AVAILABILITY_ONLY = "availability-only"
UPDATE_POLICY_NEVER = "never"
_UPDATE_POLICIES = (
    UPDATE_POLICY_FF_ONLY,
    UPDATE_POLICY_AVAILABILITY_ONLY,
    UPDATE_POLICY_NEVER,
)

_REQUIRED_ENTRY_KEYS = (
    "checkout_id",
    "logical_repo",
    "remote_ref",
    "target_account",
    "target_path",
    "mode",
    "update_policy",
)
_OPTIONAL_ENTRY_KEYS = ("health_contract", "restart_contract")
_KNOWN_ENTRY_KEYS = frozenset(_REQUIRED_ENTRY_KEYS) | frozenset(_OPTIONAL_ENTRY_KEYS)

_MODE_KEYS = ("pinned", "tracking")
RESTART_CONTRACT_KEYS = ("operation_family", "target_unit")

_TOP_LEVEL_KEYS = ("schema_version", "checkouts")

# A git commit sha: 7–40 lowercase hex (abbreviated through full-length).
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# A git branch name: no whitespace, no control chars, no URL markers, non-empty.
_BRANCH_RE = re.compile(r"^[^\s:@\\]+$")
# operation_family: dotted stable segments, e.g. "service.restart".
_OP_FAMILY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
# a systemd-ish unit token, e.g. "kino.service".
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
# markers that betray a raw remote URL where a logical id / literal is required.
_URL_MARKERS = ("://", "@", ":")


class RepositoryLifecycleError(ValueError):
    """A `checkouts/` spec (or a reconciler input) cannot be accepted.

    Always carries the source file and checkout_id where known — fail-fast config
    validation, not a runtime condition a caller should catch and route around.
    """


@dataclass(frozen=True)
class RestartContract:
    """A guarded-inert brokered intent (ORG-052 op-family contract shape). Emitted
    into a plan/receipt, NEVER dispatched in phase-1 (broker unbuilt, T-2652)."""

    operation_family: str
    target_unit: str


@dataclass(frozen=True)
class CheckoutMode:
    """Exactly one of `pinned` / `tracking` is set (the loader enforces this)."""

    pinned: str | None = None
    tracking: str | None = None

    @property
    def is_pinned(self) -> bool:
        return self.pinned is not None

    @property
    def desired(self) -> str:
        """The desired ref/sha handle: the pin sha, or the tracked branch name."""
        return self.pinned if self.pinned is not None else self.tracking  # type: ignore[return-value]


@dataclass(frozen=True)
class CheckoutEntry:
    checkout_id: str
    logical_repo: str
    remote_ref: str
    target_account: str
    target_path: str
    mode: CheckoutMode
    update_policy: str
    health_contract: tuple[str, ...] = ()
    restart_contract: RestartContract | None = None
    source_file: str = ""


@dataclass(frozen=True)
class CheckoutSpec:
    checkouts: dict[str, CheckoutEntry]

    def by_logical_repo(self, logical_repo: str) -> tuple[CheckoutEntry, ...]:
        """Every checkout that materializes `logical_repo`, ordered by checkout_id.

        A logical repo may be checked out on more than one account/path, so this
        returns a tuple, not a single entry (the T-3182 join fans out this way)."""
        return tuple(
            self.checkouts[cid]
            for cid in sorted(self.checkouts)
            if self.checkouts[cid].logical_repo == logical_repo
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _abspath(value: object, *, path: str) -> str:
    text = _string(value, path=path)
    if not text.startswith("/") or "\0" in text:
        raise RepositoryLifecycleError(f"{path} must be an absolute POSIX path")
    # "//"-prefixed paths survive normpath unchanged (POSIX); refused (T-2917 D32).
    # A trailing '/' or a '.' segment is un-normalised too, so it is refused (T-5071).
    if text.startswith("//") or os.path.normpath(text) != text or ".." in Path(text).parts:
        raise RepositoryLifecycleError(
            f"{path} must be a normalised absolute path (no '..', '.', '//' or trailing '/')"
        )
    return text


def _no_url(text: str, *, path: str) -> None:
    if any(marker in text for marker in _URL_MARKERS):
        raise RepositoryLifecycleError(
            f"{path} must be a logical identity / literal, not a remote URL "
            f"(remotes resolve through ORG-046)"
        )


def _parse_mode(value: object, *, source_file: str, checkout_id: str) -> CheckoutMode:
    if not isinstance(value, dict):
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' mode must be a mapping"
        )
    keys = set(value)
    unknown = keys - set(_MODE_KEYS)
    if unknown:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' mode has unknown key(s): "
            f"{', '.join(sorted(unknown))} (allowed: {', '.join(_MODE_KEYS)})"
        )
    if keys == {"pinned"}:
        sha = _string(value["pinned"], path=f"{source_file}:{checkout_id}.mode.pinned")
        if _SHA_RE.fullmatch(sha) is None:
            raise RepositoryLifecycleError(
                f"{source_file}: checkout '{checkout_id}' mode.pinned must be a "
                f"7–40 char lowercase hex commit sha"
            )
        return CheckoutMode(pinned=sha)
    if keys == {"tracking"}:
        branch = _string(value["tracking"], path=f"{source_file}:{checkout_id}.mode.tracking")
        if _BRANCH_RE.fullmatch(branch) is None:
            raise RepositoryLifecycleError(
                f"{source_file}: checkout '{checkout_id}' mode.tracking must be a "
                f"branch name (no whitespace or URL markers)"
            )
        return CheckoutMode(tracking=branch)
    raise RepositoryLifecycleError(
        f"{source_file}: checkout '{checkout_id}' mode must set EXACTLY ONE of "
        f"{{pinned, tracking}} (got: {sorted(keys) or 'neither'})"
    )


def _parse_restart_contract(
    value: object, *, source_file: str, checkout_id: str
) -> RestartContract:
    try:
        item = _exact_mapping(
            value, path=f"{source_file}:{checkout_id}.restart_contract", keys=RESTART_CONTRACT_KEYS
        )
    except ComponentCatalogueError as exc:
        raise RepositoryLifecycleError(str(exc)) from exc
    family = _string(item["operation_family"], path=f"{checkout_id}.restart_contract.operation_family")
    if _OP_FAMILY_RE.fullmatch(family) is None:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' restart_contract.operation_family "
            f"must be a dotted op-family id (e.g. 'service.restart')"
        )
    unit = _string(item["target_unit"], path=f"{checkout_id}.restart_contract.target_unit")
    if _UNIT_RE.fullmatch(unit) is None:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' restart_contract.target_unit "
            f"must be a unit token (e.g. 'kino.service')"
        )
    return RestartContract(operation_family=family, target_unit=unit)


def _parse_entry(
    raw: object,
    *,
    source_file: str,
    known_repos: frozenset[str] | None,
    known_accounts: frozenset[str] | None,
) -> CheckoutEntry:
    if not isinstance(raw, dict):
        raise RepositoryLifecycleError(f"{source_file}: checkout entry must be a mapping")
    keys = set(raw)
    missing = [k for k in _REQUIRED_ENTRY_KEYS if k not in raw]
    if missing:
        cid = raw.get("checkout_id", "<unnamed>")
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{cid}' missing required key(s): {', '.join(missing)}"
        )
    unknown = keys - _KNOWN_ENTRY_KEYS
    if unknown:
        cid = raw.get("checkout_id", "<unnamed>")
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{cid}' has unknown key(s): {', '.join(sorted(unknown))}"
        )

    try:
        checkout_id = _stable_id(raw["checkout_id"], path=f"{source_file}.checkout_id")
        logical_repo = _stable_id(raw["logical_repo"], path=f"{source_file}:{raw['checkout_id']}.logical_repo")
    except ComponentCatalogueError as exc:
        raise RepositoryLifecycleError(str(exc)) from exc
    _no_url(logical_repo, path=f"{source_file}: checkout '{checkout_id}' logical_repo")

    remote_ref = _string(raw["remote_ref"], path=f"{source_file}:{checkout_id}.remote_ref")
    if remote_ref != REMOTE_REF_MANIFEST:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' remote_ref must be the literal "
            f"'{REMOTE_REF_MANIFEST}' (v1 resolves remotes through ORG-046, never a raw URL)"
        )

    target_account = _string(raw["target_account"], path=f"{source_file}:{checkout_id}.target_account")
    account_name = target_account.split(":", 1)[1] if target_account.startswith("role:") else target_account
    if not account_name or "/" in target_account or "://" in target_account:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' target_account must be 'role:<role-key>' "
            f"or a bare account name"
        )

    target_path = _abspath(raw["target_path"], path=f"{source_file}:{checkout_id}.target_path")

    update_policy = _string(raw["update_policy"], path=f"{source_file}:{checkout_id}.update_policy")
    if update_policy not in _UPDATE_POLICIES:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' update_policy '{update_policy}' invalid "
            f"(must be one of {', '.join(_UPDATE_POLICIES)})"
        )

    mode = _parse_mode(raw["mode"], source_file=source_file, checkout_id=checkout_id)

    health_raw = raw.get("health_contract", [])
    try:
        health_contract = _string_tuple(health_raw, path=f"{source_file}:{checkout_id}.health_contract")
    except ComponentCatalogueError as exc:
        raise RepositoryLifecycleError(str(exc)) from exc

    restart_raw = raw.get("restart_contract")
    restart_contract = (
        None
        if restart_raw is None
        else _parse_restart_contract(restart_raw, source_file=source_file, checkout_id=checkout_id)
    )

    if known_repos is not None and logical_repo not in known_repos:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' logical_repo '{logical_repo}' is not a "
            f"known repos.yaml repo"
        )
    if known_accounts is not None and account_name not in known_accounts:
        raise RepositoryLifecycleError(
            f"{source_file}: checkout '{checkout_id}' target_account '{target_account}' resolves "
            f"to unknown account '{account_name}'"
        )

    return CheckoutEntry(
        checkout_id=checkout_id,
        logical_repo=logical_repo,
        remote_ref=remote_ref,
        target_account=target_account,
        target_path=target_path,
        mode=mode,
        update_policy=update_policy,
        health_contract=health_contract,
        restart_contract=restart_contract,
        source_file=source_file,
    )


def parse_checkout_file(
    data: object,
    *,
    source_file: str,
    known_repos: frozenset[str] | None = None,
    known_accounts: frozenset[str] | None = None,
) -> tuple[CheckoutEntry, ...]:
    """Parse one already-decoded `checkouts/*.yaml` object into its entries.

    Hermetic seam for tests: build the dict, call this, no file needed."""
    try:
        top = _exact_mapping(data, path=source_file, keys=_TOP_LEVEL_KEYS)
    except ComponentCatalogueError as exc:
        raise RepositoryLifecycleError(str(exc)) from exc
    if type(top["schema_version"]) is not int or top["schema_version"] != SCHEMA_VERSION:
        raise RepositoryLifecycleError(f"{source_file}: schema_version must equal {SCHEMA_VERSION}")
    checkouts_raw = top["checkouts"]
    if not isinstance(checkouts_raw, list):
        raise RepositoryLifecycleError(f"{source_file}: checkouts must be a list")
    return tuple(
        _parse_entry(
            entry, source_file=source_file, known_repos=known_repos, known_accounts=known_accounts
        )
        for entry in checkouts_raw
    )


def load_checkout_spec(
    checkouts_dir: Path,
    *,
    known_repos: frozenset[str] | None = None,
    known_accounts: frozenset[str] | None = None,
) -> CheckoutSpec:
    """Load + validate every `*.yaml` under `checkouts_dir` into a `CheckoutSpec`.

    Raises `RepositoryLifecycleError` on any structural/semantic violation. A
    `checkout_id` is unique across every file (a duplicate names both sources).
    """
    if not checkouts_dir.is_dir():
        raise RepositoryLifecycleError(f"checkouts dir not found: {checkouts_dir}")

    checkouts: dict[str, CheckoutEntry] = {}
    # global.yaml first (stable ordering), then the rest sorted.
    paths = []
    global_path = checkouts_dir / GLOBAL_FILE
    if global_path.is_file():
        paths.append(global_path)
    for path in sorted(checkouts_dir.glob("*.yaml")):
        if path.name != GLOBAL_FILE:
            paths.append(path)

    for path in paths:
        try:
            data, _raw = _load_yaml(path)
        except ComponentCatalogueError as exc:
            raise RepositoryLifecycleError(f"{path.name}: {exc}") from exc
        for entry in parse_checkout_file(
            data, source_file=path.name, known_repos=known_repos, known_accounts=known_accounts
        ):
            if entry.checkout_id in checkouts:
                prior = checkouts[entry.checkout_id]
                raise RepositoryLifecycleError(
                    f"duplicate checkout_id '{entry.checkout_id}' "
                    f"({prior.source_file} vs {entry.source_file})"
                )
            checkouts[entry.checkout_id] = entry

    return CheckoutSpec(checkouts=checkouts)


# ---------------------------------------------------------------------------
# Generated schema (byte-compared in tests; written to schemas/)
# ---------------------------------------------------------------------------


def _checkout_spec_schema() -> dict[str, object]:
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    text = {"type": "string", "minLength": 1}
    mode = {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 1,
        "properties": {
            "pinned": {"type": "string", "pattern": _SHA_RE.pattern},
            "tracking": {"type": "string", "pattern": _BRANCH_RE.pattern},
        },
        "oneOf": [{"required": ["pinned"]}, {"required": ["tracking"]}],
    }
    restart_contract = {
        "anyOf": [
            {"type": "null"},
            _object_schema(
                RESTART_CONTRACT_KEYS,
                {
                    "operation_family": {"type": "string", "pattern": _OP_FAMILY_RE.pattern},
                    "target_unit": {"type": "string", "pattern": _UNIT_RE.pattern},
                },
            ),
        ]
    }
    entry = {
        "type": "object",
        "required": list(_REQUIRED_ENTRY_KEYS),
        "additionalProperties": False,
        "properties": {
            "checkout_id": stable,
            "logical_repo": stable,
            "remote_ref": {"const": REMOTE_REF_MANIFEST},
            "target_account": text,
            "target_path": {"type": "string", "pattern": "^/"},
            "mode": mode,
            "update_policy": {"type": "string", "enum": list(_UPDATE_POLICIES)},
            "health_contract": _array_schema(),
            "restart_contract": restart_contract,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/checkout-spec.schema.json",
        "title": "Pantheon T-2951 declarative checkout-lifecycle spec",
        "type": "object",
        "required": list(_TOP_LEVEL_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "checkouts": _array_schema(entry),
        },
    }


def generate_checkout_spec_schema() -> bytes:
    return _json_bytes(_checkout_spec_schema())
