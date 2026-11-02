"""The closed root op enum and the server-resolved manifest (T-5084, GE).

ORG-052 §8 / gate 17: a caller never supplies a uid, path, unit/policy/sudoers
text, argv, executable or sudo target. An op names a family plus manifest ids
(and, for the paired families, a closed ``state``); every other value is
resolved here from the manifest. A family outside ``FAMILIES`` does not exist.

CURRENT (co-author recovery; D15 approved at 71fd484cc): the per-family parameter
schemas and the allowlist rules below are Recovery-approved.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from tools.host_control.envelope import ORG082_SUBSTRATE_FAMILIES

#: The closed root op enum. Literal on purpose: a new family is a reviewed edit
#: here AND in ORG-082 §1 (ORG-082 says the same commit); the check below
#: refuses import if the two ever differ.
FAMILIES: Final[frozenset[str]] = frozenset(
    {
        "account.ensure",
        "account.retire",
        "group.ensure",
        "group.member",
        "acl.converge",
        "owner_mode.set",
        "sudoers_fragment.ensure",
        "sudoers_fragment.retire",
        "unit.system.install",
        "unit.system.enable",
        "dbus_policy.install",
        "dbus_policy.retire",
        "mailbox_dir.ensure",
        "mailbox_dir.retire",
        "linger.enable",
        "linger.disable",
    }
)
if FAMILIES != ORG082_SUBSTRATE_FAMILIES:  # pragma: no cover - import-time tripwire
    raise ImportError("host_substrate.FAMILIES drifted from host_control.envelope.ORG082_SUBSTRATE_FAMILIES (ORG-082 §1)")

#: Exactly these desired keys, per family. Nothing else is accepted.
PARAMS: Final[Mapping[str, frozenset[str]]] = {
    "account.ensure": frozenset({"account"}),
    "account.retire": frozenset({"account"}),
    "group.ensure": frozenset({"group"}),
    "group.member": frozenset({"group", "account", "state"}),
    "acl.converge": frozenset({"path"}),
    "owner_mode.set": frozenset({"path"}),
    "sudoers_fragment.ensure": frozenset({"template", "sha256"}),
    "sudoers_fragment.retire": frozenset({"template"}),
    "unit.system.install": frozenset({"unit", "state", "sha256"}),
    "unit.system.enable": frozenset({"unit", "state"}),
    "dbus_policy.install": frozenset({"policy", "sha256"}),
    "dbus_policy.retire": frozenset({"policy"}),
    "mailbox_dir.ensure": frozenset({"mailbox"}),
    "mailbox_dir.retire": frozenset({"mailbox"}),
    "linger.enable": frozenset({"account"}),
    "linger.disable": frozenset({"account"}),
}
assert set(PARAMS) == FAMILIES

STATES: Final[Mapping[str, frozenset[str]]] = {
    "group.member": frozenset({"present", "absent"}),
    "unit.system.install": frozenset({"present", "absent"}),
    "unit.system.enable": frozenset({"enabled", "disabled"}),
}

MANIFEST_SCHEMA = "pantheon-host-substrate-manifest/v0"
UNIT_DIR = "/etc/systemd/system/"
DBUS_DIR = "/etc/dbus-1/system.d/"
SUDOERS_DIR = "/etc/sudoers.d/"

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")  # POSIX-portable account/group name
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_MODE_RE = re.compile(r"^0?[0-7]{3,4}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ACL_RE = re.compile(r"^(?:default:)?(?:user|group):[a-z_][a-z0-9_-]{0,31}:[r-][w-][x-]$|^(?:default:)?(?:user|group|mask|other)::[r-][w-][x-]$")
_SHELLS = frozenset({"/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/sbin/nologin"})
_SUDOERS_BASENAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # sudoers.d skips names with '.' or '~'
_SUDO_CMD_RE = re.compile(r"^/[A-Za-z0-9_./-]+(?: [A-Za-z0-9_./@=-]+)*$")
#: sudoers glob/escape/separator characters that never appear in a command
#: (the codex_recovery_sudoers._FORBIDDEN_CHARS discipline, re-stated).
_SUDO_FORBIDDEN = frozenset("*?[]\\,:\"'!&|;$`<>(){}")


class ManifestError(ValueError):
    """The manifest (the server-side registry of values) is refused."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_abs(where: str, path: Any) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ManifestError(f"{where}: {path!r} is not an absolute path")
    if posixpath.normpath(path) != path or "/../" in f"{path}/" or path.endswith("/"):
        raise ManifestError(f"{where}: {path!r} is not a normalized path")
    return path


