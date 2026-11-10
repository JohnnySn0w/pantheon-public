"""harness-manager root half: the pure seat-identity resolver (T-5827, T-5091 U2, GF2).

The harness manager's ROOT-context needs for a seat route through host-substrate
(HO ruling Q1, ORG-080 :571 / A-4): no second HostBackend. This module turns the
typed per-role ``identity`` block in the committed instance.yaml into per-seat
``pantheon-host-substrate-manifest/v0`` FRAGMENTS, and host-substrate's planner and
handler merge them into their manifest through a strictly additive seam (T-5829
ruling (1), Option A). Pure: it reads only the documents it is handed, never
passwd, group, git or the network. Observation always arrives as the substrate
subject mapping (``passwd:<a>``, ``group:<g>``, ``member:<g>:<a>``,
``owner_mode:<path>``, ``acl:<path>``) the caller injects.

Identity block (T-5829 ruling (2); ``root_half`` approved as option alpha, 2026-11-02):

    roles.<role>.identity:
      uid: <int, explicit, never allocated>
      shell: <families._SHELLS>
      home_mode: "<octal string>"
      groups: [<supplementary group names>]
      acl: [<ACL entries on /home/<account>>]    # [] = ACL not managed
      root_half: observe-only | managed

Only ``managed`` seats emit a fragment. A fragment carries the seat's own account
(uid, ``/home/<account>``, shell, linger), its home as the single installer root,
and the home's owner/mode (and ACL when declared): ``account.ensure``,
``linger.enable``, ``owner_mode.set`` and optionally ``acl.converge``. Shared
groups and the hub mailbox dir are OUT of U2 (no seat-owned home under ruling
(1)(e); T-5827 r1 section 3). Memberships are IN scope for the seat's own account
only (T-6167 Q3 reverses T-5829 (1)(e) for memberships alone), under four binding
conditions: (a) the seat's own account only; (b) only into a group that already
exists (a fragment never creates a group: it names groups of the base manifest);
(c) only groups the seat's own identity block names, never a root-equivalent group
(``ROOT_EQUIVALENT_GROUPS``, a hard refusal) and never a private group; (d) the
receipt records the pre-state and a rollback removes exactly the membership it
added, leaving a membership that already existed. The base manifest's ``groups``
is empty today, so real-fleet use waits on Recovery's signed base groups (T-5846 item 1).

Conditions enforced here (T-5829):
  (a) ``merge``: fragments only ADD ids; a collision with the base or another
      fragment refuses, never overrides.
  (c) ``merge``: the merged digest (families' sha256 over sorted-key canonical
      JSON) is order-independent; the one list a fragment adds to
      (``installer_roots``) is emitted sorted.
  (e) ``check_seat_scope``: a seat fragment names only its own account, home,
      paths and ACL qualifiers; root, any system id (<1000) or another seat's
      account or home refuses.
  (f) ``seat_merge`` returns each fragment's digest for the plan to record (as
      ``fragment_sha256:<role>`` provenance entries); the handler compares the
      recorded set to a recomputation at apply.
  (2) ``drift_check`` / ``seat_merge(plan_time=True)``: an existing account must
      match its block; an absent managed account is a new seat only when the
      observation proves passwd fully listed (``passwd_fully_listed``, R1-1); a ``managed`` existing account with gid != uid refuses
      (families account.ensure derives gid == uid; never re-gid a live account).
  (g) new ``managed`` seats: uid in the fleet range and unused, no
      root-equivalent group, home_mode no more permissive than 0750, ACL only on
      the seat's own home (structural: the block's ACL has no path field).

``drift_check`` is the same fail-closed check as a pure function over ANY block
(observe-only included). It is NOT wired into host-substrate's plan (no fleet-wide
refusal from a seat the observation does not carry; mason amendment 2), and
harness-manager's seat plan receives no passwd/group observation, so it is
unwired there too (follow-up).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from tools.germinate.host_substrate import families as F

ROOT_HALF = ("observe-only", "managed")
IDENTITY_KEYS = frozenset({"uid", "shell", "home_mode", "groups", "acl", "root_half"})
#: Recovery condition (g): never a member of these on a new seat.
ROOT_EQUIVALENT_GROUPS = frozenset({"sudo", "wheel", "admin", "root", "adm", "shadow", "disk", "docker", "lxd"})
#: The fleet uid range, restated from tools/codex_recovery.py MIN_FLEET_UID /
#: MAX_FLEET_UID (pinned equal by test; not imported: that module is a broker).
MIN_FLEET_UID = 1000
MAX_FLEET_UID = 60000
#: The loosest permitted home mode (g): no bit outside rwxr-x---.
MAX_HOME_MODE = 0o750
HOME_ROOT = "/home/"

#: Sections a seat fragment may never populate (ruling (1)(e) and (3)).
_FORBIDDEN_FRAGMENT_SECTIONS = ("groups", "mailbox_dirs", "units", "dbus_policies", "sudoers_templates",
                                "legacy_requirements")
#: Manifest metadata a fragment may not claim (signoff is the base's alone).
_FORBIDDEN_FRAGMENT_FIELDS = ("status", "co_author", "signoff")
#: The keyed sections a merge unions (add-only).
MAPPING_SECTIONS = ("groups", "accounts", "paths", "mailbox_dirs", "units", "dbus_policies", "sudoers_templates")
_NAMED_ACL_RE = re.compile(r"^(?:default:)?(user|group):([^:]+):[r-][w-][x-]$")


class IdentityRefused(ValueError):
    """An identity block, a fragment, a merge or an observation is refused."""


@dataclass(frozen=True)
class Identity:
    role: str
    account: str
    uid: int
    shell: str
    home_mode: str
    groups: tuple[str, ...]
    acl: tuple[str, ...]
    root_half: str

    @property
    def home(self) -> str:
        return HOME_ROOT + self.account


@dataclass(frozen=True)
class Finding:
    """One drift-check result. ``code``: drift | could-not-ask | not-family-expressible | unsafe-new-seat."""

    role: str
    account: str
    code: str
    field: str
    detail: str

    def text(self) -> str:
        return f"{self.role} ({self.account}) {self.code} {self.field}: {self.detail}"


@dataclass(frozen=True)
class SeatMerge:
    doc: Mapping[str, Any]
    fragment_sha256: dict[str, str]


# ---------------------------------------------------------------------------
# The identity block
# ---------------------------------------------------------------------------


def load_instance(path: Path) -> Any:
    """The committed instance.yaml at ``path``; None when the checkout has none."""

    path = Path(path)
    if not path.exists():
        return None
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except (OSError, yaml.YAMLError) as exc:
        raise IdentityRefused(f"instance.yaml unreadable: {exc}") from None


def _roles(instance_doc: Any) -> Mapping[str, Any]:
    roles = instance_doc.get("roles") if isinstance(instance_doc, dict) else None
    if not isinstance(roles, dict):
        raise IdentityRefused("instance.yaml has no roles mapping")
    return roles


def seat_accounts(instance_doc: Any) -> frozenset[str]:
    """Every seat account: ``roles.*.account``."""

    return frozenset(b["account"] for b in _roles(instance_doc).values()
                     if isinstance(b, dict) and isinstance(b.get("account"), str))


def _identity(role: str, binding: Mapping[str, Any]) -> Identity:
    where = f"roles.{role}.identity"
    block = binding.get("identity")
    if not isinstance(block, dict):
        raise IdentityRefused(f"{where}: must be a mapping")
    unknown, missing = set(block) - IDENTITY_KEYS, IDENTITY_KEYS - set(block)
    if unknown:
        raise IdentityRefused(f"{where}: unknown fields {sorted(map(str, unknown))}")
    if missing:
        raise IdentityRefused(f"{where}: missing fields {sorted(missing)} (uid is explicit, never auto-allocated)")
    account = binding.get("account")
    try:
        F._name(f"roles.{role}.account", account)
    except F.ManifestError as exc:
        raise IdentityRefused(str(exc)) from None
    uid = block["uid"]
    if not isinstance(uid, int) or isinstance(uid, bool):
        raise IdentityRefused(f"{where}.uid: must be an explicit int")
    if not isinstance(block["shell"], str) or block["shell"] not in F._SHELLS:
        raise IdentityRefused(f"{where}.shell: {block['shell']!r} is not allowlisted")
    try:
        mode = F._mode(f"{where}.home_mode", block["home_mode"])
    except F.ManifestError as exc:
        raise IdentityRefused(str(exc)) from None
    groups, acl = block["groups"], block["acl"]
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups) or len(set(groups)) != len(groups):
        raise IdentityRefused(f"{where}.groups: must be a list of distinct group names")
    for g in groups:
        try:
            F._name(f"{where}.groups", g)
        except F.ManifestError as exc:
            raise IdentityRefused(str(exc)) from None
    if not isinstance(acl, list) or not all(isinstance(a, str) for a in acl):
        raise IdentityRefused(f"{where}.acl: must be a list of ACL entry strings")
    if block["root_half"] not in ROOT_HALF:
        raise IdentityRefused(f"{where}.root_half: must be one of {list(ROOT_HALF)}")
    return Identity(str(role), account, uid, block["shell"], mode, tuple(sorted(groups)), tuple(acl), block["root_half"])


def identities(instance_doc: Any) -> dict[str, Identity]:
    """Every role carrying an ``identity`` block, validated. A role without one is skipped."""

    out: dict[str, Identity] = {}
    for role, binding in _roles(instance_doc).items():
        if isinstance(binding, dict) and "identity" in binding:
            out[str(role)] = _identity(str(role), binding)
    accounts = [i.account for i in out.values()]
    uids = [i.uid for i in out.values()]
    if len(set(accounts)) != len(accounts) or len(set(uids)) != len(uids):
        raise IdentityRefused("identity blocks: duplicate account or uid across roles")
    return out


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------


def fragment_for(ident: Identity) -> dict[str, Any]:
    """The seat's ``pantheon-host-substrate-manifest/v0`` fragment."""

    bad = sorted(set(ident.groups) & ROOT_EQUIVALENT_GROUPS)
    if bad:  # T-6167 Q3 (c): a hard refusal for every managed seat, new or existing
        raise IdentityRefused(f"roles.{ident.role}.identity.groups: {bad} is root-equivalent; never a seat membership")
    path: dict[str, Any] = {"owner": ident.account, "group": ident.account, "mode": ident.home_mode}
    if ident.acl:
        path["acl"] = list(ident.acl)
    account: dict[str, Any] = {"uid": ident.uid, "home": ident.home, "shell": ident.shell, "linger": True}
    if ident.groups:
        account["groups"] = sorted(ident.groups)
    return {
        "schema": F.MANIFEST_SCHEMA,
        "note": f"seat fragment for role {ident.role}, resolved by harness_manager_root from instance.yaml (T-5827)",
        "accounts": {ident.account: account},
        "installer_roots": [ident.home],
        "paths": {ident.home: path},
    }


