"""``germinate plan --preflight``: the read-only GO/NO-GO receipt (ORG-080 §1; T-3191 i/ii/viii; T-5860).

A preflight proves, BEFORE an operator window opens, that a planned run is
executable. It never mutates, never takes a claim, never writes run state and
never consumes a grant. The receipt goes to stdout only (the operator keeps it);
``apply --preflight-receipt`` is the one, opt-in consumer.

This module holds the engine-generic pieces; ``Engine.preflight`` (executor.py)
runs the checks:

* ``required_capabilities`` -- T-3191 (i): the run-as capabilities a plan needs,
  derived from each op's declared ``privilege`` / ``owner`` (none: nothing;
  seat: run as the owner; root: run as root). No persona or component names.
  A ``role:<key>`` owner resolves to an account through the planner checkout's
  instance manifest ``roles`` (data, not code); an unresolvable owner is a NO-GO.
* ``parse_sudo_listing`` / ``evaluate_capabilities`` -- the operator's
  ``sudo -n -l`` output classified into known rules and unknown lines. v0 holds
  a capability only by self (the operator IS the account, or uid 0 for root) or
  by a known rule whose runas user list names the account or ``ALL``, tagged
  ``NOPASSWD`` (an unattended ``-n`` run cannot answer a password prompt), with
  the command exactly ``ALL`` and no ``!`` negation in that rule. An unknown line
  never grants (fail-closed); a restricted-command rule is reported, not held
  (PROVISIONAL until the cross-account launch argv is fixed at live enablement).
* ``sudo_outcome`` (T-5921) -- a non-zero ``sudo -n -l`` is classified, not just
  "unavailable": ``password-required`` (the ``sudo: a password is required``
  line; under the default ``listpw=any`` sudoers(5) rule this means no NOPASSWD
  entry exists for the operator) and ``not-allowed`` are typed
  ``no-passwordless-sudo`` misses; anything else is ``unobservable``. A section
  header naming another host is foreign (T-5860 r2 nit 2).
* the observation port -- ``PreflightPort``. ``SimulatedPreflightPort`` is the
  ONLY implementation in engine/: it reads
  ``<simulated-host-root>/preflight/operator.json``. The live READ-ONLY reader
  (T-5921) is ``tools/germinate/preflight_live.py``, outside engine/ (the safety
  lint bans process reach here), dark-pinned per seat; nothing in this module
  can reach the host.
* the component hook (T-5921) -- see "COMPONENT PREFLIGHT HOOK" below.
* the receipt -- canonical JSON sealed by ``receipt_digest`` (tamper-evident
  against accidental edits; it is not an authority token: the operator is the
  trusted principal, and apply re-binds it to the current plan digest).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Protocol

RECEIPT_SCHEMA: Final = "pantheon-germinate-preflight-receipt/v1"  # T-5921: v1 adds the ``component`` check
SIM_OBSERVATION_SCHEMA: Final = "pantheon-germinate-sim-preflight/v0"
SIM_OBSERVATION_REL: Final = ("preflight", "operator.json")
TIER_SIMULATED: Final = "simulated"

DEFAULT_TTL_SECONDS: Final = 3600
MIN_TTL_SECONDS: Final = 60
MAX_TTL_SECONDS: Final = 86400
FUTURE_SKEW_SECONDS: Final = 60
MAX_RECEIPT_FILE_BYTES: Final = 1 << 20
MAX_OBSERVATION_BYTES: Final = 1 << 16

CHECK_IDS: Final = ("plan", "plan-digest", "plan-encodable", "gates", "grants", "capability", "source-hold", "component")
PASS, FAIL, SKIP = "pass", "fail", "skip"
STATUSES: Final = frozenset({PASS, FAIL, SKIP})
VERDICTS: Final = frozenset({"GO", "NO-GO"})
RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "verdict",
        "plan",
        "observation_tier",
        "evidence_scope",
        "observed_at",
        "expires_at",
        "ttl_seconds",
        "operator",
        "checks",
        "receipt_digest",
    }
)
PLAN_KEYS: Final = frozenset({"leg", "scope", "system", "instance", "plan_digest", "op_registry_digest", "nodes"})
CHECK_KEYS: Final = frozenset({"id", "status", "reason", "detail", "evidence"})

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
_REV_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME_RE = re.compile(r"[a-z_][a-z0-9_.-]*\$?\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_MODE_RE = re.compile(r"[0-7]{4}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"([A-Z_]+):\s*")
#: sudoers tags (sudoers(5) "Tag_Spec"). Any other ``WORD:`` prefix (or an
#: option such as ``CWD=``) makes the rule unknown: it never grants.
SUDO_TAGS: Final = frozenset(
    {
        "NOPASSWD", "PASSWD", "SETENV", "NOSETENV", "NOEXEC", "EXEC", "LOG_INPUT", "NOLOG_INPUT",
        "LOG_OUTPUT", "NOLOG_OUTPUT", "MAIL", "NOMAIL", "FOLLOW", "NOFOLLOW", "INTERCEPT", "NOINTERCEPT",
    }
)


class PreflightError(Exception):
    """A typed preflight or receipt refusal (``code`` is the refusal code)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class Unobservable(Exception):
    """The port could not observe what was asked (fail-closed: never a pass)."""


