#!/usr/bin/env python3
"""health_subscriptions — the health collector's subscription set as DATA (T-5980, ORG-080).

The fleet health collector (tools/health_collector.py) reads every uniform health emitter
(ORG-068 SS92). WHICH emitters it reads used to be code (a tuple of host services plus a
persona fan-out). It is now derived from the registry's emitter declarations:

  (a) contract ``emitter:`` blocks, scanned over tools/germinate/components/*.yaml
      (NON_CONTRACT_SUFFIXES files are skipped: invariant lists, manifests, sidecars):
        * ``interface: org.pantheon.PersonaController1`` (session bus): fans out over the
          persona roster as ``persona-controller.<persona>``; registry_component is the
          declaring contract's id.
        * ``interface: org.pantheon.Health1`` (system bus): one row per ``endpoints:`` item
          ``{component_id, bus_name, object_path}``; ``endpoints`` is REQUIRED for Health1
          and REJECTED for PersonaController1.
  (b) the sidecar tools/germinate/components/health-collector.subscriptions.yaml: rows for
      a component that is a real emitter but has no emitter declaration yet (today the
      Action Broker's two Health1 services, T-2920). A sidecar row is refused as soon as
      its registry_component's own contract declares an emitter (retire the row).

Every malformed or contradictory declaration raises SubscriptionError; nothing is guessed
and nothing is silently dropped. Pure reads of YAML files: no bus, no registry/systems.yaml
unless the caller passes ``registry_ids``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

HEALTH1_INTERFACE = "org.pantheon.Health1"
PC_INTERFACE = "org.pantheon.PersonaController1"
INTERFACE_BUS = {PC_INTERFACE: "session", HEALTH1_INTERFACE: "system"}

COMPONENTS_DIR = Path(__file__).resolve().parent / "germinate" / "components"
SIDECAR_NAME = "health-collector.subscriptions.yaml"
#: files under components/ that are not component contracts (the ONE list; the handler imports it)
NON_CONTRACT_SUFFIXES = (".invariants.yaml", ".manifest.yaml", ".subscriptions.yaml")

_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_BUS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")
_OBJECT_PATH_RE = re.compile(r"^/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*$")
_TICKET_RE = re.compile(r"^T-[0-9]+$")
_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")   # == health_export._IDENT_RE (persona / account)
_MAX_NAME = 255


class SubscriptionError(ValueError):
    """The registry's emitter declarations do not yield a usable subscription set."""


@dataclass(frozen=True)
class Subscription:
    component_id: str
    kind: str                     # "host_service" | "persona_controller"
    registry_component: str
    bus_class: str                # "system" | "session"
    interface: str
    bus_name: str | None = None   # host services only
    object_path: str | None = None
    persona: str | None = None    # persona controllers only
    account: str | None = None


def _is_str(v: object, pattern: re.Pattern) -> bool:
    return isinstance(v, str) and len(v) <= _MAX_NAME and v.isascii() and bool(pattern.fullmatch(v))


def contract_paths(components_dir: Path) -> list[Path]:
    return [p for p in sorted(Path(components_dir).glob("*.yaml")) if not p.name.endswith(NON_CONTRACT_SUFFIXES)]


