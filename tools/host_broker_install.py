#!/usr/bin/env python3
"""T-2920 U1: the PURE planning core of the ORG-052 host-broker installer.

Intent (T-2920, arc T-2652): a clean box converges to the whole authority
boundary from a reviewed root-run plan, reproducibly. This module is the PLAN
side only. It turns a closed install manifest
(``pantheon-host-broker-install/v1``) plus the root seat registry
(``pantheon-host-seat-registry/v1``) plus the repository bytes into one ordered,
sha-pinned plan, emitted as canonical JSON with a plan digest. The same inputs
give byte-identical plan bytes.

It never mutates a host. There is no apply verb, no subprocess, no network and
no read outside the given inputs and this repository. ``LIVE_ACTUATION_ENABLED``
is the literal ``False`` (the ``tools/germinate/host_substrate/ports.py``
precedent) and an AST test pins it. Apply, verify and rollback are U3's; the
nspawn rehearsal is U4's.

Shape (HO rulings 2026-10-27, recorded on T-2920):

* Q1 HYBRID: ORG-082-family artifacts (accounts, owner/mode of the root config
  tree, system units, unit enablement, D-Bus policies) are marked
  ``route: family`` and carry the exact family params. They reach the
  host-substrate families through a RENDER-STAGING source: every family-routed
  file gets a staging-relative path, and ``substrate_manifest_fragment`` emits a
  ``pantheon-host-substrate-manifest/v0`` fragment whose ``source`` fields are
  staging-relative. ``families.load_manifest(fragment, repo_root=<staging>)``
  then validates and pins them with NO change to FAMILIES or PARAMS. The one
  additive host-substrate change (U3) is letting the handlers take that
  manifest and ``repo_root`` instead of the hard-coded pair at
  host-substrate_handlers.py:28-33. Everything else is ``route: tool``.
* Q6 LAYOUT: a versioned root-owned framework tree
  ``/usr/local/lib/pantheon/framework/<rev>/`` serving both daemons and the
  plan producer. ``<rev>`` is content-addressed (the closure digest prefix).
  The ``active`` symlink is the installer's committed-rev pointer and rollback
  target, switched with one rename. Every config, trusted root and launcher
  names the CONCRETE ``<rev>``: ``RootOwnedFileLoader`` opens each component
  with O_NOFOLLOW, so a symlink can never be a trusted root. Daemons switch at
  restart with newly rendered rev-pinned configs (recorded as a residual).
* The CLOSURE (``derive_closure``) is the exact file set the daemons and the
  producer load from that copy; ``tools/test_host_broker_install_closure.py``
  materializes it alone and runs every load path under ``python3 -I``.
* Q5 the monitor account is in the account set. Q7 dpkg packages are
  verify-and-record entries. Q11 healthsig is excluded explicitly.
* operator-only values (bot token, human id, approver identities) are
  REFERENCES ONLY (``operator-batch:<n>`` ids and paths), never inline.

CLI (dark)::

    git ls-files -z > T
    python3 tools/host_broker_install.py plan --manifest M --registry R --tracked-files T [--out P]
    python3 tools/host_broker_install.py names [--check]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from tools.germinate.engine.contract import contained_module_path, handlers_path_for  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.registry.contract_digest import PINNED_PYYAML_VERSIONS  # noqa: E402
from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS  # noqa: E402
from tools.mcp_allowlist_registry import RegistryError, load_registry  # noqa: E402

#: DARK by construction (T-2920 HARD BOUNDARY). U1 has no apply path at all;
#: this literal is the switch a later reviewed unit would have to flip.
LIVE_ACTUATION_ENABLED: Final = False

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DIST_REL: Final = "tools/host_control/dist"
EXAMPLE_MANIFEST: Final = REPO_ROOT / "tools" / "host_broker_install.manifest.example.json"
NAMES_JSON: Final = REPO_ROOT / "tools" / "host_broker_install.names.json"
NAMES_MD: Final = REPO_ROOT / "tools" / "host_broker_install.names.md"

MANIFEST_SCHEMA: Final = "pantheon-host-broker-install/v1"
PLAN_SCHEMA: Final = "pantheon-host-broker-install-plan/v1"
NAMES_SCHEMA: Final = "pantheon-host-broker-install-names/v1"
REGISTRY_SCHEMA: Final = "pantheon-host-seat-registry/v1"
EXECUTOR_CONFIG_SCHEMA: Final = "pantheon-host-executor-config/v1"

#: The approval journal v1 DDL digest baseline (T-2919 FLAG on T-2920: the v1
#: DDL was amended in place; this is the baseline T-2920 installs against and
#: never migrates, Q10).
APPROVAL_JOURNAL_SCHEMA_DIGEST_V1: Final = "bf169e0452fd0d1ccc3c012afdd2f12de8acda58ae8131545e878023b03617ff"

#: Fixed binaries for the producer registration argv (recorded, never run here).
SETPRIV: Final = "/usr/bin/setpriv"
#: Recovery D15 (T-2920 U4b): the producer's setpriv runs with these three flags, in this
#: order, after --clear-groups. The uid drop alone leaves setuid / file-cap binaries
#: as a way back to privilege.
SETPRIV_HARDENING: Final = ("--no-new-privs", "--inh-caps=-all", "--bounding-set=-all")
ENV: Final = "/usr/bin/env"
PYTHON3: Final = "/usr/bin/python3"

ACCOUNT_ROLES: Final = ("broker", "approval", "ingress", "monitor")

#: MED-1: what root does with a producer output before it installs it. The
#: producer runs as nobody and writes into a fresh nobody-owned 0700 dir, so
#: root never trusts the path or the bytes on sight.
PRODUCER_ROOT_CHECKS: Final = (
    "open the --out path as root with O_RDONLY|O_NOFOLLOW; refuse a symlink or a non-regular file",
    "fstat: owner is the producer user, mode 0644, size <= host_control.registry.PLAN_MAX_BYTES",
    "sha256 of the bytes read == the sha256 in the producer's printed plan record",
    "InstallPlan.from_dict(bytes).validate_digest() is true and the plan target equals the seat record",
    "install those SAME bytes (never the path) to the broker plans dir as root:root 0644, then re-hash the installed file",
    "remove the staging dir after registration; a pre-existing staging dir refuses the run",
)

#: Entry modules of every process that loads the framework copy, including
#: U2's broker and approval daemon entrypoints (#1367, the launcher modules).
CLOSURE_ENTRY_MODULES: Final = (
    "tools.host_control.__main__",  # broker daemon: python3 -I -m tools.host_control (U2)
    "tools.host_control.approval.__main__",  # approval daemon: python3 -I -m tools.host_control.approval (U2)
    "tools.host_control.dbus_service",  # broker service
    "tools.host_control.runtime_table",  # broker load_daemon_runtime
    "tools.host_control.approval.dbus_service",
    "tools.host_control.approval.service",
    "tools.host_control.executor.__main__",
    "tools.host_control.executor.dbus_service",
    "tools.host_control.telegram_ingress.__main__",
    "tools.host_control.telegram_ingress.main",
    "tools.host_plan_producer",  # T-5533 registration step
    "tools.persona_bringup",  # executor direct-fire runs it with python3 -I from the copy
    "tools.germinate.seat_child",  # T-6167 PR-4: the root launcher's seat child, python3 -I -B -c from the copy
)

#: Non-Python files the copy must carry, each with the reader that needs it.
#: (Derived additions: component module/handlers files from registry rows, the
#: registered template ids, and the policy bundle dirs; see derive_closure.)
CLOSURE_DATA_FILES: Final = (
    ("registry/systems.yaml", "load_repo_op_registry (runtime_table.load_checkout_op_registry)"),
    ("instance.yaml", "agent_role_map import-time read; executor direct-fire --manifest"),
    ("tools/mcp_allowlist.yaml", "persona_installer and host-broker MCP admission gate"),
    # T-5961: the fleet-usage periodic timer instance of a codex seat is its appserver seat name
    ("harness/codex/appserver_seats.json", "persona_installer _fleet_usage_seat (codex fleet-usage timer instance)"),
)
CLOSURE_DATA_DIRS: Final = (
    # (dir, file suffix filter or "", reader). Top-level files only.
    ("tools/systemd", "", "persona_installer _SYSTEMD_TEMPLATE_DIR (templates read per manifest)"),
    ("pinned-rules", ".md", "persona_brief / executor _brief_check / codex standing instructions"),
)

#: External imports allowed in the closure, mapped to their verify-only package.
EXTERNAL_PACKAGES: Final = {"yaml": "python3-yaml", "dbus": "python3-dbus", "gi": "python3-gi"}

#: Q7 + Q-D: verify-and-record, never install.
REQUIRED_VERIFY_PACKAGES: Final = frozenset({"python3", "python3-dbus", "python3-gi", "python3-yaml"})

NONE_LINES: Final = (
    {"id": "polkit", "reason": "no polkit action or rule: authority is D-Bus policy plus server-side uid re-checks (ORG-052 sections 2/5)"},
    {"id": "sockets", "reason": "no .socket units: every service is Type=dbus or bus-activated"},
    {"id": "tmpfiles", "reason": "no tmpfiles.d entries: state roots are systemd StateDirectory= (created by systemd)"},
    {"id": "logrotate", "reason": "no logrotate config: services log to the journal only"},
)
RESIDUALS: Final = (
    {"id": "no-cgroup-limits", "detail": "the units set no MemoryMax/CPUQuota/TasksMax; recorded, not added here"},
    {"id": "active-switch-at-restart", "detail": "the Q6 atomic switch is atomic for the installer pointer (one rename of 'active'); daemons switch at restart with new rev-pinned configs, because the O_NOFOLLOW loader refuses a symlinked trusted root"},
    {"id": "ingress-egress-af-inet", "detail": "the ingress unit allows AF_INET/AF_INET6 (Q8); egress narrowing to api.telegram.org is later hardening with the operator"},
    {"id": "service-uids-in-normal-user-range", "detail": "families account.ensure requires uid >= 1000, so the service accounts sit in the normal-user range (operator-visible name-table item)"},
    {"id": "skel-copied-into-service-homes", "detail": "account.ensure always runs useradd --create-home; homes sit under /var/lib/pantheon/home, apart from every StateDirectory"},
)
EXCLUSIONS: Final = (
    {"id": "excl:healthsig", "reason": "Q11: pantheon-healthsig and the health-signal writer are the ORG-050 installer's (ORG-097/RFC-003), not T-2920's",
     "covers": ["tools/host_control/dist/pantheon-health-signal-writer.service", "account:pantheon-healthsig"]},
)

#: The paths section: fixed keys. owner/group are "root" or an account role.
PATH_KEYS: Final = (
    "etc_root", "broker_etc", "broker_plans", "broker_manifests", "approval_etc", "executor_etc",
    "ingress_etc", "ingress_credentials", "lib_root", "framework_base", "var_lib_root", "home_parent",
    "producer_run_root", "producer_staging",
)
FILE_KEYS: Final = ("seat_registry", "approver_policy", "broker_policy", "executor_config", "broker_config", "approval_config",
             "ingress_config")
EXECUTABLE_KEYS: Final = ("broker", "approval", "ingress", "executor")
RUNTIME_KEYS: Final = ("host", "approval")
#: Recovery D15 ruling (T-2920 U3a, build_runtime runs repo code AS ROOT): the output
#: dirs and launchers are FIXED literals (build_runtime.py hard-codes them too),
#: and --adapter comes only from this 2-value set.
RUNTIME_ADAPTERS: Final = {
    "host": {"prefix": "/usr/local/lib/pantheon/host-mcp-adapter",
             "launcher": "/usr/local/libexec/pantheon-host-mcp-adapter", "package": "tools/host_mcp_adapter",
             "modules_literal": "PACKAGED_MODULES"},
    "approval": {"prefix": "/usr/local/lib/pantheon/approval-mcp-adapter",
                 "launcher": "/usr/local/libexec/pantheon-approval-mcp-adapter", "package": "tools/approval_mcp_adapter",
                 "modules_literal": "APPROVAL_PACKAGED_MODULES"},
}
BUILD_RUNTIME_MODULE: Final = "tools.host_mcp_adapter.dist.build_runtime"
BUILD_RUNTIME_REL: Final = "tools/host_mcp_adapter/dist/build_runtime.py"
BUILD_RUNTIME_DATA: Final = ("tools/host_mcp_adapter/dist/requirements.in", "tools/host_mcp_adapter/dist/requirements.lock")

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SEAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MODE_RE = re.compile(r"^0[0-7]{3}$")
_REF_RE = re.compile(r"^operator-batch:[0-9]{1,2}$")
#: 20+ chars of an id/secret alphabet with a digit in it, or 6+ digits in a row
_TOKENISH = re.compile(r"(?=[A-Za-z0-9_:-]*[0-9])[A-Za-z0-9_:-]{20,}|[0-9]{6,}")
MANIFEST_STATUSES: Final = frozenset({"provisional-pending-ratification", "ratified"})
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")
#: Characters a substituted value may never carry (newline/quote/XML/systemd
#: specifiers), so no value can inject a unit line, an XML node or JSON.
_UNSAFE_VALUE = frozenset("\n\r\t \"'<>&%$`\\;@")


class InstallPlanError(ValueError):
    """The inputs are refused; the message names the field."""


# ---------------------------------------------------------------------------
# canonical bytes
# ---------------------------------------------------------------------------


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InstallPlanError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_json_strict(raw: bytes, what: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallPlanError(f"{what}: not valid JSON: {exc}") from None


# ---------------------------------------------------------------------------
# 1. the manifest (closed, versioned)
# ---------------------------------------------------------------------------


def _closed(where: str, value: Any, keys: Iterable[str]) -> Mapping[str, Any]:
    keys = frozenset(keys)
    if not isinstance(value, dict):
        raise InstallPlanError(f"{where}: must be a mapping")
    unknown, missing = set(value) - keys, keys - set(value)
    if unknown:
        raise InstallPlanError(f"{where}: unknown fields {sorted(unknown)}")
    if missing:
        raise InstallPlanError(f"{where}: missing fields {sorted(missing)}")
    return value


def _abs_path(where: str, value: Any) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or posixpath.normpath(value) != value or value == "/" or set(value) & _UNSAFE_VALUE
            or "\x00" in value):
        raise InstallPlanError(f"{where}: {value!r} is not a normalized absolute path")
    return value


def _str(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InstallPlanError(f"{where}: must be a non-empty string")
    return value


def _prose(where: str, value: Any) -> str:
    """A short human sentence: bounded length, words with spaces, and no
    digit-only or token-shaped run (no id or secret can hide in it)."""

    if (not isinstance(value, str) or not 16 <= len(value) <= 300 or " " not in value
            or not re.search(r"[A-Za-z]{3,}", value) or _TOKENISH.search(value) or value.strip().isdigit()):
        raise InstallPlanError(f"{where}: must be a short prose sentence (16-300 chars, no token-shaped runs)")
    return value


def _ref(where: str, value: Any) -> str:
    if not isinstance(value, str) or not _REF_RE.match(value):
        raise InstallPlanError(f"{where}: {value!r} must be a reference id operator-batch:<n>, never an inline value")
    return value


@dataclass(frozen=True)
class InstallManifest:
    doc: Mapping[str, Any]
    sha256: str

    @property
    def revision(self) -> str:
        return self.doc["revision"]

    def account(self, role: str) -> Mapping[str, Any]:
        return self.doc["accounts"][role]

    def account_name(self, role: str) -> str:
        return self.doc["accounts"][role]["name"]

    def home(self, role: str) -> str:
        return posixpath.join(self.path("home_parent"), self.account_name(role))

    def path(self, key: str) -> str:
        return self.doc["paths"][key]["path"]

    def file(self, key: str) -> str:
        return self.doc["files"][key]["path"]

    @property
    def protocol_test_seat_ids(self) -> tuple[str, ...]:
        """The only seats whose installer plans are registered with the broker
        (ORG-052 section 8 clarification, #1665): disposable protocol-test seats."""

        return tuple(self.doc["plan_registration"]["protocol_test_seat_ids"])


def parse_manifest(raw: bytes) -> InstallManifest:
    """Validate the closed ``pantheon-host-broker-install/v1`` manifest."""

    doc = load_json_strict(raw, "manifest")
    top = ("schema", "revision", "status", "accounts", "account_defaults", "groups", "paths", "files", "executables",
           "framework", "mailbox_hub", "approvers", "refs", "packages_verify", "runtimes", "producer", "plan_registration")
    _closed("manifest", doc, top)
    if doc["schema"] != MANIFEST_SCHEMA:
        raise InstallPlanError(f"manifest schema must be {MANIFEST_SCHEMA}")
    if not isinstance(doc["revision"], str) or not _REVISION_RE.match(doc["revision"]):
        raise InstallPlanError("revision: must be an identifier")
    if doc["status"] not in MANIFEST_STATUSES:
        raise InstallPlanError(f"status: must be one of {sorted(MANIFEST_STATUSES)}")

    accounts = _closed("accounts", doc["accounts"], ACCOUNT_ROLES)
    names: set[str] = set()
    uids: set[int] = set()
    for role in ACCOUNT_ROLES:
        spec = _closed(f"accounts.{role}", accounts[role], ("name", "uid"))
        if not isinstance(spec["name"], str) or not _NAME_RE.match(spec["name"]):
            raise InstallPlanError(f"accounts.{role}.name: {spec['name']!r} is not an account name")
        uid = spec["uid"]
        # families.load_manifest requires uid >= 1000 (account.ensure, gid == uid)
        if not isinstance(uid, int) or isinstance(uid, bool) or not 1000 <= uid <= 59999:
            raise InstallPlanError(f"accounts.{role}.uid: must be an int in 1000..59999 (families account.ensure)")
        if spec["name"] in names or uid in uids:
            raise InstallPlanError(f"accounts.{role}: duplicate account name or uid")
        names.add(spec["name"])
        uids.add(uid)

    defaults = _closed("account_defaults", doc["account_defaults"], ("shell", "locked"))
    if defaults["shell"] != "/usr/sbin/nologin":
        raise InstallPlanError("account_defaults.shell: service accounts are /usr/sbin/nologin")
    if defaults["locked"] is not True:
        raise InstallPlanError("account_defaults.locked: service accounts are locked (Q5)")

    groups = _closed("groups", doc["groups"], ("entries", "none_reason"))
    if groups["entries"] != []:
        raise InstallPlanError("groups.entries: only user-private groups (gid == uid) exist; extra groups are refused")
    _prose("groups.none_reason", groups["none_reason"])

    owners = {"root", *ACCOUNT_ROLES, "producer"}
    paths = _closed("paths", doc["paths"], PATH_KEYS)
    seen_paths: set[str] = set()
    for key in PATH_KEYS:
        spec = _closed(f"paths.{key}", paths[key], ("path", "owner", "group", "mode"))
        path = _abs_path(f"paths.{key}.path", spec["path"])
        if path in seen_paths:
            raise InstallPlanError(f"paths.{key}: duplicate path")
        seen_paths.add(path)
        if spec["owner"] not in owners or spec["group"] not in owners:
            raise InstallPlanError(f"paths.{key}: owner/group must be root or an account role")
        if not isinstance(spec["mode"], str) or not _MODE_RE.match(spec["mode"]):
            raise InstallPlanError(f"paths.{key}.mode: must be a 4-digit octal string")
    if paths["ingress_credentials"]["mode"] != "0700" or paths["ingress_credentials"]["owner"] != "ingress":
        raise InstallPlanError("paths.ingress_credentials: the credentials parent is 0700, owned by the ingress account")
    # MED-1: the producer (nobody) writes its plan with O_EXCL into the staging
    # dir, so that dir is owned by the producer user, 0700, under a root-owned
    # run root; "producer" is valid for that one path only.
    staging = paths["producer_staging"]
    if (staging["owner"], staging["group"], staging["mode"]) != ("producer", "producer", "0700"):
        raise InstallPlanError("paths.producer_staging: must be owner producer, group producer, mode 0700 (the producer writes there)")
    if posixpath.dirname(staging["path"]) != paths["producer_run_root"]["path"]:
        raise InstallPlanError("paths.producer_staging: must be a direct child of paths.producer_run_root")
    for key in PATH_KEYS:
        if key != "producer_staging" and "producer" in (paths[key]["owner"], paths[key]["group"]):
            raise InstallPlanError(f"paths.{key}: only producer_staging may be owned by the producer user")

    files = _closed("files", doc["files"], FILE_KEYS)
    for key in FILE_KEYS:
        optional = {"approver_policy": ("path", "ref"), "broker_policy": ("path", "ref"), "broker_config": ("path", "placeholder"),
                    "approval_config": ("path", "placeholder")}.get(key, ("path",))
        spec = _closed(f"files.{key}", files[key], optional)
        _abs_path(f"files.{key}.path", spec["path"])
        if "ref" in spec:
            _ref(f"files.{key}.ref", spec["ref"])
        if "placeholder" in spec and spec["placeholder"] != "U2 config schema":
            raise InstallPlanError(f"files.{key}.placeholder: must be 'U2 config schema'")

    executables = _closed("executables", doc["executables"], EXECUTABLE_KEYS)
    for key in EXECUTABLE_KEYS:
        _abs_path(f"executables.{key}", executables[key])

    framework = _closed("framework", doc["framework"], ("base_key", "active_link", "keep_previous"))
    if framework["base_key"] != "framework_base":
        raise InstallPlanError("framework.base_key: must name paths.framework_base")
    if framework["active_link"] != "active":
        raise InstallPlanError("framework.active_link: must be 'active' (Q6)")
    if framework["keep_previous"] != 1:
        raise InstallPlanError("framework.keep_previous: exactly the previous rev is kept (Q6)")

    _abs_path("mailbox_hub", doc["mailbox_hub"])

    approvers = _closed("approvers", doc["approvers"], ("seat_ids", "policy_ref"))
    if not isinstance(approvers["seat_ids"], list) or not all(
            isinstance(s, str) and _SEAT_ID_RE.match(s) for s in approvers["seat_ids"]):
        raise InstallPlanError("approvers.seat_ids: must be a list of seat ids (references into the seat registry)")
    if len(set(approvers["seat_ids"])) != len(approvers["seat_ids"]):
        raise InstallPlanError("approvers.seat_ids: duplicates are refused")
    _ref("approvers.policy_ref", approvers["policy_ref"])

    refs = _closed("refs", doc["refs"], ("telegram_bot_token", "telegram_human"))
    token = _closed("refs.telegram_bot_token", refs["telegram_bot_token"], ("credential_id", "source_ref"))
    if not isinstance(token["credential_id"], str) or not _IDENT_RE.match(token["credential_id"]):
        raise InstallPlanError("refs.telegram_bot_token.credential_id: must be an identifier")
    _ref("refs.telegram_bot_token.source_ref", token["source_ref"])
    human = _closed("refs.telegram_human", refs["telegram_human"], ("source_ref",))
    _ref("refs.telegram_human.source_ref", human["source_ref"])

    packages = doc["packages_verify"]
    if not isinstance(packages, list) or set(packages) != REQUIRED_VERIFY_PACKAGES or len(packages) != len(set(packages)):
        raise InstallPlanError(f"packages_verify: must be exactly {sorted(REQUIRED_VERIFY_PACKAGES)} (verify-only, Q7)")

    runtimes = _closed("runtimes", doc["runtimes"], RUNTIME_KEYS)
    for key in RUNTIME_KEYS:
        spec = _closed(f"runtimes.{key}", runtimes[key], ("prefix", "launcher"))
        _abs_path(f"runtimes.{key}.prefix", spec["prefix"])
        _abs_path(f"runtimes.{key}.launcher", spec["launcher"])
        fixed = RUNTIME_ADAPTERS[key]
        if (spec["prefix"], spec["launcher"]) != (fixed["prefix"], fixed["launcher"]):
            raise InstallPlanError(f"runtimes.{key}.launcher and .prefix are fixed literals (Recovery D15): "
                                   f"{fixed['prefix']}, {fixed['launcher']}")

    # ORG-052 section 8 clarification (#1665): the broker is sudo-on-behalf for running
    # seats; seat installation is the germination engine's. Production seat plans are
    # NOT registered; only a disposable protocol-test seat named here is (ORG-052 E37).
    plan_reg = _closed("plan_registration", doc["plan_registration"], ("protocol_test_seat_ids",))
    test_ids = plan_reg["protocol_test_seat_ids"]
    if not isinstance(test_ids, list) or not all(isinstance(s, str) and _SEAT_ID_RE.match(s) for s in test_ids):
        raise InstallPlanError("plan_registration.protocol_test_seat_ids: must be a list of seat ids")
    if len(set(test_ids)) != len(test_ids):
        raise InstallPlanError("plan_registration.protocol_test_seat_ids: duplicates are refused")
    if set(test_ids) & set(approvers["seat_ids"]):
        raise InstallPlanError("plan_registration.protocol_test_seat_ids: an approver seat cannot be a protocol-test seat")

    producer = _closed("producer", doc["producer"], ("user", "group", "via"))
    if (producer["user"], producer["group"], producer["via"]) != ("nobody", "nogroup", "setpriv"):
        raise InstallPlanError("producer: runs as nobody:nogroup via setpriv (Q-C): no new account, no new grant")
    return InstallManifest(doc, sha256_bytes(raw))


# ---------------------------------------------------------------------------
# the seat registry input (pure parse; mirrors tools/host_control/registry.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seat:
    seat_id: str
    uid: int
    persona: str
    role: str
    instance: str
    account: str
    harness: str
    install_root: str

    @property
    def seat_root(self) -> str:
        return posixpath.normpath(posixpath.join(self.install_root, "home", self.account))

    def record(self) -> dict[str, Any]:
        return {"seat_id": self.seat_id, "uid": self.uid, "persona": self.persona, "role": self.role,
                "instance": self.instance, "account": self.account, "harness": self.harness,
                "install_root": self.install_root}


@dataclass(frozen=True)
class SeatRegistryInput:
    doc: Mapping[str, Any]
    seats: tuple[Seat, ...]
    manifests: tuple[Mapping[str, Any], ...]
    sha256: str

    def seat(self, seat_id: str) -> Seat:
        for seat in self.seats:
            if seat.seat_id == seat_id:
                return seat
        raise InstallPlanError(f"seat {seat_id!r} is not in the seat registry")


def parse_registry(raw: bytes) -> SeatRegistryInput:
    """Validate a pre-registration seat registry with the same field sets as
    ``host_control.registry.SeatRegistry.load`` (which needs the root loader
    and real host paths, so it is not used at plan time)."""

    from tools.host_control.registry import _MANIFEST_KEYS, _SEAT_KEYS

    doc = load_json_strict(raw, "registry")
    if isinstance(doc, dict) and "plans" in doc:
        if doc["plans"] != []:
            raise InstallPlanError("registry: plans must be empty; plan registration is a step of this plan (T-5533)")
        doc = {k: v for k, v in doc.items() if k != "plans"}
    _closed("registry", doc, ("schema", "seats", "manifests"))
    if doc["schema"] != REGISTRY_SCHEMA:
        raise InstallPlanError(f"registry schema must be {REGISTRY_SCHEMA}")
    if not isinstance(doc["seats"], list) or not doc["seats"]:
        raise InstallPlanError("registry seats must be a non-empty list")
    if not isinstance(doc["manifests"], list) or not doc["manifests"]:
        raise InstallPlanError("registry manifests must be a non-empty list")
    seats: list[Seat] = []
    for index, item in enumerate(doc["seats"]):
        value = _closed(f"registry.seats[{index}]", item, _SEAT_KEYS)
        seat_id = value["seat_id"]
        if not isinstance(seat_id, str) or not _SEAT_ID_RE.match(seat_id):
            # stricter than the registry: a seat id names a unit instance and
            # policy files, so ':' (no unit-file regex admits it) is refused
            raise InstallPlanError(f"registry.seats[{index}].seat_id: {seat_id!r} cannot name a unit or policy file")
        uid = value["uid"]
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 1000:
            raise InstallPlanError(f"registry.seats[{index}].uid: must be an int >= 1000")
        if not isinstance(value["account"], str) or not _NAME_RE.match(value["account"]):
            raise InstallPlanError(f"registry.seats[{index}].account: not an account name")
        for field in ("persona", "role", "instance"):
            if not isinstance(value[field], str) or not _IDENT_RE.match(value[field]):
                raise InstallPlanError(f"registry.seats[{index}].{field}: not an identifier")
        if value["harness"] not in ("claude", "codex"):
            raise InstallPlanError(f"registry.seats[{index}].harness: must be claude or codex")
        root = value["install_root"]
        if not isinstance(root, str) or not root.startswith("/") or posixpath.normpath(root) != root or root.startswith("//"):
            raise InstallPlanError(f"registry.seats[{index}].install_root: not a normalized absolute path")
        seats.append(Seat(seat_id, uid, value["persona"], value["role"], value["instance"], value["account"],
                          value["harness"], root))
    if len({s.seat_id for s in seats}) != len(seats) or len({s.uid for s in seats}) != len(seats):
        raise InstallPlanError("registry: duplicate seat id or uid")
    manifests = []
    for index, item in enumerate(doc["manifests"]):
        value = _closed(f"registry.manifests[{index}]", item, _MANIFEST_KEYS)
        for field in ("revision", "framework_revision"):
            if not isinstance(value[field], str) or not _IDENT_RE.match(value[field]):
                raise InstallPlanError(f"registry.manifests[{index}].{field}: not an identifier")
        _abs_path(f"registry.manifests[{index}].path", value["path"])
        for field in ("sha256", "framework_sha256"):
            if not isinstance(value[field], str) or not _HEX_RE.match(value[field]):
                raise InstallPlanError(f"registry.manifests[{index}].{field}: not a sha256")
        ids = value["template_ids"]
        if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or not all(
                isinstance(t, str) and t and not t.startswith("/") and ".." not in t.split("/") for t in ids)):
            raise InstallPlanError(f"registry.manifests[{index}].template_ids: invalid")
        manifests.append(value)
    if len({m["revision"] for m in manifests}) != len(manifests):
        raise InstallPlanError("registry: duplicate manifest revision")
    return SeatRegistryInput(doc, tuple(sorted(seats, key=lambda s: s.seat_id)),
                             tuple(sorted(manifests, key=lambda m: m["revision"])), sha256_bytes(raw))


