"""Role-gated harness registration for the packaged host and approver MCP
adapters (ORG-052 §7, Unit 4b, T-2916).

Pure generation code at the ORG-031 harness-adapter boundary. It turns one
logical, closed registration per adapter into Codex ``config.toml`` and
Claude Code MCP/permission fragments. It writes nothing and registers
nothing live: applying a fragment to a seat is the head orchestrator's
enable leg.

**Who gets what.** Every registered seat gets the host adapter
(``pantheon-host``, seven tools). A seat gets the approver adapter
(``pantheon-approval``, four tools) iff ``is_policy_approver``: the SAME
predicate the HostApproval1 Reviewer gate runs server-side
(``ApproverResolver.subject_for_uid`` over root-owned approver policy),
applied to the seat's uid from the root-owned seat registry. Presentation
therefore cannot drift from authority. It stays presentation: removing a
registration is not revocation, and a non-approver that launches the
binary is still denied server-side (ORG-052 §5/§7).

**What a seat config can never contain** (ORG-052 §7): a broker token, a
D-Bus address override, a Unix uid, bus policy, or an executor path.
``StdioMcpRegistration`` has no field that could carry any of them, so
they are inexpressible; ``assert_registration_clean`` independently scans
every rendered document (key allowlist, packaged-command allowlist, value
denylist, integer placement and uid check) and every renderer runs it
before returning, failing closed.

**Root inputs.** The approver policy and seat registry are read only by
``load_root_inputs`` (root-ownership validated by ``RootOwnedFileLoader``);
the paths below are PROVISIONAL until T-2920 fixes the install layout and
are never opened by the test suite.

``SUPPORT_MATRIX`` records, per harness behaviour, whether it is proven
in-repo (naming the test) or unsupported until real-client / real-host
evidence exists (ORG-052 gate 8). Documentation is never evidence there.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

from tools.approval_mcp_adapter import contract as approval_contract
from tools.host_mcp_adapter import contract as host_contract

# --------------------------------------------------------------------
# packaged adapters (launcher paths mirror tools/host_mcp_adapter/dist/
# build_runtime.py profiles; parity-tested)
# --------------------------------------------------------------------

HOST_SERVER_NAME: Final[str] = "pantheon-host"
APPROVAL_SERVER_NAME: Final[str] = "pantheon-approval"
HOST_ADAPTER_COMMAND: Final[str] = "/usr/local/libexec/pantheon-host-mcp-adapter"
APPROVAL_ADAPTER_COMMAND: Final[str] = "/usr/local/libexec/pantheon-approval-mcp-adapter"

# PROVISIONAL root-owned inputs (T-2920 owns the final paths). Named here
# for the head orchestrator's enable-leg command; never opened in tests.
ROOT_APPROVER_POLICY_PATH: Final[Path] = Path("/etc/pantheon/host-approval/approver-policy.json")
ROOT_SEAT_REGISTRY_PATH: Final[Path] = Path("/etc/pantheon/host-broker/seat-registry.json")

# Startup: the adapters never touch the bus at initialize/tools-list, so
# startup is fast; 10 s is headroom for a cold venv import. Tool calls:
# the adapters bound their own waits at CONNECT (2 s) + WRITE (30 s); the
# client timeout sits above that so the adapter's bounded error, not a
# client abort, is what the model sees.
STARTUP_TIMEOUT_SEC: Final[int] = 10
TOOL_TIMEOUT_SEC: Final[int] = 40

HARNESSES: Final[tuple[str, ...]] = ("claude", "codex")


@dataclass(frozen=True)
class StdioMcpRegistration:
    """One logical adapter registration. CLOSED field set: there is no
    args, env, url, header, cwd, uid or policy field, so a forbidden value
    has nowhere to go."""

    server_name: str
    command: str
    tool_names: tuple[str, ...]
    startup_timeout_sec: int
    tool_timeout_sec: int
    required: bool


HOST_REGISTRATION: Final[StdioMcpRegistration] = StdioMcpRegistration(
    server_name=HOST_SERVER_NAME,
    command=HOST_ADAPTER_COMMAND,
    tool_names=tuple(tool.name for tool in host_contract.TOOLS),
    startup_timeout_sec=STARTUP_TIMEOUT_SEC,
    tool_timeout_sec=TOOL_TIMEOUT_SEC,
    # optional: a missing or broken launcher must not make `codex exec`
    # exit (required=true does); the adapter itself starts with its
    # service absent (gate 6)
    required=False,
)
APPROVAL_REGISTRATION: Final[StdioMcpRegistration] = StdioMcpRegistration(
    server_name=APPROVAL_SERVER_NAME,
    command=APPROVAL_ADAPTER_COMMAND,
    tool_names=tuple(tool.name for tool in approval_contract.TOOLS),
    startup_timeout_sec=STARTUP_TIMEOUT_SEC,
    tool_timeout_sec=TOOL_TIMEOUT_SEC,
    required=False,
)
REGISTRATIONS_BY_NAME: Final[Mapping[str, StdioMcpRegistration]] = {
    reg.server_name: reg for reg in (HOST_REGISTRATION, APPROVAL_REGISTRATION)
}


class RegistrationError(Exception):
    """Generation refused (fail closed): nothing is rendered."""


# --------------------------------------------------------------------
# the policy gate
# --------------------------------------------------------------------


def is_policy_approver(policy: Any, seat_uid: int) -> bool:
    """True iff the HostApproval1 Reviewer gate would accept ``seat_uid``.

    Delegates to the landed ``ApproverResolver`` - the exact server-side
    predicate - so zero matches (requester-only) and ambiguous matches
    (two approver subjects on one uid) both yield no registration, just as
    both are refused on the bus. Human-decision-source approver subjects
    do NOT resolve (T-2919 Q4): they have no Reviewer standing at all -
    their only path is the provider-authenticated HumanIngress - so they
    get no approver registration either (same predicate, SUPPORT_MATRIX).
    """

    from tools.host_control.approval.identity import ApproverResolver

    return ApproverResolver(policy).subject_for_uid(seat_uid) is not None


def seat_registrations(*, approver: bool) -> tuple[StdioMcpRegistration, ...]:
    """Host adapter always; approver adapter only behind the policy gate."""

    if approver:
        return (HOST_REGISTRATION, APPROVAL_REGISTRATION)
    return (HOST_REGISTRATION,)


# --------------------------------------------------------------------
# the forbidden-field scan (fail closed; run by every renderer)
# --------------------------------------------------------------------

PACKAGED_COMMANDS: Final[frozenset[str]] = frozenset(
    reg.command for reg in REGISTRATIONS_BY_NAME.values()
)
CODEX_SERVER_KEYS: Final[frozenset[str]] = frozenset(
    ("command", "startup_timeout_sec", "tool_timeout_sec", "required", "enabled_tools", "tools")
)
CODEX_TOOL_KEYS: Final[frozenset[str]] = frozenset(("approval_mode",))
# The ONLY per-tool value the renderer emits; any other string is refused.
CODEX_APPROVAL_MODE: Final[str] = "approve"
CLAUDE_SERVER_KEYS: Final[frozenset[str]] = frozenset(("type", "command", "args"))
INTEGER_KEYS: Final[frozenset[str]] = frozenset(("startup_timeout_sec", "tool_timeout_sec"))
BOOLEAN_KEYS: Final[frozenset[str]] = frozenset(("required",))
# Values that would mean a token, a bus-address override, bus policy or an
# executor path leaked into seat config (ORG-052 §7).
FORBIDDEN_VALUE_PATTERNS: Final[tuple[str, ...]] = (
    r"dbus_",
    r"unix:path=",
    r"unix:abstract=",
    r"/run/dbus",
    r"/var/run/dbus",
    r"busconfig",
    r"<policy",
    r"send_destination",
    r"hostexecutor",
    r"host-executor",
    r"executor",
    r"token",
    r"secret",
    r"bearer",
    r"password",
    r"credential",
)
_FORBIDDEN_VALUE_RE = re.compile("|".join(FORBIDDEN_VALUE_PATTERNS), re.IGNORECASE)

# D-Bus server addresses, two arms (both apply; either refuses):
# - every transport the D-Bus specification defines, by name, including the
#   ones that take no key=value pairs ("autolaunch:", "systemd:");
# - a general address shape - "<transport>:<key>=" anywhere in the value,
#   after start or a ';' list separator - so a transport added later still
#   cannot carry an override through.
# No legitimate registration value contains a ':' at all (commands are
# absolute paths, tool and server names are closed identifiers), so neither
# arm can refuse a clean fragment.
DBUS_TRANSPORT_PREFIXES: Final[tuple[str, ...]] = (
    "unix:", "unixexec:", "tcp:", "nonce-tcp:", "launchd:", "autolaunch:", "systemd:", "vsock:",
)
_DBUS_TRANSPORT_RE = re.compile(
    r"(?:^|[;\s])(?:" + "|".join(re.escape(p) for p in DBUS_TRANSPORT_PREFIXES) + r")",
    re.IGNORECASE,
)
_DBUS_ADDRESS_SHAPE_RE = re.compile(r"(?:^|[;\s])[a-z][a-z0-9-]*:[a-z_]+=", re.IGNORECASE)
# Common credential token prefixes (GitHub ghp_/gho_/ghu_/ghs_/ghr_ and
# github_pat_, OpenAI/Anthropic-style sk-, Slack xox?-), at a word start.
_TOKEN_PREFIX_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:gh[opusr]_|github_pat_|sk-|xox[a-z]?-)", re.IGNORECASE)


def _forbidden_string(value: str, known_uids: frozenset[int]) -> str | None:
    """The reason a string value may not appear in seat config, or None."""

    if _FORBIDDEN_VALUE_RE.search(value):
        return "forbidden value"
    if _DBUS_TRANSPORT_RE.search(value) or _DBUS_ADDRESS_SHAPE_RE.search(value):
        return "D-Bus address"
    if _TOKEN_PREFIX_RE.search(value):
        return "token-shaped value"
    stripped = value.strip()
    if stripped in {str(uid) for uid in known_uids}:
        return "a registered uid as a string"
    if stripped.isdigit():
        # no registration value is numeric text: refuse uid-shaped strings
        # even when the uid is not one the caller passed in
        return "numeric string (uid-shaped)"
    return None


def _walk(node: Any, path: tuple[str, ...]) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, node
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _walk(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (f"[{index}]",))


def _deep_scan(document: Any, known_uids: frozenset[int]) -> None:
    for path, value in _walk(document, ()):
        if path and _FORBIDDEN_VALUE_RE.search(path[-1]):
            raise RegistrationError(f"forbidden key at {'.'.join(path)}")
        if isinstance(value, bool):
            if not path or path[-1] not in BOOLEAN_KEYS:
                raise RegistrationError(f"unexpected boolean at {'.'.join(path)}")
        elif isinstance(value, (int, float)):
            if not path or path[-1] not in INTEGER_KEYS:
                raise RegistrationError(f"unexpected number at {'.'.join(path)}")
            if value in known_uids:
                raise RegistrationError(f"a registered uid appears at {'.'.join(path)}")
        elif isinstance(value, str):
            reason = _forbidden_string(value, known_uids)
            if reason is not None:
                raise RegistrationError(f"{reason} at {'.'.join(path)}")


def _check_command(server_name: str, command: object) -> StdioMcpRegistration:
    registration = REGISTRATIONS_BY_NAME.get(server_name)
    if registration is None:
        raise RegistrationError(f"unknown server {server_name!r}")
    if command not in PACKAGED_COMMANDS or command != registration.command:
        raise RegistrationError(f"{server_name}: command is not its packaged launcher")
    return registration


def _scan_codex(document: Mapping[str, Any]) -> None:
    if set(document) != {"mcp_servers"} or not isinstance(document["mcp_servers"], Mapping):
        raise RegistrationError("codex document must hold exactly mcp_servers")
    for name, table in document["mcp_servers"].items():
        if not isinstance(table, Mapping) or not set(table) <= CODEX_SERVER_KEYS:
            raise RegistrationError(f"{name}: key outside the codex allowlist")
        registration = _check_command(name, table.get("command"))
        if table.get("enabled_tools") != list(registration.tool_names):
            raise RegistrationError(f"{name}: enabled_tools must equal the adapter contract")
        tools = table.get("tools", {})
        if not isinstance(tools, Mapping) or not set(tools) <= set(registration.tool_names):
            raise RegistrationError(f"{name}: per-tool table names an unknown tool")
        for tool, settings in tools.items():
            if not isinstance(settings, Mapping) or not set(settings) <= CODEX_TOOL_KEYS:
                raise RegistrationError(f"{name}.{tool}: key outside the per-tool allowlist")
            if settings.get("approval_mode", CODEX_APPROVAL_MODE) != CODEX_APPROVAL_MODE:
                raise RegistrationError(f"{name}.{tool}: approval_mode must be {CODEX_APPROVAL_MODE!r}")


def _scan_claude(document: Mapping[str, Any]) -> None:
    if set(document) != {"claude_json", "settings_json"}:
        raise RegistrationError("claude document must hold exactly claude_json and settings_json")
    claude_json, settings_json = document["claude_json"], document["settings_json"]
    if not isinstance(claude_json, Mapping) or set(claude_json) != {"mcpServers"}:
        raise RegistrationError("claude_json must hold exactly mcpServers")
    servers = claude_json["mcpServers"]
    if not isinstance(servers, Mapping):
        raise RegistrationError("mcpServers must be a mapping")
    for name, entry in servers.items():
        if not isinstance(entry, Mapping) or not set(entry) <= CLAUDE_SERVER_KEYS:
            raise RegistrationError(f"{name}: key outside the claude allowlist")
        _check_command(name, entry.get("command"))
        if entry.get("type") != "stdio" or entry.get("args") != []:
            raise RegistrationError(f"{name}: must be a stdio server with no arguments")
    if not isinstance(settings_json, Mapping) or set(settings_json) != {"permissions"}:
        raise RegistrationError("settings_json must hold exactly permissions")
    permissions = settings_json["permissions"]
    if not isinstance(permissions, Mapping) or set(permissions) != {"allow"}:
        raise RegistrationError("permissions must hold exactly allow")
    if permissions["allow"] != [f"mcp__{name}" for name in servers]:
        raise RegistrationError("permission rules must name exactly the registered servers")


def assert_registration_clean(harness: str, document: Mapping[str, Any], *, known_uids: Iterable[int] = ()) -> None:
    """Fail closed unless ``document`` is a clean registration for ``harness``.

    Structural allowlists per harness, the packaged-command allowlist, then
    a deep denylist walk over every key and value; numbers only at timeout
    keys and never equal to a registered uid; booleans only at
    ``required``.
    """

    if harness == "codex":
        _scan_codex(document)
    elif harness == "claude":
        _scan_claude(document)
    else:
        raise RegistrationError(f"unsupported harness {harness!r}")
    _deep_scan(document, frozenset(known_uids))


# --------------------------------------------------------------------
# renderers (bridge surfaces under ORG-031; they never change tool names,
# schemas, server policy, identity, approval requirements or receipts)
# --------------------------------------------------------------------

_HEADER = "# GENERATED by tools/harness_adapter/host_mcp_registration.py (T-2916). Do not edit.\n"
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


def _toml_string(value: str) -> str:
    # ASCII-only by construction; JSON escaping is valid TOML basic-string
    # escaping for ASCII.
    if not value.isascii():
        raise RegistrationError("non-ASCII value")
    return json.dumps(value)


def _toml_key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else _toml_string(value)


def render_codex(registrations: Iterable[StdioMcpRegistration], *, known_uids: Iterable[int] = ()) -> str:
    """Codex ``config.toml`` fragment: one ``[mcp_servers.<name>]`` table
    per adapter (packaged command, startup/tool timeouts, optional startup
    posture, tool allowlist) plus a per-tool ``approval_mode = "approve"``
    table. Codex prompts on first use of a new MCP tool even under
    ``approval_policy = "never"`` (T-1033 live find); the unattended
    posture pre-answers it. That prompt is defence in depth only: the
    broker and approval service authorize every call (ORG-052 §7)."""

    lines = [_HEADER]
    for reg in registrations:
        table = f"mcp_servers.{_toml_key(reg.server_name)}"
        lines.append(f"[{table}]")
        lines.append(f"command = {_toml_string(reg.command)}")
        lines.append(f"startup_timeout_sec = {int(reg.startup_timeout_sec)}")
        lines.append(f"tool_timeout_sec = {int(reg.tool_timeout_sec)}")
        lines.append(f"required = {'true' if reg.required else 'false'}")
        lines.append("enabled_tools = [" + ", ".join(_toml_string(name) for name in reg.tool_names) + "]")
        lines.append("")
        for name in reg.tool_names:
            lines.append(f"[{table}.tools.{_toml_key(name)}]")
            lines.append(f"approval_mode = {_toml_string(CODEX_APPROVAL_MODE)}")
            lines.append("")
    text = "\n".join(lines)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistrationError("rendered codex fragment is not valid TOML") from exc
    assert_registration_clean("codex", document, known_uids=known_uids)
    return text


def claude_document(registrations: Iterable[StdioMcpRegistration]) -> dict[str, Any]:
    regs = tuple(registrations)
    return {
        # user-scope ~/.claude.json `mcpServers` (T-3559 D1 shape); no
        # per-server timeout key is emitted - none is verified (gate 8)
        "claude_json": {
            "mcpServers": {
                reg.server_name: {"type": "stdio", "command": reg.command, "args": []}
                for reg in regs
            }
        },
        # ~/.claude/settings.json permission presentation: server-level
        # rules, so the dotted tool names never appear in a rule
        "settings_json": {"permissions": {"allow": [f"mcp__{reg.server_name}" for reg in regs]}},
    }


def render_claude(registrations: Iterable[StdioMcpRegistration], *, known_uids: Iterable[int] = ()) -> str:
    """Claude Code fragments as one canonical JSON document with two parts:
    ``claude_json`` (the user-scope ``mcpServers`` entries) and
    ``settings_json`` (the permission rules). Applying them is the enable
    leg's, through the existing installer path."""

    document = claude_document(registrations)
    text = json.dumps(document, indent=2, sort_keys=False, ensure_ascii=True) + "\n"
    assert_registration_clean("claude", json.loads(text), known_uids=known_uids)
    return text