# ----------------------------------------------------------------------
# time
# ----------------------------------------------------------------------


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def format_time(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).strftime(_ISO_FMT)


def parse_time(value: Any) -> _dt.datetime:
    if not isinstance(value, str):
        raise ValueError("not a timestamp string")
    return _dt.datetime.strptime(value, _ISO_FMT).replace(tzinfo=_dt.timezone.utc)


# ----------------------------------------------------------------------
# observations and the port
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorIdentity:
    user: str
    uid: int
    #: T-5921 (T-5860 r2 nit 2): the short hostname. When known, only a
    #: ``... on <host>:`` sudo section for THIS host is read; None skips the check.
    host: str | None = None


@dataclass(frozen=True)
class SudoListing:
    """``sudo -n -l`` as run by the operator: exit code, stdout and stderr (LC_ALL=C at live)."""

    exit_code: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class CheckoutObservation:
    head: str
    clean: bool


@dataclass(frozen=True)
class PathStat:
    """``lstat`` of one path (never followed). ``kind`` is one of ``PATH_KINDS``;
    ``mode`` is the permission bits as 4 octal digits (``"0600"``)."""

    kind: str
    uid: int
    gid: int
    mode: str


@dataclass(frozen=True)
class AccountInfo:
    uid: int
    home: str


PATH_KINDS: Final = frozenset({"file", "directory", "symlink", "socket", "fifo", "char", "block"})


class PreflightPort(Protocol):
    tier: str

    def operator(self) -> OperatorIdentity: ...

    def sudo_listing(self) -> SudoListing: ...

    def checkout(self, repo_root: Path) -> CheckoutObservation: ...

    # T-5921: the read-only probe a component hook sees (through ``Probe``). Each
    # raises ``Unobservable`` when it cannot observe; None means observed ABSENT.

    def path_stat(self, path: str) -> PathStat | None: ...

    def file_sha256(self, path: str) -> str | None: ...

    def read_json(self, path: str) -> Any: ...

    def account(self, name: str) -> AccountInfo | None: ...


def checked_path(path: Any) -> str:
    """A probe path must be absolute, normalized text (no ``..``, no NUL); else Unobservable."""

    if not (isinstance(path, str) and path.startswith("/") and "\0" not in path and posixpath.normpath(path) == path):
        raise Unobservable(f"probe path {path!r} is not an absolute normalized path")
    return path


def checked_account(name: Any) -> str:
    if not (isinstance(name, str) and _NAME_RE.match(name)):
        raise Unobservable(f"account name {name!r} is malformed")
    return name