# ---------------------------------------------------------------------------
# 2. resolver and renderer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSpec:
    vars: frozenset[str]
    #: regexes for literal '@' occurrences allowed in the RENDERED text
    literal_at: tuple[str, ...] = ()
    #: variables that may stay unresolved (operator-only; rendered at apply)
    deferred: frozenset[str] = frozenset()
    json_typed: bool = False


TEMPLATES: Final[Mapping[str, TemplateSpec]] = {
    "pantheon-host-executor@.service": TemplateSpec(
        frozenset({"SEAT_ACCOUNT", "SEAT_UID", "SEAT_ROOT", "MAILBOX_HUB"}),
        literal_at=(r"pantheon-host-executor@<seat_id>\.service", r"user@[0-9]+\.service",
                    r"(?m)^SystemCallFilter=@system-service$"),
    ),
    "org.pantheon.HostExecutor1.service.template": TemplateSpec(
        frozenset({"SEAT_UID", "SEAT_ACCOUNT", "SEAT_ID"}),
        literal_at=(r"pantheon-host-executor@[A-Za-z0-9._-]+\.service",),
    ),
    "pantheon-host-broker-seat.conf.template": TemplateSpec(frozenset({"SEAT_ACCOUNT"})),
    "pantheon-host-executor-seat.conf.template": TemplateSpec(frozenset({"SEAT_ACCOUNT", "SEAT_UID"})),
    "pantheon-host-approval-approver.conf.template": TemplateSpec(frozenset({"APPROVER_ACCOUNT"})),
    "pantheon-host-approval-telegram-ingress.json.template": TemplateSpec(
        frozenset({"INGRESS_UID", "APPROVAL_UID", "INGRESS_GID", "ALLOWLISTED_TELEGRAM_USER_ID",
                   "ALLOWLISTED_TELEGRAM_CHAT_ID"}),
        deferred=frozenset({"ALLOWLISTED_TELEGRAM_USER_ID", "ALLOWLISTED_TELEGRAM_CHAT_ID"}),
        json_typed=True,
    ),
}
#: The ONLY variables anywhere that may be deferred (Q-F), and only in that template.
DEFERRED_VARS: Final = frozenset({"ALLOWLISTED_TELEGRAM_USER_ID", "ALLOWLISTED_TELEGRAM_CHAT_ID"})


