"""T-2920 U3b: the SubstratePort protocol, the read-only host OBSERVE layer and put_argv.

Pure tests. Parsers run over RECORDED outputs (fixtures/host_observe.json: real
binaries inside the T-2918 userns sandbox, except the hand-recorded systemctl
show cases, see its provenance). The end-to-end leg drives the REAL RootHelper
through a port double built from the real ``observe`` / ``put_argv`` /
``parse_*`` functions over an in-memory argv-interpreting FakeHost. No test
constructs the host port live or runs a command on the real host.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.host_substrate import ports as P

from .sandbox import launcher
from .sandbox import port as sandbox_port
from .sandbox.launcher import SandboxRunner
from .test_gate_acceptance_sandbox import AVAILABLE as SANDBOX_AVAILABLE
from .test_gate_acceptance_sandbox import WHY as SANDBOX_WHY
from .test_gate_acceptance_sandbox import BOOTSTRAP_OPS, MANIFEST, SOURCES, TEMPLATE

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "host_observe.json").read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]
M = F.load_manifest(MANIFEST)
FRAG = TEMPLATE["path"]
FRAG_SHA = F.sha256_text(F.sudoers_text(TEMPLATE))


def res(rc: int = 0, out: str = "", err: str = "") -> CommandResult:
    return CommandResult(rc, out, err)


def recorded(label: str) -> list[CommandResult]:
    return [CommandResult(r["returncode"], r["stdout"], r["stderr"]) for r in CASES[label]]


def subject_of(label: str) -> str:
    return label.split(":", 1)[1]


def resolved_after() -> dict[str, Any]:
    """What the SIMULATED port holds after the bootstrap ops: subject -> resolved.after."""

    return {F.resolve(f, p, M).subject: F.resolve(f, p, M).after for f, p in BOOTSTRAP_OPS}


# --- recorded real-binary outputs -> the simulated port's value shapes -------------


@pytest.mark.parametrize("label", sorted(k for k in CASES if k.startswith("converged:")))
def test_converged_observations_parse_to_the_simulated_value(label):
    subject = subject_of(label)
    assert [r["argv"] for r in CASES[label]] == P.observe_argv(subject)  # fixtures match today's argv
    value = P.parse_observation(subject, recorded(label))
    assert value == resolved_after()[subject]  # like with like: verify compares the sim's shape
    assert value == FIXTURE["sandbox_get"][subject]  # and the T-2918 sandbox port agrees


@pytest.mark.parametrize("label", sorted(k for k in CASES if k.startswith("fresh:")))
def test_fresh_host_observations(label):
    subject = subject_of(label)
    value = P.parse_observation(subject, recorded(label))
    if subject == "owner_mode:/srv/pantheon/shared":  # the dir exists before convergence
        assert value == {"owner": "root", "group": "root", "mode": "0755"}
    elif subject == "acl:/srv/pantheon/shared":
        assert value == {"entries": ["group::r-x", "other::r-x", "user::rwx"]}
    else:
        assert value is None


def test_a_retired_account_reads_locked_from_the_expiry_field():
    assert P.parse_observation("passwd:alice", recorded("retired:passwd:alice")) == {
        "uid": 3101, "gid": 3101, "home": "/home/alice", "shell": "/bin/bash", "locked": True}


def test_a_setgid_directory_acl_has_no_flags_line_and_parses():
    assert P.parse_observation("acl:/srv/pantheon/mbox-alice", recorded("setgid-dir-acl:/srv/pantheon/mbox-alice")) == {
        "entries": ["group::rwx", "other::---", "user::rwx"]}


def test_a_symlink_acl_argument_is_refused_with_physical():
    """Recorded: getfacl --physical on a symlink prints NOTHING (rc 0); without it,
    getfacl follows the link (review MED). The empty output fails closed."""

    with pytest.raises(P.ObservationError, match="unrecognised ACL"):
        P.parse_observation("acl:/srv/pantheon/link", recorded("symlink-acl:/srv/pantheon/link"))
    assert [r["argv"] for r in CASES["symlink-acl:/srv/pantheon/link"]] == P.observe_argv("acl:/srv/pantheon/link")
    assert "--physical" in P.GETFACL_FLAGS


def test_an_absent_path_acl_is_none():
    assert P.parse_observation("acl:/srv/pantheon/missing", recorded("absent-acl:/srv/pantheon/missing")) is None


def test_a_directory_at_a_file_subject_fails_closed():
    with pytest.raises(P.ObservationError, match="not a regular file"):
        P.parse_observation("file:/etc/systemd/system", recorded("dir-as-file:/etc/systemd/system"))


def test_snapshot_is_passwd_and_group_only_with_exactly_what_preflight_reads():
    """Recovery D15: no shadow enumeration. preflight reads uid/gid/home/shell (helper
    _passwd_entries) and group gid (_group_collision); nothing reads locked."""

    assert P.snapshot_argv() == [[P.GETENT, "passwd"], [P.GETENT, "group"]]
    assert [r["argv"] for r in CASES["snapshot"]] == P.snapshot_argv()
    snap = P.parse_snapshot(recorded("snapshot"))
    assert snap == {
        "passwd:root": {"uid": 0, "gid": 0, "home": "/root", "shell": "/bin/bash"},
        "passwd:daemon": {"uid": 1, "gid": 1, "home": "/usr/sbin", "shell": "/usr/sbin/nologin"},
        "passwd:alice": {"uid": 3101, "gid": 3101, "home": "/home/alice", "shell": "/bin/bash"},
        "group:root": {"gid": 0}, "group:daemon": {"gid": 1}, "group:pmail": {"gid": 3001}, "group:alice": {"gid": 3101},
    }


def test_visudo_verdicts():
    assert P.parse_visudo(recorded("visudo:ok")[0]) == "ok"
    assert P.parse_visudo(recorded("visudo:error")[0]) == "error"
    assert P.parse_visudo(res(0, "parsed OK\n", "warning: unused alias\n")) == "warning"
    assert P.visudo_argv("/run/stage/x") == [["/usr/sbin/visudo", "-cf", "/run/stage/x"]]
    with pytest.raises(P.ObservationError):
        P.visudo_argv("relative/x")


@pytest.mark.parametrize("case, want", [("enabled", True), ("disabled", None), ("not-installed", None)])
def test_systemctl_show_unit_file_state_hand_recorded_tier(case, want):
    """HAND-RECORDED tier (the sandbox fakes systemctl); real-surface check is U4's."""

    r = FIXTURE["systemctl_show_hand_recorded"][case]
    assert P.parse_observation("enabled:t2918.service", [res(r["returncode"], r["stdout"], r["stderr"])]) is want


@pytest.mark.parametrize("case", ["static", "masked"])
def test_systemctl_states_other_than_enabled_or_disabled_fail_closed(case):
    r = FIXTURE["systemctl_show_hand_recorded"][case]
    with pytest.raises(P.ObservationError, match="neither enabled nor disabled"):
        P.parse_observation("enabled:t2918.service", [res(r["returncode"], r["stdout"], r["stderr"])])


# --- fail closed --------------------------------------------------------------------

ENOENT = "stat: cannot statx '/x': No such file or directory\n"
PW = "alice:x:3101:3101::/home/alice:/bin/bash\n"
SH = "alice:x:20753:0:99999:7:::\n"
STAT_DIR = "directory:alice:pmail:750\n"


@pytest.mark.parametrize(
    "subject, results, match",
    [
        ("passwd:alice", [res(0, "alice:x:3101:3101:/home/alice:/bin/bash\n"), res(0, SH)], "malformed"),
        ("passwd:alice", [res(0, "alice:x:31o1:3101::/home/alice:/bin/bash\n"), res(0, SH)], "decimal"),
        ("passwd:alice", [res(0, PW.replace("alice", "alicia", 1)), res(0, SH)], "answered for"),
        ("passwd:alice", [res(0, PW), res(2)], "no shadow entry"),
        ("passwd:alice", [res(2), res(0, SH)], "shadow entry with no passwd"),
        ("passwd:alice", [res(0, PW), res(0, "alice:x:20753:0:99999:7::never:\n")], "field 8"),
        ("passwd:alice", [res(1, "", "getent: bad\n"), res(2)], "exited 1"),
        ("passwd:alice", [res(0, PW + PW), res(0, SH)], "exactly one output line"),
        ("passwd:alice", [res(0, PW.rstrip("\n")), res(0, SH)], "exactly one output line"),
        ("passwd:alice", [res(0, PW)], "expected 2 results"),
        ("group:pmail", [res(0, "pmail:x:3001\n")], "malformed"),
        ("group:pmail", [res(0, "pmail:x:-1:\n")], "decimal"),
        ("member:pmail:alice", [res(3)], "exited 3"),
        ("owner_mode:/srv/x", [res(0, "symbolic link:root:root:777\n")], "symbolic link"),
        ("owner_mode:/srv/x", [res(0, "directory:UNKNOWN:root:750\n")], "resolvable name"),
        ("owner_mode:/srv/x", [res(0, "directory:root:root:7a0\n")], "not octal"),
        ("owner_mode:/srv/x", [res(0, "directory:root:750\n")], "unexpected stat output"),
        ("owner_mode:/srv/x", [res(1, "", "stat: cannot statx '/srv/x': Permission denied\n")], "exited 1"),
        ("owner_mode:/srv/x", [res(1, "", "stat: '/srv/x': Aucun fichier ou dossier de ce type\n")], "exited 1"),  # non-C locale
        ("dir:/srv/x", [res(0, "regular file:root:root:644\n")], "not a directory"),
        ("linger:alice", [res(0, STAT_DIR)], "linger flag is a directory"),
        ("file:/etc/x", [res(0, "regular file:root:root:644\n"), res(0, "a" * 64 + "  /etc/y\n")], "unexpected sha256sum"),
        ("file:/etc/x", [res(0, "regular file:root:root:644\n"), res(0, "A" * 64 + "  /etc/x\n")], "unexpected sha256sum"),
        ("file:/etc/x", [res(0, "regular file:root:root:644\n"), res(0, "\\" + "a" * 64 + "  /etc/x\n")], "unexpected sha256sum"),
        ("file:/etc/x", [res(0, "regular file:root:root:644\n"), res(1, "", "sha256sum: /etc/x: Permission denied\n")], "sha256sum exited"),
        ("file:/etc/x", [res(1, "", ENOENT), res(0, "a" * 64 + "  /etc/x\n")], "stat says absent"),
        ("acl:/srv/x", [res(0, "# flags: -s-\nuser::rwx\ngroup::r-x\nother::---\n\n")], "unrecognised ACL"),
        ("acl:/srv/x", [res(0, "user::rwx\nuser:bob:rwx\t#effective:r-x\nother::---\n\n")], "unrecognised ACL"),
        ("acl:/srv/x", [res(0, "user::rwx\nuser:4242:rwx\nother::---\n\n")], "unrecognised ACL"),
        ("acl:/srv/x", [res(0, "\n")], "unrecognised ACL"),
        ("acl:/srv/x", [res(1, "", "getfacl: /srv/x: Operation not supported\n")], "getfacl exited"),
        ("enabled:x.service", [res(1, "", "Failed to connect to bus\n")], "systemctl show exited"),
        ("enabled:x.service", [res(0, "enabled\nenabled\n")], "exactly one output line"),
    ],
)
def test_unrecognised_output_fails_closed(subject, results, match):
    with pytest.raises(P.ObservationError, match=match):
        P.parse_observation(subject, results)