def is_uint(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class SimulatedPreflightPort:
    """The CI tier: observations come from one file in the simulated host, never the host.

    ``<root>/preflight/operator.json``::

        {"schema": "pantheon-germinate-sim-preflight/v0",
         "operator": {"user": str, "uid": int, "host": str (optional)},
         "sudo_listing": {"exit_code": int, "stdout": str, "stderr": str (optional)},
         "checkout": {"head": "<40 or 64 hex>", "clean": bool},
         "probe": {"accounts": {name: {"uid": int, "home": "/abs"}},
                   "paths": {"/abs": {"kind": str, "uid": int, "gid": int, "mode": "0600"}},
                   "files": {"/abs": "<sha256 hex>"},
                   "json": {"/abs": <any JSON>}}}

    A missing file or section, or any malformed value, is ``Unobservable``.
    The simulated checkout is per simulated host, not per ``repo_root``. In
    ``probe`` (T-5921) a path or account not listed is observed ABSENT (None);
    the string ``"unobservable"`` in place of an entry makes that probe raise.
    """

    tier = TIER_SIMULATED

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def path(self) -> Path:
        return self.root.joinpath(*SIM_OBSERVATION_REL)

    def _doc(self) -> Mapping[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            raise Unobservable("no simulated preflight observation file") from None
        except OSError as exc:
            raise Unobservable(f"simulated observation file cannot be opened: {exc.strerror}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unobservable("simulated observation file is not a regular file")
            data = os.read(fd, MAX_OBSERVATION_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_OBSERVATION_BYTES:
            raise Unobservable("simulated observation file is oversized")
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Unobservable(f"simulated observation file is not JSON: {exc}") from None
        if not isinstance(doc, dict) or doc.get("schema") != SIM_OBSERVATION_SCHEMA:
            raise Unobservable(f"simulated observation file is not {SIM_OBSERVATION_SCHEMA}")
        return doc

    def _section(self, name: str) -> Mapping[str, Any]:
        value = self._doc().get(name)
        if not isinstance(value, dict):
            raise Unobservable(f"simulated observation has no {name!r} section")
        return value

    def operator(self) -> OperatorIdentity:
        s = self._section("operator")
        user, uid, host = s.get("user"), s.get("uid"), s.get("host")
        if not (isinstance(user, str) and _NAME_RE.match(user)) or not is_uint(uid):
            raise Unobservable("simulated operator identity is malformed")
        if host is not None and not (isinstance(host, str) and _HOST_RE.match(host)):
            raise Unobservable("simulated operator host is malformed")
        return OperatorIdentity(user, uid, host)

    def sudo_listing(self) -> SudoListing:
        s = self._section("sudo_listing")
        code, out, err = s.get("exit_code"), s.get("stdout"), s.get("stderr", "")
        if not (isinstance(code, int) and not isinstance(code, bool)) or not isinstance(out, str) or not isinstance(err, str):
            raise Unobservable("simulated sudo listing is malformed")
        return SudoListing(code, out, err)

    def checkout(self, repo_root: Path) -> CheckoutObservation:
        s = self._section("checkout")
        head, clean = s.get("head"), s.get("clean")
        if not (isinstance(head, str) and _REV_RE.match(head)) or not isinstance(clean, bool):
            raise Unobservable("simulated checkout observation is malformed")
        return CheckoutObservation(head, clean)

    def _probe(self, table_name: str, key: str) -> Any:
        table = self._section("probe").get(table_name, {})
        if not isinstance(table, dict):
            raise Unobservable(f"simulated probe table {table_name!r} is malformed")
        value = table.get(key)
        if value == "unobservable":
            raise Unobservable(f"simulated probe {table_name} {key!r} is unobservable")
        return value

    def path_stat(self, path: str) -> PathStat | None:
        e = self._probe("paths", checked_path(path))
        if e is None:
            return None
        if not (
            isinstance(e, dict) and e.get("kind") in PATH_KINDS and is_uint(e.get("uid")) and is_uint(e.get("gid"))
            and isinstance(e.get("mode"), str) and _MODE_RE.match(e["mode"])
        ):
            raise Unobservable(f"simulated path stat for {path!r} is malformed")
        return PathStat(e["kind"], e["uid"], e["gid"], e["mode"])

    def file_sha256(self, path: str) -> str | None:
        e = self._probe("files", checked_path(path))
        if e is None:
            return None
        if not (isinstance(e, str) and _SHA_RE.match(e)):
            raise Unobservable(f"simulated file digest for {path!r} is malformed")
        return e

    def read_json(self, path: str) -> Any:
        return self._probe("json", checked_path(path))

    def account(self, name: str) -> AccountInfo | None:
        e = self._probe("accounts", checked_account(name))
        if e is None:
            return None
        if not (isinstance(e, dict) and is_uint(e.get("uid")) and isinstance(e.get("home"), str) and e["home"].startswith("/")):
            raise Unobservable(f"simulated account {name!r} is malformed")
        return AccountInfo(e["uid"], e["home"])


# ----------------------------------------------------------------------
# T-3191 (i): required capabilities
# ----------------------------------------------------------------------


def run_as_target(privilege: str, owner: str) -> str | None:
    """The account an op must run as, from its declared fields; None = no capability needed.
    An unknown privilege refuses (fail-closed), never "needs nothing"."""

    if privilege == "root":
        return "root"
    if privilege == "seat":
        return owner
    if privilege == "none":
        return None
    raise PreflightError("privilege-unknown", f"privilege {privilege!r} is not none, seat or root")


ROLE_PREFIX: Final = "role:"
INSTANCE_MANIFEST: Final = "instance.yaml"


def role_accounts(repo_root: Path) -> dict[str, str]:
    """``{role key: account}`` from the planner checkout's instance manifest ``roles``
    (read-only data, the binding a ``role:<key>`` owner names); {} when absent or unreadable."""

    import yaml

    try:
        doc = yaml.safe_load((Path(repo_root) / INSTANCE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    roles = doc.get("roles") if isinstance(doc, dict) else None
    if not isinstance(roles, dict):
        return {}
    return {
        k: v["account"]
        for k, v in roles.items()
        if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("account"), str) and _NAME_RE.match(v["account"])
    }


def resolve_targets(
    required: Mapping[str, list[str]], roles: Mapping[str, str]
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, list[str]]]:
    """Resolve ``role:<key>`` owners to accounts: (by account, {role owner: account}, unresolved)."""

    resolved: dict[str, list[str]] = {}
    via: dict[str, str] = {}
    unresolved: dict[str, list[str]] = {}
    for target, labels in required.items():
        account: str | None = target
        if target.startswith(ROLE_PREFIX):
            account = roles.get(target[len(ROLE_PREFIX):])
            if account is None:
                unresolved[target] = list(labels)
                continue
            via[target] = account
        elif not _NAME_RE.match(target):
            unresolved[target] = list(labels)
            continue
        resolved.setdefault(account, []).extend(labels)
    return {k: resolved[k] for k in sorted(resolved)}, via, unresolved


def required_capabilities(entries: Iterable[tuple[str, str, str]]) -> dict[str, list[str]]:
    """``{run-as target: [op labels]}`` from ``(label, privilege, owner)`` triples."""

    out: dict[str, list[str]] = {}
    for label, privilege, owner in entries:
        target = run_as_target(privilege, owner)
        if target is not None:
            out.setdefault(target, []).append(label)
    return {k: out[k] for k in sorted(out)}


# ----------------------------------------------------------------------
# sudo -n -l classification
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SudoRule:
    raw: str
    runas_users: tuple[str, ...]
    runas_groups: tuple[str, ...]
    #: (effective tags, command) per command item; tags carry forward across items.
    items: tuple[tuple[frozenset[str], str], ...]

    @property
    def negated(self) -> bool:
        return any(cmd.startswith("!") for _tags, cmd in self.items)


@dataclass(frozen=True)
class ParsedListing:
    rules: tuple[SudoRule, ...]
    unparsed: tuple[str, ...] = field(default_factory=tuple)
    #: T-5921 (Recovery co-review F2): the subset of ``unparsed`` from the operator's OWN
    #: section. Any entry here makes the whole listing unobservable (fail-closed).
    own_unparsed: tuple[str, ...] = field(default_factory=tuple)


def _split_commas(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


def _parse_rule(raw: str) -> SudoRule | None:
    """One ``(runas) TAGS: cmd, cmd`` rule, or None when anything is not a known form."""

    text = raw.strip()
    if not text.startswith("("):
        return None
    close = text.find(")")
    if close < 0:
        return None
    runas, rest = text[1:close], text[close + 1 :].strip()
    users_text, sep, groups_text = runas.partition(":")
    users = tuple(u for u in _split_commas(users_text) if u) if users_text.strip() else ()
    groups = tuple(g for g in _split_commas(groups_text) if g) if sep and groups_text.strip() else ()
    for token in users + groups:
        if token != "ALL" and not _NAME_RE.match(token):
            return None  # %group, #uid, +netgroup, an alias, a negation: unknown
    if not rest:
        return None
    items: list[tuple[frozenset[str], str]] = []
    tags: set[str] = set()
    for part in _split_commas(rest):
        while True:
            m = _TAG_RE.match(part)
            if not m:
                break
            if m.group(1) not in SUDO_TAGS:
                return None
            tag = m.group(1)
            opposite = tag[2:] if tag.startswith("NO") else "NO" + tag
            tags.discard(opposite)
            tags.add(tag)
            part = part[m.end() :]
        if not part or "=" in part.split(" ", 1)[0]:
            return None  # an empty command or an option (CWD=, ROLE=, ...): unknown
        items.append((frozenset(tags), part))
    return SudoRule(raw, users, groups, tuple(items))


_SECTION_RE = re.compile(r"User (\S+) may run the following commands on (.*):\Z")


def _same_host(listed: str, host: str) -> bool:
    """sudo prints the short host name unless ``fqdn`` is set: compare the first labels."""

    return listed.split(".", 1)[0] == host.split(".", 1)[0]


#: T-5921: the outcome of a ``sudo -n -l`` run, before any rule is read.
SUDO_LISTED, SUDO_PASSWORD_REQUIRED, SUDO_NOT_ALLOWED, SUDO_UNOBSERVABLE = "listed", "password-required", "not-allowed", "unobservable"
_PASSWORD_LINE = "sudo: a password is required"
_NOT_ALLOWED_RE = re.compile(r"(?:Sorry, user \S+ may not run sudo on \S+\.|User \S+ is not allowed to run sudo on \S+\.)\Z")


def sudo_outcome(listing: SudoListing) -> str:
    """Classify a ``sudo -n -l`` run (exit code plus both streams).

    * ``listed``: exit 0; the rules are parsed by ``parse_sudo_listing``.
    * ``password-required``: exit 1 with the line ``sudo: a password is required``
      (either stream). ``-n`` forbids the prompt; under the default ``listpw=any``
      (sudoers(5)) listing needs a password only when NO entry for the user is
      NOPASSWD, so the operator holds no passwordless capability. Under another
      ``listpw`` the inference is weaker, but the result never grants (fail-closed).
    * ``not-allowed``: exit 1 with sudo's "may not run sudo" / "is not allowed"
      line: no rule at all.
    * ``unobservable``: anything else (another exit code, an unknown message).
    """

    if listing.exit_code == 0:
        return SUDO_LISTED
    lines = [line.strip() for line in (listing.stderr + "\n" + listing.stdout).splitlines() if line.strip()]
    if listing.exit_code == 1 and _PASSWORD_LINE in lines:
        return SUDO_PASSWORD_REQUIRED
    if listing.exit_code == 1 and any(_NOT_ALLOWED_RE.match(line) for line in lines):
        return SUDO_NOT_ALLOWED
    return SUDO_UNOBSERVABLE


def parse_sudo_listing(text: str, user: str | None = None, host: str | None = None) -> ParsedListing:
    """Classify ``sudo -n -l`` stdout into known rules (in listing order) and unknown lines.

    Only a ``User <user> may run the following commands on <host>:`` section is
    read, and, when ``user`` is given, only the one whose header names ``user``
    (and, when ``host`` is given, names this host; T-5860 r2 nit 2): a foreign
    section grants nothing (its header and lines go to ``unparsed``). The
    ``Matching Defaults`` / ``Runas and Command-specific defaults`` sections are
    ignored.

    Wrapping (T-5921 co-review F1, from a real capture): sudo wraps a long
    entry onto continuation lines indented DEEPER than the entry's own line, at
    any space, mid-command and with or without a trailing comma. Every line
    indented deeper than the current entry's line is joined onto it with one
    space. A line at the entry indent starts a new entry. Anything that does not
    parse is an unparsed line; in the operator's own section it is also recorded
    in ``own_unparsed``, which ``evaluate_capabilities`` treats as an
    unobservable listing (F2: fail-closed, a lost fragment can hide a negation).
    """

    rules: list[SudoRule] = []
    unparsed: list[str] = []
    own_unparsed: list[str] = []
    section = None  # None (not a rules section) | "own" | "foreign"
    pending: str | None = None
    pending_indent = 0

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        rule = _parse_rule(pending) if section == "own" else None
        if rule is None:
            unparsed.append(pending.strip())
            if section == "own":
                own_unparsed.append(pending.strip())
        else:
            rules.append(rule)
        pending = None

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        if not line[:1].isspace():
            flush()
            m = _SECTION_RE.match(line.rstrip())
            if m is None:
                section = None
            elif (user is None or m.group(1) == user) and (host is None or _same_host(m.group(2), host)):
                section = "own"
            else:
                section = "foreign"
                unparsed.append(line.rstrip())
            continue
        if section is None:
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if pending is not None and indent > pending_indent:
            pending = pending.rstrip() + " " + stripped
            continue
        flush()
        pending, pending_indent = stripped, indent
    flush()
    return ParsedListing(tuple(rules), tuple(unparsed), tuple(own_unparsed))


def _covers(rule: SudoRule, target: str) -> bool:
    return target in rule.runas_users or "ALL" in rule.runas_users


def classify_target(rules: Iterable[SudoRule], target: str) -> str | None:
    """sudoers is LAST-MATCH-WINS and ``sudo -l`` lists rules in order, so walk every rule
    covering ``target`` in listing order (and each rule's items in order):

    * an ``ALL`` item sets the state: ``held`` with NOPASSWD, else ``password-required``;
    * a later restricted item WITHOUT NOPASSWD demotes ``held`` to ``password-required``
      (it may be the last match for the command the engine runs: fail closed);
    * ANY negated item in a covering rule makes the result ``negated``.

    Returns ``held``, ``password-required``, ``restricted-command``, ``negated``, or
    None when no rule covers ``target``."""

    covering = [r for r in rules if _covers(r, target)]
    if not covering:
        return None
    state: str | None = None
    negated = False
    for rule in covering:
        for tags, cmd in rule.items:
            if cmd.startswith("!"):
                negated = True
            elif cmd == "ALL":
                state = "held" if "NOPASSWD" in tags else "password-required"
            elif "NOPASSWD" not in tags and state == "held":
                state = "password-required"
    if negated:
        return "negated"
    return state or "restricted-command"


def rule_classification(rule: SudoRule, target: str) -> str | None:
    """``classify_target`` over the one rule: how it alone relates to running as ``target``."""

    return classify_target([rule], target)


def evaluate_capabilities(
    required: Mapping[str, list[str]],
    operator: OperatorIdentity | None,
    listing: SudoListing | None,
    *,
    operator_error: str | None = None,
    listing_error: str | None = None,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The ``capability`` check result for ``required`` against what the operator holds.

    A ``role:<key>`` owner resolves through ``roles`` (``role_accounts``); an owner
    that resolves to no account name is ``owner-unresolved`` (fail-closed)."""

    evidence: dict[str, Any] = {"required": {k: list(v) for k, v in required.items()}, "held": {}, "missing": {}, "unparsed": []}
    if not required:
        return check("capability", PASS, None, "the plan needs no run-as capability", evidence)
    required, via, unresolved = resolve_targets(required, roles or {})
    if via:
        evidence["resolved"] = via
    if unresolved:
        evidence["unresolved"] = unresolved
        detail = "; ".join(f"owner {t!r} resolves to no account ({', '.join(unresolved[t][:5])})" for t in sorted(unresolved))
        return check("capability", FAIL, "owner-unresolved", detail, evidence)
    if operator is None:
        return check("capability", FAIL, "operator-unobservable", f"operator identity unobservable: {operator_error}", evidence)
    evidence["operator"] = {"user": operator.user, "uid": operator.uid}
    if operator.host is not None:
        evidence["operator"]["host"] = operator.host
    parsed: ParsedListing | None = None
    outcome = None
    if listing is not None:
        outcome = sudo_outcome(listing)
        evidence["sudo_listing"] = outcome
        if outcome == SUDO_LISTED:
            parsed = parse_sudo_listing(listing.stdout, operator.user, operator.host)
            evidence["unparsed"] = list(parsed.unparsed)
            if parsed.own_unparsed:  # Recovery co-review F2: fail CLOSED on any own-section line we cannot read
                n = len(parsed.own_unparsed)
                evidence["sudo_listing"] = outcome = SUDO_UNOBSERVABLE
                listing_error = f"{n} unparsed line(s) in the operator's own section"
                parsed = None
        elif outcome == SUDO_UNOBSERVABLE:
            listing_error = f"sudo -n -l exited {listing.exit_code}"
    no_passwordless = outcome in (SUDO_PASSWORD_REQUIRED, SUDO_NOT_ALLOWED)
    for target in required:
        if target == operator.user or (target == "root" and operator.uid == 0):
            evidence["held"][target] = "self"
            continue
        if no_passwordless:
            evidence["missing"][target] = "no-passwordless-sudo"
            continue
        if parsed is None:
            evidence["missing"][target] = "sudo-listing-unavailable"
            continue
        verdict = classify_target(parsed.rules, target)
        if verdict == "held":
            evidence["held"][target] = "sudo"
        else:
            evidence["missing"][target] = verdict or "no-rule"
    if not evidence["missing"]:
        return check("capability", PASS, None, f"every run-as capability is held ({len(required)})", evidence)
    missing = evidence["missing"]
    if parsed is None and not no_passwordless:
        detail = f"the sudo listing is unavailable ({listing_error}); needed: " + ", ".join(sorted(missing))
        return check("capability", FAIL, "sudo-listing-unavailable", detail, evidence)
    detail = "; ".join(f"run-as {t} ({missing[t]}) for {', '.join(required[t][:5])}" for t in sorted(missing))
    if no_passwordless:
        detail += f" [sudo -n -l: {outcome}]"
    return check("capability", FAIL, "capability-missing", detail, evidence)


# ----------------------------------------------------------------------
# COMPONENT PREFLIGHT HOOK (T-5921; T-3191 viii component items)
# ----------------------------------------------------------------------
#
# SCHEMA (pantheon-germinate-component-preflight/v1), reviewable on its own:
#
# Registration. A component's executor-side module ``<id>_handlers.py`` MAY export
# ``PREFLIGHT``, a callable, beside ``VERIFY`` (engine/components.py; digest-pinned
# like every handlers module, T-5477). Absent = the component has no component
# checks (``"hook": "none"`` in the receipt; never a failure: the hook is optional).
#
# Call. ``PREFLIGHT(ctx)`` once per plan node, only when the plan computed. ``ctx`` is
# a ``PreflightContext`` (frozen):
#   component  str            the registry id
#   leg        str            install | configure | update | remove | repair
#   scope      str            "system" or "seat-<account>"
#   ops        tuple[dict]    the node's operations, ``Operation.to_json_obj()`` form
#   desired    dict[str,str]  the contract-resolved desired values for this node
#   repo_root  str            the planner checkout (read-only data)
#   tier       str            the observation tier (``simulated`` | ``live-readonly``)
#   probe      Probe          the ONLY host view: four read-only observations:
#     probe.path_stat(path)   -> PathStat(kind, uid, gid, mode "0NNN") | None (absent); lstat, never followed
#     probe.file_sha256(path) -> "<64 hex>" | None; a regular file only, never followed, bounded
#     probe.read_json(path)   -> JSON value | None; a regular file only, never followed, bounded
#     probe.account(name)     -> AccountInfo(uid, home) | None
#   Each raises ``Unobservable`` when it cannot observe. Paths must be absolute and
#   normalized. The hook gets no backend, engine, runner or writable handle: it is
#   observation-only and read-only by construction.
#
# Result. A non-empty list of checks, each a dict with exactly these keys
# (``evidence`` may be omitted, meaning {}):
#   id        str   [a-z0-9][a-z0-9-]{0,63}, unique within the component
#   status    str   pass | fail | vacuous   (vacuous = does not apply; never hides a fail)
#   reason    str | None   None on pass; a short code on fail and vacuous (required)
#   detail    str   one human line
#   evidence  dict  JSON-canonical data (no NaN), included verbatim in the receipt
#
# Fold. The receipt's 8th check, ``component``, is FAIL when any sub-check fails
# (reason ``component-check-failed``) or any hook raises, returns a malformed result,
# or cannot be given a port (reason ``component-hook-failed``; fail-closed, like the
# VERIFY alarm); else PASS. Its evidence is
#   {"components": {<id>: {"hook": "registered" | "none", "checks": [<sub-check>...],
#                          "error": "<code>: <message>"   (only on a hook failure)}}}
# Component checks are point-in-time: ``apply --preflight-receipt`` does not re-run
# them (it re-checks only source-hold).

COMPONENT_CHECK_SCHEMA: Final = "pantheon-germinate-component-preflight/v1"
COMPONENT_STATUSES: Final = frozenset({"pass", "fail", "vacuous"})
COMPONENT_CHECK_KEYS: Final = frozenset({"id", "status", "reason", "detail", "evidence"})
_COMPONENT_CHECK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class Probe:
    """The read-only view a component hook gets: four observations, nothing else."""

    __slots__ = ("_port",)

    def __init__(self, port: PreflightPort) -> None:
        self._port = port

    def path_stat(self, path: str) -> PathStat | None:
        return self._port.path_stat(checked_path(path))

    def file_sha256(self, path: str) -> str | None:
        return self._port.file_sha256(checked_path(path))

    def read_json(self, path: str) -> Any:
        return self._port.read_json(checked_path(path))

    def account(self, name: str) -> AccountInfo | None:
        return self._port.account(checked_account(name))


@dataclass(frozen=True)
class PreflightContext:
    component: str
    leg: str
    scope: str
    ops: tuple[Mapping[str, Any], ...]
    desired: Mapping[str, str]
    repo_root: str
    tier: str
    probe: Probe


def component_checks(raw: Any) -> list[dict[str, Any]]:
    """Validate a hook's result against the schema above; ``component-check-malformed`` otherwise."""

    def bad(why: str) -> PreflightError:
        return PreflightError("component-check-malformed", why)

    if not isinstance(raw, (list, tuple)) or not raw:
        raise bad("the hook must return a non-empty list of checks")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or not (set(item) <= COMPONENT_CHECK_KEYS and set(item) >= COMPONENT_CHECK_KEYS - {"evidence"}):
            raise bad(f"check fields must be {sorted(COMPONENT_CHECK_KEYS)}: {item!r:.120}")
        cid, status, reason, detail = item["id"], item["status"], item["reason"], item["detail"]
        evidence = item.get("evidence", {})
        if not (isinstance(cid, str) and _COMPONENT_CHECK_ID_RE.match(cid)) or cid in seen:
            raise bad(f"check id {cid!r} is malformed or repeated")
        seen.add(cid)
        if status not in COMPONENT_STATUSES:
            raise bad(f"{cid}: status {status!r} is not pass, fail or vacuous")
        if (status == "pass") != (reason is None) or (reason is not None and not (isinstance(reason, str) and reason)):
            raise bad(f"{cid}: reason must be None on pass and a code on fail or vacuous")
        if not isinstance(detail, str) or not isinstance(evidence, Mapping):
            raise bad(f"{cid}: detail must be text and evidence a mapping")
        try:
            evidence = json.loads(_canonical(dict(evidence)))
        except (TypeError, ValueError) as exc:
            raise bad(f"{cid}: evidence is not canonical JSON: {exc}") from None
        out.append({"id": cid, "status": status, "reason": reason, "detail": detail, "evidence": evidence})
    return out


def fold_component_checks(per_component: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The receipt's ``component`` check from ``{id: {"hook", "checks", "error"?}}``."""

    evidence = {"components": {cid: dict(v) for cid, v in sorted(per_component.items())}}
    errors = [f"{cid} ({v['error']})" for cid, v in sorted(per_component.items()) if v.get("error")]
    failed = [
        f"{cid}/{c['id']} ({c['reason']})"
        for cid, v in sorted(per_component.items())
        for c in v.get("checks", [])
        if c["status"] == "fail"
    ]
    if errors:
        return check("component", FAIL, "component-hook-failed", "; ".join((errors + failed)[:5]), evidence)
    if failed:
        return check("component", FAIL, "component-check-failed", "; ".join(failed[:5]), evidence)
    subs = [c for v in per_component.values() for c in v.get("checks", [])]
    vacuous = sum(1 for c in subs if c["status"] == "vacuous")
    hookless = sum(1 for v in per_component.values() if v.get("hook") == "none")
    return check(
        "component", PASS, None,
        f"{len(subs) - vacuous} component checks pass, {vacuous} vacuous; {hookless} component(s) without a hook", evidence,
    )


# ----------------------------------------------------------------------
# receipt
# ----------------------------------------------------------------------


def check(cid: str, status: str, reason: str | None, detail: str, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "reason": reason, "detail": detail, "evidence": dict(evidence or {})}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def receipt_digest(body: Mapping[str, Any]) -> str:
    unsealed = {k: v for k, v in body.items() if k != "receipt_digest"}
    return "sha256:" + hashlib.sha256(_canonical(unsealed)).hexdigest()


def seal(body: Mapping[str, Any]) -> dict[str, Any]:
    sealed = dict(body)
    sealed["receipt_digest"] = receipt_digest(sealed)
    return sealed


def verdict_of(checks: Iterable[Mapping[str, Any]]) -> str:
    checks = list(checks)
    return "GO" if checks and all(c["status"] == PASS for c in checks) else "NO-GO"


def validate_receipt(receipt: Any) -> dict[str, Any]:
    """Shape, verdict consistency and digest self-check; ``preflight-receipt-malformed`` otherwise."""

    def bad(why: str) -> PreflightError:
        return PreflightError("preflight-receipt-malformed", why)

    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise bad("receipt fields differ from " + RECEIPT_SCHEMA)
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise bad(f"schema is {receipt['schema']!r}, not {RECEIPT_SCHEMA}")
    if receipt["verdict"] not in VERDICTS:
        raise bad("verdict is neither GO nor NO-GO")
    plan = receipt["plan"]
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise bad("plan fields differ")
    checks = receipt["checks"]
    if not isinstance(checks, list) or not all(isinstance(c, dict) and set(c) == CHECK_KEYS and c["status"] in STATUSES for c in checks):
        raise bad("checks are malformed")
    if [c["id"] for c in checks] != list(CHECK_IDS):
        raise bad("checks are not the expected set, in order")
    if verdict_of(checks) != receipt["verdict"]:
        raise bad("verdict disagrees with the checks")
    try:
        observed, expires = parse_time(receipt["observed_at"]), parse_time(receipt["expires_at"])
    except ValueError:
        raise bad("observed_at/expires_at are not UTC timestamps") from None
    ttl = receipt["ttl_seconds"]
    if not (isinstance(ttl, int) and not isinstance(ttl, bool)) or expires - observed != _dt.timedelta(seconds=ttl):
        raise bad("expires_at is not observed_at + ttl_seconds")
    if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise bad(f"ttl_seconds {ttl} is outside {MIN_TTL_SECONDS}..{MAX_TTL_SECONDS}")
    if not (isinstance(receipt["receipt_digest"], str) and _DIGEST_RE.match(receipt["receipt_digest"])):
        raise bad("receipt_digest is malformed")
    if receipt_digest(receipt) != receipt["receipt_digest"]:
        raise bad("receipt_digest does not match the receipt body (edited?)")
    return receipt


def accepted_gates_token(accept: frozenset[str] | None) -> str | list[str] | None:
    """The receipt's record of ``--accept-gates``: None (not given), ``"all"`` (bare flag) or the sorted ids."""

    if accept is None:
        return None
    return "all" if not accept else sorted(accept)


def receipt_accepted_gates(receipt: Mapping[str, Any]) -> Any:
    for c in receipt["checks"]:
        if c["id"] == "gates":
            return c["evidence"].get("accepted", "<absent>")
    return "<absent>"


def receipt_head(receipt: Mapping[str, Any]) -> str | None:
    for c in receipt["checks"]:
        if c["id"] == "source-hold":
            head = c["evidence"].get("head")
            return head if isinstance(head, str) else None
    return None


def load_receipt_file(path: Path) -> dict[str, Any]:
    """The receipt from a saved ``plan --preflight`` stdout: the last ``{"result": {"receipt": ...}}`` line."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise PreflightError("preflight-receipt-unreadable", f"{Path(path).name!r} cannot be opened (no symlinks): {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PreflightError("preflight-receipt-unreadable", "receipt file is not a regular file")
        data = os.read(fd, MAX_RECEIPT_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RECEIPT_FILE_BYTES:
        raise PreflightError("preflight-receipt-unreadable", "receipt file is oversized")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PreflightError("preflight-receipt-unreadable", "receipt file is not UTF-8") from None
    found = None
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict) and "receipt" in obj["result"]:
            found = obj["result"]["receipt"]
    if found is None:
        raise PreflightError("preflight-receipt-unreadable", "no plan --preflight result line in the file")
    return found


Clock = Callable[[], _dt.datetime]