RENDERERS: Final[Mapping[str, Any]] = {"codex": render_codex, "claude": render_claude}


def parse_rendered(harness: str, text: str) -> dict[str, Any]:
    if harness == "codex":
        return tomllib.loads(text)
    if harness == "claude":
        return json.loads(text)
    raise RegistrationError(f"unsupported harness {harness!r}")


def launch_specs(harness: str, text: str) -> dict[str, tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]]:
    """Server name -> (command, args, env): the launch tuple this fragment
    specifies; real-client launch is unsupported (T-2924). The adapter's
    server-observed tools/list depends only on this tuple, so fragments
    specifying equal tuples observe the same server."""

    document = parse_rendered(harness, text)
    if harness == "codex":
        servers = document["mcp_servers"]
        return {
            name: (table["command"], tuple(table.get("args", [])), tuple(sorted(table.get("env", {}).items())))
            for name, table in servers.items()
        }
    servers = document["claude_json"]["mcpServers"]
    return {
        name: (entry["command"], tuple(entry.get("args", [])), tuple(sorted(entry.get("env", {}).items())))
        for name, entry in servers.items()
    }


# --------------------------------------------------------------------
# per-seat generation
# --------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedRegistration:
    seat_id: str
    harness: str
    approver: bool
    servers: tuple[str, ...]
    text: str