@pytest.mark.parametrize(
    "results, match",
    [
        ([res(0, PW + PW), res(0, "")], "duplicate"),
        ([res(0, PW), res(0, "pmail:x:3001:\npmail:x:3001:\n")], "duplicate"),
        ([res(2), res(0, "")], "exited 2"),
        ([res(0, PW), res(0, ""), res(0, "")], "expected 2 results"),
        ([res(0, "alice:x:3101\n"), res(0, "")], "malformed"),
    ],
)
def test_snapshot_fails_closed(results, match):
    with pytest.raises(P.ObservationError, match=match):
        P.parse_snapshot(results)


@pytest.mark.parametrize(
    "subject",
    ["passwd:-alice", "passwd:Alice", "passwd:", "group:a b", "member:pmail", "member:pmail:-x", "file:etc/x",
     "file:/etc/../x", "file:/", "file:/etc/x/", "acl:/srv/x\n", "enabled:-x.service", "enabled:a/b.service",
     "staged:passwd:alice", "bogus:x", "nocolon", 123, "file://etc/x", "file:/etc//x", "acl:/srv/../etc",
     "dir:/srv/pantheon/..", "enabled:..", "enabled:."],
)
def test_subjects_are_shape_checked_before_any_argv_exists(subject):
    with pytest.raises(P.ObservationError):
        P.observe_argv(subject)


# --- binaries and the Recovery argv table (pin) -----------------------------------------

#: The binaries an observe argv may start with; systemctl and visudo only with
#: their read-only verbs (show / -cf).
READ_ONLY_BINARIES = frozenset({P.GETENT, P.STAT, P.SHA256SUM, P.GETFACL, P.SYSTEMCTL, P.VISUDO})
READ_ONLY_VERB = {P.SYSTEMCTL: "show", P.VISUDO: "-cf"}

SAMPLES = {  # subject -> {operand: placeholder}
    "passwd:alice": {"alice": "<A>"},
    "group:pmail": {"pmail": "<G>"},
    "member:pmail:alice": {"pmail": "<G>"},
    "owner_mode:/srv/pantheon/shared": {"/srv/pantheon/shared": "<P>"},
    "dir:/srv/pantheon/mbox-alice": {"/srv/pantheon/mbox-alice": "<P>"},
    "file:/etc/systemd/system/t2918.service": {"/etc/systemd/system/t2918.service": "<P>"},
    "acl:/srv/pantheon/shared": {"/srv/pantheon/shared": "<P>"},
    "linger:alice": {P.LINGER_DIR + "alice": P.LINGER_DIR + "<A>"},
    "enabled:t2918.service": {"t2918.service": "<U>"},
}


def _template(argv, operands):
    return tuple(operands.get(a, a) for a in argv)


def _emitted_observe_templates():
    assert {s.split(":", 1)[0] for s in SAMPLES} == P.OBSERVE_KINDS  # every kind is rendered
    return {_template(a, ops) for s, ops in SAMPLES.items() for a in P.observe_argv(s)}


def _emitted_dark_templates():
    frag = F.resolve("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": FRAG_SHA}, M)
    return ({tuple(a) for a in P.snapshot_argv()}
            | {_template(a, {"/staged/x": "<STAGED>"}) for a in P.visudo_argv("/staged/x")}
            | {_template(a, {FRAG + P.SUDOERS_TMP_SUFFIX: "<P>" + P.SUDOERS_TMP_SUFFIX})
               for a in P.sudoers_tmp_cleanup_argv(frag)})


CLEANUP_CLASS = "mutating, failure-path cleanup, ruled T-5652"


def test_the_argv_table_pins_every_argv_the_observe_layer_can_emit():
    """mason condition (1): every emittable argv is read-only AND matches the table exactly."""

    table = P.OBSERVE_ARGV_TABLE
    templates = [row[0] for row in table]
    assert len(templates) == len(set(templates))
    observe_rows = {row[0] for row in table if row[4] == "observe()"}
    dark_rows = {row[0] for row in table if row[4] == "HostSubstratePort (dark)"}
    assert observe_rows | dark_rows == set(templates)
    assert _emitted_observe_templates() == observe_rows
    assert _emitted_dark_templates() == dark_rows
    mutating = [row for row in table if not row[1].startswith("read-only")]
    assert [(row[0], row[1], row[3], row[4]) for row in mutating] == [
        ((P.UNLINK, "<P>" + P.SUDOERS_TMP_SUFFIX), CLEANUP_CLASS, ("sudoers_fragment.ensure",), "HostSubstratePort (dark)")]
    for template, classification, purpose, kinds, _reach in table:
        if classification == CLEANUP_CLASS:
            continue
        assert template[0] in READ_ONLY_BINARIES, template
        assert template[0] not in READ_ONLY_VERB or template[1] == READ_ONLY_VERB[template[0]], template
        assert classification.startswith("read-only") and purpose and kinds


def test_observe_is_marked_callable_outside_the_dark_gate_for_recovery():
    """mason condition (3)."""

    assert {row[4] for row in P.OBSERVE_ARGV_TABLE} == {"observe()", "HostSubstratePort (dark)"}
    assert "OUTSIDE the dark gate" in P.observe.__doc__
    src = (PKG / "ports.py").read_text(encoding="utf-8")
    assert "#: callable OUTSIDE the dark gate" in src  # the OBSERVE_ARGV_TABLE comment


def test_observe_runs_exactly_the_observe_argv_through_the_injected_runner():
    runner = FakeCommandRunner(default=res(2))
    assert P.observe(runner, "passwd:alice") is None
    assert [list(c) for c in runner.calls] == P.observe_argv("passwd:alice")


PORTS_MOD = "tools.germinate.host_substrate.ports"


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _observe_callers(tree: ast.AST, rel: Path) -> list[str]:
    """Every reference to host_substrate.ports.observe in one module: an imported or
    star-imported name, an alias attribute, or a fully-qualified attribute chain."""

    in_pkg = rel.parent == Path("tools/germinate/host_substrate")
    aliases, names, star = set(), set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == PORTS_MOD and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == PORTS_MOD or (in_pkg and node.level == 1 and mod == "ports"):
                names |= {a.asname or a.name for a in node.names if a.name == "observe"}
                star = star or any(a.name == "*" for a in node.names)
            if mod == "tools.germinate.host_substrate" or (in_pkg and node.level == 1 and mod == ""):
                aliases |= {a.asname or a.name for a in node.names if a.name == "ports"}
                star = star or any(a.name == "*" for a in node.names)
    hits = [f"imports observe as {n}" for n in sorted(names)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "observe":
            owner = _dotted(node.value)
            if owner in aliases or owner == PORTS_MOD or (owner or "").endswith("host_substrate.ports"):
                hits.append(f"{owner}.observe at line {node.lineno}")
        if star and isinstance(node, ast.Name) and node.id == "observe":
            hits.append(f"star-imported observe at line {node.lineno}")
    return hits


def _repo_python_files():
    skip = {".git", "__pycache__", ".claude", "node_modules", ".venv", "venv"}
    for path in sorted(REPO.rglob("*.py")):
        if not skip.intersection(path.relative_to(REPO).parts):
            yield path


def test_no_module_outside_ports_and_its_tests_calls_observe():
    """mason condition (2), widened per review: the WHOLE repo's .py files, aliases,
    fully-qualified chains and star imports. A new caller is a D15 change (recovery)."""

    offenders, scanned = [], 0
    tests_dir = PKG / "tests"
    for path in _repo_python_files():
        if path == PKG / "ports.py" or tests_dir in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
        if "observe" not in text or ("ports" not in text and "host_substrate" not in text):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue  # not python this interpreter parses; it cannot import ports either
        offenders += [f"{path.relative_to(REPO)}: {hit}" for hit in _observe_callers(tree, path.relative_to(REPO))]
    assert scanned > 1000 and offenders == []


@pytest.mark.parametrize(
    "src, rel",
    [
        ("from tools.germinate.host_substrate import ports as Q\nQ.observe(r, 's')\n", "tools/x.py"),
        ("from tools.germinate.host_substrate import ports\nports.observe(r, 's')\n", "x.py"),
        ("import tools.germinate.host_substrate.ports\ntools.germinate.host_substrate.ports.observe(r, 's')\n", "x.py"),
        ("import tools.germinate.host_substrate.ports as P2\nP2.observe(r, 's')\n", "x.py"),
        ("from tools.germinate.host_substrate.ports import *\nobserve(r, 's')\n", "x.py"),
        ("from tools.germinate.host_substrate.ports import observe as o\n", "x.py"),
        ("from .ports import observe\n", "tools/germinate/host_substrate/x.py"),
        ("from . import ports\nports.observe(r, 's')\n", "tools/germinate/host_substrate/x.py"),
    ],
    ids=["alias", "module-name", "fully-qualified", "import-as", "star", "from-import-as", "relative", "relative-module"],
)
def test_the_scan_would_catch_a_caller(src, rel):
    assert _observe_callers(ast.parse(src), Path(rel)) != []


def test_the_scan_ignores_handler_observe_methods():
    src = "class H:\n    def observe(self, c, s):\n        return 1\nh.observe(c, s)\n"
    assert _observe_callers(ast.parse(src), Path("tools/x.py")) == []


def test_observation_binaries_are_final_absolute_and_match_the_sandbox_port():
    assert (P.GETENT, P.STAT, P.SHA256SUM, P.GETFACL) == (
        sandbox_port.GETENT, sandbox_port.STAT, sandbox_port.SHA256SUM, sandbox_port.GETFACL)
    tree = ast.parse((PKG / "ports.py").read_text(encoding="utf-8"))
    finals = {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
              and isinstance(n.annotation, ast.Name) and n.annotation.id == "Final"}
    assert {"GETENT", "STAT", "SHA256SUM", "GETFACL", "LINGER_DIR", "STAT_FORMAT", "OBSERVE_ARGV_TABLE"} <= finals
    for b in READ_ONLY_BINARIES:
        assert b.startswith("/usr/")


def test_staged_prefix_matches_the_helper():
    assert P.STAGED_PREFIX == Hp.STAGED_PREFIX


# --- the protocol ----------------------------------------------------------------------

PROTOCOL = ("get", "put", "visudo_check", "snapshot", "path_type", "path_stat")


@pytest.mark.parametrize("cls", [P.SimulatedSubstratePort, sandbox_port.SandboxSubstratePort, P.HostSubstratePort],
                         ids=lambda c: c.__name__)
def test_every_port_satisfies_the_substrate_port_protocol(cls):
    for name in PROTOCOL:
        want = list(inspect.signature(getattr(P.SubstratePort, name)).parameters)
        assert list(inspect.signature(getattr(cls, name)).parameters) == want, (cls, name)


def test_root_helper_is_typed_against_the_protocol():
    assert inspect.signature(Hp.RootHelper.__init__).parameters["port"].annotation == "SubstratePort"
    assert {n for n in dir(P.SubstratePort) if not n.startswith("_")} == set(PROTOCOL)


# --- dark ---------------------------------------------------------------------------------


def test_the_host_port_still_refuses_with_a_manifest_and_staged_sources():
    runner = FakeCommandRunner()
    with pytest.raises(P.LiveActuationDisabled, match="dark"):
        P.HostSubstratePort(runner, M, {FRAG_SHA: "/staged/x"})
    assert runner.calls == []


# --- put_argv: only the Recovery-approved argv --------------------------------------------

STAGED = {FRAG_SHA: "/staged/frag", **{sha: f"/staged/{sha}" for sha in SOURCES}}


def _r(family, params):
    return F.resolve(family, params, M)


@pytest.mark.parametrize("family, params", BOOTSTRAP_OPS, ids=[f for f, _ in BOOTSTRAP_OPS])
def test_put_argv_from_absent_is_exactly_apply_argv(family, params):
    r = _r(family, params)
    current = {"owner": "root", "group": "root", "mode": "0755"} if family == "owner_mode.set" else None
    staged = STAGED.get((r.after or {}).get("sha256", "")) if isinstance(r.after, dict) else None
    assert P.put_argv(r.subject, current, r.after, M, STAGED) == P.apply_argv(r, staged_source=staged)


def test_put_argv_account_transitions():
    ensure = _r("account.ensure", {"account": "alice"})
    retire = _r("account.retire", {"account": "alice"})
    s = ensure.subject
    assert P.put_argv(s, None, retire.after, M, {}) == P.apply_argv(ensure) + P.apply_argv(retire)
    assert P.put_argv(s, ensure.after, retire.after, M, {}) == P.apply_argv(retire)
    assert P.put_argv(s, retire.after, ensure.after, M, {}) == P.rollback_argv(retire, ensure.after)
    assert P.put_argv(s, ensure.after, None, M, {}) == P.rollback_argv(ensure, None)
    assert P.put_argv(s, None, None, M, {}) == []
    assert P.put_argv(s, ensure.after, ensure.after, M, {}) == []
    with pytest.raises(P.ArgvGap, match="manifest identity"):
        P.put_argv(s, None, {**ensure.after, "home": "/home/elsewhere"}, M, {})


def test_put_argv_file_restores_and_retires():
    unit = _r("unit.system.install", {"unit": "t2918.service", "state": "present", "sha256": next(iter(SOURCES))})
    frag = _r("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": FRAG_SHA})
    assert P.put_argv(unit.subject, unit.after, None, M, STAGED) == P.apply_argv(
        _r("unit.system.install", {"unit": "t2918.service", "state": "absent", "sha256": unit.after["sha256"]}))
    assert P.put_argv(frag.subject, frag.after, None, M, STAGED) == P.apply_argv(
        _r("sudoers_fragment.retire", {"template": "alice-probe"}))
    assert P.put_argv(frag.subject, None, None, M, STAGED) == []
    prior = {"sha256": "f" * 64, "mode": "0440", "owner": "root"}
    assert P.put_argv(frag.subject, frag.after, prior, M, {"f" * 64: "/staged/prior"})[0][-2:] == ["/staged/prior", FRAG + ".pantheon-tmp"]