class RenderError(InstallPlanError):
    """A template could not be rendered closed."""


def _check_value(name: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RenderError(f"@{name}@: value must be an int or a string")
    text = str(value)
    if not text or set(text) & _UNSAFE_VALUE or "\x00" in text:
        raise RenderError(f"@{name}@: value {text!r} carries a forbidden character")
    return text


def render_template(name: str, text: str, values: Mapping[str, Any], *, allow_deferred: bool = False) -> str:
    """Substitute every ``@VAR@``. Unknown or unused declared variables, a
    missing value, or ANY '@' left after rendering (outside the template's
    allowlisted literal contexts) fail. ``allow_deferred`` keeps the template's
    declared operator-only tokens in place (used only to pin the partial render)."""

    spec = TEMPLATES.get(name)
    if spec is None:
        raise RenderError(f"{name}: not a known template")
    found = set(_TOKEN_RE.findall(text))
    if found - spec.vars:
        raise RenderError(f"{name}: unknown variables {sorted(found - spec.vars)}")
    if spec.vars - found:
        raise RenderError(f"{name}: declared but unused variables {sorted(spec.vars - found)}")
    if not spec.deferred <= DEFERRED_VARS:
        raise RenderError(f"{name}: only {sorted(DEFERRED_VARS)} may be deferred")

    def resolve(var: str) -> str | None:
        """The checked value text, or None for a kept deferred token."""

        if var in spec.deferred and var not in values:
            if allow_deferred:
                return None
            raise RenderError(f"{name}: @{var}@ is an operator-only input, rendered at apply (deferred)")
        if var not in values:
            raise RenderError(f"{name}: no value for @{var}@")
        return _check_value(var, values[var])

    def substitute(match: re.Match[str]) -> str:
        text_value = resolve(match.group(1))
        return match.group(0) if text_value is None else text_value

    if spec.json_typed:
        # a token that is a whole JSON string value renders as a typed JSON
        # literal: an int stays an int (the ingress config parser needs ints)
        def typed(match: re.Match[str]) -> str:
            var = match.group(1)
            text_value = resolve(var)
            if text_value is None:
                return match.group(0)
            value = values[var]
            return json.dumps(value) if isinstance(value, int) else json.dumps(text_value)

        out = _TOKEN_RE.sub(substitute, re.sub(r'"@([A-Z][A-Z0-9_]*)@"', typed, text))
    else:
        out = _TOKEN_RE.sub(substitute, text)
    residue = out
    for pattern in spec.literal_at:
        residue = re.sub(pattern, "", residue)
    if allow_deferred:
        for var in spec.deferred:
            residue = residue.replace(f"@{var}@", "")
    if "@" in residue:
        raise RenderError(f"{name}: a leftover '@' survived rendering")
    if spec.json_typed:
        try:
            json.loads(out)
        except json.JSONDecodeError as exc:
            raise RenderError(f"{name}: the rendered output is not valid JSON: {exc}") from None
    return out


def dist_bytes(repo_root: Path, name: str) -> bytes:
    return (repo_root / DIST_REL / name).read_bytes()


# ---------------------------------------------------------------------------
# 4. the framework-copy closure
# ---------------------------------------------------------------------------


class ClosureError(InstallPlanError):
    """The closure cannot be derived self-contained."""


@dataclass(frozen=True)
class Closure:
    files: tuple[str, ...]
    external_imports: frozenset[str]
    digest: str

    @property
    def rev(self) -> str:
        return self.digest[:16]


def _module_file(repo_root: Path, dotted: str) -> Path | None:
    base = repo_root.joinpath(*dotted.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if base.is_dir() and not base.is_symlink():
        return base  # a PEP 420 namespace package (e.g. tools/oom): no file of its own
    return None


def _package_of(dotted: str, file: Path) -> str:
    return dotted if file.name == "__init__.py" or file.is_dir() else dotted.rpartition(".")[0]


def _lazy_exports(tree: ast.Module) -> list[str]:
    """A module-level ``_EXPORTS = {name: "tools.x"}`` table (PEP 562 lazy
    loader, tools/host_control/__init__.py) names modules the walk follows."""

    out = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in node.targets):
            value = ast.literal_eval(node.value)
            out.extend(v for v in value.values() if isinstance(v, str) and v.startswith("tools."))
    return out


def import_closure(repo_root: Path, entries: Sequence[str]) -> tuple[set[str], set[str]]:
    """Static import walk (module- AND function-level imports) from ``entries``.
    Returns (repo-relative .py files, external top-level module names)."""

    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    files: set[str] = set()
    external: set[str] = set()
    seen: set[str] = set()
    queue = list(entries)

    def want(dotted: str) -> None:
        """Queue ``dotted`` and every parent package; refuse what does not resolve."""

        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if _module_file(repo_root, prefix) is None:
                raise ClosureError(f"import {dotted!r} does not resolve inside the repo")
            if prefix not in seen:
                seen.add(prefix)
                queue.append(prefix)

    for entry in entries:
        want(entry)
    while queue:
        dotted = queue.pop()
        file = _module_file(repo_root, dotted)
        if file is None:  # pragma: no cover - want() resolved it
            raise ClosureError(f"{dotted!r} vanished")
        if file.is_symlink():
            raise ClosureError(f"{file}: a symlink cannot enter the framework copy")
        if file.is_dir():
            continue
        files.add(file.relative_to(repo_root).as_posix())
        tree = ast.parse(file.read_bytes(), filename=str(file))
        package = _package_of(dotted, file)
        for target in _lazy_exports(tree):
            want(target)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "tools":
                        want(alias.name)
                    elif _module_file(repo_root, f"tools.{alias.name}") is not None:
                        want(f"tools.{alias.name}")  # a sibling-script import (tools/ on sys.path)
                    elif top not in stdlib:
                        external.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = package.split(".")
                    if node.level > 1:
                        base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                top = base.split(".")[0]
                if top == "tools":
                    want(base)
                    for alias in node.names:
                        if alias.name != "*" and _module_file(repo_root, f"{base}.{alias.name}") is not None:
                            want(f"{base}.{alias.name}")
                elif top and _module_file(repo_root, f"tools.{base}") is not None:
                    want(f"tools.{base}")  # a sibling-script import (tools/ on sys.path)
                    for alias in node.names:
                        if alias.name != "*" and _module_file(repo_root, f"tools.{base}.{alias.name}") is not None:
                            want(f"tools.{base}.{alias.name}")
                elif top and top not in stdlib:
                    external.add(top)
    return files, external


def _component_files(repo_root: Path) -> set[str]:
    """Every component module + handlers file the op-registry loader reads
    (``components.discover_doc`` row filter, import_handlers=False)."""

    from tools.germinate.registry import schema as RS

    doc = yaml.safe_load((repo_root / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    out: set[str] = set()
    for row in (doc.get("systems") or []) if isinstance(doc, dict) else []:
        if not isinstance(row, dict) or row.get("kind") != RS.KIND_COMPONENT:
            continue
        contract = row.get("contract")
        if not isinstance(contract, dict) or contract.get("repo") != RS.INTERNAL_REPO:
            continue
        if not RS.DIGEST_RE.match(str(contract.get("digest"))):
            continue
        module = contained_module_path(repo_root, contract.get("module"))
        for path in (module, handlers_path_for(module)):
            if path.is_file():
                out.add(path.relative_to(repo_root.resolve()).as_posix())
    return out


def _policy_bundle_dirs(repo_root: Path) -> list[str]:
    doc = yaml.safe_load((repo_root / "instance.yaml").read_text(encoding="utf-8"))
    bundle = (((doc or {}).get("persona_installer") or {}).get("policy") or {}).get("bundle") or []
    out = []
    for entry in bundle:
        rel = str(entry).rstrip("/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise ClosureError(f"policy bundle entry {entry!r} is not repo-relative")
        out.append(rel)
    return out


def _walk_files(repo_root: Path, rel_dir: str, tracked: frozenset[str], *, recursive: bool,
                suffix: str = "") -> set[str]:
    """Files under ``rel_dir`` that git TRACKS (MED-2): an untracked or ignored
    stray (an editor swap file, a local .env or key) never enters the copy and
    never moves the content-addressed rev."""

    base = repo_root / rel_dir
    if not base.is_dir():
        raise ClosureError(f"{rel_dir}: closure data dir is missing")
    out: set[str] = set()
    iterator = base.rglob("*") if recursive else base.iterdir()
    for path in iterator:
        rel = path.relative_to(repo_root)
        if any(part.startswith(".") or part == "__pycache__" or part == "tests" for part in rel.parts):
            continue
        if path.name.startswith("test_"):  # repo tests never ship in the copy
            continue
        if rel.as_posix() not in tracked:
            continue
        if path.is_symlink():
            raise ClosureError(f"{rel}: a symlink cannot enter the framework copy")
        if path.is_file() and path.name.endswith(suffix):
            out.add(rel.as_posix())
    return out


def parse_tracked_files(raw: bytes) -> frozenset[str]:
    """The output of ``git ls-files -z`` (NUL-separated repo-relative paths).
    Given as an INPUT so this module stays subprocess-free (AST pin)."""

    try:
        names = [n for n in raw.decode("utf-8").split("\0") if n]
    except UnicodeDecodeError as exc:
        raise InstallPlanError(f"tracked files: not UTF-8: {exc}") from None
    if not names:
        raise InstallPlanError("tracked files: empty list (expected `git ls-files -z` output)")
    for name in names:
        if name.startswith("/") or ".." in name.split("/") or "\n" in name:
            raise InstallPlanError(f"tracked files: {name!r} is not a repo-relative path")
    return frozenset(names)


def runtime_build_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """Everything build_runtime.py (run AS ROOT from the framework copy) reads:
    its own import closure (the U1 walker), the lock and its .in, and the
    packaged modules of BOTH adapter profiles (AST-read literals). Recovery D15."""

    repo_root = Path(repo_root)
    py_files, external = import_closure(repo_root, (BUILD_RUNTIME_MODULE,))
    if external:
        raise ClosureError(f"build_runtime.py imports external modules {sorted(external)}")
    files = set(py_files) | set(BUILD_RUNTIME_DATA)
    for spec in RUNTIME_ADAPTERS.values():
        for name in _literal(repo_root, BUILD_RUNTIME_REL, spec["modules_literal"]):
            files.add(f"{spec['package']}/{name}")
    for rel in files:
        if not (repo_root / rel).is_file():
            raise ClosureError(f"{rel}: a build_runtime input is missing")
    return sorted(files)


def derive_closure(repo_root: Path = REPO_ROOT, *, tracked: frozenset[str], template_ids: Iterable[str] = ()) -> Closure:
    """The exact file set the copy must hold (see the module docstring),
    intersected with the TRACKED-file list. A required file that git does not
    track is refused by name (``ClosureError``)."""

    repo_root = Path(repo_root)
    tracked = frozenset(tracked)
    py_files, external = import_closure(repo_root, CLOSURE_ENTRY_MODULES)
    files = set(py_files)
    for rel, _reader in CLOSURE_DATA_FILES:
        if not (repo_root / rel).is_file():
            raise ClosureError(f"{rel}: closure data file is missing")
        files.add(rel)
    for rel_dir, suffix, _reader in CLOSURE_DATA_DIRS:
        files |= _walk_files(repo_root, rel_dir, tracked, recursive=False, suffix=suffix)
    for rel_dir in _policy_bundle_dirs(repo_root):
        files |= _walk_files(repo_root, rel_dir, tracked, recursive=True)
    files |= _component_files(repo_root)
    files |= set(runtime_build_files(repo_root))  # Recovery D15: the root build runs from the copy
    for rel in template_ids:
        if not (repo_root / rel).is_file():
            raise ClosureError(f"registered template id {rel!r} is not in the repo")
        files.add(rel)
    for rel in (*FRAMEWORK_CODE_ARTIFACTS, "tools/runtime_invocation_receipt.py", "tools/host_plan_producer.py"):
        if rel not in files:
            raise ClosureError(f"{rel} is missing from the closure")
    unknown = external - set(EXTERNAL_PACKAGES)
    if unknown:
        raise ClosureError(f"closure imports external modules with no verify-only package: {sorted(unknown)}")
    untracked = sorted(rel for rel in files if rel not in tracked)
    if untracked:
        raise ClosureError(f"closure-required files are not tracked by git: {untracked[:10]}")
    for rel in files:
        if (repo_root / rel).is_symlink():
            raise ClosureError(f"{rel}: a symlink cannot enter the framework copy")
    ordered = tuple(sorted(files))
    digest = hashlib.sha256()
    for rel in ordered:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((repo_root / rel).read_bytes())
        digest.update(b"\0")
    return Closure(ordered, frozenset(external), digest.hexdigest())


def framework_artifact_digest(repo_root: Path, template_ids: Sequence[str]) -> str:
    """The registry ``framework_sha256`` definition (registry.check_framework_digest,
    persona_installer._artifact_digest): sorted relpath NUL bytes NUL."""

    digest = hashlib.sha256()
    for rel in sorted((*FRAMEWORK_CODE_ARTIFACTS, *template_ids)):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((Path(repo_root) / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def materialize_closure(closure: Closure, repo_root: Path, dest: Path) -> None:
    """Copy the closure (regular files only) under ``dest`` (tests and U3/U4)."""

    for rel in closure.files:
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(repo_root) / rel).read_bytes())


# ---------------------------------------------------------------------------
# AST reads of pinned literals (no import of the module)
# ---------------------------------------------------------------------------


def _literal(repo_root: Path, rel: str, name: str) -> Any:
    tree = ast.parse((Path(repo_root) / rel).read_bytes())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
            return ast.literal_eval(node.value) if not isinstance(node.value, ast.Dict) else {
                ast.literal_eval(k): None for k in node.value.keys if k is not None}
    raise InstallPlanError(f"{rel}: no module-level {name}")


def _mcp_allowlist(repo_root: Path) -> Mapping[str, Any]:
    """Read the sole MCP registry without importing the full installer."""
    try:
        registry = load_registry(Path(repo_root) / "tools/mcp_allowlist.yaml")
    except RegistryError as exc:
        raise InstallPlanError("MCP allowlist authority is missing or malformed") from exc
    return registry.servers


def _unit_value(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith(f"{key}=")]


# ---------------------------------------------------------------------------
# 3 + 5. the plan
# ---------------------------------------------------------------------------

STATIC_UNITS: Final = ("pantheon-host-broker.service", "pantheon-host-approval.service",
                       "pantheon-host-approval-telegram-ingress.service")
STATIC_POLICIES: Final = ("org.pantheon.HostBroker1.conf", "org.pantheon.HostApproval1.conf",
                          "org.pantheon.HostApprovalTelegram1.conf", "org.pantheon.HostExecutor1.conf")
UNIT_DIR: Final = F.UNIT_DIR
DBUS_DIR: Final = F.DBUS_DIR
ACTIVATION_DIR: Final = "/usr/share/dbus-1/system-services/"

PHASES: Final = ("P0", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")
KIND_RANK: Final = {k: i for i, k in enumerate((
    "package_verify", "precondition", "journal_schema_check",
    "dir", "account", "owner_mode", "framework_tree", "framework_file", "framework_active_link", "runtime",
    "launcher", "config", "registry", "registered_manifest", "credential_ref_registration",
    "plan_registration", "registry_post_registration",
    "dbus_policy", "unit", "activation", "credential", "unit_enable", "journal_root",
))}
GATES: Final = {
    "package_verify": ["E35"], "precondition": ["E35"], "journal_schema_check": ["E35"],
    "dir": ["E35"], "account": ["E35", "E19"], "owner_mode": ["E35"], "runtime": ["E35", "E7"],
    "framework_tree": ["E35", "E36"], "framework_file": ["E35", "E36"], "framework_active_link": ["E35", "E36"],
    "launcher": ["E35", "E19"], "config": ["E35"], "registry": ["E35", "E9"], "registered_manifest": ["E35", "E9"],
    "credential_ref_registration": ["E35", "E21"], "plan_registration": ["E35"],
    "registry_post_registration": ["E35", "E9"], "dbus_policy": ["E35", "E10"], "unit": ["E35", "E19"],
    "activation": ["E35", "E10"], "credential": ["E35", "E21"], "unit_enable": ["E35"], "journal_root": ["E35"],
}


@dataclass
class _Builder:
    entries: list[dict[str, Any]]
    staging: dict[str, bytes]
    #: entry id -> the exact bytes an entry pins (U3a apply installs these)
    contents: dict[str, bytes] = field(default_factory=dict)

    def add(self, *, id: str, phase: str, kind: str, path: str | None = None, owner: str | None = None,
            group: str | None = None, mode: str | None = None, content: bytes | None = None,
            content_sha256: str | None = None, deferred: str | None = None, source: Mapping[str, Any],
            after: Sequence[str] = (), hold: str | None = None, inputs: Mapping[str, Any] | None = None,
            sub: int = 0) -> None:
        if phase not in PHASES or kind not in KIND_RANK:
            raise AssertionError(f"{id}: bad phase/kind")
        if content is not None:
            content_sha256 = sha256_bytes(content)
        if content_sha256 is None and deferred is None and kind not in (
                "package_verify", "precondition", "journal_schema_check", "account", "owner_mode", "dir",
                "unit_enable", "journal_root", "credential_ref_registration", "framework_tree"):
            raise AssertionError(f"{id}: an entry without content sha must name why (deferred)")
        if source.get("route") == "family" and "staging" in source and content is not None:
            self.staging[source["staging"]] = content
        if content is not None:
            self.contents[id] = content
        entry: dict[str, Any] = {
            "id": id, "phase": phase, "kind": kind, "path": path, "owner": owner, "group": group, "mode": mode,
            "content_sha256": content_sha256, "source": dict(source), "gate": GATES[kind], "after": sorted(after),
            "_sub": sub,
        }
        if deferred is not None:
            entry["deferred"] = deferred
        if hold is not None:
            entry["hold"] = hold
        if inputs:
            entry["inputs"] = dict(inputs)
        self.entries.append(entry)


def _owner(manifest: InstallManifest, who: str, *, group: bool = False) -> str:
    if who == "root":
        return "root"
    if who == "producer":
        return manifest.doc["producer"]["group" if group else "user"]
    return manifest.account_name(who)


def _consistency(manifest: InstallManifest, repo_root: Path) -> None:
    """The manifest restates values the staged artifacts and code already pin;
    any drift between them is refused here, before a plan exists."""

    m = manifest
    units = {name: dist_bytes(repo_root, name).decode("utf-8") for name in STATIC_UNITS}
    exec_unit = dist_bytes(repo_root, "pantheon-host-executor@.service").decode("utf-8")
    checks = [
        ("executables.broker", _unit_value(units["pantheon-host-broker.service"], "ExecStart")[0].split()[0], m.doc["executables"]["broker"]),
        ("executables.approval", _unit_value(units["pantheon-host-approval.service"], "ExecStart")[0].split()[0], m.doc["executables"]["approval"]),
        ("executables.ingress", _unit_value(units["pantheon-host-approval-telegram-ingress.service"], "ExecStart")[0].split()[0], m.doc["executables"]["ingress"]),
        ("executables.executor", _unit_value(exec_unit, "ExecStart")[0].split()[0], m.doc["executables"]["executor"]),
        ("files.ingress_config", _unit_value(units["pantheon-host-approval-telegram-ingress.service"], "ExecStart")[0].split()[-1], m.file("ingress_config")),
        ("accounts.broker", _unit_value(units["pantheon-host-broker.service"], "User")[0], m.account_name("broker")),
        ("accounts.approval", _unit_value(units["pantheon-host-approval.service"], "User")[0], m.account_name("approval")),
        ("accounts.ingress", _unit_value(units["pantheon-host-approval-telegram-ingress.service"], "User")[0], m.account_name("ingress")),
    ]
    from tools.harness_adapter.host_mcp_registration import (
        APPROVAL_ADAPTER_COMMAND, HOST_ADAPTER_COMMAND, ROOT_APPROVER_POLICY_PATH, ROOT_SEAT_REGISTRY_PATH)
    from tools.host_control.executor.config import DEFAULT_CONFIG_PATH
    checks += [
        ("files.executor_config", str(DEFAULT_CONFIG_PATH), m.file("executor_config")),
        ("files.seat_registry", str(ROOT_SEAT_REGISTRY_PATH), m.file("seat_registry")),
        ("files.approver_policy", str(ROOT_APPROVER_POLICY_PATH), m.file("approver_policy")),
        ("runtimes.host.launcher", HOST_ADAPTER_COMMAND, m.doc["runtimes"]["host"]["launcher"]),
        ("runtimes.approval.launcher", APPROVAL_ADAPTER_COMMAND, m.doc["runtimes"]["approval"]["launcher"]),
    ]
    template = json.loads(re.sub(r'"@[A-Z_]+@"', "0", dist_bytes(repo_root, "pantheon-host-approval-telegram-ingress.json.template").decode("utf-8")))
    checks += [
        ("refs.telegram_bot_token.credential_id", template["credential"]["credential_id"], m.doc["refs"]["telegram_bot_token"]["credential_id"]),
        ("paths.ingress_credentials", posixpath.dirname(template["credential"]["location"]), m.path("ingress_credentials")),
        ("paths.ingress_etc", posixpath.dirname(m.file("ingress_config")), m.path("ingress_etc")),
        ("paths.executor_etc", posixpath.dirname(m.file("executor_config")), m.path("executor_etc")),
        ("paths.broker_etc", posixpath.dirname(m.file("seat_registry")), m.path("broker_etc")),
        ("paths.approval_etc", posixpath.dirname(m.file("approver_policy")), m.path("approval_etc")),
    ]
    for where, pinned, given in checks:
        if pinned != given:
            raise InstallPlanError(f"{where}: manifest says {given!r} but the staged artifact/code pins {pinned!r}")
    if not m.path("framework_base").startswith(m.path("lib_root") + "/"):
        raise InstallPlanError("paths.framework_base: must sit under paths.lib_root")
    if not m.path("home_parent").startswith(m.path("var_lib_root") + "/"):
        raise InstallPlanError("paths.home_parent: must sit under paths.var_lib_root")
    for key in ("broker_etc", "approval_etc", "executor_etc", "ingress_etc"):
        if posixpath.dirname(m.path(key)) != m.path("etc_root"):
            raise InstallPlanError(f"paths.{key}: must be a direct child of paths.etc_root")
    if posixpath.dirname(m.path("ingress_credentials")) != m.path("ingress_etc"):
        raise InstallPlanError("paths.ingress_credentials: must be a direct child of paths.ingress_etc")
    for key in ("broker_plans", "broker_manifests"):
        if posixpath.dirname(m.path(key)) != m.path("broker_etc"):
            raise InstallPlanError(f"paths.{key}: must be a direct child of paths.broker_etc")
    for key in ("broker_config", "broker_policy"):
        if posixpath.dirname(m.file(key)) != m.path("broker_etc"):
            raise InstallPlanError(f"files.{key}: must sit in paths.broker_etc")
    if posixpath.dirname(m.file("approval_config")) != m.path("approval_etc"):
        raise InstallPlanError("files.approval_config: must sit in paths.approval_etc")


def _state_dir(unit_text: str) -> str:
    return "/var/lib/" + _unit_value(unit_text, "StateDirectory")[0]


def _instance_role_identities(repo_root: Path) -> tuple[set[str], set[int]]:
    """Every instance.yaml role identity (account name, uid): data, not persona names."""

    doc = yaml.safe_load((repo_root / "instance.yaml").read_text(encoding="utf-8"))
    roles = (doc or {}).get("roles") if isinstance(doc, dict) else None
    accounts: set[str] = set()
    uids: set[int] = set()
    for spec in (roles or {}).values():
        if not isinstance(spec, dict):
            continue
        if isinstance(spec.get("account"), str):
            accounts.add(spec["account"])
        uid = (spec.get("identity") or {}).get("uid") if isinstance(spec.get("identity"), dict) else None
        if isinstance(uid, int) and not isinstance(uid, bool):
            uids.add(uid)
    return accounts, uids


def _check_protocol_test_seats(m: InstallManifest, registry: SeatRegistryInput, repo_root: Path) -> None:
    ids = m.protocol_test_seat_ids
    if not ids:
        return
    accounts, uids = _instance_role_identities(repo_root)
    for seat_id in ids:
        seat = registry.seat(seat_id)  # a marker id absent from the registry is refused
        if seat.account in accounts or seat.uid in uids:
            raise InstallPlanError(
                f"plan_registration.protocol_test_seat_ids: seat {seat_id!r} account or uid is an instance.yaml role "
                "identity: a production seat is never a protocol-test seat (ORG-052 section 8, #1665)")


def build_plan(manifest: InstallManifest, registry: SeatRegistryInput, repo_root: Path = REPO_ROOT,
               *, tracked: frozenset[str] | None = None,
               closure: Closure | None = None,
               contents: dict[str, bytes] | None = None) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return (plan document, family render-staging files). ``tracked`` is the
    ``git ls-files -z`` list (MED-2); a caller may pass a closure derived from it.
    ``contents`` (optional, U3a) is filled with {entry id: pinned bytes} for every
    entry whose ``content_sha256`` the plan computed from bytes."""

    repo_root = Path(repo_root)
    m = manifest
    _consistency(m, repo_root)
    names = {m.account_name(r) for r in ACCOUNT_ROLES}
    uids = {m.account(r)["uid"] for r in ACCOUNT_ROLES}
    for seat in registry.seats:
        if seat.account in names or seat.uid in uids:
            raise InstallPlanError(f"seat {seat.seat_id}: account or uid collides with a service account")
    for seat_id in m.doc["approvers"]["seat_ids"]:
        registry.seat(seat_id)
    _check_protocol_test_seats(m, registry, repo_root)
    template_ids = sorted({t for rec in registry.manifests for t in rec["template_ids"]})
    if closure is None:
        if tracked is None:
            raise InstallPlanError("the tracked-file list is required (git ls-files -z output)")
        closure = derive_closure(repo_root, tracked=tracked, template_ids=template_ids)
    for rec in registry.manifests:
        want = framework_artifact_digest(repo_root, rec["template_ids"])
        if rec["framework_sha256"] != want:
            raise InstallPlanError(
                f"registry manifest {rec['revision']}: framework_sha256 is stale against the framework closure ({want})")
        if posixpath.dirname(rec["path"]) != m.path("broker_manifests"):
            raise InstallPlanError(f"registry manifest {rec['revision']}: path must sit in paths.broker_manifests")

    fw_base = m.path("framework_base")
    fw_root = posixpath.join(fw_base, closure.rev)
    b = _Builder([], {})
    tool = lambda action, **kw: {"route": "tool", "action": action, **kw}  # noqa: E731

    # P0 preconditions -------------------------------------------------------
    for package in sorted(m.doc["packages_verify"]):
        inputs: dict[str, Any] = {"package": package}
        if package == "python3-yaml":
            # the broker load path digests component contracts, which refuses an
            # unpinned PyYAML emitter: a host whose SYSTEM PyYAML is unpinned is
            # refused at VERIFY time (the host fact is unknowable at plan time)
            inputs.update(accepted_versions=sorted(PINNED_PYYAML_VERSIONS),
                          accepted_versions_source="tools.germinate.registry.contract_digest.PINNED_PYYAML_VERSIONS",
                          probe=[PYTHON3, "-I", "-c", "import yaml; print(yaml.__version__)"],
                          refuse_at_verify="the probed version is not in accepted_versions")
        b.add(id=f"package:{package}", phase="P0", kind="package_verify",
              source=tool("dpkg-verify-and-record", never="install"), inputs=inputs)
    b.add(id="precondition:mailbox-hub", phase="P0", kind="precondition", path=m.doc["mailbox_hub"],
          source=tool("verify-exists-no-symlink-component"),
          inputs={"why": "ReadWritePaths=@MAILBOX_HUB@: a missing hub fails the executor unit (T-2917 MR-2 carry-in 2); D33"})
    allowlist = _mcp_allowlist(repo_root)
    needed = ["pantheon-approval", "pantheon-host"]
    b.add(id="precondition:mcp-allowlist", phase="P0", kind="precondition", path=None,
          source=tool("verify-framework-copy-allowlist-rows"),
          inputs={"servers": needed, "observed": "met" if set(needed) <= set(allowlist) else "unmet",
                  "why": "F3: the _CLAUDE_MCP_ALLOWLIST rows (a FRAMEWORK_CODE_ARTIFACTS file) precede the framework copy; partner edit in mcp-adoption.md is orchestrator's"})
    approval_digest = _literal(repo_root, "tools/host_control/approval/journal.py", "SCHEMA_DIGEST")
    if approval_digest != APPROVAL_JOURNAL_SCHEMA_DIGEST_V1:
        raise InstallPlanError("approval journal SCHEMA_DIGEST differs from the v1 baseline (T-2920 never migrates, Q10)")
    broker_digest = _literal(repo_root, "tools/host_control/journal.py", "SCHEMA_DIGEST")
    b.add(id="journal-schema:approval", phase="P0", kind="journal_schema_check", source=tool("verify-schema-digest"),
          inputs={"module": "tools/host_control/approval/journal.py", "schema_digest": approval_digest,
                  "baseline": "v1 (T-2919 FLAG)"})
    b.add(id="journal-schema:broker", phase="P0", kind="journal_schema_check", source=tool("verify-schema-digest"),
          inputs={"module": "tools/host_control/journal.py", "schema_digest": broker_digest})

    # P2 accounts ------------------------------------------------------------
    paths = m.doc["paths"]

    def dir_entry(key: str, phase: str, after: Sequence[str] = (), source: Mapping[str, Any] | None = None) -> str:
        spec = paths[key]
        eid = f"dir:{spec['path']}"
        b.add(id=eid, phase=phase, kind="dir", path=spec["path"], owner=_owner(m, spec["owner"]),
              group=_owner(m, spec["group"], group=True), mode=spec["mode"], source=source or tool("install-d"),
              after=after)
        return eid

    dir_entry("var_lib_root", "P2")
    dir_entry("home_parent", "P2", [f"dir:{m.path('var_lib_root')}"])
    for role in ACCOUNT_ROLES:
        name = m.account_name(role)
        b.add(id=f"account:{name}", phase="P2", kind="account", path=m.home(role), owner=name, group=name,
              source={"route": "family", "family": "account.ensure", "params": {"account": name}},
              after=[f"dir:{m.path('home_parent')}"], hold="D3 P2: userdel orphans state once daemons write",
              inputs={"uid": m.account(role)["uid"], "gid": m.account(role)["uid"], "shell": m.doc["account_defaults"]["shell"],
                      "locked": True, "role": role,
                      "lock": "from the useradd default (no password is ever set, so the shadow password field is '!'); "
                              "U3 verify checks the shadow entry's password field is '!' or '*'"})

    # P4 framework copy ------------------------------------------------------
    dir_entry("lib_root", "P4")
    fw_dir = dir_entry("framework_base", "P4", [f"dir:{m.path('lib_root')}"])
    b.add(id=f"framework:{closure.rev}", phase="P4", kind="framework_tree", path=fw_root, owner="root", group="root",
          mode="0755", content_sha256=closure.digest, source=tool("materialize-closure"), after=[fw_dir, "precondition:mcp-allowlist"],
          inputs={"files": len(closure.files), "external_imports": sorted(closure.external_imports)})
    for rel in closure.files:
        b.add(id=f"framework-file:{rel}", phase="P4", kind="framework_file", path=posixpath.join(fw_root, rel),
              owner="root", group="root", mode="0644", content=(repo_root / rel).read_bytes(),
              source=tool("install-file"), after=[f"framework:{closure.rev}"])
    link = posixpath.join(fw_base, m.doc["framework"]["active_link"])
    b.add(id=f"framework-active:{link}", phase="P4", kind="framework_active_link", path=link, owner="root", group="root",
          content=closure.rev.encode("ascii"), source=tool("symlink-then-rename", target=closure.rev, keep_previous=1),
          after=[f"framework:{closure.rev}"],
          inputs={"note": "installer committed-rev pointer + rollback target; configs and launchers name the concrete rev (O_NOFOLLOW loader)"})

    # P4 runtimes: built AS ROOT by build_runtime.py run FROM the framework copy
    # (Recovery D15): the content sha covers build_runtime.py, its import closure, the
    # lock and both adapters' packaged modules; the wheelhouse is pinned by the
    # lock's hashes and recorded name+sha at apply.
    build_inputs = runtime_build_files(repo_root)
    missing = [rel for rel in build_inputs if rel not in closure.files]
    if missing:
        raise InstallPlanError(f"build_runtime inputs are not in the framework closure: {missing}")
    for key in RUNTIME_KEYS:
        spec = m.doc["runtimes"][key]
        inputs_digest = hashlib.sha256()
        for rel in build_inputs:
            inputs_digest.update(rel.encode() + b"\0" + (repo_root / rel).read_bytes() + b"\0")
        b.add(id=f"runtime:{key}", phase="P4", kind="runtime", path=spec["prefix"], owner="root", group="root",
              mode="0755", content_sha256=inputs_digest.hexdigest(),
              deferred="output tree sha recorded in the U3 receipt (verify_artifact.py --expect-owner 0)",
              source=tool("build_runtime", adapter=key, launcher=spec["launcher"], runs_from=f"{fw_root}/{BUILD_RUNTIME_REL}"),
              after=[f"account:{m.account_name('broker')}", f"framework:{closure.rev}"],
              inputs={"content_sha256_is": "build-input digest (framework-copy bytes)", "build_inputs": build_inputs,
                      "env": ["LC_ALL=C", "PATH=/usr/bin:/bin"], "wheelhouse": "lock-pinned; name+sha recorded at apply"})

    # P5 root configs, registries, refs -------------------------------------
    etc_dirs = []
    prev = dir_entry("etc_root", "P5")
    etc_dirs.append(prev)
    for key in ("broker_etc", "broker_plans", "broker_manifests", "approval_etc", "executor_etc", "ingress_etc",
                "ingress_credentials"):
        parent = posixpath.dirname(m.path(key))
        etc_dirs.append(dir_entry(key, "P5", [f"dir:{parent}"] + ([f"account:{m.account_name(paths[key]['owner'])}"]
                                                                    if paths[key]["owner"] != "root" else [])))
    for key in ("etc_root", "broker_etc", "broker_plans", "broker_manifests", "approval_etc", "executor_etc",
                "ingress_etc", "ingress_credentials"):
        spec = paths[key]
        b.add(id=f"owner-mode:{spec['path']}", phase="P5", kind="owner_mode", path=spec["path"],
              owner=_owner(m, spec["owner"]), group=_owner(m, spec["group"], group=True), mode=spec["mode"],
              source={"route": "family", "family": "owner_mode.set", "params": {"path": spec["path"]}},
              after=[f"dir:{spec['path']}"])
    launcher_modules = {
        "broker": ["-m", "tools.host_control"],
        "approval": ["-m", "tools.host_control.approval"],
        "ingress": ["-m", "tools.host_control.telegram_ingress", "--config", m.file("ingress_config")],
        "executor": ["-m", "tools.host_control.executor"],
    }
    for key in ("broker", "approval", "ingress", "executor"):
        path = m.doc["executables"][key]
        b.add(id=f"launcher:{path}", phase="P5", kind="launcher", path=path, owner="root", group="root", mode="0755",
              deferred="launcher bytes: render in U3 after U2 merges",
              source=tool("install-launcher", framework_root=fw_root), after=[f"framework:{closure.rev}"],
              inputs={"exec": [PYTHON3, "-I", *launcher_modules[key]], "cwd": fw_root,
                      "note": "runs from inside the concrete framework rev, never via 'active'"})
    executor_config = {
        "schema": EXECUTOR_CONFIG_SCHEMA,
        "registry_path": m.file("seat_registry"),
        "trusted_roots": [m.path("broker_etc"), fw_root],
        "broker_uid": m.account("broker")["uid"],
        "framework_root": fw_root,
        "mailbox_hub": m.doc["mailbox_hub"],
    }
    from tools.host_control.executor.config import parse_executor_config
    try:
        parse_executor_config(executor_config)
    except Exception as exc:  # RegistryError: refuse the plan, naming it
        raise InstallPlanError(f"executor config refused by its own parser: {exc}") from None
    b.add(id=f"config:{m.file('executor_config')}", phase="P5", kind="config", path=m.file("executor_config"),
          owner="root", group="root", mode="0644", content=(canonical(executor_config) + "\n").encode("ascii"),
          source=tool("install-file"), after=[f"dir:{m.path('executor_etc')}", f"framework:{closure.rev}"])
    monitor_uids = [m.account("monitor")["uid"]]
    u2_configs = {
        "broker_config": {
            "schema": "pantheon-host-broker-config/v1",
            "keys": ["schema", "trusted_roots", "registry_path", "policy_path", "framework_root", "journal_path",
                     "approval_uid", "monitor_uids", "host_operation_families", "plan_ttl_seconds",
                     "broker_revision", "compensate (optional, default false)"],
            "known_values": {"framework_root": fw_root, "registry_path": m.file("seat_registry"),
                             "policy_path": m.file("broker_policy"), "broker_revision": closure.rev,
                             "trusted_roots": [m.path("broker_etc"), fw_root],
                             "journal_path": _state_dir(dist_bytes(repo_root, "pantheon-host-broker.service").decode("utf-8"))
                             + "/broker.sqlite3",
                             "approval_uid": m.account("approval")["uid"], "monitor_uids": monitor_uids,
                             "compensate": False},
            "refs": {"policy_path": m.doc["files"]["broker_policy"]["ref"]},
            "apply_inputs": ["host_operation_families", "plan_ttl_seconds"],
            "notes": ["framework_root must be the concrete rev; 'active' is refused (U2)",
                      "monitor_uids must be exactly the dedicated pantheon-hostmonitor uid (U2 review LOW-3; U3 enforces)",
                      "compensate stays false for the first fire (HO Q4)"],
        },
        "approval_config": {
            "schema": "pantheon-host-approval-config/v1",
            "keys": ["schema", "trusted_roots", "approver_policy_path", "journal_path", "broker_uid", "monitor_uids",
                     "human_ingress{ingress_uid, human_subject, provider_user_id, provider_chat_id}"],
            "known_values": {"approver_policy_path": m.file("approver_policy"), "broker_uid": m.account("broker")["uid"],
                             "trusted_roots": [m.path("approval_etc")],
                             "journal_path": _state_dir(dist_bytes(repo_root, "pantheon-host-approval.service").decode("utf-8"))
                             + "/approval.sqlite3",
                             "monitor_uids": monitor_uids, "human_ingress.ingress_uid": m.account("ingress")["uid"]},
            "refs": {"human_ingress.human_subject": m.doc["refs"]["telegram_human"]["source_ref"],
                     "human_ingress.provider_user_id": m.doc["refs"]["telegram_human"]["source_ref"],
                     "human_ingress.provider_chat_id": m.doc["refs"]["telegram_human"]["source_ref"]},
            "notes": ["ids are decimal strings; uids must be nonzero (U2)",
                      "monitor_uids must be exactly the dedicated pantheon-hostmonitor uid (U2 review LOW-3; U3 enforces)"],
        },
    }
    for key in ("broker_config", "approval_config"):
        path = m.file(key)
        b.add(id=f"config:{path}", phase="P5", kind="config", path=path, owner="root", group="root", mode="0644",
              deferred="U2 config schema: render in U3 after U2 merges", source=tool("install-file"),
              after=[f"dir:{posixpath.dirname(path)}", f"framework:{closure.rev}"],
              inputs=u2_configs[key])
    ingress_template = dist_bytes(repo_root, "pantheon-host-approval-telegram-ingress.json.template")
    ingress_values = {"INGRESS_UID": m.account("ingress")["uid"], "APPROVAL_UID": m.account("approval")["uid"],
                      "INGRESS_GID": m.account("ingress")["uid"]}
    partial = render_template("pantheon-host-approval-telegram-ingress.json.template", ingress_template.decode("utf-8"),
                              ingress_values, allow_deferred=True)
    b.add(id=f"config:{m.file('ingress_config')}", phase="P5", kind="config", path=m.file("ingress_config"),
          owner="root", group=m.account_name("ingress"), mode="0640",
          deferred="operator-only allowlist values (operator-batch:2) rendered at apply (Q-F)",
          source=tool("render-at-apply", template=f"{DIST_REL}/pantheon-host-approval-telegram-ingress.json.template"),
          after=[f"dir:{m.path('ingress_etc')}", f"account:{m.account_name('ingress')}"],
          inputs={"template_sha256": sha256_bytes(ingress_template), "partial_render_sha256": sha256_bytes(partial.encode("utf-8")),
                  "resolved": {k: ingress_values[k] for k in sorted(ingress_values)},
                  "deferred_vars": sorted(DEFERRED_VARS), "ref": m.doc["refs"]["telegram_human"]["source_ref"]})
    registry_bytes = (canonical(registry.doc) + "\n").encode("ascii")
    b.add(id=f"registry:{m.file('seat_registry')}", phase="P5", kind="registry", path=m.file("seat_registry"),
          owner="root", group="root", mode="0644", content=registry_bytes, source=tool("install-file"),
          after=[f"dir:{m.path('broker_etc')}"], inputs={"stage": "pre-registration (plans absent; production seats register none, #1665)"})
    b.add(id=f"registry:{m.file('approver_policy')}", phase="P5", kind="registry", path=m.file("approver_policy"),
          owner="root", group="root", mode="0644", deferred=f"approver identities and policy are operator-only ({m.doc['files']['approver_policy']['ref']})",
          source=tool("install-file", restart_after="pantheon-host-approval.service"),
          after=[f"dir:{m.path('approval_etc')}"],
          inputs={"approver_seat_ids": sorted(m.doc["approvers"]["seat_ids"]), "schema": "pantheon-approval-policy/v1",
                  "note": "no hot reload: every approver-policy install restarts HostApproval1"})
    b.add(id=f"registry:{m.file('broker_policy')}", phase="P5", kind="registry", path=m.file("broker_policy"),
          owner="root", group="root", mode="0644",
          deferred=f"broker host policy grants are operator-only ({m.doc['files']['broker_policy']['ref']})",
          source=tool("install-file", restart_after="pantheon-host-broker.service"),
          after=[f"dir:{m.path('broker_etc')}"],
          inputs={"schema": "pantheon-host-policy/v1", "loader": "tools.host_control.policy.HostPolicy.load",
                  "note": "the broker config's policy_path; HostPolicy is loaded at broker start (no hot reload)"})
    for rec in registry.manifests:
        b.add(id=f"registered-manifest:{rec['revision']}", phase="P5", kind="registered_manifest", path=rec["path"],
              owner="root", group="root", mode="0644", content_sha256=rec["sha256"],
              source=tool("install-registered-manifest-bytes"), after=[f"dir:{m.path('broker_manifests')}"])
    token_path = posixpath.join(m.path("ingress_credentials"), "bot-token")
    b.add(id=f"credential-ref:{m.doc['refs']['telegram_bot_token']['credential_id']}", phase="P5",
          kind="credential_ref_registration", path=token_path, source=tool("register-credential-ref"),
          after=[f"dir:{m.path('ingress_credentials')}"],
          inputs={"credential_id": m.doc["refs"]["telegram_bot_token"]["credential_id"], "location": token_path,
                  "owner_uid": m.account("ingress")["uid"], "ticket": "T-2919"})

    # P6 plan registration (T-5533 a-d) --------------------------------------
    # Only seats in plan_registration.protocol_test_seat_ids are registered with the
    # broker (ORG-052 section 8 clarification, #1665; seat installation is the
    # germination engine's). Zero such seats: no P6 entries at all.
    staging = m.path("producer_staging")
    producer = m.doc["producer"]
    reg_ids = []
    test_ids = set(m.protocol_test_seat_ids)
    for rec in registry.manifests:
        for seat in registry.seats:
            if seat.seat_id not in test_ids:
                continue
            out = posixpath.join(staging, f"{seat.seat_id}-{rec['revision']}.json")
            dest = posixpath.join(m.path("broker_plans"), f"{seat.seat_id}-{rec['revision']}.json")
            argv = [SETPRIV, f"--reuid={producer['user']}", f"--regid={producer['group']}", "--clear-groups",
                    *SETPRIV_HARDENING, "--", ENV, "-i",
                    "PATH=/usr/bin:/bin", PYTHON3, "-I", posixpath.join(fw_root, "tools/host_plan_producer.py"),
                    "--framework-root", fw_root, "--seat-record", canonical(seat.record()),
                    "--manifest-record", canonical(dict(rec)), "--out", out]
            eid = f"plan-registration:{seat.seat_id}:{rec['revision']}"
            reg_ids.append(eid)
            b.add(id=eid, phase="P6", kind="plan_registration", path=dest, owner="root", group="root", mode="0644",
                  deferred="host_plan_producer output; sha256 recorded at registration and appended to the registry plans list",
                  source=tool("produce-then-install", producer_argv=argv, producer_cwd=fw_root),
                  after=[f"framework:{closure.rev}", f"registered-manifest:{rec['revision']}", f"dir:{staging}",
                         f"registry:{m.file('seat_registry')}"],
                  inputs={"producer_writes": out, "expected_writer": f"{producer['user']}:{producer['group']}",
                          "protocol_test_seat": True, "root_side_checks": list(PRODUCER_ROOT_CHECKS)})
    # the units wait on the final registry state: registry-post, or the pre-registration entry when none
    registry_final = f"registry-post:{m.file('seat_registry')}" if reg_ids else f"registry:{m.file('seat_registry')}"
    if reg_ids:  # no protocol-test seat: no producer dirs, no registry-post, the registry keeps no plans key
        dir_entry("producer_run_root", "P6")
        dir_entry("producer_staging", "P6", [f"dir:{m.path('producer_run_root')}"],
                  source=tool("install-d-fresh", must_not_exist=True, remove_after_registration=True,
                              why="host_plan_producer opens --out O_EXCL as the producer user; it has no stdout or fd "
                                  "output mode, and its source stays unchanged"))
        b.add(id=f"registry-post:{m.file('seat_registry')}", phase="P6", kind="registry_post_registration",
              path=m.file("seat_registry"), owner="root", group="root", mode="0644",
              deferred="pre-registration bytes plus the printed {seat_id, manifest_revision, path, sha256} records",
              source=tool("append-plan-records"), after=reg_ids)

    # P7 bus policies, units, activation --------------------------------------
    def family_file(kind: str, family: str, dest_dir: str, basename: str, content: bytes, stage_dir: str,
                    after: Sequence[str], extra: Mapping[str, Any] | None = None) -> None:
        staging_rel = f"{stage_dir}/{basename}"
        sha = sha256_bytes(content)
        param_key = "unit" if family == "unit.system.install" else "policy"
        params: dict[str, Any] = {param_key: basename, "sha256": sha}
        if family == "unit.system.install":
            params["state"] = "present"
        b.add(id=f"{kind}:{basename}", phase="P7", kind=kind, path=dest_dir + basename, owner="root", group="root",
              mode="0644", content=content, source={"route": "family", "family": family, "params": params,
                                                     "staging": staging_rel}, after=after, inputs=extra,
              sub=1 if extra and "seat_id" in extra else 0)

    for name in STATIC_POLICIES:
        family_file("dbus_policy", "dbus_policy.install", DBUS_DIR, name, dist_bytes(repo_root, name), "dbus",
                    [f"account:{m.account_name(r)}" for r in ACCOUNT_ROLES])
    seat_tpl = dist_bytes(repo_root, "pantheon-host-broker-seat.conf.template").decode("utf-8")
    exec_tpl = dist_bytes(repo_root, "pantheon-host-executor-seat.conf.template").decode("utf-8")
    unit_tpl = dist_bytes(repo_root, "pantheon-host-executor@.service").decode("utf-8")
    act_tpl = dist_bytes(repo_root, "org.pantheon.HostExecutor1.service.template").decode("utf-8")
    appr_tpl = dist_bytes(repo_root, "pantheon-host-approval-approver.conf.template").decode("utf-8")
    for seat in registry.seats:
        values = {"SEAT_ACCOUNT": seat.account, "SEAT_UID": seat.uid, "SEAT_ID": seat.seat_id,
                  "SEAT_ROOT": seat.seat_root, "MAILBOX_HUB": m.doc["mailbox_hub"]}
        family_file("dbus_policy", "dbus_policy.install", DBUS_DIR, f"org.pantheon.HostBroker1.seat-{seat.seat_id}.conf",
                    render_template("pantheon-host-broker-seat.conf.template", seat_tpl,
                                    {"SEAT_ACCOUNT": seat.account}).encode("utf-8"), "dbus",
                    ["dbus_policy:org.pantheon.HostBroker1.conf"], {"seat_id": seat.seat_id})
        family_file("dbus_policy", "dbus_policy.install", DBUS_DIR, f"org.pantheon.HostExecutor1.seat-{seat.seat_id}.conf",
                    render_template("pantheon-host-executor-seat.conf.template", exec_tpl,
                                    {"SEAT_ACCOUNT": seat.account, "SEAT_UID": seat.uid}).encode("utf-8"), "dbus",
                    ["dbus_policy:org.pantheon.HostExecutor1.conf"], {"seat_id": seat.seat_id})
        unit_name = f"pantheon-host-executor@{seat.seat_id}.service"
        family_file("unit", "unit.system.install", UNIT_DIR, unit_name,
                    render_template("pantheon-host-executor@.service", unit_tpl,
                                    {k: values[k] for k in ("SEAT_ACCOUNT", "SEAT_UID", "SEAT_ROOT", "MAILBOX_HUB")}).encode("utf-8"),
                    "units", ["precondition:mailbox-hub", f"config:{m.file('executor_config')}",
                              f"launcher:{m.doc['executables']['executor']}",
                              f"dbus_policy:org.pantheon.HostExecutor1.seat-{seat.seat_id}.conf"],
                    {"seat_id": seat.seat_id, "note": "no [Install]: bus activation is the only start path"})
        act_name = f"org.pantheon.HostExecutor1.U{seat.uid}.service"
        content = render_template("org.pantheon.HostExecutor1.service.template", act_tpl,
                                  {"SEAT_UID": seat.uid, "SEAT_ACCOUNT": seat.account, "SEAT_ID": seat.seat_id}).encode("utf-8")
        b.add(id=f"activation:{act_name}", phase="P7", kind="activation", path=ACTIVATION_DIR + act_name, owner="root",
              group="root", mode="0644", content=content,
              source=tool("install-file", why="DBUS_DIR (families.py) does not cover /usr/share/dbus-1/system-services; mandatory, the executor unit has no [Install]"),
              after=[f"unit:{unit_name}"], inputs={"seat_id": seat.seat_id})
    for seat_id in sorted(m.doc["approvers"]["seat_ids"]):
        seat = registry.seat(seat_id)
        family_file("dbus_policy", "dbus_policy.install", DBUS_DIR, f"org.pantheon.HostApproval1.approver-{seat_id}.conf",
                    render_template("pantheon-host-approval-approver.conf.template", appr_tpl,
                                    {"APPROVER_ACCOUNT": seat.account}).encode("utf-8"), "dbus",
                    ["dbus_policy:org.pantheon.HostApproval1.conf", f"registry:{m.file('approver_policy')}"],
                    {"seat_id": seat_id})
    unit_after = {
        "pantheon-host-broker.service": ["broker_config", "broker", "broker_policy"],
        "pantheon-host-approval.service": ["approval_config", "approval"],
        "pantheon-host-approval-telegram-ingress.service": ["ingress_config", "ingress"],
    }
    for name in STATIC_UNITS:
        cfg, exe, *policy = unit_after[name]
        family_file("unit", "unit.system.install", UNIT_DIR, name, dist_bytes(repo_root, name), "units",
                    [f"config:{m.file(cfg)}", f"launcher:{m.doc['executables'][exe]}",
                     *(f"registry:{m.file(k)}" for k in policy)])

    # P8 the token file (the operator input) ---------------------------------------
    b.add(id=f"credential:{token_path}", phase="P8", kind="credential", path=token_path,
          owner=m.account_name("ingress"), group=m.account_name("ingress"), mode="0600",
          deferred=f"a NEW dedicated BotFather bot token ({m.doc['refs']['telegram_bot_token']['source_ref']}); never a persona token, never inline",
          source=tool("place-credential-file"), hold="D3 P8: secret placement",
          after=[f"owner-mode:{m.path('ingress_credentials')}",
                 f"credential-ref:{m.doc['refs']['telegram_bot_token']['credential_id']}"])

    # P9 enables and state roots ------------------------------------------------
    order = {"pantheon-host-approval.service": [], "pantheon-host-broker.service": ["unit_enable:pantheon-host-approval.service"],
             "pantheon-host-approval-telegram-ingress.service": ["unit_enable:pantheon-host-broker.service", f"credential:{token_path}"]}
    for sub, name in enumerate(("pantheon-host-approval.service", "pantheon-host-broker.service",
                                "pantheon-host-approval-telegram-ingress.service")):
        b.add(id=f"unit_enable:{name}", phase="P9", kind="unit_enable", path=UNIT_DIR + name,
              source={"route": "family", "family": "unit.system.enable", "params": {"unit": name, "state": "enabled"}},
              after=[f"unit:{name}", registry_final, *order[name]],
              hold="D3 P9: ingress start is outward-facing" if "ingress" in name else None, sub=sub)
        text = dist_bytes(repo_root, name).decode("utf-8")
        state = _state_dir(text)
        owner = _unit_value(text, "User")[0]
        b.add(id=f"journal-root:{state}", phase="P9", kind="journal_root", path=state, owner=owner, group=owner,
              mode=_unit_value(text, "StateDirectoryMode")[0],
              source=tool("verify-after-start", created_by="systemd StateDirectory=", backup_before_rerun=True),
              after=[f"unit_enable:{name}"], inputs={"q10": "back up before any rerun or rollback; never migrate (T-2922)"})

    # order: phase, kind rank, an explicit sub-rank (static before rendered,
    # approval before broker before ingress), then id
    b.entries.sort(key=lambda e: (PHASES.index(e["phase"]), KIND_RANK[e["kind"]], e["_sub"], e["id"]))
    for entry in b.entries:
        del entry["_sub"]
    ids = [e["id"] for e in b.entries]
    if len(set(ids)) != len(ids):
        raise InstallPlanError("duplicate plan entry ids")
    position = {eid: i for i, eid in enumerate(ids)}
    for i, entry in enumerate(b.entries):
        entry["seq"] = i
        for dep in entry["after"]:
            if dep not in position:
                raise InstallPlanError(f"{entry['id']}: depends on unknown entry {dep!r}")
            if position[dep] >= i:
                raise InstallPlanError(f"{entry['id']}: ordered before its dependency {dep!r}")

    fragment = substrate_manifest_fragment(m, b.entries)
    doc: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "inputs": {"manifest_sha256": m.sha256, "manifest_revision": m.revision, "registry_sha256": registry.sha256,
                   "dist_sha256": _dist_digest(repo_root)},
        "framework": {"rev": closure.rev, "closure_sha256": closure.digest, "root": fw_root,
                      "active_link": link, "files": len(closure.files)},
        "substrate_fragment_sha256": sha256_bytes(canonical(fragment).encode("ascii")),
        "entries": b.entries,
        "exclusions": [dict(e) for e in EXCLUSIONS],
        "none": [dict(n) for n in NONE_LINES],
        "residuals": [dict(r) for r in RESIDUALS],
        # host facts only verify can see: U3 verify refuses when any of these fails
        "verify_time_refusals": sorted(e["id"] for e in b.entries if (e.get("inputs") or {}).get("refuse_at_verify")),
        "blocking_preconditions": sorted(
            e["id"] for e in b.entries
            if e["kind"] == "precondition" and (e.get("inputs") or {}).get("observed") == "unmet"),
        "live_actuation_enabled": LIVE_ACTUATION_ENABLED,
    }
    doc["plan_sha256"] = sha256_bytes(canonical(doc).encode("ascii"))
    if contents is not None:
        contents.update(b.contents)
    return doc, dict(sorted(b.staging.items()))


def _dist_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((repo_root / DIST_REL).iterdir()):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def plan_bytes(doc: Mapping[str, Any]) -> bytes:
    return (canonical(doc) + "\n").encode("ascii")


def verify_plan_digest(doc: Mapping[str, Any]) -> bool:
    body = {k: v for k, v in doc.items() if k != "plan_sha256"}
    return doc.get("plan_sha256") == sha256_bytes(canonical(body).encode("ascii"))


# ---------------------------------------------------------------------------
# 3. the render-staging seam: a host-substrate manifest fragment
# ---------------------------------------------------------------------------


def substrate_manifest_fragment(manifest: InstallManifest, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A ``pantheon-host-substrate-manifest/v0`` fragment for every family-routed
    entry; unit/policy ``source`` fields are RENDER-STAGING-relative, so
    ``families.load_manifest(fragment, repo_root=<staging dir>)`` pins them."""

    m = manifest
    accounts = {}
    for role in ACCOUNT_ROLES:
        accounts[m.account_name(role)] = {"uid": m.account(role)["uid"], "home": m.home(role),
                                          "shell": m.doc["account_defaults"]["shell"]}
    paths = {}
    units = {}
    policies = {}
    for e in entries:
        src = e["source"]
        if src.get("route") != "family":
            continue
        if src["family"] == "owner_mode.set":
            paths[e["path"]] = {"owner": e["owner"], "group": e["group"], "mode": e["mode"]}
        elif src["family"] == "unit.system.install":
            units[src["params"]["unit"]] = {"path": e["path"], "source": src["staging"], "sha256": e["content_sha256"],
                                            "enable": src["params"]["unit"] in STATIC_UNITS}
        elif src["family"] == "dbus_policy.install":
            policies[src["params"]["policy"]] = {"path": e["path"], "source": src["staging"], "sha256": e["content_sha256"]}
    return {
        "schema": F.MANIFEST_SCHEMA,
        "status": "PROVISIONAL",
        "note": f"rendered by tools/host_broker_install.py from {MANIFEST_SCHEMA} revision {m.revision}",
        "groups": {},
        "accounts": accounts,
        "installer_roots": [m.path("etc_root")],
        "paths": paths,
        "mailbox_dirs": {},
        "units": units,
        "dbus_policies": policies,
        "sudoers_templates": {},
        "legacy_requirements": [],
    }


def write_staging(staging: Mapping[str, bytes], dest: Path) -> None:
    for rel, data in staging.items():
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


# ---------------------------------------------------------------------------
# 5. the name table
# ---------------------------------------------------------------------------

NAME_SCAN: Final = (
    "tools/host_control", "tools/harness_adapter/host_mcp_registration.py",
    "tools/host_mcp_adapter/dist/build_runtime.py", "tools/approval_mcp_adapter/__init__.py",
    "tools/harness_adapter/golden",
)


def _scan_files(repo_root: Path) -> list[tuple[str, str]]:
    out = []
    for rel in NAME_SCAN:
        base = repo_root / rel
        candidates = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in candidates:
            r = path.relative_to(repo_root).as_posix()
            if "__pycache__" in r or path.name.startswith("test_") or "/tests/" in r:
                continue
            if path.suffix not in (".py", ".md", ".conf", ".service", ".template", ".json", ".toml"):
                continue
            out.append((r, path.read_text(encoding="utf-8", errors="replace")))
    return out


def _used_in(files: list[tuple[str, str]], needle: str) -> list[str]:
    # whole-name match: "pantheon-hostapproval" must not match "pantheon-hostapproval-ingress"
    pattern = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(needle) + r"(?![A-Za-z0-9_-])")
    return sorted(r for r, text in files if pattern.search(text))


def name_rows(manifest: InstallManifest, repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    m = manifest
    files = _scan_files(repo_root)
    rows: list[dict[str, Any]] = []

    def row(kind: str, item: str, provisional: str, final: str, minted: str, migration: str, *, needle: str | None = None,
            status: str = "PROVISIONAL") -> None:
        rows.append({"kind": kind, "item": item, "provisional": provisional, "proposed_final": final,
                     "status": status, "used_in": _used_in(files, needle or provisional), "minted_in": minted,
                     "rename_later_is_migration": migration})

    acct_minted = {"broker": "T-2912", "approval": "T-2913 (name from the T-2912 broker policy)",
                   "ingress": "T-2913", "monitor": "T-2943 / T-2914"}
    for role in ACCOUNT_ROLES:
        name = m.account_name(role)
        row("account", f"{role} service account", name, name, acct_minted[role],
            "yes: unit User=, bus-policy user=, file and journal ownership; usermod -l plus a reviewed reinstall")
    row("account", "healthsig service account (EXCLUDED, Q11)", "pantheon-healthsig", "out of scope (ORG-050 installer)",
        "T-3652", "n/a here", status="EXCLUDED")
    for role in ACCOUNT_ROLES:
        uid = str(m.account(role)["uid"])
        row("uid", f"{role} uid (gid == uid, user-private group)", uid, uid, "T-2920 U1",
            "yes: chown of homes, configs and journal roots. NOTE: families account.ensure requires uid >= 1000, "
            "so these service accounts sit in the normal-user range (operator-visible); collision with live accounts is U3 preflight",
            needle="\x00never\x00")
    row("group", "groups", "user-private only (gid == uid)", "user-private only", "T-2920 U1",
        "yes, together with the account", needle="\x00never\x00")
    for role in ACCOUNT_ROLES:
        home = m.home(role)
        row("path", f"{role} home (useradd --create-home)", home, home, "T-2920 U1 (Q-B)",
            "light: usermod -d; holds no state (skel only), separate from every StateDirectory")
    row("account", "plan-producer user (T-5533 step a)", "nobody:nogroup via setpriv", "nobody:nogroup via setpriv",
        "T-2920 U1 (Q-C)", "no: holds no state, no new account or grant", needle="\x00never\x00")
    row("bus_name", "broker bus name", "org.pantheon.HostBroker1", "org.pantheon.HostBroker1", "ORG-052 section 4",
        "yes: adapters, bus policy, health monitor", status="ADR-NAMED")
    row("bus_name", "approval bus name", "org.pantheon.HostApproval1", "org.pantheon.HostApproval1", "ORG-052 section 5",
        "yes: adapters, broker, ingress, bus policy", status="ADR-NAMED")
    row("bus_name", "Telegram ingress bus name", "org.pantheon.HostApprovalTelegram1", "org.pantheon.HostApprovalTelegram1",
        "T-2919 (ORG-052 section 5 table)", "yes: approval service and bus policy")
    row("bus_name", "per-seat executor bus name", "org.pantheon.HostExecutor1.U<uid>", "org.pantheon.HostExecutor1.U<uid>",
        "T-2917 5b (ruling i5)", "yes: broker, activation files, per-seat policy", needle="org.pantheon.HostExecutor1.U")
    for key, label, minted in (("broker", "broker entrypoint", "T-2912"), ("approval", "approval entrypoint", "T-2913"),
                               ("ingress", "ingress entrypoint", "T-2919"), ("executor", "executor entrypoint", "T-2917 5b (B5)")):
        path = m.doc["executables"][key]
        row("path", label, path, path, minted, "code + reinstall: unit ExecStart; no data")
    for key, label in (("host", "host MCP adapter"), ("approval", "approval MCP adapter")):
        spec = m.doc["runtimes"][key]
        row("path", f"{label} launcher", spec["launcher"], spec["launcher"], "T-2915 / T-2916",
            "code + re-registration: harness MCP config of every registered seat")
        row("path", f"{label} runtime prefix", spec["prefix"], spec["prefix"], "T-2915 / T-2916", "code + reinstall; no data",
            needle=spec["prefix"].lstrip("/"))
    for key, label, minted in (
            ("executor_config", "executor config", "T-2917 5b (B5)"), ("ingress_config", "ingress config", "T-2919"),
            ("seat_registry", "seat registry", "T-2916 (host_mcp_registration)"),
            ("approver_policy", "approver policy", "T-2916 (host_mcp_registration)"),
            ("broker_policy", "broker host policy (pantheon-host-policy/v1 grants)", "T-2920 U3a"),
            ("broker_config", "broker config (U2 schema)", "T-2920 U1"), ("approval_config", "approval config (U2 schema)", "T-2920 U1")):
        path = m.file(key)
        row("path", label, path, path, minted, "code + reinstall: root-owned file rewritten by a new plan; no data")
    token = posixpath.join(m.path("ingress_credentials"), "bot-token")
    row("path", "Telegram bot-token file (0600, parent 0700)", token, token, "T-2919", "re-place the credential + CredentialRef")
    path_rows = [("broker_plans", "registered plans dir"), ("broker_manifests", "registered manifests dir")]
    if m.protocol_test_seat_ids:  # producer dirs exist in the plan only with a protocol-test seat
        path_rows += [("producer_run_root", "producer run root (root:root 0755)"),
                      ("producer_staging", "producer staging dir (fresh per run, nobody:nogroup 0700)")]
    for key, label in path_rows:
        row("path", label, m.path(key), m.path(key), "T-2920 U1", "no: regenerated per framework update")
    for name, minted in zip(STATIC_UNITS, ("T-2912", "T-2913", "T-2919")):
        state = _state_dir(dist_bytes(repo_root, name).decode("utf-8"))
        row("path", f"state/journal root of {name}", state, state, minted,
            "YES: journal data lives here; Q10 backs up, never migrates", needle=state.rsplit("/", 1)[1])
    row("path", "framework tree", m.path("framework_base") + "/<rev>", m.path("framework_base") + "/<rev>", "T-2920 U1 (Q6)",
        "no: versioned and content-addressed; a new rev is a new plan", needle="/usr/local/lib/pantheon")
    row("path", "framework committed-rev pointer", m.path("framework_base") + "/active", m.path("framework_base") + "/active",
        "T-2920 U1 (Q6)", "no", needle="\x00never\x00")
    row("file_name", "per-seat broker policy fragment", "/etc/dbus-1/system.d/org.pantheon.HostBroker1.seat-<seat_id>.conf",
        "same", "T-2920 U1", "no: re-rendered per plan", needle="\x00never\x00")
    row("file_name", "per-seat executor policy fragment", "/etc/dbus-1/system.d/org.pantheon.HostExecutor1.seat-<seat_id>.conf",
        "same", "T-2920 U1", "no: re-rendered per plan", needle="\x00never\x00")
    row("file_name", "per-approver policy fragment", "/etc/dbus-1/system.d/org.pantheon.HostApproval1.approver-<seat_id>.conf",
        "same", "T-2920 U1", "no: re-rendered per plan", needle="\x00never\x00")
    row("file_name", "per-seat executor unit", "/etc/systemd/system/pantheon-host-executor@<seat_id>.service", "same",
        "T-2917 5b", "no: re-rendered per plan", needle="pantheon-host-executor@")
    row("file_name", "per-seat activation file", "/usr/share/dbus-1/system-services/org.pantheon.HostExecutor1.U<uid>.service",
        "same", "T-2917 5b", "no: re-rendered per plan", needle="system-services")
    for schema, label, minted in (
            ("pantheon-host-executor-config/v1", "executor config schema", "T-2917 5b (B5)"),
            ("pantheon-telegram-ingress-config/v1", "ingress config schema", "T-2919"),
            ("pantheon-host-broker-config/v1", "broker config schema", "T-2920 U2 (#1367)"),
            ("pantheon-host-approval-config/v1", "approval config schema", "T-2920 U2 (#1367)"),
            (MANIFEST_SCHEMA, "install manifest schema", "T-2920 U1"), (PLAN_SCHEMA, "install plan schema", "T-2920 U1")):
        row("schema", label, schema, schema, minted, "yes for readers: a schema bump is a versioned migration")
    return rows


def names_document(manifest: InstallManifest, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    return {"schema": NAMES_SCHEMA, "manifest_revision": manifest.revision, "generator": "tools/host_broker_install.py names",
            "rows": name_rows(manifest, repo_root),
            "out_of_scope": "the ORG-052 wire vocabulary (error names, journal events, evidence labels; ADR §Provisional wire names) "
                            "is not install surface; its canonical table is tools/host_control/README.md "
                            "'Provisional names pending ratification'"}


def names_markdown(doc: Mapping[str, Any]) -> str:
    lines = [
        "# ORG-052 host-broker install: final name table (for ratification)",
        "",
        f"GENERATED by `{doc['generator']}` from `tools/host_broker_install.manifest.example.json` "
        f"(revision `{doc['manifest_revision']}`). Do not hand-edit; regenerate and commit.",
        "",
        "Every account, group, bus name and path the T-2920 install leg creates or names. ORG-052 §Ratification surface (item 12) and "
        "§Provisional wire names keep these provisional: ratification authorizes implementation, not enablement or "
        "specific grants. The proposed final value equals the provisional one unless the row says otherwise. "
        "\"Rename later is a migration\" says what renaming after the first install costs.",
        "",
        "| Kind | Item | Provisional | Proposed final | Status | Minted in | Rename later is a migration | Used in |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in doc["rows"]:
        used = "<br>".join(f"`{u}`" for u in r["used_in"]) or "(new in T-2920)"
        lines.append(f"| {r['kind']} | {r['item']} | `{r['provisional']}` | `{r['proposed_final']}` | {r['status']} | "
                     f"{r['minted_in']} | {r['rename_later_is_migration']} | {used} |")
    lines += ["", f"Out of scope: {doc['out_of_scope']}.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. the dark CLI
# ---------------------------------------------------------------------------


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.fchmod(descriptor, 0o644)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan", help="print or write the sha-pinned install plan (no host mutation)")
    plan_p.add_argument("--manifest", required=True, type=Path)
    plan_p.add_argument("--registry", required=True, type=Path)
    plan_p.add_argument("--tracked-files", required=True, type=Path,
                        help="the output of `git ls-files -z` for this checkout (the closure is intersected with it)")
    plan_p.add_argument("--out", type=Path, help="write the plan here (must not exist) instead of stdout")
    names_p = sub.add_parser("names", help="regenerate (or --check) the name table")
    names_p.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            manifest = parse_manifest(args.manifest.read_bytes())
            registry = parse_registry(args.registry.read_bytes())
            tracked = parse_tracked_files(args.tracked_files.read_bytes())
            doc, _staging = build_plan(manifest, registry, tracked=tracked)
            data = plan_bytes(doc)
            if args.out is not None:
                _write_exclusive(args.out, data)
                print(canonical({"plan_sha256": doc["plan_sha256"], "path": str(args.out)}))
            else:
                sys.stdout.write(data.decode("ascii"))
            return 0
        manifest = parse_manifest(EXAMPLE_MANIFEST.read_bytes())
        doc = names_document(manifest)
        json_bytes = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        md_bytes = names_markdown(doc).encode("utf-8")
        if args.check:
            stale = [p.name for p, want in ((NAMES_JSON, json_bytes), (NAMES_MD, md_bytes))
                     if not p.is_file() or p.read_bytes() != want]
            if stale:
                print(f"host_broker_install: stale name table {stale}; run `python3 tools/host_broker_install.py names`",
                      file=sys.stderr)
                return 1
            return 0
        NAMES_JSON.write_bytes(json_bytes)
        NAMES_MD.write_bytes(md_bytes)
        return 0
    except (InstallPlanError, OSError) as exc:
        print(f"host_broker_install: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