def _under(where: str, path: str, root: str) -> str:
    if not path.startswith(root) or "/" in path[len(root):]:
        raise ManifestError(f"{where}: {path!r} must be a direct child of {root}")
    return path


def _name(where: str, value: Any) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ManifestError(f"{where}: {value!r} is not an allowlisted account/group name shape")
    return value


def _mode(where: str, value: Any) -> str:
    if not isinstance(value, str) or not _MODE_RE.match(value):
        raise ManifestError(f"{where}: mode {value!r} must be an octal string")
    return value.zfill(4)


def _rwx(digit: int) -> str:
    """One octal permission digit as ACL perms text (5 -> 'r-x')."""

    return "".join(c if digit & v else "-" for c, v in (("r", 4), ("w", 2), ("x", 1)))


def _mapping_section(doc: Mapping[str, Any], name: str) -> dict[str, Any]:
    if name not in doc:
        return {}
    value = doc[name]
    if not isinstance(value, dict):
        raise ManifestError(f"{name}: must be a mapping")
    return value


def _list_section(doc: Mapping[str, Any], name: str) -> list[Any]:
    if name not in doc:
        return []
    value = doc[name]
    if not isinstance(value, list):
        raise ManifestError(f"{name}: must be a list")
    return value


def _entry(where: str, value: Any, *, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return one closed-schema manifest entry or a classified refusal."""

    if not isinstance(value, dict):
        raise ManifestError(f"{where}: must be a mapping")
    unknown = set(value) - required - optional
    if unknown:
        raise ManifestError(f"{where}: unknown fields {sorted(unknown, key=repr)!r}")
    missing = required - set(value)
    if missing:
        raise ManifestError(f"{where}: missing fields {sorted(missing)}")
    return value


def sudoers_lines(template: Mapping[str, Any]) -> list[str]:
    """The exact fragment body for a template: one line per allowlisted command."""

    head = f"# pantheon host-substrate sudoers template {template['id']} (ticket {template['ticket']}, expires {template['expiry']})"
    rules = [f"{template['grantee']} ALL=({template['runas']}) NOPASSWD: {cmd}" for cmd in template["commands"]]
    return [head, *rules]


def sudoers_text(template: Mapping[str, Any]) -> str:
    return "\n".join(sudoers_lines(template)) + "\n"


def check_sudoers_template(where: str, template: Mapping[str, Any], accounts: Mapping[str, Any]) -> None:
    """ORG-052 §8 / gate 20: grantee, run-as, exact commands, expiry, owning ticket; no ALL, no globs."""

    grantee, runas = template.get("grantee"), template.get("runas")
    if not isinstance(grantee, str) or grantee not in accounts:
        raise ManifestError(f"{where}: grantee {grantee!r} is not a manifest account")
    if not isinstance(runas, str) or (runas != "root" and runas not in accounts):
        raise ManifestError(f"{where}: run-as {runas!r} is neither root nor a manifest account")
    if not (isinstance(template.get("ticket"), str) and re.match(r"^T-\d{4,}$", template["ticket"])):
        raise ManifestError(f"{where}: owning ticket must be a T-NNNN id")
    if not (isinstance(template.get("expiry"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", template["expiry"])):
        raise ManifestError(f"{where}: expiry must be YYYY-MM-DD")
    commands = template.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ManifestError(f"{where}: commands must be a non-empty list of exact command lines")
    for cmd in commands:
        if not isinstance(cmd, str) or cmd.split(" ")[0] == "ALL" or "ALL" in cmd.split(" "):
            raise ManifestError(f"{where}: {cmd!r}: ALL grants are refused")
        if not _SUDO_CMD_RE.match(cmd) or _SUDO_FORBIDDEN & set(cmd):
            raise ManifestError(f"{where}: {cmd!r} is not an exact absolute command line (no globs or sudoers specials)")


@dataclass(frozen=True)
class Manifest:
    """The validated, server-resolved value registry. Root-owned on a real host;
    the in-repo CURRENT registry for the simulated tier."""

    doc: Mapping[str, Any]
    sha256: str

    def section(self, name: str) -> Mapping[str, Any]:
        return self.doc.get(name) or {}


def load_manifest(doc: Any, *, repo_root: Path | None = None) -> Manifest:
    """Validate a manifest document. ``repo_root`` resolves unit/policy sources
    and checks their pinned hashes (planner and helper both pass it)."""

    if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"manifest schema must be {MANIFEST_SCHEMA}")
    known = {"schema", "status", "co_author", "signoff", "note", "groups", "accounts", "installer_roots", "paths",
             "mailbox_dirs", "units", "dbus_policies", "sudoers_templates", "legacy_requirements"}
    unknown = set(doc) - known
    if unknown:
        raise ManifestError(f"manifest has unknown sections {sorted(unknown, key=repr)!r}")
    for field in ("status", "co_author", "signoff", "note"):
        if field in doc and (not isinstance(doc[field], str) or not doc[field]):
            raise ManifestError(f"{field}: must be a non-empty string")

    groups = _mapping_section(doc, "groups")
    group_gids: dict[int, str] = {}
    for g, spec in groups.items():
        _name(f"groups.{g}", g)
        spec = _entry(f"groups.{g}", spec, required=frozenset({"gid"}))
        if not isinstance(spec["gid"], int) or isinstance(spec["gid"], bool) or spec["gid"] < 1000:
            raise ManifestError(f"groups.{g}: gid must be an int >= 1000")
        prior = group_gids.get(spec["gid"])
        if prior is not None:
            raise ManifestError(f"groups.{g}.gid: duplicate gid {spec['gid']} also used by {prior}")
        group_gids[spec["gid"]] = g

    accounts = _mapping_section(doc, "accounts")
    uids: dict[int, str] = {}
    for a, spec in accounts.items():
        _name(f"accounts.{a}", a)
        spec = _entry(
            f"accounts.{a}", spec, required=frozenset({"uid", "home", "shell"}), optional=frozenset({"linger", "groups"})
        )
        if (not isinstance(spec["uid"], int) or isinstance(spec["uid"], bool) or spec["uid"] < 1000
                or spec["uid"] in uids):
            raise ManifestError(f"accounts.{a}: uid must be a unique int >= 1000")
        uids[spec["uid"]] = a
        if a in groups:
            raise ManifestError(f"accounts.{a}: collides with an allowlisted group (user-private group gid == uid)")
        _norm_abs(f"accounts.{a}.home", spec.get("home"))
        if not isinstance(spec["shell"], str) or spec["shell"] not in _SHELLS:
            raise ManifestError(f"accounts.{a}: shell {spec.get('shell')!r} is not allowlisted")
        if "linger" in spec and not isinstance(spec["linger"], bool):
            raise ManifestError(f"accounts.{a}.linger: must be a boolean")
        memberships = spec.get("groups", [])
        if not isinstance(memberships, list):
            raise ManifestError(f"accounts.{a}.groups: must be a list")
        for g in memberships:
            if not isinstance(g, str):
                raise ManifestError(f"accounts.{a}.groups: every membership must be a group name string")
            if g not in groups:
                raise ManifestError(f"accounts.{a}: group {g!r} is not allowlisted")
        if len(set(memberships)) != len(memberships):
            raise ManifestError(f"accounts.{a}.groups: duplicate memberships are refused")
    for uid, account in uids.items():
        if uid in group_gids:
            raise ManifestError(
                f"accounts.{account}.uid: private-group gid {uid} collides with allowlisted group {group_gids[uid]!r}"
            )

    roots = [_norm_abs(f"installer_roots[{index}]", root)
             for index, root in enumerate(_list_section(doc, "installer_roots"))]
    if len(set(roots)) != len(roots):
        raise ManifestError("installer_roots: duplicate paths are refused")
    owners = set(accounts) | {"root"}
    group_names = set(groups) | set(accounts) | {"root"}

    def _owned_path(where: str, path: Any, spec: Mapping[str, Any]) -> None:
        path = _norm_abs(where, path)
        if not any(path == r or path.startswith(r + "/") for r in roots):
            raise ManifestError(f"{where}: {path!r} is not below a registered installer-owned root")
        if (not isinstance(spec.get("owner"), str) or spec.get("owner") not in owners
                or not isinstance(spec.get("group"), str) or spec.get("group") not in group_names):
            raise ManifestError(f"{where}: owner/group must be manifest accounts/groups or root")
        _mode(where, spec.get("mode"))

    paths = _mapping_section(doc, "paths")
    for p, spec in paths.items():
        spec = _entry(
            f"paths.{p}", spec, required=frozenset({"owner", "group", "mode"}), optional=frozenset({"acl"})
        )
        _owned_path(f"paths.{p}", p, spec)
        acl = spec.get("acl", [])
        if not isinstance(acl, list):
            raise ManifestError(f"paths.{p}.acl: must be a list")
        for acl_entry in acl:
            if not isinstance(acl_entry, str) or not _ACL_RE.match(acl_entry):
                raise ManifestError(f"paths.{p}: ACL entry {acl_entry!r} is not an allowlisted shape")
        # T-5665 (Recovery, D15): at most ONE entry per key (prefix, tag, qualifier), the entry
        # with its perms stripped ('default:user:alice:rwx' -> 'default:user:alice').
        # setfacl --set accepts duplicates and keeps the LAST, so observe would never match
        # the declared list. This subsumes the exact-string dedup and T-5653's one-mask rule.
        # LOAD-BEARING: _ACL_RE admits only long-form tags and NAME qualifiers (no u:/g:/d:
        # short forms, no numeric ids), which is what makes the textual key canonical.
        seen: dict[str, str] = {}
        for acl_entry in acl:
            key = acl_entry.rsplit(":", 1)[0]
            if key in seen:
                raise ManifestError(
                    f"paths.{p}.acl: {seen[key]!r} and {acl_entry!r} both set {key}: (one entry per prefix, "
                    f"tag and qualifier; setfacl --set keeps the last; T-5665)")
            seen[key] = acl_entry
        # T-5510 (F2): acl.converge runs `setfacl --set`, which REPLACES the whole ACL, so a
        # list of named entries alone is a malformed ACL on a real host ("Missing or wrong
        # entry"). Base entries are declared, never derived from `mode`: with named entries
        # the mode's group bits are the mask, and deriving would fight owner_mode.set.
        if acl:
            for prefix in ("", "default:"):
                if prefix and not any(a.startswith("default:") for a in acl):
                    continue
                missing = [f"{prefix}{t}::" for t in ("user", "group", "other")
                           if not any(a.startswith(f"{prefix}{t}::") for a in acl)]
                if missing:
                    raise ManifestError(
                        f"paths.{p}.acl: setfacl --set needs the base entries; missing {', '.join(missing)}")
            # T-5652 (Recovery, D15): with a named user:/group: entry, `setfacl --set` computes a
            # mask:: entry the manifest did not declare, so observe (which compares exactly and
            # never normalizes) would see drift forever. Named entries REQUIRE the mask.
            for prefix in ("", "default:"):
                named = any(re.match(rf"^{prefix}(?:user|group):[^:]+:", a) for a in acl)
                if named and not any(a.startswith(f"{prefix}mask::") for a in acl):
                    raise ManifestError(
                        f"paths.{p}.acl: named entries need {prefix}mask:: (setfacl --set would compute one; T-5652)")
            # T-5653 (Recovery, D15): the complete mask rule. Per prefix at most ONE mask:: (now
            # enforced by the T-5665 one-entry-per-key rule above), and a
            # mask:: ONLY with named entries (with T-5652 above: mask-iff-named). For the ACCESS
            # prefix the mask IS the mode's group bits on a real host (setfacl writes one into the
            # other, chmod the reverse), so they must agree or acl.converge and owner_mode.set
            # fight and verify never converges. The default mask has no mode constraint.
            for prefix in ("", "default:"):
                masks = [a for a in acl if a.startswith(f"{prefix}mask::")]
                named = any(re.match(rf"^{prefix}(?:user|group):[^:]+:", a) for a in acl)
                if masks and not named:
                    raise ManifestError(
                        f"paths.{p}.acl: a lone {prefix}mask:: with no named user:/group: entry is refused (T-5653)")
                if masks and not prefix:
                    group_bits = _rwx(int(_mode(f"paths.{p}", spec["mode"])[-2]))
                    if masks[0].rsplit(":", 1)[1] != group_bits:
                        raise ManifestError(
                            f"paths.{p}.acl: {masks[0]} must equal the mode's group bits {group_bits} "
                            f"(mode {spec['mode']}; T-5653)")
            # T-5658 (Recovery, D15): the ACCESS base entries ARE the mode bits on a real host
            # (setfacl --set writes them into the mode, chmod writes the mode back), so they
            # must agree or acl.converge and owner_mode.set fight forever. user:: == owner
            # bits, other:: == other bits; with no named entry (so, by T-5652/T-5653, no mask)
            # group:: == group bits; with named entries the mask holds the group bits (T-5653
            # above) and group:: is free. (A duplicate base entry never gets here: T-5665
            # refuses it above.) The default: prefix is unconstrained. Never rewritten.
            mode = _mode(f"paths.{p}", spec["mode"])
            named = any(re.match(r"^(?:user|group):[^:]+:", a) for a in acl)
            bases = [("user", "owner", mode[-3]), ("other", "other", mode[-1])]
            if not named:
                bases.append(("group", "group", mode[-2]))
            for tag, what, digit in bases:
                want = _rwx(int(digit))
                for a in acl:
                    if a.startswith(f"{tag}::") and a.rsplit(":", 1)[1] != want:
                        raise ManifestError(
                            f"paths.{p}.acl: base entry {a} must equal the mode's {what} bits {want} "
                            f"(mode {spec['mode']}; T-5658)")

    mailbox_dirs = _mapping_section(doc, "mailbox_dirs")
    dir_paths: dict[str, str] = {}
    for m, spec in mailbox_dirs.items():
        if not isinstance(m, str) or not _ID_RE.match(m):
            raise ManifestError(f"mailbox_dirs.{m}: bad id")
        spec = _entry(
            f"mailbox_dirs.{m}", spec, required=frozenset({"path", "owner", "group", "mode"})
        )
        _owned_path(f"mailbox_dirs.{m}", spec["path"], spec)
        # T-2920 U3d review LOW-1: one entry per path, so a dir: subject (and its T-5657
        # designated owner) resolves to exactly one manifest entry.
        if spec["path"] in dir_paths:
            raise ManifestError(f"mailbox_dirs.{m}.path: {spec['path']!r} duplicates mailbox_dirs.{dir_paths[spec['path']]}")
        dir_paths[spec["path"]] = m

    for section, root in (("units", UNIT_DIR), ("dbus_policies", DBUS_DIR)):
        entries = _mapping_section(doc, section)
        for uid_, spec in entries.items():
            where = f"{section}.{uid_}"
            if not isinstance(uid_, str) or not _ID_RE.match(uid_):
                raise ManifestError(f"{where}: bad id")
            optional = frozenset({"enable"}) if section == "units" else frozenset()
            spec = _entry(where, spec, required=frozenset({"path", "source", "sha256"}), optional=optional)
            if "enable" in spec and not isinstance(spec["enable"], bool):
                raise ManifestError(f"{where}.enable: must be a boolean")
            dest = _norm_abs(where, spec["path"])
            if section == "units":
                rel = dest[len(root):] if dest.startswith(root) else None
                # a unit file or ONE drop-in level (<unit>.d/<name>.conf)
                if rel is None or not re.match(r"^[A-Za-z0-9@_.-]+(?:\.d/[A-Za-z0-9_.-]+\.conf)?$", rel):
                    raise ManifestError(f"{where}: {dest!r} is not a unit file or drop-in under {root}")
            else:
                _under(where, dest, root)
            if section == "units" and spec.get("enable") and ".d/" in dest:
                raise ManifestError(f"{where}: a drop-in cannot be enabled")
            if not (isinstance(spec["sha256"], str) and _SHA_RE.match(spec["sha256"])):
                raise ManifestError(f"{where}: sha256 pin missing")
            src = spec["source"]
            if not isinstance(src, str) or src.startswith("/") or ".." in src.split("/"):
                raise ManifestError(f"{where}: source must be a repo-relative path")
            if repo_root is not None:
                try:
                    data = (repo_root / src).read_bytes()
                except OSError as exc:
                    raise ManifestError(f"{where}: source {src!r} unreadable: {exc}") from None
                if hashlib.sha256(data).hexdigest() != spec["sha256"]:
                    raise ManifestError(f"{where}: source {src!r} does not match its pinned sha256")
    sudoers_templates = _mapping_section(doc, "sudoers_templates")
    for t, spec in sudoers_templates.items():
        where = f"sudoers_templates.{t}"
        if not isinstance(t, str) or not _ID_RE.match(t):
            raise ManifestError(f"{where}: bad id")
        spec = _entry(
            where,
            spec,
            required=frozenset({"id", "path", "grantee", "runas", "commands", "ticket", "expiry"}),
            optional=frozenset({"prior_sha256"}),
        )
        if spec["id"] != t:
            raise ManifestError(f"{where}: the entry must restate its id")
        dest = _under(where, _norm_abs(where, spec["path"]), SUDOERS_DIR)
        if not _SUDOERS_BASENAME_RE.match(dest[len(SUDOERS_DIR):]):
            raise ManifestError(f"{where}: fragment name must have no '.' or '~' (sudoers.d would skip it)")
        check_sudoers_template(where, spec, accounts)
        prior_digests = spec.get("prior_sha256", [])
        if not isinstance(prior_digests, list):
            raise ManifestError(f"{where}.prior_sha256: must be a list")
        for prior in prior_digests:
            if not (isinstance(prior, str) and _SHA_RE.match(prior)):
                raise ManifestError(f"{where}: prior_sha256 entries must be hex digests")
        if len(set(prior_digests)) != len(prior_digests):
            raise ManifestError(f"{where}.prior_sha256: duplicate entries are refused")

    for index, raw in enumerate(_list_section(doc, "legacy_requirements")):
        where = f"legacy_requirements[{index}]"
        requirement = _entry(where, raw, required=frozenset({"path", "note"}))
        _norm_abs(f"{where}.path", requirement["path"])
        if not isinstance(requirement["note"], str) or not requirement["note"]:
            raise ManifestError(f"{where}.note: must be a non-empty string")
    return Manifest(doc, hashlib.sha256(canonical(doc).encode("ascii")).hexdigest())


# ---------------------------------------------------------------------------
# Subjects and desired values (shared by the planner and the helper)
# ---------------------------------------------------------------------------


class ParamError(ValueError):
    """An op's parameters are outside the family's typed schema or the manifest."""


@dataclass(frozen=True)
class Resolved:
    family: str
    subject: str            # the one piece of host state this op converges
    after: Any              # its desired value (None = absent)
    entry: Mapping[str, Any]  # the manifest entry every value came from


def _need(section: Mapping[str, Any], key: str, what: str) -> Mapping[str, Any]:
    if key not in section:
        raise ParamError(f"{what} {key!r} is not in the manifest")
    return section[key]


def resolve(family: str, params: Mapping[str, str], manifest: Manifest) -> Resolved:
    """Server-side resolution: ids in, every concrete value from the manifest."""

    if family not in FAMILIES:
        raise ParamError(f"{family!r} is not a root op family (closed enum, ORG-082 §1)")
    if set(params) != PARAMS[family]:
        raise ParamError(f"{family}: parameters must be exactly {sorted(PARAMS[family])}, got {sorted(params)}")
    if family in STATES and params["state"] not in STATES[family]:
        raise ParamError(f"{family}: state {params['state']!r} not in {sorted(STATES[family])}")
    m = manifest
    if family in ("account.ensure", "account.retire"):
        spec = _need(m.section("accounts"), params["account"], "account")
        after = {"uid": spec["uid"], "gid": spec["uid"], "home": spec["home"], "shell": spec["shell"], "locked": family == "account.retire"}
        return Resolved(family, f"passwd:{params['account']}", after, spec)
    if family == "group.ensure":
        spec = _need(m.section("groups"), params["group"], "group")
        return Resolved(family, f"group:{params['group']}", {"gid": spec["gid"]}, spec)
    if family == "group.member":
        acct = _need(m.section("accounts"), params["account"], "account")
        _need(m.section("groups"), params["group"], "group")
        if params["group"] not in (acct.get("groups") or []) and params["state"] == "present":
            raise ParamError(f"group.member: {params['account']} is not allowlisted into {params['group']}")
        return Resolved(family, f"member:{params['group']}:{params['account']}", True if params["state"] == "present" else None, acct)
    if family in ("acl.converge", "owner_mode.set"):
        spec = _need(m.section("paths"), params["path"], "path")
        if family == "acl.converge":
            return Resolved(family, f"acl:{params['path']}", {"entries": sorted(spec.get("acl") or [])}, spec)
        return Resolved(family, f"owner_mode:{params['path']}", {"owner": spec["owner"], "group": spec["group"], "mode": _mode("mode", spec["mode"])}, spec)
    if family in ("mailbox_dir.ensure", "mailbox_dir.retire"):
        spec = _need(m.section("mailbox_dirs"), params["mailbox"], "mailbox")
        # Retire LOCKS the directory (mode 0000); contents are never removed (gate 34).
        mode = "0000" if family == "mailbox_dir.retire" else _mode("mode", spec["mode"])
        return Resolved(family, f"dir:{spec['path']}", {"owner": spec["owner"], "group": spec["group"], "mode": mode}, spec)
    if family in ("linger.enable", "linger.disable"):
        spec = _need(m.section("accounts"), params["account"], "account")
        return Resolved(family, f"linger:{params['account']}", True if family == "linger.enable" else None, spec)
    if family in ("unit.system.install", "unit.system.enable"):
        spec = _need(m.section("units"), params["unit"], "unit")
        if family == "unit.system.enable":
            if ".d/" in spec["path"]:
                raise ParamError(f"unit.system.enable: {params['unit']} is a drop-in, not an enableable unit")
            return Resolved(family, f"enabled:{posixpath.basename(spec['path'])}", True if params["state"] == "enabled" else None, spec)
        if params["sha256"] != spec["sha256"]:
            raise ParamError(f"unit.system.install: sha256 does not match the manifest pin for {params['unit']}")
        after = {"sha256": spec["sha256"], "mode": "0644", "owner": "root"} if params["state"] == "present" else None
        return Resolved(family, f"file:{spec['path']}", after, spec)
    if family in ("dbus_policy.install", "dbus_policy.retire"):
        spec = _need(m.section("dbus_policies"), params["policy"], "dbus policy")
        if family == "dbus_policy.install" and params["sha256"] != spec["sha256"]:
            raise ParamError("dbus_policy.install: sha256 does not match the manifest pin")
        after = {"sha256": spec["sha256"], "mode": "0644", "owner": "root"} if family == "dbus_policy.install" else None
        return Resolved(family, f"file:{spec['path']}", after, spec)
    # sudoers_fragment.ensure / .retire
    spec = _need(m.section("sudoers_templates"), params["template"], "sudoers template")
    want = sha256_text(sudoers_text(spec))
    if family == "sudoers_fragment.ensure":
        if params["sha256"] != want:
            raise ParamError("sudoers_fragment.ensure: sha256 does not match the rendered template")
        return Resolved(family, f"file:{spec['path']}", {"sha256": want, "mode": "0440", "owner": "root"}, spec)
    return Resolved(family, f"file:{spec['path']}", None, spec)