@pytest.mark.parametrize(
    "subject, current, value, match",
    [
        ("group:pmail", {"gid": 3001}, None, "removes a group"),
        ("group:pmail", {"gid": 3999}, {"gid": 3001}, "changes a gid"),
        ("dir:/srv/pantheon/mbox-alice", {"owner": "alice", "group": "pmail", "mode": "2770"}, None, "gate 34"),
        ("acl:/srv/pantheon/shared", {"entries": ["user::rwx"]}, None, "non-empty ACL"),
        ("acl:/srv/pantheon/shared", None, {"entries": []}, "non-empty ACL"),
        ("owner_mode:/srv/pantheon/shared", None, None, "owner/group/mode"),
        ("owner_mode:/srv/pantheon/shared", None, {"owner": "-x", "group": "root", "mode": "0750"}, "owner/group/mode"),
        ("file:/etc/systemd/system/t2918.service", None, {"sha256": "e" * 64, "mode": "0644", "owner": "root"}, "no staged"),
        ("file:/usr/local/sbin/pantheon_oom_shield.sh", None, {"sha256": "e" * 64}, "verify-only"),
        ("passwd:mallory", None, None, "not in the manifest"),
        ("member:pmail:alice", None, "yes", "True or None"),
        ("enabled:nope.service", None, True, "enableable"),
        ("staged:passwd:alice", None, None, "staged"),
        ("file:relative", None, None, "absolute"),
    ],
)
def test_put_argv_refuses_what_no_approved_argv_covers(subject, current, value, match):
    with pytest.raises(P.ArgvGap, match=match):
        P.put_argv(subject, current, value, M, STAGED)


def test_put_argv_refuses_a_bad_staged_path():
    frag = _r("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": FRAG_SHA})
    with pytest.raises(P.ArgvGap, match="absolute"):
        P.put_argv(frag.subject, None, frag.after, M, {FRAG_SHA: "staged/frag"})


def test_put_argv_only_ever_emits_apply_or_rollback_binaries():
    approved = {P.GROUPADD, P.USERADD, P.USERMOD, P.USERDEL, P.GROUPDEL, P.GPASSWD, P.SETFACL, P.CHOWN, P.CHMOD,
                P.INSTALL, P.UNLINK, P.MV, P.VISUDO, P.SUDO, P.LOGINCTL, P.SYSTEMCTL}
    for family, params in BOOTSTRAP_OPS:
        r = _r(family, params)
        for argv in P.put_argv(r.subject, None, r.after, M, STAGED):
            assert argv[0] in approved


def test_manifest_subjects_cover_every_resolved_subject_and_legacy_files():
    subjects = P.manifest_subjects(M)
    assert set(resolved_after()) <= subjects
    legacy = F.load_manifest({**MANIFEST, "legacy_requirements": [{"path": "/usr/bin/earlyoom", "note": "x"}]})
    assert "file:/usr/bin/earlyoom" in P.manifest_subjects(legacy)
    assert all(s.split(":", 1)[0] in P.OBSERVE_KINDS for s in subjects)
    for s in subjects:
        P.observe_argv(s)  # every manifest subject has a shape-valid observe argv


# --- end to end: the REAL RootHelper over a port double built from the real functions --


class FakeHost:
    """An in-memory host that interprets the approved argv and answers the observe
    argv in the recorded output formats (C locale). Strict: unknown argv raise."""

    def __init__(self, staged: dict[str, bytes]):
        self.passwd: dict[str, dict[str, Any]] = {"root": {"uid": 0, "gid": 0, "home": "/root", "shell": "/bin/bash"}}
        self.expiry: dict[str, str] = {"root": ""}
        self.groups: dict[str, dict[str, Any]] = {"root": {"gid": 0, "members": []}}
        # T-5657: a correctly installed host's ancestors are root-owned, non-g/o-writable dirs.
        self.files: dict[str, dict[str, Any]] = {p: self._dir("root", "root", "755")
                                                 for p in ("/", "/srv", "/srv/pantheon", "/srv/pantheon/shared")}
        self.files.update({p: {"type": "regular file", "owner": "root", "group": "root", "mode": "600", "data": d}
                           for p, d in staged.items()})
        self.linger: set[str] = set()
        self.enabled: set[str] = set()
        self.visudo: dict[str, tuple[int, str]] = {}
        self.log: list[tuple[str, ...]] = []
        self.fail: set[tuple[str, ...]] = set()

    @staticmethod
    def _dir(owner, group, mode):
        return {"type": "directory", "owner": owner, "group": group, "mode": mode, "acl": None}

    def run(self, argv):
        argv = list(argv)
        self.log.append(tuple(argv))
        if tuple(argv) in self.fail:
            return res(1, "", "injected failure\n")
        b, a = argv[0], argv[1:]
        if b == P.GETENT:
            return self._getent(*a)
        if b == P.STAT and a[:2] == [P.STAT_FORMAT, "--"]:
            path = a[2]
            if path.startswith(P.LINGER_DIR):
                if path[len(P.LINGER_DIR):] not in self.linger:
                    return res(1, "", f"stat: cannot statx '{path}': No such file or directory\n")
                return res(0, "regular empty file:root:root:644\n")
            f = self.files.get(path)
            if f is None:
                return res(1, "", f"stat: cannot statx '{path}': No such file or directory\n")
            return res(0, f"{f['type']}:{f['owner']}:{f['group']}:{f['mode']}\n")
        if b == P.SHA256SUM and a[0] == "--":
            f = self.files.get(a[1])
            if f is None or f["type"] != "regular file":
                return res(1, "", f"sha256sum: {a[1]}: No such file or directory\n")
            return res(0, f"{hashlib.sha256(f['data']).hexdigest()}  {a[1]}\n")
        if b == P.GETFACL and a[:-1] == [*P.GETFACL_FLAGS, "--"]:
            f = self.files.get(a[-1])
            if f is None:
                return res(1, "", f"getfacl: {a[-1]}: No such file or directory\n")
            entries = f.get("acl") or self._base_acl(f["mode"])
            return res(0, "".join(e + "\n" for e in entries) + "\n")
        if b == P.SYSTEMCTL and a[:4] == ["show", P.UNIT_FILE_STATE, "--value", "--"]:
            unit = a[4]
            if unit in self.enabled:
                return res(0, "enabled\n")
            return res(0, "disabled\n" if f"/etc/systemd/system/{unit}" in self.files else "\n")
        return self._mutate(b, a)

    @staticmethod
    def _setfacl_set(entries):
        """Like real ``setfacl --set``: with a named entry and no mask, it COMPUTES one
        (the union of the group class), which is the T-5652 drift."""

        out = list(entries)
        for prefix in ("", "default:"):
            group_class = [e for e in entries if e.startswith(f"{prefix}group:")
                           or (e.startswith(f"{prefix}user:") and not e.startswith(f"{prefix}user::"))]
            named = [e for e in group_class if not e.startswith(f"{prefix}group::")]
            if named and not any(e.startswith(f"{prefix}mask::") for e in entries):
                perms = [e.rsplit(":", 1)[1] for e in group_class]
                out.append(f"{prefix}mask::" + "".join(c if any(p[i] == c for p in perms) else "-" for i, c in enumerate("rwx")))
        return out

    def _no_follow(self, path):
        f = self.files[path]
        if f["type"] == "symbolic link":  # setfacl --physical / chown -h act on the link itself
            raise AssertionError(f"FakeHost: a no-follow mutator reached the symlink {path}")
        return f

    @staticmethod
    def _perm_digit(perms):
        return sum(v for c, v in zip(perms, (4, 2, 1)) if c != "-")

    @staticmethod
    def _base_acl(mode):
        bits = lambda d: "".join(c if int(d) & v else "-" for c, v in (("r", 4), ("w", 2), ("x", 1)))  # noqa: E731
        m = mode.zfill(3)[-3:]
        return [f"user::{bits(m[0])}", f"group::{bits(m[1])}", f"other::{bits(m[2])}"]

    def _getent(self, db, key=None):
        if db == "passwd":
            rows = {n: f"{n}:x:{p['uid']}:{p['gid']}::{p['home']}:{p['shell']}" for n, p in self.passwd.items()}
        elif db == "shadow":
            rows = {n: f"{n}:x:20753:0:99999:7::{self.expiry[n]}:" for n in self.passwd}
        else:
            rows = {n: f"{n}:x:{g['gid']}:{','.join(g['members'])}" for n, g in self.groups.items()}
        if key is None:
            return res(0, "".join(r + "\n" for r in rows.values()))
        return res(0, rows[key] + "\n") if key in rows else res(2)

    def _mutate(self, b, a):  # noqa: C901 - one branch per approved binary
        ok = res(0)
        if b == P.GROUPADD:
            gid, name = int(a[1]), a[2]
            if name in self.groups or any(g["gid"] == gid for g in self.groups.values()):
                return res(9, "", "groupadd: exists\n")
            self.groups[name] = {"gid": gid, "members": []}
        elif b == P.USERADD:
            opt = lambda flag: a[a.index(flag) + 1]  # noqa: E731
            name = a[-1]
            self.passwd[name] = {"uid": int(opt("--uid")), "gid": int(opt("--gid")), "home": opt("--home-dir"),
                                 "shell": opt("--shell")}
            self.expiry[name] = ""
            self.files[opt("--home-dir")] = self._dir(name, name, "750")
        elif b == P.USERMOD:
            self.expiry[a[-1]] = "1" if a[0] == "--lock" else ""
        elif b == P.USERDEL:
            del self.passwd[a[0]], self.expiry[a[0]]
        elif b == P.GROUPDEL:
            del self.groups[a[0]]
        elif b == P.GPASSWD:
            members = self.groups[a[2]]["members"]
            if a[0] == "--add" and a[1] not in members:
                members.append(a[1])
            elif a[0] == "--delete":
                members.remove(a[1])
        elif b == P.SETFACL:
            # T-5653: only the ruled shape is interpreted; an old-shape argv fails loudly.
            assert a[:2] == ["--physical", "--set"] and len(a) == 4, a
            f = self._no_follow(a[3])
            f["acl"] = self._setfacl_set(a[2].split(","))
            # POSIX (T-5653 + T-5658): the ACCESS base entries ARE the mode bits. user:: writes
            # the owner bits, other:: the other bits, and the group bits come from mask:: when
            # there is one (computed or declared), else from group::.
            base = {t: next((e.rsplit(":", 1)[1] for e in f["acl"] if e.startswith(f"{t}::")), None)
                    for t in ("user", "group", "mask", "other")}
            m = f["mode"].zfill(4)
            digits = [m[-3], m[-2], m[-1]]
            for i, perms in enumerate((base["user"], base["mask"] or base["group"], base["other"])):
                if perms is not None:
                    digits[i] = str(self._perm_digit(perms))
            f["mode"] = oct(int(m[:-3] + "".join(digits), 8))[2:]
        elif b == P.CHOWN:
            assert a[0] == "-h" and len(a) == 3, a
            owner, group = a[1].split(":")
            self._no_follow(a[2]).update(owner=owner, group=group)
        elif b == P.CHMOD:
            f = self.files[a[1]]  # chmod FOLLOWS a symlink (no no-follow on Linux)
            f["mode"] = oct(int(a[0], 8))[2:]
            if f.get("acl"):  # POSIX (T-5653 + T-5658): chmod rewrites the ACCESS base entries
                user, group, other = (e.rsplit(":", 1)[1] for e in self._base_acl(f["mode"]))
                has_mask = any(e.startswith("mask::") for e in f["acl"])
                new = {"user::": user, "other::": other, ("mask::" if has_mask else "group::"): group}
                f["acl"] = [next((k + v for k, v in new.items() if e.startswith(k)), e) for e in f["acl"]]
        elif b == P.INSTALL and a[0] == "-d":
            self.files[a[-1]] = self._dir(a[2], a[4], oct(int(a[6], 8))[2:])
        elif b == P.INSTALL:
            src, dest = a[-2], a[-1]
            self.files[dest] = {"type": "regular file", "owner": a[1], "group": a[3], "mode": oct(int(a[5], 8))[2:],
                                "data": self.files[src]["data"]}
        elif b == P.UNLINK:
            if a[0] not in self.files:
                return res(1, "", f"unlink: cannot unlink '{a[0]}': No such file or directory\n")
            del self.files[a[0]]
        elif b == P.MV:
            self.files[a[-1]] = self.files.pop(a[-2])
        elif b == P.VISUDO and a[0] == "-cf":
            rc, err = self.visudo.get(a[1], (0, ""))
            return res(rc, f"{a[1]}: parsed OK\n" if rc == 0 else "", err)
        elif b == P.VISUDO and a == ["-c"]:
            return res(0, "/etc/sudoers: parsed OK\n")
        elif b == P.SUDO and a[:2] == ["-l", "-U"]:
            return res(0, f"User {a[2]} may run the following commands\n")
        elif b == P.LOGINCTL:
            (self.linger.add if a[0] == "enable-linger" else self.linger.discard)(a[1])
        elif b == P.SYSTEMCTL and a[0] in ("enable", "disable"):
            (self.enabled.add if a[0] == "enable" else self.enabled.discard)(a[1])
        elif b == P.SYSTEMCTL and a in (["daemon-reload"], ["reload", "dbus.service"]):
            pass
        else:
            raise AssertionError(f"FakeHost: unexpected argv {[b, *a]}")
        return ok