def fragment_sha256(fragment: Mapping[str, Any]) -> str:
    return F.sha256_text(F.canonical(fragment))


def own_path(account: str, path: str) -> bool:
    """The (e)/(g) own-path predicate: the seat's home or anything below it.
    ``path`` must already be normalized and absolute (``families._norm_abs``)."""

    home = HOME_ROOT + account
    return path == home or path.startswith(home + "/")


def _observed_id(subjects: Mapping[str, Any], tag: str, name: str) -> int | None:
    value = subjects.get(f"passwd:{name}" if tag == "user" else f"group:{name}")
    if not isinstance(value, dict):
        return None
    got = value.get("uid" if tag == "user" else "gid")
    return got if isinstance(got, int) and not isinstance(got, bool) else None


def check_acl_qualifiers(where: str, account: str, entries: Any, *, seat_accounts: frozenset[str],
                         subjects: Mapping[str, Any]) -> None:
    """(e): a named ACL qualifier is the seat itself, or resolves (observed) to a
    non-system, non-seat id. Root, a system id, another seat or an unobserved name refuses."""

    for entry in entries or []:
        m = _NAMED_ACL_RE.match(entry) if isinstance(entry, str) else None
        if m is None:
            continue  # base entries (user::, group::, mask::, other::) name nobody
        tag, name = m.groups()
        if name == account:
            continue
        if name == "root":
            raise IdentityRefused(f"{where}: ACL entry {entry!r} names root (T-5829 (e))")
        if name in seat_accounts:
            raise IdentityRefused(f"{where}: ACL entry {entry!r} names another seat's account (T-5829 (e))")
        ident = _observed_id(subjects, tag, name)
        if ident is None:
            raise IdentityRefused(f"{where}: ACL entry {entry!r}: {tag} {name!r} is not observed (could not ask)")
        if ident < MIN_FLEET_UID:
            raise IdentityRefused(f"{where}: ACL entry {entry!r} names a system id {ident} (T-5829 (e))")
        if ident > MAX_FLEET_UID:  # nobody/nogroup (65534) and other pseudo-ids (R1-5)
            raise IdentityRefused(f"{where}: ACL entry {entry!r} names id {ident}, above the fleet range (T-5829 (e))")