def known_uids_of(registry: Any, policy: Any) -> frozenset[int]:
    uids = {registry.seat(seat_id).uid for seat_id in registry.seat_ids()}
    uids.update(policy.subject_uids.values())
    return frozenset(uids)


def generate_for_seat(seat_id: str, *, registry: Any, policy: Any, harness: str | None = None) -> RenderedRegistration:
    """Render one seat's registration for the harness the root-owned seat
    registry names (or ``harness`` to render the other one for parity).
    An unknown seat refuses."""

    from tools.host_control.contracts import RegistryError

    try:
        seat = registry.seat(seat_id)
    except RegistryError as exc:
        raise RegistrationError("unknown seat") from exc
    harness = harness or seat.harness
    if harness not in RENDERERS:
        raise RegistrationError(f"unsupported harness {harness!r}")
    approver = is_policy_approver(policy, seat.uid)
    registrations = seat_registrations(approver=approver)
    text = RENDERERS[harness](registrations, known_uids=known_uids_of(registry, policy))
    return RenderedRegistration(
        seat_id=seat_id,
        harness=harness,
        approver=approver,
        servers=tuple(reg.server_name for reg in registrations),
        text=text,
    )


def load_root_inputs(
    *,
    policy_path: Path = ROOT_APPROVER_POLICY_PATH,
    registry_path: Path = ROOT_SEAT_REGISTRY_PATH,
    trusted_roots: tuple[Path, ...] = (),
    loader: Any = None,
) -> tuple[Any, Any]:
    """(SeatRegistry, ApproverPolicy) read through the root-owned trust
    boundary. ENABLE-LEG ONLY: with no ``loader`` this opens the given
    roots requiring root ownership (``RootOwnedFileLoader.open``)."""

    from tools.host_control.approval.policy import load_approver_policy
    from tools.host_control.registry import SeatRegistry
    from tools.host_control.trusted_files import RootOwnedFileLoader

    if loader is None:
        roots = tuple(dict.fromkeys((policy_path.parent, registry_path.parent, *trusted_roots)))
        loader = RootOwnedFileLoader.open(roots)
    registry = SeatRegistry.load(registry_path, files=loader)
    policy = load_approver_policy(loader, policy_path)
    return registry, policy