class FakeBackedPort:
    """A SubstratePort double. Its get/put/visudo_check/snapshot bodies are the dark
    HostSubstratePort's VERBATIM (``P.`` prefixes aside), pinned by
    ``test_the_port_double_bodies_equal_the_host_port_bodies``; only the constructor
    differs (no dark gate: it is handed a FakeHost runner)."""

    def __init__(self, runner, manifest, staged_sources):
        self._runner = runner
        self._manifest = manifest
        self._subjects = P.manifest_subjects(manifest)
        self._staged_sources = dict(staged_sources or {})
        self._staged: dict[str, Any] = {}

    def get(self, subject: str) -> Any:
        if subject.startswith(P.STAGED_PREFIX):
            return copy.deepcopy(self._staged.get(subject))
        if subject not in self._subjects:
            raise P.ObservationError(f"{subject}: not a subject this manifest names")
        return P.observe(self._runner, subject)

    def put(self, subject: str, value: Any) -> None:
        if subject.startswith(P.STAGED_PREFIX):
            if value is None:
                self._staged.pop(subject, None)
            else:
                self._staged[subject] = copy.deepcopy(value)
            return
        argvs = P.put_argv(subject, self.get(subject), value, self._manifest, self._staged_sources)
        for index, argv in enumerate(argvs):
            result = self._runner.run(list(argv))
            if result.returncode != 0:
                cleaned = all(P.cleanup_ok(self._runner.run(c))
                              for c in P.failure_cleanup_argv(subject, value, self._manifest, argvs, index))
                raise RuntimeError(f"{argv[0]} exited {result.returncode}" + ("" if cleaned else "; temp cleanup failed"))

    def visudo_check(self, sha256: str) -> str:
        staged = self._staged_sources.get(sha256)
        if staged is None:
            return "error"
        (argv,) = P.visudo_argv(staged)
        return P.parse_visudo(self._runner.run(argv))

    def snapshot(self) -> dict[str, Any]:
        return P.parse_snapshot([self._runner.run(argv) for argv in P.snapshot_argv()])

    def path_type(self, path: str) -> str | None:
        (argv,) = P.path_type_argv(path)
        return P.parse_path_type(path, self._runner.run(argv))

    def path_stat(self, path: str) -> tuple[str, str, str, str] | None:
        (argv,) = P.path_stat_argv(path)
        return P.parse_path_stat(path, self._runner.run(argv))


class _StripP(ast.NodeTransformer):
    def visit_Attribute(self, node):  # noqa: N802 - ast API
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "P":
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _method_bodies(cls_node):
    out = {}
    for fn in cls_node.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in PROTOCOL:
            out[fn.name] = ast.dump(ast.Module(body=[_StripP().visit(stmt) for stmt in fn.body], type_ignores=[]))
    return out


def test_the_port_double_bodies_equal_the_host_port_bodies():
    """Review LOW: the double is not a hand copy that can drift. A shared executor helper
    was declined on purpose: it would put a mutating runner loop OUTSIDE the dark gate."""

    def cls(path, name):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)

    # T-2920 U4b: the live bodies moved VERBATIM into the _LiveSubstrateOps mixin;
    # HostSubstratePort itself now defines only its (unchanged) dark-gated __init__
    host = _method_bodies(cls(PKG / "ports.py", "_LiveSubstrateOps"))
    double = _method_bodies(cls(Path(__file__), "FakeBackedPort"))
    assert set(host) == set(double) == set(PROTOCOL)
    assert host == double
    host_cls = cls(PKG / "ports.py", "HostSubstratePort")
    assert [n.name for n in host_cls.body if isinstance(n, ast.FunctionDef)] == ["__init__"]
    assert [b.id for b in host_cls.bases] == ["_LiveSubstrateOps"]


def _pair():
    data = {FRAG_SHA: F.sudoers_text(TEMPLATE).encode("utf-8"), **SOURCES}
    staged = {sha: f"/run/pantheon/stage/{sha}" for sha in data}
    host = FakeHost({staged[sha]: d for sha, d in data.items()})
    fake = FakeBackedPort(host, M, staged)
    return host, fake, staged


def _converge(h):
    done = []
    for family, params in BOOTSTRAP_OPS:
        step = h.admit(family, params, Hp.ACTIVATE)
        before = h.apply(step)
        assert h.verify(step).ok, (family, h.verify(step).detail)
        done.append((step, before))
    return done


def test_root_helper_converges_a_fake_host_identically_to_the_simulated_port():
    host, fake, _ = _pair()
    sim = P.SimulatedSubstratePort()
    _converge(Hp.RootHelper(fake, M, Hp.BOOTSTRAP))
    _converge(Hp.RootHelper(sim, M, Hp.BOOTSTRAP))
    for subject in sorted(P.manifest_subjects(M)):
        assert fake.get(subject) == sim.get(subject), subject
    ev = Hp.RootHelper(fake, M, Hp.BOOTSTRAP).verify(Hp.RootHelper(fake, M, Hp.BOOTSTRAP).admit(
        "sudoers_fragment.ensure", {"template": "alice-probe", "sha256": FRAG_SHA}, Hp.ACTIVATE))
    assert ev.ok and ev.data["sudo_l_negative"] == "/bin/sh"
    assert (P.SUDO, "-l", "-U", "alice") in host.log  # the approved sequence ran in full


def test_restore_puts_back_every_preimage_an_approved_argv_covers():
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    gaps = []
    for step, before in reversed(_converge(h)):
        try:
            h.restore(step, before)
        except P.ArgvGap:
            gaps.append(step.resolved.family)
            continue
        assert fake.get(step.subject) == before, step.subject
    assert gaps == ["mailbox_dir.ensure", "group.ensure"]  # no family removes a dir (gate 34) or a group


def test_retire_and_its_restore_round_trip_through_observation():
    _host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    h.apply(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE))
    step = h.admit("account.retire", {"account": "alice"}, Hp.ACTIVATE)
    before = h.apply(step)
    assert fake.get("passwd:alice")["locked"] is True and h.verify(step).ok
    h.restore(step, before)
    assert fake.get("passwd:alice")["locked"] is False


@pytest.mark.parametrize("rc, err, verdict", [(1, "syntax error\n", "error"), (0, "warning: unused\n", "warning")])
def test_a_visudo_refusal_on_the_fake_host_changes_nothing(rc, err, verdict):
    host, fake, staged = _pair()
    host.visudo[staged[FRAG_SHA]] = (rc, err)
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    h.apply(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE))
    step = h.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": FRAG_SHA}, Hp.ACTIVATE)
    with pytest.raises(Hp.PreflightRefused, match=f"visudo -cf reports {verdict}"):
        h.apply(step)
    assert fake.get(step.subject) is None


def test_snapshot_collision_preflight_matches_the_simulated_port():
    host, fake, _ = _pair()
    host.groups["pmail"] = {"gid": 3999, "members": []}
    sim = P.SimulatedSubstratePort({"subjects": {"group:pmail": {"gid": 3999}}})
    for port in (fake, sim):
        h = Hp.RootHelper(port, M, Hp.BOOTSTRAP)
        ev = h.preflight(h.admit("group.ensure", {"group": "pmail"}, Hp.ACTIVATE))
        assert (ev.ok, ev.detail) == (False, "group CONFLICT: pmail exists with gid 3999, expected 3001")