def check_seat_scope(role: str, account: str, fragment: Mapping[str, Any], *, seat_accounts: frozenset[str],
                     subjects: Mapping[str, Any], declared_groups: frozenset[str] = frozenset(),
                     base_groups: frozenset[str] = frozenset()) -> None:
    """T-5829 ruling (1)(e): seat scope, checked at merge even when every id is new.
    T-6167 Q3 memberships: each group must be non-root-equivalent, not a private (seat
    account) group, declared in this seat's own block, and already in the base manifest."""

    where = f"fragment {role}"
    home = HOME_ROOT + account
    for name in _FORBIDDEN_FRAGMENT_FIELDS:
        if name in fragment:
            raise IdentityRefused(f"{where}: a seat fragment may not carry {name!r}")
    for section in _FORBIDDEN_FRAGMENT_SECTIONS:
        if fragment.get(section):
            raise IdentityRefused(f"{where}: a seat fragment may not populate {section!r} (T-5829 (e))")
    # Reachable: a hostile instance.yaml can list root as a role account; root is refused
    # by name whatever seat_accounts holds (pinned by test_e_seat_scope_refusals[root-seat]).
    if account == "root" or account not in seat_accounts:
        raise IdentityRefused(f"{where}: {account!r} is not a seat account")
    accounts = fragment.get("accounts") or {}
    if set(accounts) != {account}:
        raise IdentityRefused(f"{where}: accounts must be exactly the seat's own {account!r}, got {sorted(accounts)}")
    spec = accounts[account]
    uid = spec.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < MIN_FLEET_UID:
        raise IdentityRefused(f"{where}: uid {uid!r} is root or a system uid (T-5829 (e))")
    if uid > MAX_FLEET_UID:  # nobody (65534) and other pseudo-uids (R1-5)
        raise IdentityRefused(f"{where}: uid {uid!r} is outside the fleet range {MIN_FLEET_UID}..{MAX_FLEET_UID}")
    if spec.get("home") != home:
        raise IdentityRefused(f"{where}: home {spec.get('home')!r} is not {home!r}")
    memberships = spec.get("groups") or []
    if not isinstance(memberships, list) or not all(isinstance(g, str) for g in memberships):
        raise IdentityRefused(f"{where}: groups must be a list of group names")
    for g in memberships:
        if g in ROOT_EQUIVALENT_GROUPS:
            raise IdentityRefused(f"{where}: group {g!r} is root-equivalent; never a seat membership (T-6167 Q3 (c))")
        if g == account or g in seat_accounts:
            raise IdentityRefused(f"{where}: group {g!r} is a seat's private group (T-6167 Q3)")
        if g not in declared_groups:
            raise IdentityRefused(f"{where}: group {g!r} is not named in this seat's identity block (T-6167 Q3 (c))")
        if g not in base_groups:
            raise IdentityRefused(f"{where}: group {g!r} is not in the base manifest's groups; "
                                  "a fragment never creates a group (T-6167 Q3 (b))")
    if list(fragment.get("installer_roots") or []) != [home]:
        raise IdentityRefused(f"{where}: installer_roots must be exactly [{home!r}]")
    for p, pspec in (fragment.get("paths") or {}).items():
        try:
            F._norm_abs(f"{where}.paths", p)
        except F.ManifestError as exc:
            raise IdentityRefused(str(exc)) from None
        if not own_path(account, p):
            raise IdentityRefused(f"{where}: path {p!r} is not the seat's own (T-5829 (e))")
        if not isinstance(pspec, dict) or pspec.get("owner") != account or pspec.get("group") != account:
            raise IdentityRefused(f"{where}: path {p!r} must be owned by {account}:{account} (T-5829 (e))")
        check_acl_qualifiers(f"{where}.paths.{p}", account, pspec.get("acl"), seat_accounts=seat_accounts,
                             subjects=subjects)


