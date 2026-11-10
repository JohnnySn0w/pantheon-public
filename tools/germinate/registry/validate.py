"""ORG-081 registry validator: schema, closed enums, evidence and adoption rules.

Three layers, each a pure function returning ``Finding`` records:

* ``validate_document`` — structure only (ORG-081 §1-§4, §6; D-1, D-2, D-3/D-11,
  D-8, D-12). Needs nothing but the parsed document.
* ``check_refs`` — repo-root reference checks: cited tickets and ADRs exist,
  ``contract.repo`` is a known repo, an ``unpinned`` internal module really is
  absent (D-6, D-9 v0 half).
* ``check_aliases`` — the D-13 bounded scheduler-alias migration.

* ``check_contracts`` — pinned rows against their contract modules (T-5074):
  the D-9 digest recompute by the sole oracle, the steward/id mirror (D-AA),
  the D-11(b) invariant-list binding, the statically checkable D-8 adoption
  predicates, and ORG-081 §7 external resolution through an ``ExternalResolver``
  (fail-closed when none is configured).

``validate_repo`` composes all four over a checkout.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from tools.germinate.registry import schema as S
from tools.germinate.registry.load import RegistryLoadError, load_file

REGISTRY_REL = Path("registry/systems.yaml")
ALIASES_REL = Path("registry/scheduler-aliases.yaml")
SCHEDULES_REL = Path("schedules")
ADR_DIR_REL = Path("architecture/adr")
TICKETS_REL = Path("tickets")
REPOS_REL = Path("repos.yaml")

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    level: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


class _Sink:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def err(self, where: str, message: str) -> None:
        self.findings.append(Finding(ERROR, where, message))

    def warn(self, where: str, message: str) -> None:
        self.findings.append(Finding(WARNING, where, message))


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == ERROR]


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_keys(sink: _Sink, where: str, obj: Mapping, required: Iterable[str], optional: Iterable[str]) -> None:
    required = set(required)
    allowed = required | set(optional)
    for key in sorted(required - set(obj), key=str):
        sink.err(where, f"missing required field {key!r}")
    for key in sorted(set(obj) - allowed, key=str):
        sink.err(where, f"field {key!r} is not allowed here")


# ---------------------------------------------------------------------------
# Cells (legs / legacy_coverage)
# ---------------------------------------------------------------------------


def _check_cell(sink: _Sink, where: str, leg: str, cell: Any) -> None:
    if not isinstance(cell, dict):
        sink.err(where, "a leg cell must be a mapping with a status")
        return
    status = cell.get("status")
    if status not in S.STATUS_FIELDS:
        sink.err(where, f"status {status!r} is not one of {', '.join(S.STATUSES)} (exact bytes)")
        return
    fields = S.STATUS_FIELDS[status]
    _check_keys(sink, where, cell, fields | {"status"}, S.CELL_OPTIONAL)
    if "note" in cell and not _nonempty_str(cell["note"]):
        sink.err(where, "note must be a non-empty string")
    if status == S.STATUS_COVERED and "evidence" in cell:
        evidence = cell["evidence"]
        if leg == "health":
            # D-11: emitter-install alone never promotes the health cell.
            if not isinstance(evidence, dict) or set(evidence) != S.HEALTH_EVIDENCE_KEYS:
                sink.err(
                    where,
                    "covered health needs evidence as a mapping with exactly "
                    f"{', '.join(sorted(S.HEALTH_EVIDENCE_KEYS))} (ORG-081 D-11)",
                )
            elif not all(_nonempty_str(v) for v in evidence.values()):
                sink.err(where, "each health evidence predicate needs a non-empty reference")
        elif not _nonempty_str(evidence):
            sink.err(where, "evidence must be a non-empty string")
    if status in (S.STATUS_PARTIAL, S.STATUS_MISSING):
        if "gap" in cell and not _nonempty_str(cell["gap"]):
            sink.err(where, "gap must be a non-empty string")
        if "ticket" in cell and not (isinstance(cell["ticket"], str) and S.TICKET_RE.match(cell["ticket"])):
            sink.err(where, f"ticket {cell['ticket']!r} is not a T-NNNN id")
    if leg == "health" and status == S.STATUS_NOT_APPLICABLE:
        # ORG-081 §2 reserves not_applicable for a leg that cannot exist; ORG-068
        # E9.C3 makes failure observability universal, so a health leg always exists.
        # Waiving it is partial_by_design with an E9.C3 acknowledgement (D-11).
        sink.err(where, "health cannot be not_applicable (ORG-068 E9.C3); use partial_by_design with a waiver")
    if status in S.WAIVED_STATUSES and "waiver" in cell:
        waiver = cell["waiver"]
        if not _nonempty_str(waiver):
            sink.err(where, "waiver must be a non-empty string")
        elif leg == "health" and status == S.STATUS_PARTIAL_BY_DESIGN and S.HEALTH_WAIVER_MARKER not in waiver:
            sink.err(
                where,
                f"a partial_by_design health waiver must acknowledge {S.HEALTH_WAIVER_MARKER} non-cover (ORG-081 D-11)",
            )


def _check_leg_block(sink: _Sink, where: str, block: Any, *, require_all: bool) -> dict:
    if not isinstance(block, dict) or not block:
        sink.err(where, "must be a non-empty mapping of legs")
        return {}
    for key in sorted(set(block) - set(S.LEGS), key=str):
        sink.err(where, f"leg {key!r} is not one of {', '.join(S.LEGS)}")
    if require_all:
        for leg in S.LEGS:
            if leg not in block:
                sink.err(where, f"missing leg {leg!r}")
    for leg in S.LEGS:
        if leg in block:
            _check_cell(sink, f"{where}.{leg}", leg, block[leg])
    return block


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _check_adr_list(sink: _Sink, where: str, value: Any, *, required_nonempty: bool) -> None:
    if not isinstance(value, list) or (required_nonempty and not value):
        sink.err(where, "adr must be a " + ("non-empty " if required_nonempty else "") + "list of ORG-NNN ids")
        return
    for item in value:
        if not (isinstance(item, str) and S.ADR_ID_RE.match(item)):
            sink.err(where, f"adr entry {item!r} is not an ORG-NNN id")


def _check_contract(sink: _Sink, where: str, contract: Any, adoption: Any) -> None:
    if not isinstance(contract, dict):
        sink.err(where, "contract must be a mapping")
        return
    _check_keys(sink, where, contract, S.CONTRACT_REQUIRED, S.CONTRACT_OPTIONAL)
    for key in ("repo", "module"):
        if key in contract and not _nonempty_str(contract[key]):
            sink.err(where, f"{key} must be a non-empty string")
    digest = contract.get("digest")
    if digest == S.DIGEST_UNPINNED:
        # PROVISIONAL (T-5073): unpinned only before any contract module exists.
        if adoption != S.ADOPTION_NOT_ADOPTED:
            sink.err(where, "digest: unpinned is allowed only when contract_adoption is not_adopted")
        if not _nonempty_str(contract.get("unpinned_reason")):
            sink.err(where, "digest: unpinned requires a non-empty unpinned_reason")
    elif isinstance(digest, str) and S.DIGEST_RE.match(digest):
        if "unpinned_reason" in contract:
            sink.err(where, "unpinned_reason is only allowed with digest: unpinned")
        if contract.get("repo") != S.INTERNAL_REPO and "repo_revision" not in contract:
            sink.err(where, "an external-repo contract with a pinned digest requires repo_revision (ORG-081 §1)")
    elif "digest" in contract:
        sink.err(where, f"digest {digest!r} is neither sha256:<64 hex> nor {S.DIGEST_UNPINNED!r}")
    if "repo_revision" in contract:
        rev = contract["repo_revision"]
        if not (isinstance(rev, str) and S.REVISION_RE.match(rev)):
            sink.err(where, f"repo_revision {rev!r} is not a 40-hex commit sha")


def _check_adoption(sink: _Sink, where: str, row: Mapping) -> None:
    adoption = row.get("contract_adoption")
    if adoption not in S.ADOPTION_VALUES:
        sink.err(where, f"contract_adoption {adoption!r} is not one of {', '.join(S.ADOPTION_VALUES)}")
        return
    has_legs = "legs" in row
    if adoption == S.ADOPTION_NOT_ADOPTED:
        for key in ("legs", "adoption_evidence", "adoption_gaps"):
            if key in row:
                sink.err(where, f"{key} is forbidden when contract_adoption is not_adopted (ORG-081 §3)")
        if "legacy_coverage" in row:
            _check_leg_block(sink, f"{where}.legacy_coverage", row["legacy_coverage"], require_all=False)
        return

    # partial / adopted
    if not has_legs:
        sink.err(where, f"legs is required when contract_adoption is {adoption}")
    legs = _check_leg_block(sink, f"{where}.legs", row["legs"], require_all=True) if has_legs else {}
    evidence = row.get("adoption_evidence")
    if not isinstance(evidence, dict):
        sink.err(where, f"adoption_evidence is required (a mapping) when contract_adoption is {adoption}")
        evidence = {}
    for key in sorted(set(evidence) - set(S.EVIDENCE_FIELDS), key=str):
        sink.err(f"{where}.adoption_evidence", f"field {key!r} is not one of {', '.join(S.EVIDENCE_FIELDS)}")
    for key, value in evidence.items():
        if key in S.EVIDENCE_FIELDS and not _nonempty_str(value):
            sink.err(f"{where}.adoption_evidence.{key}", "must be a non-empty evidence reference")
    present = {k for k in evidence if k in S.EVIDENCE_FIELDS}

    if adoption == S.ADOPTION_ADOPTED:
        for key in ("adoption_gaps", "legacy_coverage"):
            if key in row:
                sink.err(where, f"{key} is forbidden when contract_adoption is adopted (ORG-081 §3)")
        for field in S.EVIDENCE_FIELDS:
            if field not in present:
                sink.err(where, f"adopted requires adoption_evidence.{field}")
        for leg, cell in legs.items():
            if isinstance(cell, dict) and cell.get("status") in (S.STATUS_PARTIAL, S.STATUS_MISSING):
                sink.err(f"{where}.legs.{leg}", "adopted requires every leg covered or explicitly waived")
        return

    # partial: one typed gap per absent predicate (D-8)
    gaps = row.get("adoption_gaps", {})
    if not isinstance(gaps, dict):
        sink.err(where, "adoption_gaps must be a mapping keyed by predicate")
        gaps = {}
    for key, entry in gaps.items():
        gwhere = f"{where}.adoption_gaps.{key}"
        if key not in S.PREDICATE_KEYS:
            sink.err(gwhere, "not in the closed 16-predicate set (ORG-081 §1)")
            continue
        if key in S.PARTIAL_MANDATORY:
            sink.err(gwhere, "plan_install and plan_remove are mandatory for partial and cannot be gaps")
        if not isinstance(entry, dict):
            sink.err(gwhere, "a gap entry must be {gap, ticket}")
            continue
        _check_keys(sink, gwhere, entry, S.GAP_ENTRY_KEYS, ())
        if "gap" in entry and not _nonempty_str(entry["gap"]):
            sink.err(gwhere, "gap must be a non-empty string")
        if "ticket" in entry and not (isinstance(entry["ticket"], str) and S.TICKET_RE.match(entry["ticket"])):
            sink.err(gwhere, f"ticket {entry['ticket']!r} is not a T-NNNN id")
    for field in S.EVIDENCE_FIELDS:
        gap_key = f"adoption_evidence.{field}"
        if field in present and gap_key in gaps:
            sink.err(where, f"{gap_key} is present but also listed as a gap")
        if field not in present and gap_key not in gaps:
            sink.err(where, f"{gap_key} is absent and has no adoption_gaps entry")
    if not gaps:
        sink.err(where, "partial with no adoption_gaps: every predicate is present, so the row is adopted")


def _check_component(sink: _Sink, where: str, row: Mapping) -> None:
    _check_keys(sink, where, row, S.COMPONENT_REQUIRED, S.COMPONENT_OPTIONAL)
    if "adr" in row:
        _check_adr_list(sink, where, row["adr"], required_nonempty=False)
    if "contract" in row:
        _check_contract(sink, f"{where}.contract", row["contract"], row.get("contract_adoption"))
    _check_adoption(sink, where, row)


def _check_external(sink: _Sink, where: str, row: Mapping) -> None:
    _check_keys(sink, where, row, S.EXTERNAL_REQUIRED, S.EXTERNAL_OPTIONAL)
    if "adr" in row:
        _check_adr_list(sink, where, row["adr"], required_nonempty=True)
    tracking = row.get("tracking")
    if "tracking" in row:
        if not isinstance(tracking, dict) or not tracking:
            sink.err(where, "tracking must be a non-empty mapping of name -> T-NNNN")
        else:
            for name, ticket in tracking.items():
                if not (isinstance(ticket, str) and S.TICKET_RE.match(ticket)):
                    sink.err(f"{where}.tracking.{name}", f"{ticket!r} is not a T-NNNN id")
    if "relationship" in row and not _nonempty_str(row["relationship"]):
        sink.err(where, "relationship must be a non-empty string")


def validate_document(doc: Any) -> list[Finding]:
    """Structural validation of a parsed registry document."""
    sink = _Sink()
    if not isinstance(doc, dict):
        sink.err("registry", "top level must be a mapping")
        return sink.findings
    _check_keys(sink, "registry", doc, S.TOP_LEVEL_KEYS, ())
    if "schema" in doc and doc["schema"] != S.SCHEMA_ID:
        sink.err("registry", f"schema must be exactly {S.SCHEMA_ID!r}, got {doc['schema']!r}")
    systems = doc.get("systems")
    if not isinstance(systems, list):
        sink.err("registry", "systems must be a list")
        return sink.findings
    seen: set[str] = set()
    for index, row in enumerate(systems):
        if not isinstance(row, dict):
            sink.err(f"systems[{index}]", "a row must be a mapping")
            continue
        row_id = row.get("id")
        where = f"systems[{index}]" + (f" ({row_id})" if isinstance(row_id, str) else "")
        if not (isinstance(row_id, str) and S.ID_RE.match(row_id)):
            sink.err(where, f"id {row_id!r} is not a lower-kebab-case id")
        elif row_id in seen:
            sink.err(where, f"duplicate id {row_id!r}")
        else:
            seen.add(row_id)
        for key in ("title", "steward"):
            if key in row and not _nonempty_str(row[key]):
                sink.err(where, f"{key} must be a non-empty string")
        if "note" in row and not _nonempty_str(row["note"]):
            sink.err(where, "note must be a non-empty string")
        kind = row.get("kind")
        if kind == S.KIND_COMPONENT:
            _check_component(sink, where, row)
        elif kind == S.KIND_EXTERNAL_PLANE:
            _check_external(sink, where, row)
        else:
            sink.err(where, f"kind {kind!r} is not one of {', '.join(S.KINDS)}")
    return sink.findings


# ---------------------------------------------------------------------------
# Repo-root reference checks
# ---------------------------------------------------------------------------


def _iter_tickets(doc: Mapping) -> Iterable[tuple[str, str, bool]]:
    """(where, ticket, closes_a_gap). Gap tickets must be open; tracking refs need only exist."""
    for row in doc.get("systems") or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("id", "?")
        for block in ("legs", "legacy_coverage"):
            cells = row.get(block) if isinstance(row.get(block), dict) else {}
            for leg, cell in cells.items():
                if isinstance(cell, dict) and isinstance(cell.get("ticket"), str):
                    yield f"{rid}.{block}.{leg}", cell["ticket"], True
        gaps = row.get("adoption_gaps")
        if isinstance(gaps, dict):
            for key, entry in gaps.items():
                if isinstance(entry, dict) and isinstance(entry.get("ticket"), str):
                    yield f"{rid}.adoption_gaps.{key}", entry["ticket"], True
        tracking = row.get("tracking")
        if isinstance(tracking, dict):
            for name, ticket in tracking.items():
                if isinstance(ticket, str):
                    yield f"{rid}.tracking.{name}", ticket, False


def gap_cells_citing(doc: Mapping, ticket: str) -> list[str]:
    """Cell paths whose `ticket:` names `ticket` as the owner of an open gap.

    Same iteration and same `closes_gap` rule as `_check_tickets_exist`, so a ticket
    close that passes this check cannot turn `registry validate` red (T-5496).
    """
    return [where for where, cited, closes_gap in _iter_tickets(doc) if closes_gap and cited == ticket]


def known_repos(repo_root: Path) -> set[str]:
    data = load_file(repo_root / REPOS_REL) or {}
    names = {S.INTERNAL_REPO}
    for entry in data.get("repos") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.add(entry["name"])
    for entry in data.get("always_available") or []:
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str):
            names.add(entry["repo"])
    return names


def repo_pins(repo_root: Path) -> dict[str, Any]:
    """{repo name: repos.yaml ``revision`` value} for rows that carry one (ORG-081 §7, T-5113).

    Values are returned raw: the caller refuses a malformed pin rather than silently dropping it.
    """
    if not (repo_root / REPOS_REL).exists():
        return {}  # no pins: every external row stays fail-closed
    data = load_file(repo_root / REPOS_REL) or {}
    return {
        entry["name"]: entry["revision"]
        for entry in data.get("repos") or []
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and "revision" in entry
    }


def _check_tickets_exist(sink: _Sink, refs: Iterable[tuple[str, str, bool]], repo_root: Path) -> None:
    tickets_dir = repo_root / TICKETS_REL
    for where, ticket, closes_gap in refs:
        if not S.TICKET_RE.match(ticket):
            continue  # already a structural error
        hits = sorted(tickets_dir.glob(f"*/{ticket}.md"))
        if not hits:
            sink.err(where, f"ticket {ticket} not found under {TICKETS_REL}/")
        elif all(hit.parent.name == "done" for hit in hits):
            if closes_gap:
                # ORG-081 §2: a partial/missing gap names the OPEN ticket that closes it.
                sink.err(where, f"ticket {ticket} is done, so it cannot close this gap: re-cite or change the status")
            else:
                sink.warn(where, f"ticket {ticket} is done")


def alias_tickets(aliases_doc: Any) -> list[tuple[str, str, bool]]:
    entries = aliases_doc.get("aliases") if isinstance(aliases_doc, dict) else None
    out = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and isinstance(entry.get("ticket"), str):
            out.append((f"{ALIASES_REL}[{index}]", entry["ticket"], False))
    return out


def check_refs(doc: Mapping, repo_root: Path, aliases_doc: Any = None) -> list[Finding]:
    sink = _Sink()
    _check_tickets_exist(sink, list(_iter_tickets(doc)) + alias_tickets(aliases_doc), repo_root)
    adr_dir = repo_root / ADR_DIR_REL
    repos = known_repos(repo_root)
    for row in doc.get("systems") or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("id", "?")
        adrs = row.get("adr") if isinstance(row.get("adr"), list) else []
        for adr in adrs:
            if isinstance(adr, str) and S.ADR_ID_RE.match(adr) and not any(adr_dir.glob(f"{adr}-*.md")):
                sink.err(f"{rid}.adr", f"{adr} not found under {ADR_DIR_REL}/")
        contract = row.get("contract")
        if row.get("kind") != S.KIND_COMPONENT or not isinstance(contract, dict):
            continue
        repo = contract.get("repo")
        if isinstance(repo, str) and repo not in repos:
            sink.err(f"{rid}.contract", f"repo {repo!r} is not in {REPOS_REL}")
        module = contract.get("module")
        if contract.get("digest") == S.DIGEST_UNPINNED and repo == S.INTERNAL_REPO and isinstance(module, str):
            if (repo_root / module).exists():
                sink.err(
                    f"{rid}.contract",
                    f"module {module} exists, so the digest must be pinned, not {S.DIGEST_UNPINNED!r}",
                )
    return sink.findings


# ---------------------------------------------------------------------------
# Contract-module checks (T-5074: D-9, D-AA mirror, D-8 presence, D-11(b), §7)
# ---------------------------------------------------------------------------


class ExternalResolver(Protocol):
    """ORG-081 §7: fetch an external repo's contract module at a pinned revision.

    Returns a local path to the module's bytes at ``revision``, or None if the
    file does not exist there. repos.yaml's per-repo ``revision`` is the ratified
    pin (T-5113); ``tools.germinate.registry.git_resolver.GitExternalResolver`` is the
    git-backed implementation, opt-in via ``registry validate --resolve-external``.
    Without a resolver, validation refuses every pinned external row (fail-closed).
    A resolver may raise ``Exception`` for fetch failures; those become findings.
    """

    def resolve(self, repo: str, revision: str, module: str) -> Path | None: ...


#: D-8 non-evidence predicates checkable without running component code.
#: ``crash_contract`` is a property of every planned op; the engine refuses an
#: op without one at plan time, so it has no static check here.
STATIC_PREDICATES = tuple(f"plan_{leg}" for leg in ("install", "configure", "update", "remove", "repair")) + (
    "verify_contract",
    "receipt_schema",
    "sandbox",
)


def _check_one_contract(sink: _Sink, rid: str, row: Mapping, module_path: Path, op_registry: Any = None) -> None:
    from tools.germinate.engine import contract as C
    from tools.germinate.registry.contract_digest import ContractDigestError, compute_contract_digest

    where = f"{rid}.contract"
    before = len(errors(sink.findings))
    try:
        digest = compute_contract_digest(module_path)
        body = C.read_component(module_path)
    except (ContractDigestError, C.ContractError) as exc:
        sink.err(where, f"contract module unusable: {exc}")
        return
    if row["contract"].get("digest") != digest:
        sink.err(where, f"digest {row['contract'].get('digest')} != recomputed {digest} (ORG-081 D-9)")
    if body.get("contract_digest") != digest:
        sink.err(where, f"the module's own contract_digest {body.get('contract_digest')!r} != recomputed {digest}")
    if body.get("id") != rid:
        sink.err(where, f"contract module declares id {body.get('id')!r}")
    if body.get("steward") != row.get("steward"):
        sink.err(where, f"steward {row.get('steward')!r} != contract steward {body.get('steward')!r} (digest-bound mirror, D-AA)")
    for message in C.invariant_binding_errors(body) or C.invariant_digest_errors(body, module_path):
        sink.err(where, message)
    for message in C.handlers_pin_errors(body, module_path):  # T-5477: handler drift reds CI, not only runtime
        sink.err(where, message)
    if op_registry is not None and len(errors(sink.findings)) == before:
        # The engine's own plan-time load (privilege lists against the merged op
        # registry, planner_kind, legs, gates...): a contract that would block
        # every plan fails here, in CI, instead.
        try:
            C.load_contract(module_path, op_registry)
        except C.ContractError as exc:
            sink.err(where, f"the engine would refuse this contract at plan: {exc}")
    adoption = row.get("contract_adoption")
    if adoption not in (S.ADOPTION_PARTIAL, S.ADOPTION_ADOPTED):
        return
    present = C.static_predicates(body, C.planner_path_for(module_path))
    gaps = row.get("adoption_gaps") if isinstance(row.get("adoption_gaps"), dict) else {}
    for predicate in STATIC_PREDICATES:
        if present[predicate] and predicate in gaps:
            sink.err(f"{rid}.adoption_gaps.{predicate}", "listed as a gap, but the contract module ships it (ORG-081 D-8)")
        elif not present[predicate]:
            if adoption == S.ADOPTION_ADOPTED:
                sink.err(rid, f"adopted, but the contract module lacks {predicate} (ORG-081 D-8)")
            elif predicate in S.PARTIAL_MANDATORY:
                sink.err(rid, f"partial requires {predicate}, which the contract module lacks (ORG-081 §3)")
            elif predicate not in gaps:
                sink.err(rid, f"the contract module lacks {predicate} and adoption_gaps does not list it (ORG-081 D-8)")


def _merged_op_registry(sink: _Sink, doc: Mapping, repo_root: Path) -> Any:
    """The op registry the engine would load: component OP_KINDS read as data, never imported."""

    from tools.germinate.engine.components import ComponentLoadError, discover_doc, merged_kinds
    from tools.germinate.op_registry import OpRegistryError, load_op_registry

    try:
        return load_op_registry(merged_kinds(discover_doc(repo_root, doc, import_handlers=False)))
    except (ComponentLoadError, OpRegistryError) as exc:
        sink.err("registry", f"component op kinds refused: {exc}")
        return None


def check_contracts(doc: Mapping, repo_root: Path, resolver: ExternalResolver | None = None) -> list[Finding]:
    sink = _Sink()
    pinned = [
        r
        for r in doc.get("systems") or []
        if isinstance(r, dict)
        and r.get("kind") == S.KIND_COMPONENT
        and isinstance(r.get("contract"), dict)
        and isinstance(r["contract"].get("digest"), str)
        and S.DIGEST_RE.match(r["contract"]["digest"])
    ]
    op_registry = _merged_op_registry(sink, doc, repo_root) if pinned else None
    pins = repo_pins(repo_root) if pinned else {}
    for row in doc.get("systems") or []:
        if not isinstance(row, dict) or row.get("kind") != S.KIND_COMPONENT:
            continue
        contract = row.get("contract")
        module = contract.get("module") if isinstance(contract, dict) else None
        if isinstance(module, str) and (Path(module).is_absolute() or ".." in Path(module).parts):
            # containment applies to unpinned rows too: nothing may name host code outside the checkout
            if not (isinstance(contract.get("digest"), str) and S.DIGEST_RE.match(contract["digest"])):
                sink.err(f"{row.get('id', '?')}.contract", f"module {module!r} must be a relative path without '..'")
                continue
        if not isinstance(contract, dict) or not (isinstance(contract.get("digest"), str) and S.DIGEST_RE.match(contract["digest"])):
            continue  # unpinned rows are checked by check_refs
        rid = row.get("id", "?")
        module = contract.get("module")
        if not isinstance(module, str):
            continue
        from tools.germinate.engine.contract import ContractError, contained_module_path

        if Path(module).is_absolute() or ".." in Path(module).parts:
            sink.err(f"{rid}.contract", f"module {module!r} must be a relative path without '..'")
            continue
        if contract.get("repo") == S.INTERNAL_REPO:
            try:
                path = contained_module_path(repo_root, module)
            except ContractError as exc:
                sink.err(f"{rid}.contract", str(exc))
                continue
            if not path.is_file():
                sink.err(f"{rid}.contract", f"pinned module {module} does not exist (ORG-081 D-9)")
                continue
        else:
            revision = contract.get("repo_revision")
            if not isinstance(revision, str):
                continue  # structural error already reported
            repo = contract["repo"]
            pin = pins.get(repo)
            if resolver is None:
                hint = "" if pin is not None else f"; {REPOS_REL} has no revision pin for {repo}"
                sink.err(
                    f"{rid}.contract",
                    f"external contract {repo}:{module}@{revision} cannot be resolved: no resolver "
                    f"is configured (ORG-081 §7; fail-closed){hint}",
                )
                continue
            if pin is None:
                sink.err(f"{rid}.contract", f"{repo} has no revision pin in {REPOS_REL}; an unpinned external repo is refused (ORG-081 §7)")
                continue
            if not (isinstance(pin, str) and S.REVISION_RE.match(pin)):
                sink.err(f"{rid}.contract", f"{REPOS_REL} revision {pin!r} for {repo} is not a 40-hex commit sha")
                continue
            if revision != pin:
                sink.err(f"{rid}.contract", f"repo_revision {revision} != {REPOS_REL} pin {pin} for {repo} (ORG-081 D-9)")
                continue
            try:
                resolved = resolver.resolve(repo, revision, module)
            except Exception as exc:  # a fetch failure is a finding; the validator never crashes on a remote
                sink.err(f"{rid}.contract", f"external contract {repo}:{module}@{revision} cannot resolve: {exc} (ORG-081 §7)")
                continue
            if resolved is None:
                sink.err(f"{rid}.contract", f"{repo}@{revision} has no {module} (ORG-081 §7)")
                continue
            path = Path(resolved)
        _check_one_contract(sink, rid, row, path, op_registry)
    return sink.findings


# ---------------------------------------------------------------------------
# D-13 scheduler alias migration
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> _dt.date | None:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def schedule_systems(schedules_dir: Path) -> list[tuple[str, str]]:
    """(where, system) for every schedules/*.yaml entry carrying `system:`."""
    out: list[tuple[str, str]] = []
    for path in sorted(schedules_dir.glob("*.yaml")):
        data = load_file(path) or {}
        for index, entry in enumerate(data.get("entries") or []):
            if isinstance(entry, dict) and "system" in entry:
                label = entry.get("name", index)
                out.append((f"{schedules_dir.name}/{path.name}[{label}]", entry["system"]))
    return out


def check_aliases(
    doc: Mapping,
    aliases_doc: Any,
    systems: Iterable[tuple[str, str]],
    today: _dt.date,
) -> list[Finding]:
    sink = _Sink()
    ids = {r.get("id") for r in doc.get("systems") or [] if isinstance(r, dict)}
    live: dict[str, str] = {}
    if aliases_doc is not None:
        where = str(ALIASES_REL)
        if not isinstance(aliases_doc, dict):
            sink.err(where, "top level must be a mapping")
            aliases_doc = {}
        _check_keys(sink, where, aliases_doc, {"schema", "aliases"}, ())
        if aliases_doc.get("schema", S.ALIASES_SCHEMA_ID) != S.ALIASES_SCHEMA_ID:
            sink.err(where, f"schema must be exactly {S.ALIASES_SCHEMA_ID!r}")
        entries = aliases_doc.get("aliases") or []
        if not isinstance(entries, list):
            sink.err(where, "aliases must be a list")
            entries = []
        for index, entry in enumerate(entries):
            ewhere = f"{where}[{index}]"
            if not isinstance(entry, dict):
                sink.err(ewhere, "an alias entry must be a mapping")
                continue
            _check_keys(sink, ewhere, entry, {"alias", "id", "expires", "ticket"}, ())
            alias, target = entry.get("alias"), entry.get("id")
            expires = _as_date(entry.get("expires"))
            if not _nonempty_str(alias):
                sink.err(ewhere, "alias must be a non-empty string")
                continue
            if target not in ids:
                sink.err(ewhere, f"alias target {target!r} is not a registry id")
            if "expires" in entry and expires is None:
                sink.err(ewhere, "expires must be a YYYY-MM-DD date")
            if not (isinstance(entry.get("ticket"), str) and S.TICKET_RE.match(entry["ticket"])):
                sink.err(ewhere, f"ticket {entry.get('ticket')!r} is not a T-NNNN id")
            if expires is not None and today > expires:
                sink.err(ewhere, f"alias {alias!r} expired {expires.isoformat()} (ORG-081 D-13): cut the schedule over")
                continue
            if alias in live:
                sink.err(ewhere, f"duplicate alias {alias!r}")
            live[alias] = target
    for where, system in systems:
        if system in ids:
            continue
        if system in live:
            continue
        sink.err(where, f"system {system!r} is neither a registry id nor an unexpired alias (ORG-081 D-13)")
    return sink.findings


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def validate_repo(
    repo_root: Path,
    *,
    registry: Path | None = None,
    today: _dt.date | None = None,
    resolver: ExternalResolver | None = None,
) -> list[Finding]:
    today = today or _dt.date.today()
    registry = registry or repo_root / REGISTRY_REL
    try:
        doc = load_file(registry)
    except RegistryLoadError as exc:
        return [Finding(ERROR, str(registry), str(exc))]
    findings = validate_document(doc)
    if not isinstance(doc, dict):
        return findings
    try:
        aliases_path = repo_root / ALIASES_REL
        aliases_doc = load_file(aliases_path) if aliases_path.exists() else None
        findings += check_refs(doc, repo_root, aliases_doc)
        schedules_dir = repo_root / SCHEDULES_REL
        systems = schedule_systems(schedules_dir) if schedules_dir.is_dir() else []
        findings += check_aliases(doc, aliases_doc, systems, today)
        findings += check_contracts(doc, repo_root, resolver)
    except RegistryLoadError as exc:
        findings.append(Finding(ERROR, "repo", str(exc)))
    return findings