def test_account_drift_is_refused_on_the_fake_host():
    host, fake, _ = _pair()
    host.passwd["alice"] = {"uid": 3101, "gid": 3101, "home": "/home/elsewhere", "shell": "/bin/bash"}
    host.expiry["alice"] = ""
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    assert "DRIFT" in h.preflight(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE)).detail


def test_a_scheduled_stage_is_refused_and_never_touches_the_fake_host():
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.SCHEDULED)
    with pytest.raises(Hp.ScheduledRootRefused):
        h.admit("account.ensure", {"account": "alice"}, Hp.STAGE)
    assert fake.get("staged:passwd:alice") is None
    assert all(argv[0] in READ_ONLY_BINARIES for argv in host.log)  # only observation ran


# --- T-5652 (1): the sudoers temp-fragment failure-path cleanup (Recovery ruling) ---------------

TMP = FRAG + P.SUDOERS_TMP_SUFFIX
FRAG_PARAMS = {"template": "alice-probe", "sha256": FRAG_SHA}


def _sudoers_attempt(host, fake):
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    h.apply(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE))
    return h, h.admit("sudoers_fragment.ensure", FRAG_PARAMS, Hp.ACTIVATE)


def _unlinks(host):
    return [argv for argv in host.log if argv[0] == P.UNLINK]


def test_a_failing_visudo_cf_leaves_no_temp_fragment():
    host, fake, _ = _pair()
    host.visudo[TMP] = (1, f"{TMP}:1:3: syntax error\n")
    h, step = _sudoers_attempt(host, fake)
    with pytest.raises(RuntimeError, match="visudo exited 1$"):
        h.apply(step)
    assert TMP not in host.files and FRAG not in host.files
    assert _unlinks(host) == [(P.UNLINK, TMP)]


def test_a_failing_mv_leaves_no_temp_fragment():
    host, fake, _ = _pair()
    host.fail.add((P.MV, "-f", "--", TMP, FRAG))
    h, step = _sudoers_attempt(host, fake)
    with pytest.raises(RuntimeError, match="mv exited 1$"):
        h.apply(step)
    assert TMP not in host.files and FRAG not in host.files
    assert _unlinks(host) == [(P.UNLINK, TMP)]


def test_an_already_absent_temp_fragment_is_a_no_op_success():
    class VanishingTmpHost(FakeHost):
        def run(self, argv):
            if list(argv) == [P.VISUDO, "-cf", TMP]:
                self.files.pop(TMP, None)
                self.log.append(tuple(argv))
                return res(1, "", "syntax error\n")
            return super().run(argv)

    data = {FRAG_SHA: F.sudoers_text(TEMPLATE).encode("utf-8"), **SOURCES}
    staged = {sha: f"/run/pantheon/stage/{sha}" for sha in data}
    host = VanishingTmpHost({staged[sha]: d for sha, d in data.items()})
    h, step = _sudoers_attempt(host, FakeBackedPort(host, M, staged))
    with pytest.raises(RuntimeError, match="visudo exited 1$"):  # no "temp cleanup failed": ENOENT is success
        h.apply(step)
    assert _unlinks(host) == [(P.UNLINK, TMP)]
    assert P.cleanup_ok(res(1, "", f"unlink: cannot unlink '{TMP}': No such file or directory\n"))
    assert P.cleanup_ok(res(0))
    assert not P.cleanup_ok(res(1, "", f"unlink: cannot unlink '{TMP}': Permission denied\n"))


def test_a_final_visudo_c_failure_stays_on_the_restore_path():
    host, fake, _ = _pair()
    host.fail.add((P.VISUDO, "-c"))
    h, step = _sudoers_attempt(host, fake)
    with pytest.raises(RuntimeError, match="visudo exited 1$"):
        h.apply(step)
    assert _unlinks(host) == []  # no temp cleanup after the mv landed
    assert FRAG in host.files and TMP not in host.files
    host.fail.clear()
    h.restore(step, None)  # the existing restore path removes it
    assert FRAG not in host.files


def test_the_cleanup_is_only_ever_the_exact_temp_path_after_install_and_before_the_mv():
    frag = F.resolve("sudoers_fragment.ensure", FRAG_PARAMS, M)
    for family, params in [*BOOTSTRAP_OPS, ("sudoers_fragment.retire", {"template": "alice-probe"})]:
        r = F.resolve(family, params, M)
        current = {"owner": "root", "group": "root", "mode": "0755"} if family == "owner_mode.set" else (
            frag.after if family == "sudoers_fragment.retire" else None)
        argvs = P.put_argv(r.subject, current, r.after, M, STAGED)
        for index in range(-1, len(argvs) + 1):
            cleanup = P.failure_cleanup_argv(r.subject, r.after, M, argvs, index)
            if family == "sudoers_fragment.ensure" and index in (1, 2):
                assert cleanup == [[P.UNLINK, TMP]]
            else:
                assert cleanup == [], (family, index)
    assert P.sudoers_tmp_cleanup_argv(frag) == [["/usr/bin/unlink", "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp"]]


@pytest.mark.parametrize(
    "resolved",
    [
        F.Resolved("sudoers_fragment.retire", f"file:{FRAG}", None, TEMPLATE),
        F.Resolved("sudoers_fragment.ensure", "file:/etc/sudoers.d/pantheon-*", {"sha256": FRAG_SHA}, TEMPLATE),
        F.Resolved("sudoers_fragment.ensure", "file:/etc/passwd", {"sha256": FRAG_SHA}, TEMPLATE),
        F.Resolved("sudoers_fragment.ensure", "file:/etc/sudoers.d/x/y", {"sha256": FRAG_SHA}, TEMPLATE),
    ],
    ids=["retire", "glob", "outside-sudoers-d", "nested"],
)
def test_the_cleanup_builder_refuses_anything_but_a_literal_sudoers_d_temp(resolved):
    with pytest.raises(ValueError):
        P.sudoers_tmp_cleanup_argv(resolved)


# --- T-5652 (2): named ACL entries REQUIRE mask:: in the manifest -------------------------

_BASE = ["user::rwx", "group::r-x", "other::---"]
_DBASE = ["default:user::rwx", "default:group::r-x", "default:other::---"]


def _acl(entries):
    doc = copy.deepcopy(MANIFEST)
    doc["paths"]["/srv/pantheon/shared"]["acl"] = entries
    return F.load_manifest(doc)


@pytest.mark.parametrize(
    "entries, missing",
    [
        ([*_BASE, "user:alice:rwx"], "mask::"),
        ([*_BASE, "group:pmail:r-x"], "mask::"),
        ([*_BASE, "mask::r-x", "group:pmail:r-x", *_DBASE, "default:user:alice:rwx"], "default:mask::"),
        ([*_BASE, "mask::r-x", "group:pmail:r-x", *_DBASE, "default:group:pmail:r-x"], "default:mask::"),
        ([*_BASE, "default:mask::r-x", *_DBASE, "group:pmail:r-x", "default:group:pmail:r-x"], "mask::"),
    ],
    ids=["access-user", "access-group", "default-user", "default-group", "default-mask-only"],
)
def test_named_acl_entries_without_their_mask_are_refused(entries, missing):
    with pytest.raises(F.ManifestError, match=f"named entries need {missing} "):
        _acl(entries)


@pytest.mark.parametrize(
    "entries",
    [
        _BASE,
        [*_BASE, *_DBASE],
        [*_BASE, "user:alice:rwx", "mask::r-x"],  # T-5653: the access mask == mode 0750's group bits
        [*_BASE, "group:pmail:r-x", "mask::r-x", *_DBASE, "default:user:alice:rwx", "default:mask::rwx"],
    ],
    ids=["base-only", "base+default", "access-user+mask", "both-prefixes+masks"],
)
def test_acl_with_masks_for_its_named_entries_is_accepted(entries):
    _acl(entries)


def test_a_mask_without_named_entries_is_now_refused():
    """T-5653 (Recovery, D15) reverses the T-5652-era 'a lone mask is allowed' case."""
    with pytest.raises(F.ManifestError, match="a lone mask:: "):
        _acl([*_BASE, "mask::r-x"])


def _raw_manifest(mode, acl):
    """A Manifest that BYPASSES load_manifest (to show why the mask/mode rule exists)."""
    doc = copy.deepcopy(MANIFEST)
    doc["paths"]["/srv/pantheon/shared"].update(mode=mode, acl=acl)
    return F.Manifest(doc, "raw")


_SHARED_OPS = [("owner_mode.set", {"path": "/srv/pantheon/shared"}), ("acl.converge", {"path": "/srv/pantheon/shared"})]


@pytest.mark.parametrize("order", [_SHARED_OPS, _SHARED_OPS[::-1]], ids=["owner_mode-then-acl", "acl-then-owner_mode"])
def test_a_matching_mask_and_mode_converge_on_a_posix_coupled_host(order):
    """T-5653: FakeHost models the POSIX coupling (setfacl's access mask writes the mode's
    group bits; chmod rewrites the mask). An accepted manifest converges in either order:
    observe after apply equals desired for BOTH subjects."""
    acl = [*_BASE, "group:pmail:r-x", "mask::r-x"]
    m = _acl(acl)
    host, _fake, staged = _pair()
    fake = FakeBackedPort(host, m, staged)
    h = Hp.RootHelper(fake, m, Hp.BOOTSTRAP)
    for family, params in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(family, params, Hp.ACTIVATE))
    steps = [h.admit(f, p, Hp.ACTIVATE) for f, p in order]
    for step in steps:
        h.apply(step)
    for step in steps:
        assert h.verify(step).ok and fake.get(step.subject) == step.resolved.after, step.subject


def test_a_mismatched_mask_and_mode_never_converge_which_is_why_it_is_refused():
    acl = [*_BASE, "group:pmail:r-x", "mask::rwx"]
    with pytest.raises(F.ManifestError, match="group bits"):
        _acl(acl)
    m = _raw_manifest("0750", acl)
    host, _fake, staged = _pair()
    fake = FakeBackedPort(host, m, staged)
    h = Hp.RootHelper(fake, m, Hp.BOOTSTRAP)
    for family, params in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(family, params, Hp.ACTIVATE))
    steps = [h.admit(f, p, Hp.ACTIVATE) for f, p in _SHARED_OPS]
    for step in steps:
        h.apply(step)
    assert not all(h.verify(step).ok for step in steps)  # the two families fight forever


# --- T-5658 (Recovery, D15): base ACL entries must agree with the declared mode ---------------
#
# POSIX couples the ACCESS base entries to the mode bits: user:: IS the owner bits, other::
# IS the other bits, and with no named entry (so no mask) group:: IS the group bits.
# setfacl --set writes them into the mode and chmod writes the mode back into them, so a
# manifest that contradicts its own mode makes acl.converge and owner_mode.set fight forever.


def _base_acl_manifest(mode, acl):
    doc = copy.deepcopy(MANIFEST)
    doc["paths"]["/srv/pantheon/shared"].update(mode=mode, acl=acl)
    return F.load_manifest(doc)