# ---------------------------------------------------------------------------
# The add-only merge
# ---------------------------------------------------------------------------


def _nested(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def merge(base_doc: Mapping[str, Any], fragments: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """T-5829 (a)+(c): union the fragments into the base, add-only and order-independent.
    With no fragment the base document itself is returned (its digest unchanged)."""

    if not fragments:
        return base_doc
    base_groups = set(base_doc.get("groups") or {})
    for role in sorted(fragments):  # T-6167 Q3 (b): a membership names a base group; a fragment never creates one
        if fragments[role].get("groups"):
            raise IdentityRefused(f"merge: fragment {role} populates 'groups' (a fragment never creates a group)")
        for acct, spec in (fragments[role].get("accounts") or {}).items():
            absent = sorted(set(spec.get("groups") or []) - base_groups)
            if absent:
                raise IdentityRefused(f"merge: fragment {role} account {acct} names group(s) {absent} absent from the "
                                      "base groups (a fragment never creates a group; T-6167 Q3 (b))")
    merged = copy.deepcopy(dict(base_doc))
    for section in MAPPING_SECTIONS:
        source = {key: "base" for key in (base_doc.get(section) or {})}
        out = dict(merged.get(section) or {})
        for role in sorted(fragments):
            for key, value in (fragments[role].get(section) or {}).items():
                if key in source:
                    raise IdentityRefused(f"merge: {section}.{key} from fragment {role} collides with {source[key]} "
                                          "(fragments only add; T-5829 (a))")
                source[key] = f"fragment {role}"
                out[key] = copy.deepcopy(value)
        if section in merged or out:
            merged[section] = out
    roots = [(r, "base") for r in (base_doc.get("installer_roots") or [])]
    for role in sorted(fragments):
        roots += [(r, f"fragment {role}") for r in (fragments[role].get("installer_roots") or [])]
    for i, (a, sa) in enumerate(roots):
        for b, sb in roots[i + 1:]:
            if sa != sb and _nested(a, b):
                raise IdentityRefused(f"merge: installer root {a!r} ({sa}) overlaps {b!r} ({sb}) (T-5829 (a))")
    merged["installer_roots"] = sorted({r for r, _s in roots})
    uids: dict[int, str] = {}
    for name, spec in (merged.get("accounts") or {}).items():
        uid = spec.get("uid") if isinstance(spec, dict) else None
        if uid in uids:
            raise IdentityRefused(f"merge: uid {uid} of {name} collides with {uids[uid]} (T-5829 (a))")
        uids[uid] = name
    return merged


# ---------------------------------------------------------------------------
# Drift (ruling 2) and new-seat safety (g)
# ---------------------------------------------------------------------------


def passwd_fully_listed(instance_doc: Any, subjects: Mapping[str, Any]) -> bool:
    """R1-1: does the observation PROVE passwd was enumerated? Only then may a managed
    account's absence mean "new seat". The proof: ``passwd:root`` is present AND every
    role account that is not a managed block (every existing seat, plus any role with
    no block) is present. An empty, partial or root-only observation proves nothing."""

    if not isinstance(subjects.get("passwd:root"), dict):
        return False
    managed = {i.account for i in identities(instance_doc).values() if i.root_half == "managed"}
    required = seat_accounts(instance_doc) - managed
    return all(isinstance(subjects.get(f"passwd:{a}"), dict) for a in required)


def _identity_findings(ident: Identity, subjects: Mapping[str, Any], *, new_ok: bool) -> list[Finding]:
    """Fail-closed comparison of one block with the injected observation. ``new_ok``:
    the block is managed AND the observation proves passwd fully listed (R1-1)."""

    def f(code: str, field: str, detail: str) -> Finding:
        return Finding(ident.role, ident.account, code, field, detail)

    passwd = subjects.get(f"passwd:{ident.account}")
    if passwd is None:
        if not new_ok:
            why = ("passwd is not proven fully listed (needs passwd:root and every non-managed role account)"
                   if ident.root_half == "managed" else "the account is not in the observation")
            return [f("could-not-ask", "passwd", why)]
        return _new_seat_findings(ident, subjects) + _group_exists_findings(ident, subjects)
    if not isinstance(passwd, dict):
        return [f("could-not-ask", "passwd", f"malformed observation {passwd!r}")]
    out: list[Finding] = []
    out += _group_exists_findings(ident, subjects)
    for field, want in (("uid", ident.uid), ("home", ident.home), ("shell", ident.shell)):
        if passwd.get(field) != want:
            out.append(f("drift", field, f"block {want!r} != observed {passwd.get(field)!r}"))
    if ident.root_half == "managed" and passwd.get("gid") != ident.uid:
        out.append(f("not-family-expressible", "gid",
                     f"observed gid {passwd.get('gid')!r} != uid {ident.uid}: account.ensure derives gid == uid; "
                     "never re-gid a live account"))
    om_key = f"owner_mode:{ident.home}"
    want_om = {"owner": ident.account, "group": ident.account, "mode": ident.home_mode}
    if om_key not in subjects:
        out.append(f("could-not-ask", "home_mode", f"{om_key} is not in the observation"))
    elif subjects[om_key] != want_om:
        out.append(f("drift", "home_mode", f"block {want_om!r} != observed {subjects[om_key]!r}"))
    for g in ident.groups:
        key = f"member:{g}:{ident.account}"
        if key not in subjects:
            out.append(f("could-not-ask", "groups", f"{key} is not in the observation"))
        elif subjects[key] is not True:
            out.append(f("drift", "groups", f"block lists {g!r}; observed not a member"))
    suffix = f":{ident.account}"
    extra = sorted(s[len("member:"):-len(suffix)] for s, v in subjects.items()
                   if s.startswith("member:") and s.endswith(suffix) and v is True
                   and s[len("member:"):-len(suffix)] not in ident.groups and s.count(":") == 2)
    for g in extra:
        out.append(f("drift", "groups", f"observed member of {g!r}, not in the block"))
    if ident.acl:
        key = f"acl:{ident.home}"
        want_acl = {"entries": sorted(ident.acl)}
        if key not in subjects:
            out.append(f("could-not-ask", "acl", f"{key} is not in the observation"))
        elif subjects[key] != want_acl:
            out.append(f("drift", "acl", f"block {want_acl!r} != observed {subjects[key]!r}"))
    return out


def _group_exists_findings(ident: Identity, subjects: Mapping[str, Any]) -> list[Finding]:
    """T-6167 Q3 (b): a managed seat's declared group must already be observed; never created.
    A root-equivalent group is refused elsewhere (g / fragment_for) and is not repeated here."""

    if ident.root_half != "managed":
        return []
    return [Finding(ident.role, ident.account, "could-not-ask", "groups",
                    f"group:{g} is not in the observation; a seat membership never creates a group (T-6167 Q3 (b))")
            for g in ident.groups if g not in ROOT_EQUIVALENT_GROUPS and f"group:{g}" not in subjects]


def _new_seat_findings(ident: Identity, subjects: Mapping[str, Any]) -> list[Finding]:
    """Recovery condition (g), for a managed block whose account is not on the host."""

    def f(field: str, detail: str) -> Finding:
        return Finding(ident.role, ident.account, "unsafe-new-seat", field, detail)

    out: list[Finding] = []
    if not MIN_FLEET_UID <= ident.uid <= MAX_FLEET_UID:
        out.append(f("uid", f"{ident.uid} is outside the fleet range {MIN_FLEET_UID}..{MAX_FLEET_UID}"))
    for subject, value in sorted(subjects.items()):
        if subject.startswith("passwd:") and isinstance(value, dict) and value.get("uid") == ident.uid:
            out.append(f("uid", f"{ident.uid} is already used by {subject[7:]}"))
        if subject.startswith("group:") and isinstance(value, dict) and value.get("gid") == ident.uid:
            out.append(f("uid", f"{ident.uid} is already a gid of {subject[6:]} (the private group would collide)"))
    if f"group:{ident.account}" in subjects:
        out.append(f("account", f"group {ident.account!r} already exists (account.ensure creates it)"))
    for g in sorted(set(ident.groups) & ROOT_EQUIVALENT_GROUPS):
        out.append(f("groups", f"{g!r} is a root-equivalent group"))
    if int(ident.home_mode, 8) & ~MAX_HOME_MODE:
        out.append(f("home_mode", f"{ident.home_mode} is more permissive than 0750"))
    return out


def drift_check(instance_doc: Any, observed_subjects: Mapping[str, Any]) -> list[Finding]:
    """The fail-closed drift check over EVERY identity block (observe-only included).

    Pure: ``observed_subjects`` is the injected substrate observation; passwd and
    group are never read here. An existing account must match its block; an account
    the observation does not carry is ``could-not-ask`` for an observe-only block and
    a new seat (checked against (g)) for a managed one only when the observation
    proves passwd fully listed (R1-1), else ``could-not-ask``. An empty list means clean."""

    out: list[Finding] = []
    idents = identities(instance_doc)
    listed = passwd_fully_listed(instance_doc, observed_subjects)
    for role in sorted(idents):
        ident = idents[role]
        out += _identity_findings(ident, observed_subjects, new_ok=ident.root_half == "managed" and listed)
    return out


# ---------------------------------------------------------------------------
# The host-substrate seam
# ---------------------------------------------------------------------------


def seat_merge(base_doc: Mapping[str, Any], instance_doc: Any, subjects: Callable[[], Mapping[str, Any]], *,
               plan_time: bool) -> SeatMerge:
    """Base manifest + the managed seats' fragments (T-5829 ruling (1)).

    ``subjects`` is called only when a managed seat exists, so a checkout with no
    managed seat (today: every seat is observe-only) never needs, and never asks
    for, a seat observation; its manifest is the base, byte for byte. At plan time
    the managed seats are drift-checked (ruling 2) or (g)-checked; at apply the
    helper's own conflict checks run instead."""

    if instance_doc is None:
        return SeatMerge(base_doc, {})
    managed = {r: i for r, i in identities(instance_doc).items() if i.root_half == "managed"}
    if not managed:
        return SeatMerge(base_doc, {})
    observed = subjects()
    if not isinstance(observed, Mapping):
        raise IdentityRefused("no substrate observation for the managed seats: could not ask")
    if plan_time:
        listed = passwd_fully_listed(instance_doc, observed)
        findings = [x for r in sorted(managed) for x in _identity_findings(managed[r], observed, new_ok=listed)]
        if findings:
            raise IdentityRefused("identity refused: " + "; ".join(x.text() for x in findings))
    seats = seat_accounts(instance_doc)
    base_groups = base_doc.get("groups") or {}
    fragments: dict[str, dict[str, Any]] = {}
    for role in sorted(managed):
        frag = fragment_for(managed[role])
        try:
            # Validation only: a membership needs its group in the merged groups section, and a
            # fragment cannot add groups, so the check sees the base groups; the emitted fragment does not.
            F.load_manifest({**frag, "groups": copy.deepcopy(dict(base_groups))})
        except F.ManifestError as exc:
            raise IdentityRefused(f"fragment {role}: {exc}") from None
        check_seat_scope(role, managed[role].account, frag, seat_accounts=seats, subjects=observed,
                         declared_groups=frozenset(managed[role].groups), base_groups=frozenset(base_groups))
        fragments[role] = frag
    return SeatMerge(merge(base_doc, fragments), {r: fragment_sha256(f) for r, f in fragments.items()})


FRAGMENT_PROVENANCE_PREFIX = "fragment_sha256:"


def provenance_fields(fragment_digests: Mapping[str, str]) -> dict[str, str]:
    """The flat provenance entries (the engine's provenance is a string mapping)."""

    return {FRAGMENT_PROVENANCE_PREFIX + role: digest for role, digest in sorted(fragment_digests.items())}


def recorded_fragments(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """The fragment digests a plan recorded, back from its flat provenance."""

    return {k[len(FRAGMENT_PROVENANCE_PREFIX):]: v for k, v in (provenance or {}).items()
            if isinstance(k, str) and k.startswith(FRAGMENT_PROVENANCE_PREFIX)}


def provenance_refusal(recorded: Any, recomputed: Mapping[str, str]) -> str | None:
    """T-5829 (f): the plan's recorded fragment digests against a recomputation."""

    recorded = recorded or {}
    if not isinstance(recorded, dict):
        return "fragment provenance is malformed"
    extra = sorted(set(recomputed) - set(recorded))
    if extra:
        return f"unrecorded fragment(s) {extra}"
    missing = sorted(set(recorded) - set(recomputed))
    if missing:
        return f"recorded fragment(s) absent {missing}"
    changed = sorted(r for r in recorded if recorded[r] != recomputed[r])
    if changed:
        return f"fragment(s) changed since plan {changed}"
    return None
