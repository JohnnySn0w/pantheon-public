"""The host-substrate verify invariant list and its evaluator (T-5084, GE).

CURRENT (co-author recovery; D15 approved at 71fd484cc). UNWIRED: the engine's verify leg
re-checks receipted ops for drift only and does not evaluate invariant lists
yet (engine seam gap S1, T-5135). Until S1 lands, ``list_digest`` is the
stopgap that keeps the contract's ``invariant_list_digest`` honest (the GF1
framing, T-5090) and ``evaluate`` is reachable from tests only.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from . import families as F

LIST_SCHEMA = "pantheon-invariant-list/v0"

#: Every invariant id the evaluator implements; the list file must match it.
CHECKS = (
    "HS1-accounts",
    "HS2-groups",
    "HS3-memberships",
    "HS4-linger",
    "HS5-owned-paths",
    "HS6-mailbox-dirs",
    "HS7-system-units",
    "HS8-dbus-policies",
    "HS9-sudoers-fragments",
    "HS10-legacy-requirements",
)


class InvariantListError(ValueError):
    pass


def check_list(doc: Any) -> None:
    if not isinstance(doc, dict) or doc.get("schema") != LIST_SCHEMA:
        raise InvariantListError(f"invariant list schema must be {LIST_SCHEMA}")
    ids = [i.get("id") for i in doc.get("invariants") or [] if isinstance(i, dict)]
    if tuple(ids) != CHECKS:
        raise InvariantListError(f"invariant ids {ids} != the evaluator's {list(CHECKS)}")


def list_digest(doc: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of {id, invariants} (status fields excluded)."""

    body = {"id": doc["id"], "invariants": doc["invariants"]}
    return "sha256:" + hashlib.sha256(F.canonical(body).encode("ascii")).hexdigest()


def _expect(manifest: F.Manifest) -> list[tuple[str, F.Resolved]]:
    m, out = manifest, []
    for a, spec in m.section("accounts").items():
        out.append(("HS1-accounts", F.resolve("account.ensure", {"account": a}, m)))
        for g in spec.get("groups") or []:
            out.append(("HS3-memberships", F.resolve("group.member", {"group": g, "account": a, "state": "present"}, m)))
        if spec.get("linger"):
            out.append(("HS4-linger", F.resolve("linger.enable", {"account": a}, m)))
    for g in m.section("groups"):
        out.append(("HS2-groups", F.resolve("group.ensure", {"group": g}, m)))
    for p, spec in m.section("paths").items():
        out.append(("HS5-owned-paths", F.resolve("owner_mode.set", {"path": p}, m)))
        if spec.get("acl"):
            out.append(("HS5-owned-paths", F.resolve("acl.converge", {"path": p}, m)))
    for mb in m.section("mailbox_dirs"):
        out.append(("HS6-mailbox-dirs", F.resolve("mailbox_dir.ensure", {"mailbox": mb}, m)))
    for u, spec in m.section("units").items():
        out.append(("HS7-system-units", F.resolve("unit.system.install", {"unit": u, "state": "present", "sha256": spec["sha256"]}, m)))
        if spec.get("enable"):
            out.append(("HS7-system-units", F.resolve("unit.system.enable", {"unit": u, "state": "enabled"}, m)))
    for p, spec in m.section("dbus_policies").items():
        out.append(("HS8-dbus-policies", F.resolve("dbus_policy.install", {"policy": p, "sha256": spec["sha256"]}, m)))
    for t, spec in m.section("sudoers_templates").items():
        sha = F.sha256_text(F.sudoers_text(spec))
        out.append(("HS9-sudoers-fragments", F.resolve("sudoers_fragment.ensure", {"template": t, "sha256": sha}, m)))
    return out


def evaluate(manifest: F.Manifest, subjects: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read-only: one result per invariant id over an observed subject map."""

    failures: dict[str, list[str]] = {cid: [] for cid in CHECKS}
    for cid, r in _expect(manifest):
        if subjects.get(r.subject) != r.after:
            failures[cid].append(r.subject)
    for req in manifest.doc.get("legacy_requirements") or []:
        if subjects.get(f"file:{req['path']}") is None:
            failures["HS10-legacy-requirements"].append(req["path"])
    return [{"id": cid, "ok": not bad, "drift": sorted(bad)} for cid, bad in failures.items()]