@pytest.mark.parametrize(
    "mode, acl, match",
    [
        ("0750", ["user::rw-", "group::r-x", "other::---"], r"user::rw- must equal the mode's owner bits rwx"),
        ("0750", ["user::rwx", "group::r-x", "other::r--"], r"other::r-- must equal the mode's other bits ---"),
        ("0750", ["user::rwx", "group::rwx", "other::---"], r"group::rwx must equal the mode's group bits r-x"),
        ("0750", ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x", "mask::r-x", "user::r--"],
         r"'user::rwx' and 'user::r--' both set user:: .*T-5665"),  # T-5665 refuses it first
        ("0750", ["user::rw-", "group::rwx", "other::---", "group:pmail:r-x", "mask::r-x"],
         r"user::rw- must equal the mode's owner bits rwx"),
        ("0740", ["user::rwx", "group::r--", "other::---", "user:alice:rwx"], r"named entries need mask:: .*T-5652"),
    ],
    ids=["user-mismatch", "other-mismatch", "group-mismatch-no-named", "conflicting-duplicate-user",
         "user-mismatch-with-named", "named-without-mask"],
)
def test_base_acl_entries_contradicting_the_mode_are_refused(mode, acl, match):
    with pytest.raises(F.ManifestError, match=match):
        _base_acl_manifest(mode, acl)


def test_the_base_entry_refusal_is_distinct_from_the_mask_refusals():
    with pytest.raises(F.ManifestError, match="T-5658") as base:
        _base_acl_manifest("0750", ["user::rw-", "group::r-x", "other::---"])
    assert "T-5652" not in str(base.value) and "T-5653" not in str(base.value)
    with pytest.raises(F.ManifestError, match="T-5653") as mask:
        _base_acl_manifest("0750", ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x", "mask::rwx"])
    assert "T-5658" not in str(mask.value)


_ACCEPTED_BASE = [
    ("0750", ["user::rwx", "group::r-x", "other::---"]),
    ("0750", ["user::rwx", "group::rwx", "other::---", "group:pmail:r-x", "mask::r-x"]),  # named: group:: free
    ("0750", ["user::rwx", "group::r-x", "other::---", "default:user::rw-", "default:group::---",
              "default:other::r--"]),  # the default: prefix is unconstrained
    ("2770", ["user::rwx", "group::---", "other::---", "user:alice:rwx", "mask::rwx"]),
]


@pytest.mark.parametrize("mode, acl", _ACCEPTED_BASE, ids=["base-only", "named-group-free", "default-free", "setgid"])
def test_accepted_base_entries_load(mode, acl):
    _base_acl_manifest(mode, acl)


def _shared_rounds(m, order, rounds=2):
    host, _fake, staged = _pair()
    fake = FakeBackedPort(host, m, staged)
    h = Hp.RootHelper(fake, m, Hp.BOOTSTRAP)
    for family, params in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(family, params, Hp.ACTIVATE))
    steps = [h.admit(f, p, Hp.ACTIVATE) for f, p in order]
    for _ in range(rounds):
        for step in steps:
            h.apply(step)
    return h, fake, steps


@pytest.mark.parametrize("order", [_SHARED_OPS, _SHARED_OPS[::-1]], ids=["owner_mode-then-acl", "acl-then-owner_mode"])
@pytest.mark.parametrize("mode, acl", _ACCEPTED_BASE, ids=["base-only", "named-group-free", "default-free", "setgid"])
def test_accepted_base_entries_converge_on_a_base_coupled_host(mode, acl, order):
    m = _base_acl_manifest(mode, acl)
    h, fake, steps = _shared_rounds(m, order, rounds=1)
    for step in steps:
        assert h.verify(step).ok and fake.get(step.subject) == step.resolved.after, (step.subject, fake.get(step.subject))


_MISMATCHED_BASE = [
    ("0750", ["user::rw-", "group::r-x", "other::---"]),
    ("0750", ["user::rwx", "group::r-x", "other::r--"]),
    ("0750", ["user::rwx", "group::rwx", "other::---"]),
    ("0740", ["user::rwx", "group::r--", "other::---", "user:alice:rwx"]),  # Recovery: setfacl computes mask, 0740 -> 770
]


@pytest.mark.parametrize("order", [_SHARED_OPS, _SHARED_OPS[::-1]], ids=["owner_mode-then-acl", "acl-then-owner_mode"])
@pytest.mark.parametrize("mode, acl", _MISMATCHED_BASE, ids=["user", "other", "group-no-named", "named-no-mask"])
def test_a_raw_base_entry_mismatch_never_converges_which_is_why_it_is_refused(mode, acl, order):
    with pytest.raises(F.ManifestError):
        _base_acl_manifest(mode, acl)
    h, _fake, steps = _shared_rounds(_raw_manifest(mode, acl), order)
    assert not all(h.verify(step).ok for step in steps)  # the two families fight forever


def test_setfacl_computes_the_union_mask_into_the_mode_as_recovery_verified():
    """Recovery, real binaries (T-5658): setfacl --set u::rwx,g::r--,o::---,u:nobody:rwx on 0740 -> mask::rwx, 770."""
    host, _fake, _staged = _pair()
    host.files["/srv/x"] = FakeHost._dir("root", "root", "740")
    host.run([P.SETFACL, "--physical", "--set", "user::rwx,group::r--,other::---,user:nobody:rwx", "/srv/x"])
    assert host.files["/srv/x"]["mode"] == "770" and "mask::rwx" in host.files["/srv/x"]["acl"]


# --- T-5653 (2): the symlink guard on the real-host argv tier (FakeHost + port double) -----

_GUARDED = [
    ("acl.converge", {"path": "/srv/pantheon/shared"}, "/srv/pantheon/shared"),
    ("owner_mode.set", {"path": "/srv/pantheon/shared"}, "/srv/pantheon/shared"),
    ("mailbox_dir.ensure", {"mailbox": "alice"}, "/srv/pantheon/mbox-alice"),
    ("mailbox_dir.retire", {"mailbox": "alice"}, "/srv/pantheon/mbox-alice"),
]
_MUTATORS = {P.SETFACL, P.CHOWN, P.CHMOD, P.INSTALL, P.UNLINK, P.MV}


def _plant_symlink(host, path):
    host.files[path] = {"type": "symbolic link", "owner": "root", "group": "root", "mode": "777", "acl": None}


def _row4_walk(path):
    """T-5657: the row-4 stats the guard runs: every ancestor (``/`` down), then the subject."""
    return {(P.STAT, P.STAT_FORMAT, "--", p) for p in [*Hp._ancestors(path), path]}


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_a_symlink_subject_is_refused_before_any_apply_argv(family, params, path):
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    for f, p in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(f, p, Hp.ACTIVATE))
    _plant_symlink(host, path)
    mark = len(host.log)
    step = h.admit(family, params, Hp.ACTIVATE)
    ev = h.preflight(step)
    assert not ev.ok and ev.data["code"] == Hp.SymlinkSubjectRefused.code and "symbolic link" in ev.detail
    with pytest.raises(Hp.SymlinkSubjectRefused, match=f"{path} is a symbolic link"):
        h.apply(step)
    emitted = host.log[mark:]
    assert emitted and set(emitted) == _row4_walk(path)  # row-4 stats only (T-5657 walk, then the subject)
    assert not {argv[0] for argv in emitted} & _MUTATORS


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_a_symlink_swapped_in_after_apply_blocks_restore(family, params, path):
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    for f, p in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(f, p, Hp.ACTIVATE))
    if family == "mailbox_dir.retire":
        h.apply(h.admit("mailbox_dir.ensure", {"mailbox": "alice"}, Hp.ACTIVATE))
    step = h.admit(family, params, Hp.ACTIVATE)
    before = h.apply(step)
    _plant_symlink(host, path)
    mark = len(host.log)
    with pytest.raises(Hp.SymlinkSubjectRefused):
        h.restore(step, before)
    assert not {argv[0] for argv in host.log[mark:]} & _MUTATORS


@pytest.mark.parametrize(
    "stdout, rc, stderr, want",
    [
        ("symbolic link:root:root:777\n", 0, "", "symbolic link"),
        ("directory:alice:pmail:2770\n", 0, "", "directory"),
        ("regular file:root:root:644\n", 0, "", "regular file"),
        ("", 1, "stat: cannot statx '/srv/x': No such file or directory\n", None),
    ],
    ids=["symlink", "directory", "file", "absent"],
)
def test_parse_path_type_is_typed(stdout, rc, stderr, want):
    assert P.path_type_argv("/srv/x") == [[P.STAT, P.STAT_FORMAT, "--", "/srv/x"]]
    assert P.path_type_argv("/srv/x")[0] == P.observe_argv("owner_mode:/srv/x")[0]  # no new argv
    assert P.parse_path_type("/srv/x", res(rc, stdout, stderr)) == want


@pytest.mark.parametrize(
    "stdout, rc, stderr",
    [
        ("", 1, "stat: cannot statx '/srv/x': Datei oder Verzeichnis nicht gefunden\n"),
        ("", 1, "stat: cannot statx '/srv/x': Permission denied\n"),
        ("symbolic link:root:root\n", 0, ""),
        (":root:root:777\n", 0, ""),
        ("directory:root:root:rwx\n", 0, ""),
        ("directory:root:root:755", 0, ""),
        ("directory:root:root:755\nx\n", 0, ""),
        ("symbolische Verknüpfung:root:root:777\n", 0, ""),
        ("Verzeichnis:root:root:755\n", 0, ""),
        ("fifo:root:root:644\n", 0, ""),
        ("Symbolic Link:root:root:777\n", 0, ""),
    ],
    ids=["other-locale", "eacces", "three-fields", "empty-type", "bad-mode", "no-newline", "two-lines",
         "localized-symlink-type", "localized-dir-type", "unlisted-c-type", "case-variant"],
)
def test_parse_path_type_fails_closed(stdout, rc, stderr):
    with pytest.raises(P.ObservationError):
        P.parse_path_type("/srv/x", res(rc, stdout, stderr))


def test_the_file_type_allowlist_is_exactly_the_c_locale_kinds():
    """Review MED-1: an allowlist (gnulib file_type() C-locale texts), never a denylist."""
    assert P.STAT_FILE_TYPES == {"regular file", "regular empty file", "directory", "symbolic link"}


@pytest.mark.parametrize("subject", ["owner_mode:/srv/pantheon/shared", "dir:/srv/pantheon/mbox-alice",
                                     "file:/etc/systemd/system/t2918.service", "linger:alice"])
@pytest.mark.parametrize("ftype", ["symbolische Verknüpfung", "lien symbolique", "fifo", "socket"])
def test_the_stat_observe_parse_refuses_an_unrecognised_file_type(subject, ftype):
    """Review MED-1, applied to U3b's _stat parse (owner_mode / dir / file / linger observe)."""
    results = [res(0, f"{ftype}:root:root:755\n")]
    if subject.startswith("file:"):
        results.append(res(0, "a" * 64 + "  " + subject.split(":", 1)[1] + "\n"))
    with pytest.raises(P.ObservationError, match="unrecognised file type"):
        P.parse_observation(subject, results)


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_a_localized_symlink_type_is_refused_with_no_mutator_emitted(family, params, path):
    """Review MED-1 end to end: a non-C-locale runner prints a localized %F for a symlink;
    the preflight must REFUSE (not read it as 'not a symlink'), and no mutator runs."""
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    for f, p in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(f, p, Hp.ACTIVATE))
    _plant_symlink(host, path)
    host.files[path]["type"] = "symbolische Verknüpfung"
    mark = len(host.log)
    step = h.admit(family, params, Hp.ACTIVATE)
    ev = h.preflight(step)
    assert not ev.ok and "unrecognised file type" in ev.detail
    with pytest.raises(Hp.PreflightRefused, match="unrecognised file type"):
        h.apply(step)
    emitted = host.log[mark:]
    assert emitted and set(emitted) == _row4_walk(path)
    assert not {argv[0] for argv in emitted} & _MUTATORS