def _load(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SubscriptionError(f"{path.name}: unreadable ({exc.__class__.__name__})") from exc


def declared_emitters(components_dir: Path | None = None) -> dict[str, dict]:
    """``{component_id: emitter block}`` for every contract that declares an ``emitter:`` block."""

    out: dict[str, dict] = {}
    for path in contract_paths(components_dir or COMPONENTS_DIR):
        doc = _load(path)
        body = doc.get("component") if isinstance(doc, dict) else None
        if not isinstance(body, dict):
            continue
        emitter = body.get("emitter")
        if emitter is None:
            continue
        cid = body.get("id")
        if not _is_str(cid, _COMPONENT_RE):
            raise SubscriptionError(f"{path.name}: emitter declared on a contract without a usable id")
        if not isinstance(emitter, dict):
            raise SubscriptionError(f"{cid}: emitter block is not a mapping")
        out[cid] = emitter
    return out


def _host_row(where: str, row: object, registry_component: str, interface: str) -> Subscription:
    if not isinstance(row, dict):
        raise SubscriptionError(f"{where}: endpoint row is not a mapping")
    cid, bus_name, path = row.get("component_id"), row.get("bus_name"), row.get("object_path")
    if not _is_str(cid, _COMPONENT_RE):
        raise SubscriptionError(f"{where}: malformed component_id {cid!r}")
    if not _is_str(bus_name, _BUS_NAME_RE):
        raise SubscriptionError(f"{where}: {cid}: malformed bus_name {bus_name!r}")
    if not _is_str(path, _OBJECT_PATH_RE):
        raise SubscriptionError(f"{where}: {cid}: malformed object_path {path!r}")
    return Subscription(cid, "host_service", registry_component, "system", interface, bus_name, path)


def _emitter_rows(cid: str, emitter: dict, personas) -> list[Subscription]:
    interface = emitter.get("interface")
    bus = INTERFACE_BUS.get(interface)
    if bus is None:
        raise SubscriptionError(f"{cid}: emitter interface {interface!r} is not a known health interface")
    if "bus" in emitter and emitter["bus"] != bus:
        raise SubscriptionError(f"{cid}: emitter bus {emitter['bus']!r} disagrees with {interface} ({bus})")
    endpoints = emitter.get("endpoints")
    if interface == PC_INTERFACE:
        if endpoints is not None:
            raise SubscriptionError(f"{cid}: endpoints are not allowed on a {PC_INTERFACE} emitter (it fans out over the roster)")
        rows = []
        for persona, account, _pane in personas:
            if not (_is_str(persona, _IDENT_RE) and _is_str(account, _IDENT_RE)):
                continue
            rows.append(Subscription(f"persona-controller.{persona}", "persona_controller", cid, "session",
                                     interface, persona=persona, account=account))
        return rows
    if not isinstance(endpoints, list) or not endpoints:
        raise SubscriptionError(f"{cid}: a {HEALTH1_INTERFACE} emitter must declare endpoints "
                                "[{component_id, bus_name, object_path}]")
    return [_host_row(f"{cid} endpoints", row, cid, interface) for row in endpoints]


def _sidecar_rows(components_dir: Path, declared: dict[str, dict], registry_ids) -> list[Subscription]:
    path = Path(components_dir) / SIDECAR_NAME
    if not path.is_file():
        return []
    doc = _load(path) or {}
    rows = doc.get("subscriptions") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise SubscriptionError(f"{SIDECAR_NAME}: `subscriptions:` is not a list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise SubscriptionError(f"{SIDECAR_NAME}: row is not a mapping")
        reg = row.get("registry_component")
        if not _is_str(reg, _COMPONENT_RE):
            raise SubscriptionError(f"{SIDECAR_NAME}: malformed registry_component {reg!r}")
        if row.get("interface") != HEALTH1_INTERFACE:
            raise SubscriptionError(f"{SIDECAR_NAME}: {reg}: interface must be {HEALTH1_INTERFACE}, got {row.get('interface')!r}")
        if not _is_str(row.get("ticket"), _TICKET_RE):
            raise SubscriptionError(f"{SIDECAR_NAME}: {reg}: malformed ticket {row.get('ticket')!r}")
        if reg in declared:
            raise SubscriptionError(f"{SIDECAR_NAME}: {reg} now declares an emitter in its contract; "
                                    "retire its sidecar rows")
        if registry_ids is not None and reg not in registry_ids:
            raise SubscriptionError(f"{SIDECAR_NAME}: registry_component {reg!r} is not a registry system")
        out.append(_host_row(SIDECAR_NAME, row, reg, HEALTH1_INTERFACE))
    return out


def derive(personas, components_dir: Path | None = None, *, registry_ids=None) -> tuple[Subscription, ...]:
    """The subscription set for ``personas`` (the roster) from the contracts + sidecar.

    ``registry_ids``: optional set of registry system ids. The runtime collector passes None
    (its poll path reads only its own data); the configure handler passes the systems.yaml ids,
    and a sidecar registry_component outside them is then a SubscriptionError."""

    components_dir = Path(components_dir or COMPONENTS_DIR)
    personas = list(personas)
    declared = declared_emitters(components_dir)
    rows: list[Subscription] = []
    for cid in sorted(declared):
        rows.extend(_emitter_rows(cid, declared[cid], personas))
    rows.extend(_sidecar_rows(components_dir, declared, registry_ids))
    seen: set[str] = set()
    for row in rows:
        if row.component_id in seen:
            raise SubscriptionError(f"duplicate component_id {row.component_id!r} in the subscription set")
        seen.add(row.component_id)
    return tuple(rows)