# --------------------------------------------------------------------
# gate 8: what is proven in-repo vs unsupported until real evidence
# --------------------------------------------------------------------

_T = "tools.test_harness_host_mcp_registration"
_A = "tools.test_approval_mcp_adapter"

SUPPORT_MATRIX: Final[Mapping[str, tuple[str, str]]] = {
    # ---- proven in-repo (status, the test that proves it) ----
    "approver_adapter.lists_exactly_four_tools": ("proven", f"{_A}.SdkStdioTests.test_modern_era_lists_exactly_the_four_tools"),
    "approver_adapter.both_protocol_eras": ("proven", f"{_A}.SdkStdioTests.test_legacy_initialize_and_identical_canonical_tools_list"),
    "approver_seat.lists_and_routes_one_to_one_on_private_bus": ("proven", f"{_A}.ApproverPrivateBusTests.test_approver_seat_lists_exactly_four_tools_and_routes_one_to_one"),
    "requester_seat.gets_no_approver_registration": ("proven", f"{_T}.PolicyGateTests.test_requester_only_seat_gets_no_approver_entry"),
    "requester_seat.does_not_list_approval_tools": ("proven", f"{_T}.ServerObservedParityTests.test_requester_seat_union_has_no_approval_tools"),
    "requester_seat.manual_launch_and_direct_reviewer_call_denied": ("proven", f"{_A}.ApproverPrivateBusTests.test_requester_only_seat_manual_launch_lists_but_every_call_is_denied"),
    "revocation.policy_removal_denies_stale_config_and_same_uid_process": ("proven", f"{_A}.ApproverPrivateBusTests.test_policy_removal_revokes_despite_stale_config_and_registration_deletion_is_not_revocation"),
    "revocation.registration_deletion_is_not_revocation": ("proven", f"{_A}.ApproverPrivateBusTests.test_policy_removal_revokes_despite_stale_config_and_registration_deletion_is_not_revocation"),
    "revocation.generation_drops_entry_after_policy_removal": ("proven", f"{_T}.PolicyGateTests.test_generation_tracks_the_bus_scenarios"),
    "approver_adapter.starts_and_recovers_with_service_absent": ("proven", f"{_A}.ApproverPrivateBusTests.test_service_absent_then_started_same_adapter_recovers"),
    "generation.no_token_address_uid_bus_policy_or_executor_path": ("proven", f"{_T}.ForbiddenFieldScanTests.test_every_forbidden_class_is_refused"),
    "generation.approver_entry_iff_policy_approver": ("proven", f"{_T}.PolicyGateTests.test_approver_entry_iff_policy_approver"),
    "generation.golden_output": ("proven", f"{_T}.GoldenTests.test_rendered_fragments_match_golden_bytes"),
    "codex.config_toml_round_trip": ("proven", f"{_T}.GoldenTests.test_codex_fragment_round_trips_through_tomllib"),
    "parity.launch_tuple_identical_across_harnesses": ("proven", f"{_T}.LaunchTupleParityTests.test_launch_tuples_are_identical_across_harnesses"),
    "parity.server_tools_list_identical_across_harnesses": ("proven", f"{_T}.ServerObservedParityTests.test_server_observed_tools_list_is_canonically_identical"),
    "approver_artifact.gate7_fixture_form": ("proven", f"{_A}.ApprovalPackagingTests.test_fixture_root_verifies_every_check"),
    "host_artifact.refactor_byte_identical": ("proven", f"{_A}.HostProfileEquivalenceTests.test_default_profile_is_byte_identical_to_the_t2915_build"),
    "human_source_approver.gets_no_registration": ("proven", f"{_T}.PolicyGateTests.test_human_source_approver_gets_no_registration"),  # T-2919 Q4
    # ---- recorded (server parity / presentation; not a contract change) ----
    "claude.no_per_server_timeout": ("recorded", "no per-server timeout key verified for Claude Code; none emitted; the adapters bound their own waits (2 s connect, 10/30 s call)"),
    "claude.server_level_permission_rule": ("recorded", "rules name servers (mcp__pantheon-host, mcp__pantheon-approval), never dotted tool names; matching unproven - see unsupported row"),
    # ---- unsupported until real-client / real-host evidence ----
    "codex.real_client_accepts_generated_table": ("unsupported", "T-2924: the isolated codex-cli probe was blocked by the T-1345 worker guard and deliberately not retried (HO ruling on T-2916); in-repo evidence is tomllib round-trip + golden only"),
    "claude.real_client_accepts_generated_config": ("unsupported", "T-2924: no non-mutating isolated Claude Code probe exists in-repo"),
    "codex.real_client_launches_and_lists (E3)": ("unsupported", "T-2924"),
    "claude.real_client_launches_and_lists (E4)": ("unsupported", "T-2924"),
    "real_configured_approver_seat_end_to_end (E5 real)": ("unsupported", "T-2924"),
    "clients.dotted_tool_names": ("unsupported", "T-2924: legal under MCP SEP-986; client handling unproven"),
    "claude.permission_rule_matching": ("unsupported", "T-2924"),
    "codex.approval_mode_effect": ("unsupported", "T-2924"),
    "codex.required_false_startup_posture": ("unsupported", "T-2924"),
    "codex.timeout_semantics": ("unsupported", "T-2924"),
    "distinct_uid_separation_and_bus_policy_fragments": ("unsupported", "T-2920/T-2924: a private bus has one uid and a permissive policy"),
    "revocation.production_latency": ("unsupported", "T-2920: staged revocation takes effect when HostApproval1 restarts with the new policy; the policy install must restart or reload the service"),
    "dbus_activation": ("unsupported", "T-2924"),
    "root_ownership_of_runtime_bytes": ("unsupported", "T-2920"),
}


# --------------------------------------------------------------------
# enable-leg CLI (stdout only)
# --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="host_mcp_registration",
        description="Print one seat's role-gated MCP registration fragment (stdout only; applies nothing).",
    )
    parser.add_argument("--seat", required=True)
    parser.add_argument("--approver-policy", type=Path, default=ROOT_APPROVER_POLICY_PATH)
    parser.add_argument("--seat-registry", type=Path, default=ROOT_SEAT_REGISTRY_PATH)
    parser.add_argument("--trusted-root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        registry, policy = load_root_inputs(
            policy_path=args.approver_policy,
            registry_path=args.seat_registry,
            trusted_roots=tuple(args.trusted_root),
        )
        rendered = generate_for_seat(args.seat, registry=registry, policy=policy)
    except Exception as exc:  # noqa: BLE001 - fail closed with a bounded reason
        print(f"refused: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    sys.stdout.write(rendered.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