# --- T-5657 (Recovery, D15): the ancestor walk on the real-host argv tier (FakeHost + double) --


def _walk_pair():
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    for f, p in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(f, p, Hp.ACTIVATE))
    return host, fake, h


def test_ancestors_are_every_directory_from_the_root_down_to_the_parent():
    assert Hp._ancestors("/srv/pantheon/shared") == ["/", "/srv", "/srv/pantheon"]
    assert Hp._ancestors("/srv") == ["/"]


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_the_walk_stats_every_ancestor_top_down_then_the_subject_with_the_row4_stat(family, params, path):
    host, _fake, h = _walk_pair()
    mark = len(host.log)
    assert h.preflight(h.admit(family, params, Hp.ACTIVATE)).ok
    stats = [argv for argv in host.log[mark:] if argv[0] == P.STAT]
    assert stats == [(P.STAT, P.STAT_FORMAT, "--", p) for p in ("/", "/srv", "/srv/pantheon", path)]


_DIR = FakeHost._dir
_ANCESTOR_DEFECTS = [
    ("/srv", None, "is absent"),
    ("/srv", {"type": "symbolic link", "owner": "root", "group": "root", "mode": "777", "acl": None},
     "is a symbolic link"),
    ("/srv", {"type": "regular file", "owner": "root", "group": "root", "mode": "644", "acl": None, "data": b""},
     "is a 'regular file', not a directory"),
    ("/srv", _DIR("bob", "root", "755"), "is owned by 'bob', not 'root'"),
    ("/srv", _DIR("root", "root", "775"), "is group-writable"),
    ("/srv", _DIR("root", "root", "757"), "is other-writable"),
    ("/srv", _DIR("root", "root", "1777"), "is group-writable"),  # sticky is no exemption
    ("/srv", _DIR("UNKNOWN", "root", "755"), "could not be observed"),  # stat's text for an unmapped uid
    ("/srv", {**_DIR("root", "root", "755"), "type": "Verzeichnis"}, "could not be observed"),  # localized %F
    ("/", _DIR("nobody", "nogroup", "755"), "is owned by 'nobody', not 'root'"),
    ("/srv/pantheon", _DIR("root", "root", "770"), "is group-writable"),
]


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
@pytest.mark.parametrize("ancestor, record, why", _ANCESTOR_DEFECTS,
                         ids=["absent", "symlink", "regular-file", "non-root-owner", "g+w", "o+w", "sticky-1777",
                              "unmapped-owner", "localized-type", "root-dir-owner", "parent-g+w"])
def test_an_untrusted_ancestor_is_refused_loudly_before_any_mutator(family, params, path, ancestor, record, why):
    host, _fake, h = _walk_pair()
    if record is None:
        del host.files[ancestor]
    else:
        host.files[ancestor] = record
    mark = len(host.log)
    step = h.admit(family, params, Hp.ACTIVATE)
    ev = h.preflight(step)
    assert not ev.ok and ev.data == {"code": Hp.AncestorRefused.code, "ancestor": ancestor}, ev.detail
    assert f"ancestor {ancestor} {why}" in ev.detail
    with pytest.raises(Hp.AncestorRefused, match=f"ancestor {re.escape(ancestor)} "):
        h.apply(step)
    assert not {argv[0] for argv in host.log[mark:]} & _MUTATORS
    assert set(host.log[mark:]) <= _row4_walk(path)


def test_an_ancestor_refusal_is_a_preflight_refusal_with_its_own_code():
    assert issubclass(Hp.AncestorRefused, Hp.PreflightRefused)
    assert Hp.AncestorRefused.code == "ANCESTOR_REFUSED" != Hp.SymlinkSubjectRefused.code


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_the_walk_runs_in_the_stage_phase_too(family, params, path):
    host, _fake, h = _walk_pair()
    host.files["/srv"] = _DIR("root", "root", "777")
    ev = h.preflight(h.admit(family, params, Hp.STAGE))
    assert not ev.ok and ev.data["code"] == Hp.AncestorRefused.code


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_an_ancestor_made_writable_after_apply_blocks_restore_and_rollback(family, params, path):
    host, fake, h = _walk_pair()
    if family == "mailbox_dir.retire":
        h.apply(h.admit("mailbox_dir.ensure", {"mailbox": "alice"}, Hp.ACTIVATE))
    step = h.admit(family, params, Hp.ACTIVATE)
    before = h.apply(step)
    host.files["/srv/pantheon"]["mode"] = "777"
    mark = len(host.log)
    with pytest.raises(Hp.AncestorRefused, match="ancestor /srv/pantheon is group-writable"):
        h.restore(step, before)
    with pytest.raises(Hp.AncestorRefused):
        h.guarded_put(step.subject, before)
    assert not {argv[0] for argv in host.log[mark:]} & _MUTATORS


def test_no_cache_hides_a_mid_run_ancestor_change():
    """No per-run cache (DECISION): the SAME helper sees an ancestor change between ops."""
    host, _fake, h = _walk_pair()
    h.apply(h.admit("owner_mode.set", {"path": "/srv/pantheon/shared"}, Hp.ACTIVATE))
    host.files["/srv/pantheon"]["owner"] = "bob"
    with pytest.raises(Hp.AncestorRefused, match="ancestor /srv/pantheon is owned by 'bob'"):
        h.apply(h.admit("mailbox_dir.ensure", {"mailbox": "alice"}, Hp.ACTIVATE))


# --- the designated-owner rule (Recovery D15: approved with two additions) ---------------------


def _nested_pair(owner="alice"):
    """``/srv/pantheon/team/shared`` owned by ``owner``: its parent ``/srv/pantheon/team``
    is the one directory the designated owner may own."""
    doc = copy.deepcopy(MANIFEST)
    doc["paths"] = {"/srv/pantheon/team/shared": {"owner": owner, "group": "pmail", "mode": "0750"}}
    m = F.Manifest(doc, "raw") if owner not in doc["accounts"] and owner != "root" else F.load_manifest(doc)
    host, _fake, staged = _pair()
    host.files["/srv/pantheon/team"] = _DIR("root", "root", "755")
    host.files["/srv/pantheon/team/shared"] = _DIR("root", "root", "755")
    h = Hp.RootHelper(FakeBackedPort(host, m, staged), m, Hp.BOOTSTRAP)
    for f, p in BOOTSTRAP_OPS[:3]:
        h.apply(h.admit(f, p, Hp.ACTIVATE))
    return host, h, h.admit("owner_mode.set", {"path": "/srv/pantheon/team/shared"}, Hp.ACTIVATE)


def test_the_designated_owner_comes_from_the_subjects_own_manifest_entry():
    _host, h, _step = _nested_pair()
    assert h.designated_owner("owner_mode:/srv/pantheon/team/shared") == "alice"
    assert h.designated_owner("acl:/srv/pantheon/team/shared") == "alice"
    assert h.designated_owner("owner_mode:/srv/pantheon/not-in-manifest") is None
    full = Hp.RootHelper(P.SimulatedSubstratePort(), M, Hp.BOOTSTRAP)
    assert full.designated_owner("dir:/srv/pantheon/mbox-alice") == "alice"


def test_the_leafs_parent_may_be_owned_by_the_designated_owner():
    host, h, step = _nested_pair()
    host.files["/srv/pantheon/team"] = _DIR("alice", "pmail", "755")
    assert h.preflight(step).ok
    h.apply(step)
    assert h.verify(step).ok


@pytest.mark.parametrize("mode, why", [("775", "group-writable"), ("757", "other-writable"), ("1777", "group-writable")])
def test_a_designated_owner_parent_still_may_not_be_group_or_other_writable(mode, why):
    """Recovery D15 addition (1): D may write the parent; group and other may not."""
    host, h, step = _nested_pair()
    host.files["/srv/pantheon/team"] = _DIR("alice", "pmail", mode)
    ev = h.preflight(step)
    assert not ev.ok and f"ancestor /srv/pantheon/team is {why}" in ev.detail


def test_a_third_party_owning_the_leafs_parent_is_refused():
    host, h, step = _nested_pair()
    host.files["/srv/pantheon/team"] = _DIR("bob", "pmail", "755")
    ev = h.preflight(step)
    assert not ev.ok and "ancestor /srv/pantheon/team is owned by 'bob', not 'root' or 'alice'" in ev.detail


def test_the_designated_owner_may_not_own_any_ancestor_above_the_leafs_parent():
    host, h, step = _nested_pair()
    host.files["/srv/pantheon"] = _DIR("alice", "pmail", "755")
    host.files["/srv/pantheon/team"] = _DIR("alice", "pmail", "755")
    ev = h.preflight(step)
    assert not ev.ok and "ancestor /srv/pantheon is owned by 'alice', not 'root'" in ev.detail


def test_an_undeclared_designated_owner_counts_as_root_only():
    """Recovery D15 addition (2): D must be a DECLARED manifest account. A raw manifest (past
    load_manifest, which already refuses an undeclared owner) names ``carol``."""
    host, h, step = _nested_pair(owner="carol")
    assert h.designated_owner(step.subject) is None
    host.files["/srv/pantheon/team"] = _DIR("carol", "pmail", "755")
    ev = h.preflight(step)
    assert not ev.ok and "ancestor /srv/pantheon/team is owned by 'carol', not 'root'" in ev.detail


def test_a_root_designated_owner_adds_no_exception():
    """Recovery D15 addition (2): D must be NON-ROOT; a root-owned subject allows only root."""
    _host, h, step = _nested_pair(owner="root")
    assert h.designated_owner(step.subject) is None


# --- the "/" operand (Recovery D15: approved, walk-only) ---------------------------------------


def test_the_ancestor_stat_is_the_row4_template_and_slash_is_a_walk_only_operand():
    for p in ("/srv", "/srv/pantheon"):
        assert P.path_stat_argv(p) == [[P.STAT, P.STAT_FORMAT, "--", p]]
        assert P.path_stat_argv(p)[0] == P.observe_argv("owner_mode:" + p)[0]  # no new argv template
    assert P.path_stat_argv("/") == [[P.STAT, P.STAT_FORMAT, "--", "/"]]
    for bad in ("rel", "/srv/../etc", "/srv//x", "-x", "//", "/srv/", "", "/\n"):
        with pytest.raises(P.ObservationError):
            P.path_stat_argv(bad)


def test_a_subject_of_slash_is_still_refused():
    """Recovery D15: only the walk accepts "/"; the SUBJECT validator still refuses it."""
    for subject in ("owner_mode:/", "acl:/", "dir:/", "file:/"):
        with pytest.raises(P.ObservationError):
            P.observe_argv(subject)
    with pytest.raises(P.ObservationError):
        P.path_type_argv("/")
    with pytest.raises(P.ObservationError):
        P._path("subject", "/")


@pytest.mark.parametrize(
    "stdout, rc, stderr, want",
    [
        ("directory:root:root:755\n", 0, "", ("directory", "root", "root", "0755")),
        ("directory:root:root:1777\n", 0, "", ("directory", "root", "root", "1777")),
        ("symbolic link:root:root:777\n", 0, "", ("symbolic link", "root", "root", "0777")),
        ("", 1, "stat: cannot statx '/srv': No such file or directory\n", None),
    ],
    ids=["dir", "sticky", "symlink", "absent"],
)
def test_parse_path_stat_is_typed(stdout, rc, stderr, want):
    assert P.parse_path_stat("/srv", res(rc, stdout, stderr)) == want


@pytest.mark.parametrize(
    "stdout, rc, stderr",
    [
        ("directory:UNKNOWN:root:755\n", 0, ""),
        ("directory:root:root:7a5\n", 0, ""),
        ("Verzeichnis:root:root:755\n", 0, ""),
        ("directory:root:755\n", 0, ""),
        ("", 1, "stat: cannot statx '/srv': Permission denied\n"),
        ("", 1, "stat: Datei oder Verzeichnis nicht gefunden\n"),
    ],
    ids=["unmapped-owner", "bad-mode", "localized-type", "short", "eacces", "localized-enoent"],
)
def test_parse_path_stat_fails_closed(stdout, rc, stderr):
    with pytest.raises(P.ObservationError):
        P.parse_path_stat("/srv", res(rc, stdout, stderr))


@pytest.mark.nested_namespace
def test_the_sandbox_root_is_ns_root_owned_so_the_walk_sees_a_trusted_root(real_sb):
    """T-5657: enter.sh chroots into a namespace-root tmpfs (and aborts otherwise), so the
    REAL row-4 stat of "/" reads root, and a real g+w ancestor is refused."""
    real_sb.reset()
    try:
        port = sandbox_port.SandboxSubstratePort(real_sb, M, SOURCES)
        assert port.path_stat("/") == ("directory", "root", "root", "0755")
        real_sb.mkdir("/srv/pantheon/shared")
        h = Hp.RootHelper(port, M, Hp.BOOTSTRAP)
        assert h.ancestor_walk("owner_mode:/srv/pantheon/shared") is None
        assert port.runner.run([P.CHMOD, "0777", "/srv/pantheon"]).returncode == 0
        ev = h.ancestor_walk("owner_mode:/srv/pantheon/shared")
        assert ev is not None and "ancestor /srv/pantheon is group-writable" in ev.detail
    finally:
        real_sb.reset()


def test_path_type_argv_refuses_a_non_normalized_path():
    for bad in ("rel", "/srv/../etc", "/srv//x", "-x"):
        with pytest.raises(P.ObservationError):
            P.path_type_argv(bad)


def test_observe_converges_after_setfacl_set_when_the_manifest_declares_the_mask():
    host, fake, _ = _pair()
    h = Hp.RootHelper(fake, M, Hp.BOOTSTRAP)
    for family, params in BOOTSTRAP_OPS[:5]:  # through acl.converge
        step = h.admit(family, params, Hp.ACTIVATE)
        h.apply(step)
    assert h.verify(step).ok and fake.get(step.subject) == step.resolved.after
    assert "mask::r-x" in step.resolved.after["entries"]
    # Why the rule exists: a mask-less value (now unloadable as a manifest) never reads back exactly.
    maskless = {"entries": [e for e in step.resolved.after["entries"] if e != "mask::r-x"]}
    fake.put(step.subject, maskless)
    assert fake.get(step.subject) != maskless and "mask::r-x" in fake.get(step.subject)["entries"]


# --- sandbox tier: the parsers over REAL binaries (skips where the sandbox cannot run, CI included) --


@pytest.fixture(scope="module")
def real_sb(tmp_path_factory):
    if not SANDBOX_AVAILABLE:
        pytest.skip(SANDBOX_WHY)
    sb = launcher.Sandbox(tmp_path_factory.mktemp("u3b-sandbox"))
    yield sb
    sb.close()


@pytest.mark.nested_namespace
def test_sandbox_real_binaries_observe_parses_like_the_sandbox_port(real_sb):
    """REAL getent/stat/sha256sum/getfacl/visudo in the T-2918 sandbox. ``enabled:`` is
    excluded: systemctl is faked there (fake-port tier; the real check is U4's)."""

    real_sb.reset()
    real_sb.mkdir("/srv/pantheon/shared")
    runner = SandboxRunner(real_sb)
    subjects = sorted(s for s in P.manifest_subjects(M) if not s.startswith("enabled:"))
    port = sandbox_port.SandboxSubstratePort(real_sb, M, SOURCES)
    assert {s: P.observe(runner, s) for s in subjects} == {s: port.get(s) for s in subjects}
    h = Hp.RootHelper(port, M, Hp.BOOTSTRAP)
    for family, params in BOOTSTRAP_OPS:
        h.apply(h.admit(family, params, Hp.ACTIVATE))
    for s in subjects:
        assert P.observe(runner, s) == port.get(s) == resolved_after()[s], s
    snap = P.parse_snapshot([runner.run(a) for a in P.snapshot_argv()])
    identity = {k: v for k, v in resolved_after()["passwd:alice"].items() if k != "locked"}
    assert snap["passwd:alice"] == identity and snap["group:pmail"] == {"gid": 3001}
    good = real_sb.stage(F.sudoers_text(TEMPLATE).encode("utf-8"))
    bad = real_sb.stage(b"alice ALL=(root) NOPASSWD /bin/true\n")
    assert P.parse_visudo(runner.run(P.visudo_argv(good)[0])) == "ok"
    assert P.parse_visudo(runner.run(P.visudo_argv(bad)[0])) == "error"


# --- Recovery D15 ruling: row 2 projection (the shadow hash never leaves the parser) ------------

SENTINEL = "SENTINEL-HASH-DO-NOT-LEAK"
HASH = f"$6${SENTINEL}$abcdefghijklmnop"
PW_ALICE = "alice:x:3101:3101::/home/alice:/bin/bash\n"


def _no_sentinel(*objs):
    for obj in objs:
        assert SENTINEL not in str(obj) and SENTINEL not in repr(obj), obj


@pytest.mark.parametrize("expiry, locked", [("", False), ("1", True)])
def test_a_shadow_hash_never_reaches_the_observed_value(expiry, locked):
    shadow = res(0, f"alice:{HASH}:20753:0:99999:7::{expiry}:\n")
    value = P.parse_observation("passwd:alice", [res(0, PW_ALICE), shadow])
    runner = FakeCommandRunner({tuple(a): r for a, r in zip(P.observe_argv("passwd:alice"), [res(0, PW_ALICE), shadow])})
    observed = P.observe(runner, "passwd:alice")
    assert value == observed == {"uid": 3101, "gid": 3101, "home": "/home/alice", "shell": "/bin/bash", "locked": locked}
    _no_sentinel(value, observed)


@pytest.mark.parametrize(
    "shadow_stdout, match",
    [
        (f"alice:{HASH}:20753\n", "expected 9 fields, got 3"),
        (f"{HASH}\n", "expected 9 fields, got 1"),
        (f"alice:{HASH}:20753:0:99999:7::never:\n", "field 8"),
        (f"alice:{HASH}:20753:0:99999:7:::\nalice:{HASH}:20753:0:99999:7:::\n", "exactly one"),
        (f"alice:{HASH}:20753:0:99999:7:::", "exactly one"),
        (f"bob:{HASH}:20753:0:99999:7:::\n", "different account"),
        (f"alice:{HASH}:20753:0:99999:7:::\nextra-{HASH}", "exactly one"),
    ],
    ids=["three-fields", "no-colons", "bad-expiry", "two-lines", "no-newline", "other-account", "trailing-junk"],
)
def test_malformed_shadow_errors_never_echo_the_line(shadow_stdout, match):
    for passwd in (res(0, PW_ALICE),):
        with pytest.raises(P.ObservationError, match=match) as info:
            P.parse_observation("passwd:alice", [passwd, res(0, shadow_stdout)])
        _no_sentinel(info.value, info.value.args)
        assert info.value.__cause__ is None
    runner = FakeCommandRunner({tuple(a): r for a, r in zip(P.observe_argv("passwd:alice"),
                                                             [res(0, PW_ALICE), res(0, shadow_stdout)])})
    with pytest.raises(P.ObservationError) as info:
        P.observe(runner, "passwd:alice")
    _no_sentinel(info.value)


def test_shadow_error_paths_never_echo_stderr_either():
    with pytest.raises(P.ObservationError, match="getent exited 1") as info:
        P.parse_observation("passwd:alice", [res(0, PW_ALICE), res(1, f"alice:{HASH}:", f"oops {HASH}")])
    _no_sentinel(info.value)
    with pytest.raises(P.ObservationError, match="shadow entry with no passwd") as info:
        P.parse_observation("passwd:alice", [res(2), res(0, f"alice:{HASH}:20753:0:99999:7:::\n")])
    _no_sentinel(info.value)


_PKG_SRC = str(PKG)


def _package_frames(exc):
    """Every traceback frame inside the host_substrate package proper (tests excluded:
    the test's own frame holds the fixture input by construction)."""
    out, seen, todo = [], set(), [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        tb = e.__traceback__
        while tb is not None:
            path = tb.tb_frame.f_code.co_filename
            if path.startswith(_PKG_SRC) and "/tests/" not in path:
                out.append(tb.tb_frame)
            tb = tb.tb_next
        todo += [e.__cause__, e.__context__]
    return out


@pytest.mark.parametrize(
    "shadow_stdout",
    [f"alice:{HASH}:20753\n", f"alice:{HASH}:20753:0:99999:7::never:\n", f"alice:{HASH}:20753:0:99999:7:::",
     f"bob:{HASH}:20753:0:99999:7:::\n"],
    ids=["three-fields", "bad-expiry", "no-newline", "other-account"],
)
def test_no_traceback_frame_local_holds_the_shadow_record(shadow_stdout):
    """U3b review nit (carried into U3c): when the shadow parse raises, no frame's f_locals
    in the raised error's traceback may still hold the raw record (res / out / fields)."""
    for call in ("parse", "observe"):
        with pytest.raises(P.ObservationError) as info:
            if call == "parse":
                P.parse_observation("passwd:alice", [res(0, PW_ALICE), res(0, shadow_stdout)])
            else:
                P.observe(FakeCommandRunner({tuple(a): r for a, r in zip(
                    P.observe_argv("passwd:alice"), [res(0, PW_ALICE), res(0, shadow_stdout)])}), "passwd:alice")
        frames = _package_frames(info.value)
        names = {f.f_code.co_name for f in frames}
        assert "parse_observation" in names, names
        for frame in frames:
            for name, value in frame.f_locals.items():
                assert SENTINEL not in repr(value), (frame.f_code.co_name, name)


def test_every_recorded_shadow_line_carries_no_hash():
    for label, records in CASES.items():
        for r in records:
            if r["argv"][:2] == [P.GETENT, "shadow"]:
                for line in r["stdout"].splitlines():
                    assert line.split(":")[1] == "x", label  # one fixed dummy (Recovery D15, review HIGH)


# --- Recovery D15 ruling: observe() reach --------------------------------------------------------

DARK_ROWS = {row[0] for row in P.OBSERVE_ARGV_TABLE if row[4] == "HostSubstratePort (dark)"}


def test_observe_can_never_emit_the_dark_rows():
    """observe() never emits rows 9-12 (snapshot enumerations, visudo, the T-5652 unlink)."""

    assert len(DARK_ROWS) == 4
    dark_binaries_or_shapes = {P.VISUDO, P.UNLINK}
    subjects = set(SAMPLES) | set(P.manifest_subjects(M))
    for subject in sorted(subjects):
        runner = FakeCommandRunner(default=res(2))
        try:
            P.observe(runner, subject)
        except P.ObservationError:
            pass  # the default result does not parse for every kind; only the argv matter here
        for call in runner.calls:
            assert list(call) not in P.snapshot_argv(), (subject, call)  # no enumeration
            assert call[0] not in dark_binaries_or_shapes, (subject, call)
            assert len(call) > 2 or call[0] != P.GETENT, (subject, call)
        assert not ({_template(a, SAMPLES.get(subject, {})) for a in P.observe_argv(subject)} & DARK_ROWS)
