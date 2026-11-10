#!/usr/bin/env python3
"""T-2920 U4a: the nspawn REHEARSAL harness for the ORG-052 host-broker installer, DARK.

Intent (T-2920, arc T-2652): the real-path proof (D5) that the fixture tests
cannot give. The real installer code (U1 plan, U3a apply/verify/rollback, the
U3b-d host substrate) is driven end to end on a DISPOSABLE systemd-nspawn machine
on this VPS (HO ruling Q9), and the evidence gates the T-2920 live enable (Q3:
S3 + the the operator batch + a GREEN rehearsal + orchestrator's supervised fire).

This module RUNS NOTHING PRIVILEGED and never creates or boots a machine:

* ``--emit-commands``: prints the exact root command script orchestrator runs
  (``prerequisites``, ``run``, ``rollback-prerequisites``). Deterministic; a golden
  pins it. Every step is one fixed argv under ``timeout``.
* ``--check-evidence DIR``: validates the evidence that run captured and prints a
  REHEARSAL RECEIPT (canonical JSON + sha): per-check results, the carry-forward
  table and the ORG-052 gate-evidence table. rc 0 only when green.
* ``--fixture-inputs OUT``: writes the FIXTURE inputs (pure; never a real
  operator-batch value). The script runs it as nobody on the host.
* ``--tree-manifest OUT|- --root P...``: a read-only tree manifest (host-untouched,
  rollback and rerun snapshots).
* ``--plan-digest FILE``: prints a U1 plan's verified ``plan_sha256`` (read-only).

The in-machine live actuation is U4b (``tools/host_broker_rehearsal_live.py``,
verbs apply / verify / rollback / guard-probe / observe-probe, an attested
rehearsal context Recovery reviewed under D15). This module never imports it and
holds no runner, host or port; the script only NAMES that entrypoint's argv, and
its preflight aborts when the staged commit does not carry it. Neither
``LIVE_ACTUATION_ENABLED`` literal is touched.

The receipt is a REHEARSAL receipt with FIXTURE inputs
(``evidence_class: rehearsal:nspawn:fixture-inputs``, ``live: false``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

SCRIPT_SCHEMA: Final = "pantheon-host-broker-rehearsal-script/v1"
RECEIPT_SCHEMA: Final = "pantheon-host-broker-rehearsal-receipt/v1"
RUN_SCHEMA: Final = "pantheon-host-broker-rehearsal-run/v1"
#: U4b's attestation schema id; the file carries EXACTLY ATTESTATION_KEYS (interface ruling 1)
ATTESTATION_SCHEMA: Final = "pantheon-rehearsal-attestation/v1"
ATTESTATION_KEYS: Final = ("host_machine_id_sha256", "machine", "nonce_sha256", "plan_sha256", "run_id", "schema")
TREE_SCHEMA: Final = "pantheon-host-broker-rehearsal-tree/v1"
EVIDENCE_CLASS: Final = "rehearsal:nspawn:fixture-inputs"
FIXTURE_LABEL: Final = "FIXTURE: rehearsal inputs only; never a real operator-batch value"

# --- names and paths (fixed literals; the script lint pins them) -------------

RUN_ID_RE: Final = re.compile(r"[0-9a-f]{12}")
COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH_RE: Final = re.compile(r"/[A-Za-z0-9._/-]+")
MACHINE_PREFIX: Final = "pantheon-rehearsal-"

CACHE_DIR: Final = "/var/cache/pantheon-rehearsal"
BASE_TREE: Final = CACHE_DIR + "/base-noble"
WHEELHOUSE_HOST: Final = CACHE_DIR + "/wheelhouse"
LOCK_COPY: Final = CACHE_DIR + "/requirements.lock"
RUNS_DIR: Final = "/var/lib/pantheon-rehearsal"
PRESTATE_FILE: Final = RUNS_DIR + "/prereq-prestate.txt"
MACHINES_DIR: Final = "/var/lib/machines"
ATTESTATION_DIR: Final = "/etc/pantheon-rehearsal"
#: the ONE bind (interface ruling 2): the staged commit, read-only, at the same path on both sides
HOST_STAGE: Final = "/srv/rehearsal"
REPO_STAGE: Final = HOST_STAGE + "/repo"
M_REPO: Final = REPO_STAGE
#: everything else is COPIED into the machine's root fs (no further bind)
M_INPUTS: Final = "/var/lib/pantheon-rehearsal-inputs"
M_WHEELHOUSE: Final = M_INPUTS + "/wheelhouse"
M_WORK: Final = M_INPUTS + "/work"
M_RECEIPTS: Final = "/var/lib/pantheon-install-receipts"
M_CANARY: Final = "/srv/rehearsal-canary"
M_GUARD_ROOT: Final = "/srv/rehearsal-guard"
FRAMEWORK_ACTIVE: Final = "/usr/local/lib/pantheon/framework/active"

UBUNTU_SUITE: Final = "noble"
UBUNTU_MIRROR: Final = "http://archive.ubuntu.com/ubuntu"
#: python3-venv: build_runtime's venv; acl: setfacl/getfacl; sudo: visudo; procps: pgrep
DEBOOTSTRAP_INCLUDE: Final = ("systemd", "systemd-sysv", "dbus", "python3", "python3-venv", "python3-dbus",
                              "python3-gi", "python3-yaml", "acl", "sudo", "passwd", "procps")
#: T-5702: python3-venv is in universe; debootstrap reads only main unless told otherwise
DEBOOTSTRAP_COMPONENTS: Final = ("main", "universe")
PREREQ_PACKAGES: Final = ("systemd-container", "debootstrap")
LIVE_ENTRYPOINT_REL: Final = "tools/host_broker_rehearsal_live.py"
LIVE_VERBS: Final = frozenset({"apply", "verify", "rollback", "guard-probe", "observe-probe"})
HARNESS_REL: Final = "tools/host_broker_rehearsal.py"
LOCK_REL: Final = "tools/host_mcp_adapter/dist/requirements.lock"
DIST_REL: Final = "tools/host_control/dist"
INGRESS_UNIT_REL: Final = DIST_REL + "/pantheon-host-approval-telegram-ingress.service"
C_ENV_LINES: Final = ("LC_ALL=C", "PATH=/usr/bin:/bin")
ENV_PREFIX: Final = ("/usr/bin/env", "-i", *C_ENV_LINES)
#: guard-probe (U4b @0c08c32b3): rc 3 + {verb, verdict: refused, reasons} anywhere but an attested
#: machine; rc 0 + {verb, verdict: attested, machine, run_id, nonce_sha256, plan_sha256} inside it
GUARD_PROBE_REFUSED_RC: Final = 3
GUARD_PROBE_REFUSED_KEYS: Final = frozenset({"verb", "verdict", "reasons"})
GUARD_PROBE_ATTESTED_KEYS: Final = frozenset({"verb", "verdict", "machine", "run_id", "nonce_sha256", "plan_sha256"})
#: the canary must be refused for (at least) the container-file factor (U4b _container_refusals)
CONTAINER_REFUSAL_PREFIX: Final = "/run/systemd/container: "
#: U4b refuses the entrypoint unless its cwd is the read-only repo bind
LIVE_CWD: Final = "/srv/rehearsal/repo"
# --- the machine mount ALLOWLIST: the SAME rules as U4b's mount factor (@0c08c32b3; a test pins
# parity once U4b is on the branch). Recovery's STANDING RULE: there is NO knob, flag or parameter that
# widens it; an unlisted mount is reported as "attestation refused: unlisted mount <line>" so the
# captured line goes to Recovery as a D15 ruling.
MOUNT_VIRTUAL_EXACT: Final = (
    ("/proc", frozenset({"proc"})), ("/sys/fs/cgroup", frozenset({"cgroup2", "tmpfs"})),
    ("/dev", frozenset({"tmpfs"})), ("/dev/shm", frozenset({"tmpfs"})), ("/dev/pts", frozenset({"devpts"})),
    ("/dev/mqueue", frozenset({"mqueue"})), ("/dev/hugepages", frozenset({"hugetlbfs"})), ("/run", frozenset({"tmpfs"})),
    ("/tmp", frozenset({"tmpfs"})))
MOUNT_VIRTUAL_PREFIX: Final = (
    ("/sys/fs/cgroup/", frozenset({"cgroup", "cgroup2"}), False),
    ("/run/systemd/", frozenset({"tmpfs"}), False),
    ("/run/user/", frozenset({"tmpfs"}), False),
    ("/run/credentials/", frozenset({"tmpfs", "ramfs"}), True),
    ("/sys/kernel/", frozenset({"debugfs", "tracefs", "securityfs", "configfs", "fusectl"}), True))
MOUNT_RO_MASK_PREFIXES: Final = ("/proc/", "/sys/", "/run/host/")
MOUNT_CONSOLE: Final = ("/dev/console", "devpts")
UNLISTED_MOUNT: Final = "attestation refused: unlisted mount "
#: Recovery D15 W3 ruling (2026-11-01, T-5703): the mounts REAL systemd-nspawn 255 creates (rehearsal
#: 1, run 1990856d106c), EXACT; equal to U4b's RULED_MOUNTS (the parity test pins it). A mount at
#: a ruled (mountpoint, fstype) pair must match one entry or it refuses. Field meanings: U4b.
#: HOST-SIDE LIMIT: the P0 evidence carries no /proc/self/uid_map, so here "userns" checks the
#: superblock uid=/gid= against the machine /run tmpfs's own; the uid_map equality itself is
#: U4b's in-machine factor, evidenced by the attested guard-probe (C-ATTESTATION).
MOUNT_RULED: Final = (
    {"id": "W3-1", "mountpoint": "/sys", "fstype": "tmpfs", "root": "/", "root_re": None, "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": ("mode=555", "size=4096k"), "userns": True,
     "source": None, "fresh": False, "parent": None, "max": 1, "requires": None},
    {"id": "W3-2", "mountpoint": "/run/host", "fstype": "tmpfs", "root": "/host", "root_re": None, "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": None, "max": 1, "requires": None},
    {"id": "W3-3a", "mountpoint": "/proc/sys/net", "fstype": "proc", "root": "/sys/net", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": False,
     "source": "/proc", "fresh": False, "parent": "/proc/sys", "max": 1, "requires": "--private-network"},
    {"id": "W3-3b", "mountpoint": "/proc/sys/net", "fstype": "proc", "root": "/sys/net", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": False,
     "source": "/proc", "fresh": False, "parent": "/proc", "max": 1, "requires": "--private-network"},
    {"id": "W3-4a", "mountpoint": "/proc/sys/kernel/random/boot_id", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-sys-kernel-random-boot-id[0-9a-f]{16}//deleted", "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc/sys", "max": 1, "requires": None},
    {"id": "W3-4b", "mountpoint": "/proc/sys/kernel/random/boot_id", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-sys-kernel-random-boot-id[0-9a-f]{16}//deleted", "ro": False,
     "flags": ("nosuid", "nodev"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc", "max": 1, "requires": None},
    {"id": "W3-5", "mountpoint": "/proc/kmsg", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-kmsg[0-9a-f]{16}//deleted", "ro": False,
     "flags": ("nosuid", "nodev"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc", "max": 1, "requires": None},
    {"id": "W3-6", "mountpoint": "/run/lock", "fstype": "tmpfs", "root": "/", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": ("size=5120k",), "userns": True,
     "source": None, "fresh": True, "parent": "/run", "max": 1, "requires": None},
)
MOUNT_RULED_ANCHORS: Final = (("/run", "tmpfs"), ("/proc", "proc"), ("/proc/sys", "proc"))
MOUNT_RULED_FLAG_SET: Final = ("nosuid", "nodev", "noexec")

#: paths the installer treats as trusted; the wheelhouse sits under none (orchestrator ruling)
TRUSTED_DIR_ROOTS: Final = ("/etc/pantheon", "/usr/local/lib/pantheon", "/usr/local/libexec", "/var/lib/pantheon",
                            "/etc/dbus-1", "/etc/systemd", "/usr/share/dbus-1")
TRUSTED_STRING_PREFIXES: Final = ("/var/lib/pantheon-host-",)

#: HIGH-1: the host-untouched manifest watches ONLY the installer's own targets, derived from the
#: U1 plan (host_watch_spec): never all of /var/lib/pantheon or /etc/pantheon (live fleet state)
HOST_TREE_SCHEMA: Final = "pantheon-host-broker-rehearsal-host-tree/v1"
#: plan kinds that only observe (their path is not written by the installer)
WATCH_SKIP_KINDS: Final = frozenset({"package_verify", "precondition", "journal_schema_check",
                                     "credential_ref_registration", "framework_file"})
#: every dist unit is WantedBy=multi-user.target (a test pins it), so enable writes this link
WANTS_DIR: Final = "/etc/systemd/system/multi-user.target.wants"
#: the account rows the host manifest records (4 service accounts + the 3 fixture accounts)
WATCH_ACCOUNTS: Final = ("pantheon-hostapproval", "pantheon-hostapproval-ingress", "pantheon-hostbroker",
                         "pantheon-hostmonitor", "rehearsal-approver", "rehearsal-operator", "rehearsal-requester")
#: bounds on any tree manifest (entries, hashed bytes, seconds)
TREE_MAX_ENTRIES: Final = 20000
TREE_MAX_BYTES: Final = 1 << 30
TREE_MAX_SECONDS: Final = 240
MACHINE_SNAPSHOT_ROOTS: Final = ("/etc/dbus-1/system.d", "/etc/group", "/etc/pantheon", "/etc/passwd", "/etc/shadow",
                                 "/etc/systemd/system", "/run/pantheon-host-install", M_CANARY,
                                 "/usr/local/lib/pantheon", "/usr/local/libexec", "/usr/share/dbus-1/system-services",
                                 "/var/lib/pantheon")
#: never content-hashed by a tree manifest (the token is never hashed anywhere; shadow is not evidence)
NO_HASH_BASENAMES: Final = frozenset({"bot-token", "shadow", "gshadow"})

#: Recovery D15 addition 1: -U, ONE read-only bind, nothing writable, no --volatile
NSPAWN_FORBIDDEN_PREFIXES: Final = ("--bind=", "--volatile", "--private-users=no", "--private-users=0",
                                    "--capability", "--network-interface", "--network-veth", "--network-bridge",
                                    "--network-zone", "--port", "--overlay", "--tmpfs", "--inaccessible",
                                    "--register=no", "--keep-unit", "--resolv-conf", "--setenv", "--user",
                                    "--chdir", "--as-pid2", "--bind-user", "--machine-id")
READ_ONLY_BINDS: Final = (REPO_STAGE,)


def machine_name(run_id: str) -> str:
    return MACHINE_PREFIX + run_id


def run_dir(run_id: str) -> str:
    return f"{RUNS_DIR}/{run_id}"


def nspawn_argv(run_id: str) -> list[str]:
    m = machine_name(run_id)
    return ["/usr/bin/systemd-nspawn", "--quiet", "--boot", "--notify-ready=yes", "-U", "--private-network",
            f"--machine={m}", f"--directory={MACHINES_DIR}/{m}", *(f"--bind-ro={b}" for b in READ_ONLY_BINDS)]


def under_trusted_root(path: str) -> bool:
    return (any(path == r or path.startswith(r + "/") for r in TRUSTED_DIR_ROOTS)
            or any(path.startswith(p) for p in TRUSTED_STRING_PREFIXES))


def state_directory_roots(repo_root: Path = REPO_ROOT) -> list[str]:
    """``/var/lib/<StateDirectory>`` of every dist unit (each a trusted state root)."""

    out = []
    for unit in sorted((repo_root / DIST_REL).glob("*.service")):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("StateDirectory="):
                out.extend("/var/lib/" + v for v in line.split("=", 1)[1].split())
    return sorted(set(out))


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# PREREQUISITES (orchestrator runs them; the harness never executes them)
# ---------------------------------------------------------------------------

PREREQ_SIMULATION: Final = RUNS_DIR + "/prereq-simulation.txt"
PREREQ_NEW_PACKAGES: Final = RUNS_DIR + "/prereq-new-packages.txt"
#: a PRE-a rollback ARCHIVES the pre-state record here (never deletes it), under a validated UTC stamp
PREREQ_ARCHIVE: Final = RUNS_DIR + "/prereq-archive"
#: the rollback's own simulation, its Purg/Remv set and the sorted recorded list (archived with the rest)
PREREQ_PURGE_SIMULATION: Final = RUNS_DIR + "/prereq-purge-simulation.txt"
PREREQ_PURGE_SET: Final = RUNS_DIR + "/prereq-purge-set.txt"
PREREQ_NEW_SORTED: Final = RUNS_DIR + "/prereq-new-packages.sorted.txt"
#: comm -23 of the purge set against the sorted recorded list: any name here is a cascade, so refuse
PREREQ_PURGE_EXTRA: Final = RUNS_DIR + "/prereq-purge-extra.txt"
#: the recorded names dpkg knows (dpkg-query -W): the purge simulation runs over these only, so a
#: recorded package that never installed and left the apt index cannot fail it (rc 100) under set -e
PREREQ_PURGE_KNOWN: Final = RUNS_DIR + "/prereq-purge-known.txt"
#: the post-purge verify: dpkg's status of every recorded name (plus the dpkg sentinel), and the leftovers
PREREQ_VERIFY_STATUS: Final = RUNS_DIR + "/prereq-verify-status.txt"
PREREQ_VERIFY_LEFT: Final = RUNS_DIR + "/prereq-verify-left.txt"
PREREQ_RECORD_FILES: Final = (PRESTATE_FILE, PREREQ_SIMULATION, PREREQ_NEW_PACKAGES, PREREQ_PURGE_SIMULATION,
                              PREREQ_PURGE_SET, PREREQ_NEW_SORTED, PREREQ_PURGE_EXTRA, PREREQ_PURGE_KNOWN,
                              PREREQ_VERIFY_STATUS, PREREQ_VERIFY_LEFT)
#: MED-2: systemd-container is pinned to the INSTALLED systemd version (never an upgrade)
#: --no-remove guards apt state changing between the simulation and the install (valid on install;
#: on purge it refuses every purge, so the rollback guards by simulation instead)
PRE_A_INSTALL: Final = ("apt-get", "install", "-y", "--no-install-recommends", "--no-remove",
                        "systemd-container=$SYSTEMD_VERSION", "debootstrap")
PRE_A_SIMULATE: Final = ("apt-get", "-s", "install", "--no-install-recommends", "systemd-container=$SYSTEMD_VERSION",
                         "debootstrap")
PRE_A_PURGE_SIMULATE: Final = ("xargs", "-r", "-a", PREREQ_PURGE_KNOWN, "apt-get", "-s", "purge")
#: dpkg-query exits 1 for a name it does not know; xargs turns that into 123, the ONLY tolerated rc
PRE_A_PURGE_FILTER: Final = ("xargs", "-a", PREREQ_NEW_PACKAGES, "dpkg-query", "-W", "-f=${Package}\\n")
#: the post-purge VERIFY. xargs maps EVERY dpkg-query rc 1-125 to 123, so rc 123 cannot tell "an unknown
#: name" (rc 1) from "the database is unreadable" (rc 2). dpkg itself is therefore queried as a
#: SENTINEL in the same call: no "dpkg installed" line means dpkg-query could not read its database
#: and the rollback refuses. A recorded name dpkg does not know prints nothing (not installed).
PRE_A_VERIFY_SENTINEL: Final = "dpkg"
PRE_A_VERIFY_QUERY: Final = ("xargs", "-a", PREREQ_NEW_PACKAGES, "dpkg-query", "-W",
                             "-f=${Package} ${db:Status-Status}\\n", PRE_A_VERIFY_SENTINEL)
#: the real purge reads the VERIFIED simulated set (a subset of the recorded list), never the raw list
PRE_A_ROLLBACK: Final = ("xargs", "-r", "-a", PREREQ_PURGE_SET, "apt-get", "purge", "-y")
#: L2: the residual window between the purge simulation and the purge. apt state could change in it;
#: it needs root, lasts seconds and the rollback runs once under orchestrator's supervision. Accepted (apt
#: kept, not dpkg).
PRE_A_WINDOW_NOTE: Final = ("window: apt state could change between the purge simulation and the purge; it needs "
                            "root, lasts seconds, and the rollback runs once under orchestrator's supervision")
PREREQUISITES: Final = (
    {"id": "PRE-a", "what": "host packages; systemd-container pinned to the installed systemd version; the simulated "
                            "new-package list is recorded first and a simulated upgrade or removal refuses",
     "command": list(PRE_A_INSTALL),
     "rollback": list(PRE_A_ROLLBACK),
     "rollback_condition": f"purges only the Purg/Remv set of apt-get -s purge over {PREREQ_NEW_PACKAGES}; a "
                           "name in that set NOT on the recorded list refuses (never a cascade); a smaller or empty "
                           f"set (a partial or failed install) proceeds; {PRE_A_WINDOW_NOTE}"},
    {"id": "PRE-b", "what": f"one-time debootstrap of the {UBUNTU_SUITE} minbase tree (network: {UBUNTU_MIRROR})",
     "command": ["debootstrap", "--variant=minbase", "--components=" + ",".join(DEBOOTSTRAP_COMPONENTS),
                 "--include=" + ",".join(DEBOOTSTRAP_INCLUDE), UBUNTU_SUITE,
                 BASE_TREE, UBUNTU_MIRROR],
     "rollback": ["rm", "-rf", "--one-file-system", BASE_TREE], "rollback_condition": "always (cache only)"},
    {"id": "PRE-c", "what": "one-time hash-pinned wheelhouse download of the commit's requirements.lock (network: PyPI)",
     "command": ["python3", "-m", "pip", "download", "--require-hashes", "--only-binary=:all:", "--no-deps",
                 "--no-cache-dir", "-r", LOCK_COPY, "-d", WHEELHOUSE_HOST],
     "rollback": ["rm", "-rf", "--one-file-system", WHEELHOUSE_HOST], "rollback_condition": "always (cache only)"},
)


def prea_install_line() -> str:
    """The ONE real apt-get install line (PRE-a), verbatim. lint_script classifies every other
    apt/apt-get/aptitude install, upgrade, dist-upgrade or full-upgrade line as unknown."""

    return f"  timeout 900 {_argv(PRE_A_INSTALL[:5])} \"systemd-container=$SYSTEMD_VERSION\" debootstrap  # PRE-a"


def prea_rollback_lines() -> tuple[str, ...]:
    """The PRE-a rollback block, verbatim. emit_commands writes it inside rollback_prerequisites()
    and lint_script requires it there, contiguously and exactly once. It simulates the purge of the
    recorded list, takes the simulation's Purg/Remv set, refuses if ``comm -23`` finds a name in
    that set that is NOT on the recorded list (never a cascade), and otherwise purges exactly that
    verified set; an empty set (a failed or partial install) purges nothing and the records are
    still archived. Never --no-remove, which refuses every purge. prea_rollback_decision mirrors it."""

    nl, sim, ps, srt, ex, kn = (PREREQ_NEW_PACKAGES, PREREQ_PURGE_SIMULATION, PREREQ_PURGE_SET, PREREQ_NEW_SORTED,
                                PREREQ_PURGE_EXTRA, PREREQ_PURGE_KNOWN)
    vs, vl = PREREQ_VERIFY_STATUS, PREREQ_VERIFY_LEFT
    return (
        "  # ROLLBACK PRE-a (begin): simulate the purge, refuse a removal not on the recorded list, purge the rest",
        f"  # {PRE_A_WINDOW_NOTE}",
        "  # simulate over the recorded names dpkg knows; the subset check below stays against the FULL list",
        f"  if test -s {nl}; then timeout 60 {_argv(PRE_A_PURGE_FILTER)} > {kn} || test $? = 123; fi",
        f"  if test -s {nl}; then : > {sim}; fi",
        f"  if test -s {nl} && test -s {kn}; then timeout 120 {_argv(PRE_A_PURGE_SIMULATE)} > {sim}; fi",
        f"  if test -s {nl}; then grep -E '^(Purg|Remv) ' {sim} | cut -d' ' -f2 | sort -u > {ps} || true; fi",
        f"  if test -s {nl}; then sort -u {nl} > {srt}; fi",
        f"  if test -s {nl}; then timeout 60 comm -23 {ps} {srt} > {ex}; fi",
        f"  if test -s {nl} && test -s {ex}; then timeout 60 xargs -a {ex} echo 'PRE-a rollback would remove "
        "packages not in the recorded list:' >&2; exit 3; fi",
        f"  if test -s {nl} && test -s {ps}; then timeout 900 {_argv(PRE_A_ROLLBACK)}; fi",
        "  # verify BEFORE archiving: no recorded package may still be present (anything but not-installed);"
        " dpkg is the sentinel",
        f"  if test -s {nl}; then timeout 60 {_argv(PRE_A_VERIFY_QUERY)} > {vs} || test $? = 123; fi",
        f"  if test -s {nl} && ! grep -qx '{PRE_A_VERIFY_SENTINEL} installed' {vs}; then echo 'PRE-a rollback verify: "
        "dpkg-query could not read the dpkg database; refusing' >&2; exit 3; fi",
        f"  if test -s {nl}; then grep -v '^{PRE_A_VERIFY_SENTINEL} ' {vs} | grep -v ' not-installed$' | cut -d' ' -f1 "
        f"> {vl} || true; fi",
        f"  if test -s {nl} && test -s {vl}; then timeout 60 xargs -a {vl} echo 'PRE-a rollback incomplete: still "
        "installed:' >&2; exit 3; fi",
        "  # ROLLBACK PRE-a (end)",
    )


def _shell_lines(text: str) -> list[str]:
    """Lines as grep/sort/comm see them: split on \\n ONLY (a \\r stays part of the line), a
    final newline terminates rather than adds a line."""

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def prea_rollback_decision(simulation: str, recorded: str, known: str | None = None) -> str:
    """Pure mirror of the PRE-a rollback guard over canned text (no apt, no shell), with the
    shell's line semantics (``_shell_lines``: \\r is an ordinary byte, as for grep and sort):
    ``"skip"`` for an empty recorded list or an empty simulated Purg/Remv set (a failed or partial
    install; nothing purged, records archived), ``"refuse"`` when the set holds a name NOT on the
    recorded list (``comm -23`` non-empty: a cascade), else ``"purge"`` (the verified set). ``known``
    is the dpkg-query filter's output; when it is empty the simulation is the empty file."""

    if not recorded:
        return "skip"
    if known is not None and not _shell_lines(known):
        simulation = ""  # dpkg knows none of the recorded names: the simulation never runs (an empty file)
    removed = {ln.split(" ")[1] for ln in _shell_lines(simulation) if re.match(r"(Purg|Remv) ", ln)}
    if removed - set(_shell_lines(recorded)):
        return "refuse"
    return "purge" if removed else "skip"


def prea_rollback_verify(status: str, recorded: str) -> str:
    """Pure mirror of the post-purge VERIFY over canned ``dpkg-query -W -f='${Package}
    ${db:Status-Status}\\n' dpkg <recorded...>`` output: ``"archive"`` for an empty recorded list or
    when no recorded name is still present (purged, or never known to dpkg: no line); ``"refuse-db"``
    when the dpkg sentinel line is missing (dpkg-query could not read its database, whatever rc
    xargs reported); ``"refuse-left"`` when any recorded name has a status other than not-installed.
    Both refusals exit 3 BEFORE the archive step, so the records stay."""

    if not recorded:
        return "archive"
    lines = _shell_lines(status)
    if f"{PRE_A_VERIFY_SENTINEL} installed" not in lines:
        return "refuse-db"
    left = [ln.split(" ")[0] for ln in lines
            if not ln.startswith(PRE_A_VERIFY_SENTINEL + " ") and not ln.endswith(" not-installed")]
    return "refuse-left" if left else "archive"


#: known/unknown classification of apt installs: a command segment whose program is apt, apt-get or
#: aptitude and that carries one of these verbs ANYWHERE among its arguments (options may sit before
#: it) is UNKNOWN unless it is the pinned PRE-a install line, or a simulation (an exact -s option)
_APT_PROG_RE: Final = re.compile(r"(?:^|[\s/])(?:apt|apt-get|aptitude)$")
_APT_INSTALL_VERBS: Final = frozenset({"install", "reinstall", "upgrade", "dist-upgrade", "full-upgrade"})
_APT_SIM_OPTS: Final = frozenset({"-s", "--simulate", "--dry-run", "--just-print", "--no-act", "--recon"})


def apt_install_problems(code: str, *, pinned: bool) -> list[str]:
    """Classify one code line's apt install/upgrade segments (split on ; && || |)."""

    out = []
    for seg in re.split(r"\|\||&&|[;|]", code):
        toks = seg.split()
        prog = next((i for i, t in enumerate(toks) if _APT_PROG_RE.search(t.strip("'\""))), None)
        if prog is None:
            continue
        args = [t.strip("'\"") for t in toks[prog + 1:]]
        if not _APT_INSTALL_VERBS.intersection(args):
            continue
        if any(a.endswith("-") and not a.startswith("-") for a in args):
            out.append("an apt install argument ending in - removes a package")
        if not pinned and not _APT_SIM_OPTS.intersection(args):
            out.append("unknown package-install form (only the pinned PRE-a install line may install or upgrade)")
    return out


#: L5 known/unknown classification: a code line matching any package-removal verb is UNKNOWN (refused)
#: unless it is one of the pinned PRE-a rollback lines
_REMOVAL_RE: Final = re.compile(
    r"\b(?:apt|apt-get|aptitude)[\"']?(?![\w-])(?:\s+\S+)*?\s+[\"']?(?:remove|purge|autoremove|autopurge)\b"
    r"|\bdpkg[\"']?(?![\w-])(?:\s+\S+)*?\s+(?:--purge|--remove|-[A-Za-z]*[rP][A-Za-z]*)(?:[\s=;]|$)")


def allowed_rm_targets(run_id: str) -> frozenset[str]:
    rd = run_dir(run_id)
    return frozenset({BASE_TREE, WHEELHOUSE_HOST, LOCK_COPY, REPO_STAGE, f"{rd}/nonce", f"{rd}/repo.tar",
                      f"{rd}/fixture-token"})


def fixture_machine_id(run_id: str) -> str:
    """LOW-6: a fixed FIXTURE machine-id (32 hex), never empty (an empty one makes systemd
    bind a transient id that U4b's mount factor refuses); the script checks it is not the host's."""

    return "5ea1ed00" + run_id + "0" * 12


# ---------------------------------------------------------------------------
# fixed probe argv (pinned by the golden)
# ---------------------------------------------------------------------------

#: the producer's setpriv prefix after Recovery's SETPRIV ruling (U4b amends the plan code to it)
PRODUCER_SETPRIV_PREFIX: Final = ("/usr/bin/setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups",
                                  "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all")
PRIVDROP_PROBE_ARGV: Final = (*PRODUCER_SETPRIV_PREFIX, "--", "/usr/bin/cat", "/proc/self/status")
NOBODY_PREFIX: Final = ("/usr/bin/setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--no-new-privs",
                        "--")
LAUNCHER_PROBE_MODULES: Final = ("tools.host_control.dbus_service", "tools.host_control.runtime_table",
                                 "tools.host_control.approval.dbus_service", "tools.host_control.approval.service")
LAUNCHER_PROBE_CODE: Final = (
    "import importlib, json, os, sys; "
    f"r = os.path.realpath({FRAMEWORK_ACTIVE!r}); sys.path.insert(0, r); "
    f"[importlib.import_module(m) for m in {LAUNCHER_PROBE_MODULES!r}]; "
    "fs = sorted({os.path.realpath(f) for f in (getattr(v, '__file__', None) for v in list(sys.modules.values())) if f}); "
    "print(json.dumps({'framework_root': r, 'files': fs}, sort_keys=True))")
GATE32A_PROBE_CODE: Final = (
    f"import json, os, sys; sys.path.insert(0, {M_REPO!r}); from pathlib import Path; "
    "from tools.host_control.runtime_table import load_checkout_op_registry; "
    f"r = os.path.realpath({FRAMEWORK_ACTIVE!r}); "
    "print(json.dumps({'framework_root': r, 'root_digest': load_checkout_op_registry(Path(r)).digest}, sort_keys=True))")
MDWE_PROBE_CODE: Final = (
    "import dbus, dbus.mainloop.glib; from gi.repository import GLib; "
    "dbus.mainloop.glib.DBusGMainLoop(set_as_default=True); print('imports_ok', flush=True); "
    "fired = []; loop = GLib.MainLoop(); "
    "GLib.idle_add(lambda: fired.append(1) or loop.quit() or False); "
    "GLib.timeout_add_seconds(5, lambda: loop.quit() or False); loop.run(); "
    "print('callback_fired' if fired else 'callback_missing')")
#: [Service] keys of the ingress unit the MDWE probe copies (a test pins this against the dist unit)
MDWE_PROBE_PROPERTIES: Final = (
    "User", "Group", "NoNewPrivileges", "CapabilityBoundingSet", "AmbientCapabilities", "ProtectSystem", "ProtectHome",
    "PrivateTmp", "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
    "ProtectControlGroups", "ProtectClock", "ProtectHostname", "RestrictAddressFamilies", "RestrictNamespaces",
    "RestrictRealtime", "RestrictSUIDSGID", "LockPersonality", "MemoryDenyWriteExecute", "SystemCallFilter",
    "SystemCallErrorNumber", "SystemCallArchitectures", "UMask")
#: the systemctl-show real-surface probe (CF-2): a fixture unit installed before boot
PROBE_UNIT: Final = "rehearsal-probe.service"
PROBE_UNIT_TEXT: Final = ("[Unit]\\nDescription=rehearsal systemctl-show probe (FIXTURE)\\n[Service]\\nType=oneshot\\n"
                          "ExecStart=/bin/true\\n[Install]\\nWantedBy=multi-user.target\\n")
SYSTEMCTL_PROBE_CASES: Final = (("disabled", PROBE_UNIT), ("enabled", PROBE_UNIT), ("absent", "rehearsal-absent.service"),
                                ("static-refused", "systemd-journald.service"))
#: > HEALTH_REFRESH_SECONDS (driver.py, 30 s) so the broker's health cache has refreshed
HEALTH_SETTLE_SECONDS: Final = 35
SEAT_UID: Final = 2001
ACTIVATION_FILE: Final = f"/usr/share/dbus-1/system-services/org.pantheon.HostExecutor1.U{SEAT_UID}.service"
ACTIVATION_ASIDE: Final = M_WORK + "/activation-aside.service"
EXECUTOR_BUS_NAME: Final = f"org.pantheon.HostExecutor1.U{SEAT_UID}"
SERVICE_ACCOUNTS: Final = {"broker": ("pantheon-hostbroker", 1901), "approval": ("pantheon-hostapproval", 1902),
                           "ingress": ("pantheon-hostapproval-ingress", 1903), "monitor": ("pantheon-hostmonitor", 1904)}
DAEMONS: Final = {"broker": ("pantheon-host-broker.service", "org.pantheon.HostBroker1",
                             "/usr/local/libexec/pantheon-host-broker"),
                  "approval": ("pantheon-host-approval.service", "org.pantheon.HostApproval1",
                               "/usr/local/libexec/pantheon-host-approval")}
INGRESS_UNIT: Final = "pantheon-host-approval-telegram-ingress.service"
INGRESS_BUS_NAME: Final = "org.pantheon.HostApprovalTelegram1"
#: ingress startup refusal codes that would mean a step BEFORE getMe failed (main.py / build_core)
INGRESS_EARLY_FAILURES: Final = ("detail=config_refused", "detail=dbus_unavailable", "detail=wrong_uid",
                                 "detail=credential_unavailable", "detail=credential_invalid")
INGRESS_GETME_FAILED: Final = "telegram-ingress event=api_error detail=get_me_failed"
TOKEN_SCAN_DIRS: Final = ("/etc/dbus-1/system.d", "/etc/pantheon", "/etc/systemd/system", "/usr/local/libexec",
                          "/var/lib/pantheon-host-approval", "/var/lib/pantheon-host-broker", M_RECEIPTS)
STDLIB_PREFIXES: Final = ("/usr/lib/python3.12/", "/usr/lib/python3/dist-packages/")

# --- fault cases (planted through fixed argv; driven through U4b apply/rollback) ---

FAULT_CASES: Final = {
    "F1": {"kind": "dir leaf", "plant": "/etc/pantheon/host-executor", "target": M_CANARY + "/F1",
           "failed_entry": "dir:/etc/pantheon/host-executor", "marker": "is not a directory"},
    "F2": {"kind": "ancestor", "plant": "/usr/local/libexec", "target": M_CANARY + "/F2",
           "failed_entry": "runtime:approval", "marker": "symlink component"},
    "F3": {"kind": "token destination", "plant": "/etc/pantheon/host-approval-telegram/credentials/bot-token",
           "target": M_CANARY + "/F3/target",
           "failed_entry": "credential:/etc/pantheon/host-approval-telegram/credentials/bot-token",
           "marker": "planted symlink is refused"},
}
GUARD_MANIFEST_LABEL: Final = "rehearsal-fixture-guard-manifest (B6): NOT the T-2920 install manifest"
#: residual paths a rollback may leave changed: accounts are sticky (T-2921), so their rows and homes
ROLLBACK_RESIDUAL_FILES: Final = frozenset({"/etc/passwd", "/etc/group", "/etc/shadow",
                                           # the two parent dir ENTRIES of the sticky homes (not their subtrees)
                                           "/var/lib/pantheon", "/var/lib/pantheon/home"})
ROLLBACK_RESIDUAL_DIRS: Final = ("/var/lib/pantheon/home",)

# --- the evidence set (closed; every file is a raw capture of one fixed step) ---

EVIDENCE_FILES: Final = (
    "run.json", "host-prestate.txt", "host-plan.json", "host-before.json", "host-after.json", "host-canary.json",
    "host-canary.rc",
    "boot-state.txt", "P0-env.txt", "P0-mountinfo.txt",
    *(f"P0-systemctl-{c}.{x}" for c, _ in SYSTEMCTL_PROBE_CASES for x in ("txt", "rc")),
    "plan.json", "attestation.json", "machine-guard-probe.json",
    *(f"{c}-{x}" for c in FAULT_CASES for x in ("S0.json", "apply.out", "apply.rc", "receipt.json", "S1.json",
                                                 "rollback.out", "S2.json", "accounts.txt")),
    "apply.out", "main-receipt.json", "probe-producer-privdrop.txt",
    *(f"unit-{r}.txt" for r in DAEMONS), *(f"busowner-{r}.txt" for r in DAEMONS), *(f"proc-{r}.txt" for r in DAEMONS),
    *(f"cwd-{r}.txt" for r in DAEMONS), "launcher-imports.json", "broker-journal.txt", "gate32a-root.json",
    "ingress-start.rc", "ingress-journal.txt", "unit-ingress.txt", "ingress-owner.rc", "mdwe-probe.txt",
    "activation-sha-before.txt", "health-degraded.txt", "activation-sha-restored.txt", "health-ready.txt",
    "start-service.txt", "start-service.rc", "executor-owner.txt", "verify.out",
    "rerun-S0.json", "rerun.out", "rerun-receipt.json", "rerun-S1.json", "journal-all.txt",
    "token-scan-machine.txt", "token-scan-machine.rc", "token-scan-host.txt", "token-scan-host.rc",
)

PHASES: Final = (
    {"id": "R0", "where": "host", "what": "preflight (euid 0, tools, free names, no host attestation dir, prereq pre-state), nonce and host machine-id sha, run.json; stage the commit's tracked files at /srv/rehearsal/repo; the FIXTURE inputs and the host plan (as nobody); U4b entrypoint presence; the host-before manifest of ONLY the plan-derived installer targets; FORBIDDEN-ON-HOST CANARY (guard-probe must refuse, rc 3)"},
    {"id": "R1", "where": "host", "what": "image: per-run copy of the cached base tree; copy inputs, tracked list and wheelhouse into the machine fs; plant the nonce, hostname, empty machine-id, fixture token and the systemctl probe unit"},
    {"id": "R2", "where": "host", "what": "boot: the pinned nspawn argv (-U, ONE read-only bind, --private-network) in a bounded transient unit"},
    {"id": "P0", "where": "machine", "what": "env (CF-1), mountinfo, systemctl real surface (CF-2); fixture preconditions (operator and seat accounts, mailbox hub, receipts dir, /usr/local/libexec, canaries)"},
    {"id": "P1", "where": "machine", "what": "U1 plan from the staged commit with the FIXTURE inputs, as nobody"},
    {"id": "R3", "where": "host", "what": "attestation: root writes attestation.json (0600) binding machine, run, nonce, host machine-id and the P1 plan sha; in-machine guard-probe must admit"},
    {"id": "F", "where": "machine", "what": "fault cycles F1-F3: plant a symlink, U4b apply stops partial, the canary is untouched, U4b rollback restores (CF-3/9/10)"},
    {"id": "P2-P9", "where": "machine", "what": "U4b apply --execute: accounts, runtimes, framework copy, configs, registrations, bus policy, units, activation, token, enable, journal roots"},
    {"id": "P6", "where": "machine", "what": "probe: producer privilege drop (the SAME setpriv prefix running cat /proc/self/status; not the producer itself)"},
    {"id": "P9", "where": "machine", "what": "start approval then broker (CF-5/6/11); ingress steps 1-3 then fail-closed at getMe plus the MDWE probe (CF-5 partial); degraded-then-ready (CF-7, B5/B7); U4b verify"},
    {"id": "P10", "where": "-", "what": "MCP allowlist precondition observed by the plan only; adapter registration is out of the rehearsal"},
    {"id": "P11", "where": "-", "what": "first fire: NOT rehearsed (orchestrator's supervised fire); the gate-26/30 compensate hook is recorded DISABLED (Q4)"},
    {"id": "P12", "where": "machine", "what": "identical rerun is a no-op (CF-8) with Q10 backups; token scans in the machine and over the evidence (CF-10)"},
    {"id": "R9", "where": "host", "what": "host-after manifest; teardown on EXIT (poweroff, stop, remove, unstage)"},
)


# ---------------------------------------------------------------------------
# --emit-commands
# ---------------------------------------------------------------------------


def _q(value: str) -> str:
    return shlex.quote(value)


def _argv(parts: Sequence[str]) -> str:
    return " ".join(_q(p) for p in parts)


def _validate_emit_args(run_id: str, commit: str, repo: str) -> None:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("--run-id must be 12 lowercase hex characters")
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("--repo-commit must be a full 40-hex commit id")
    if not _SAFE_PATH_RE.fullmatch(repo) or posixpath.normpath(repo) != repo or ".." in repo.split("/"):
        raise ValueError("--repo must be a normalized absolute path of [A-Za-z0-9._/-]")


def ingress_sandbox_properties(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """The ingress unit's [Service] values for MDWE_PROBE_PROPERTIES, in that order."""

    values: dict[str, str] = {}
    section = None
    for line in (repo_root / INGRESS_UNIT_REL).read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            section = line.strip()
        elif section == "[Service]" and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return [(k, values[k]) for k in MDWE_PROBE_PROPERTIES]


def fixture_input_names() -> tuple[str, ...]:
    return ("FIXTURE.json", "approver-policy.json", "guard-manifest.json", "host-policy.json", "inputs.json",
            "m1.yaml", "manifest.json", "registry.json")


def emit_commands(run_id: str, commit: str, repo: str, repo_root: Path = REPO_ROOT) -> str:
    """The deterministic root script for orchestrator. Fixed argv everywhere; the only runtime
    values are the nonce, its sha, the host machine-id sha, the plan sha, the token and rcs."""

    _validate_emit_args(run_id, commit, repo)
    m = machine_name(run_id)
    rd = run_dir(run_id)
    md = f"{MACHINES_DIR}/{m}"
    ev = f"{rd}/evidence"
    git = ["git", "-C", repo, "-c", f"safe.directory={repo}"]
    harness = [*ENV_PREFIX, "/usr/bin/python3", "-I", f"{REPO_STAGE}/{HARNESS_REL}"]
    mrun = ["/usr/bin/systemd-run", f"--machine={m}", "--pipe", "--wait", "--quiet", "--working-directory=/", "--",
            *ENV_PREFIX]
    nonce_arg = '--nonce-sha256 "$NONCE_SHA256"'

    def mstep(argv: Sequence[str], out: str | None, t: int = 60) -> str:
        line = f"  timeout {t} {_argv([*mrun, *argv])}"
        return line + (f" > {ev}/{out}" if out else "")

    live_run = [*mrun[:5], f"--working-directory={LIVE_CWD}", *mrun[6:]]

    def live(verb: str, extra: Sequence[str], out: str | None, t: int) -> str:
        if verb not in LIVE_VERBS:
            raise ValueError(verb)
        head = _argv([*live_run, "/usr/bin/python3", "-I", f"{M_REPO}/{LIVE_ENTRYPOINT_REL}", verb])
        line = f"  timeout {t} {head} {nonce_arg}" + (f" {_argv(extra)}" if extra else "")
        return line + (f" > {ev}/{out}" if out else "")

    def rc_step(cmd: str, out_rc: str, expect: str | None) -> list[str]:
        lines = ["  rc=0", cmd + " || rc=$?", f"  printf '%s\\n' \"$rc\" > {ev}/{out_rc}"]
        if expect is not None:
            lines.append(f'  test "$rc" {expect}')
        return lines

    def tree(out: str) -> str:
        roots = [x for r in MACHINE_SNAPSHOT_ROOTS for x in ("--root", r)]
        return mstep(["/usr/bin/python3", "-I", f"{M_REPO}/{HARNESS_REL}", "--tree-manifest", "-", *roots], out, 300)

    plan_args = ["--manifest", f"{M_INPUTS}/manifest.json", "--registry", f"{M_INPUTS}/registry.json",
                 "--tracked-files", f"{M_INPUTS}/tracked-files"]
    apply_args = ["--plan", f"{M_WORK}/plan.json", *plan_args, "--inputs", f"{M_INPUTS}/inputs.json"]

    def receipt(name: str) -> list[str]:
        return [*apply_args, "--receipt", f"{M_RECEIPTS}/{name}.json"]
    busctl = ["/usr/bin/busctl", "--system"]
    dbus_call = [*busctl, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus"]
    monitor, mon_uid = SERVICE_ACCOUNTS["monitor"]
    health_get = ["/usr/bin/setpriv", f"--reuid={monitor}", f"--regid={monitor}", "--clear-groups", "--no-new-privs",
                  "--", *busctl, "get-property", "org.pantheon.HostBroker1", "/org/pantheon/HostBroker1/Health",
                  "org.pantheon.Health1", "Status"]

    L: list[str] = []
    a = L.append
    a("#!/bin/bash")
    a(f"# {SCRIPT_SCHEMA} (T-2920 U4a). GENERATED by tools/host_broker_rehearsal.py --emit-commands; do not edit.")
    a(f"# run id {run_id}; machine {m}; repo {repo} at commit {commit}.")
    a(f"# {FIXTURE_LABEL}.")
    a("# Run as root by orchestrator:  bash THIS prerequisites | run | rollback-prerequisites")
    a("# Every step is one fixed argv under timeout; a failure exits non-zero and the EXIT trap tears down.")
    a(f"# In-machine actuation is ONLY via the U4b entrypoint {LIVE_ENTRYPOINT_REL} (attested; refuses elsewhere).")
    a("# KNOWN FIRST-RUN RISK: U4b's mountinfo allowlist refuses a rw /sys/fs/bpf or /sys/fs/pstore (the 7 real")
    a("# nspawn 255 mounts are accepted per Recovery D15 W3, T-5703). If systemd mounts one inside the machine, every")
    a("# U4b call refuses (rc 3); P0 captures /proc/self/mountinfo BEFORE any in-machine U4b call, so that refusal")
    a("# is diagnosable from the evidence.")
    a("# After a PARTIAL prerequisites failure, run rollback-prerequisites before re-running prerequisites.")
    a("set -euo pipefail")
    a("umask 077")
    a("export LC_ALL=C")
    a("export PATH=/usr/sbin:/usr/bin:/sbin:/bin")
    a("")
    a("# ======================================================================")
    a("# PREREQUISITES: orchestrator runs these ONCE (root, network). The harness never executes them.")
    for pre in PREREQUISITES:
        a(f"#   {pre['id']}: {pre['what']}")
        a(f"#     command:  {' '.join(pre['command'])}")
        a(f"#     ROLLBACK: {' '.join(pre['rollback'])}   ({pre['rollback_condition']})")
    a("# ======================================================================")
    a("prerequisites() {")
    a('  test "$(id -u)" = 0')
    a(f"  install -d -m 0755 {RUNS_DIR}")
    for f in (PRESTATE_FILE, PREREQ_SIMULATION, PREREQ_NEW_PACKAGES):
        a(f"  test ! -e {f}")
    for pkg in PREREQ_PACKAGES:
        a(f"  if dpkg-query -W -f='${{Status}}\\n' {pkg} 2>/dev/null | grep -qx 'install ok installed'; "
          f"then echo '{pkg} present'; else echo '{pkg} absent'; fi >> {PRESTATE_FILE}")
    a("  SYSTEMD_VERSION=\"$(dpkg-query -W -f='${Version}' systemd)\"")
    a("  printf '%s\\n' \"$SYSTEMD_VERSION\" | grep -qxE '[0-9A-Za-z.+~:-]+'")
    a(f"  echo \"systemd-version $SYSTEMD_VERSION\" >> {PRESTATE_FILE}")
    a(f"  timeout 120 {_argv(PRE_A_SIMULATE[:4])} \"systemd-container=$SYSTEMD_VERSION\" debootstrap > {PREREQ_SIMULATION}")
    a(f"  if grep -qE '^(Inst [^ ]+ \\[|Remv )' {PREREQ_SIMULATION}; then echo 'PRE-a would upgrade or remove an "
      "installed package; refusing' >&2; exit 3; fi")
    a(f"  grep '^Inst ' {PREREQ_SIMULATION} | cut -d' ' -f2 > {PREREQ_NEW_PACKAGES} || true")
    a(prea_install_line())
    a(f"  install -d -m 0755 {CACHE_DIR}")
    a(f"  test -e {BASE_TREE} || timeout 1800 {_argv(PREREQUISITES[1]['command'])}  # PRE-b")
    a(f"  timeout 120 {_argv([*git, 'show', f'{commit}:{LOCK_REL}'])} > {LOCK_COPY}")
    a(f"  test -e {WHEELHOUSE_HOST} || timeout 1800 {_argv(PREREQUISITES[2]['command'])}  # PRE-c")
    a("}")
    a("")
    a("rollback_prerequisites() {")
    a('  test "$(id -u)" = 0')
    a(f"  test -f {PRESTATE_FILE}")
    a(f"  timeout 300 rm -rf --one-file-system {WHEELHOUSE_HOST}  # ROLLBACK PRE-c")
    a(f"  timeout 300 rm -rf --one-file-system {BASE_TREE}  # ROLLBACK PRE-b")
    a(f"  rm -f {LOCK_COPY}")
    for ln in prea_rollback_lines():
        a(ln)
    a("  # archive (never delete) the record of what was installed and rolled back; prerequisites can re-run")
    a("  PREREQ_TS=\"$(date -u +%Y%m%dT%H%M%SZ)\"")
    a("  printf '%s\\n' \"$PREREQ_TS\" | grep -qxE '[0-9]{8}T[0-9]{6}Z'")
    a(f"  install -d -m 0755 {PREREQ_ARCHIVE}")
    a(f"  test ! -e \"{PREREQ_ARCHIVE}/$PREREQ_TS\"")
    a(f"  install -d -m 0755 \"{PREREQ_ARCHIVE}/$PREREQ_TS\"")
    for f in PREREQ_RECORD_FILES:
        a(f"  if test -e {f}; then timeout 60 mv -n -- {f} \"{PREREQ_ARCHIVE}/$PREREQ_TS/\"; fi")
    for f in PREREQ_RECORD_FILES:
        a(f"  test ! -e {f}")
    a("}")
    a("")
    a("teardown() {")
    a("  set +e")
    a(f"  timeout 60 machinectl poweroff {m}")
    a(f"  timeout 180 systemctl stop {m}.service")
    a(f"  timeout 300 machinectl remove {m} || echo 'LEFTOVER MACHINE: machinectl remove {m} failed' >&2")
    a(f"  test ! -e {md} || {{ echo 'LEFTOVER MACHINE: {md} still exists; remove it by hand with machinectl remove "
      f"{m}' >&2; }}")
    a(f"  timeout 300 rm -rf --one-file-system {REPO_STAGE}")
    a(f"  rmdir {HOST_STAGE}")
    a(f"  rm -f {rd}/nonce")
    a(f"  rm -f {rd}/repo.tar")
    a(f"  rm -f {rd}/fixture-token")
    a("}")
    a("")
    a("run() {")
    a("  # --- R0 preflight, staging and the FORBIDDEN-ON-HOST canary")
    a('  test "$(id -u)" = 0')
    for b in ("/usr/bin/systemd-nspawn", "/usr/bin/machinectl", "/usr/bin/systemd-run", "/usr/bin/setpriv"):
        a(f"  test -x {b}")
    for d in (BASE_TREE, WHEELHOUSE_HOST):
        a(f"  test -d {d}")
    for p in (ATTESTATION_DIR, HOST_STAGE, md, rd):
        a(f"  test ! -e {p}")
    a(f"  install -d -m 0755 {RUNS_DIR} {rd} {ev} {HOST_STAGE} {REPO_STAGE}")
    a("  trap teardown EXIT")
    a(f"  head -c 32 /dev/urandom > {rd}/nonce")
    a(f"  NONCE_SHA256=\"$(sha256sum {rd}/nonce | cut -d' ' -f1)\"")
    a("  HOST_MACHINE_ID_SHA256=\"$(sha256sum /etc/machine-id | cut -d' ' -f1)\"")
    a(f"  echo 'euid 0' > {ev}/host-prestate.txt")
    for pkg in PREREQ_PACKAGES:
        a(f"  if dpkg-query -W -f='${{Status}}\\n' {pkg} 2>/dev/null | grep -qx 'install ok installed'; "
          f"then echo '{pkg} present'; else echo '{pkg} absent'; fi >> {ev}/host-prestate.txt")
    a(f"  echo 'attestation-dir absent' >> {ev}/host-prestate.txt")
    run_doc = ('{"compensate_enabled":false,"evidence_class":"' + EVIDENCE_CLASS + '","fixture":true,'
               '"host_machine_id_sha256":"%s","live":false,"machine":"' + m + '","nonce_sha256":"%s",'
               '"repo_commit":"' + commit + '","run_id":"' + run_id + '","schema":"' + RUN_SCHEMA + '"}\\n')
    a(f"  printf '{run_doc}' \"$HOST_MACHINE_ID_SHA256\" \"$NONCE_SHA256\" > {ev}/run.json")
    a(f"  timeout 300 {_argv([*git, 'archive', '--format=tar', f'--output={rd}/repo.tar', commit])}")
    a(f"  timeout 300 tar -x -f {rd}/repo.tar -C {REPO_STAGE}")
    a(f"  timeout 120 {_argv([*git, 'ls-tree', '-r', '-z', '--name-only', commit])} > {rd}/tracked-files")
    a(f"  test -f {REPO_STAGE}/{LIVE_ENTRYPOINT_REL} || {{ echo 'the U4b entrypoint {LIVE_ENTRYPOINT_REL} is absent "
      f"from the staged commit; refusing' >&2; exit 3; }}")
    a(f"  install -d -o nobody -g nogroup -m 0700 {rd}/inputs-staging")
    a(f"  timeout 300 {_argv([*NOBODY_PREFIX, *harness, '--fixture-inputs', f'{rd}/inputs-staging/out', '--repo', REPO_STAGE])}")
    a(f"  chmod 0644 {rd}/tracked-files")
    a(f"  timeout 600 {_argv([*NOBODY_PREFIX, *ENV_PREFIX, '/usr/bin/python3', '-I', f'{REPO_STAGE}/tools/host_broker_install.py', 'plan', '--manifest', f'{rd}/inputs-staging/out/manifest.json', '--registry', f'{rd}/inputs-staging/out/registry.json', '--tracked-files', f'{rd}/tracked-files', '--out', f'{rd}/inputs-staging/host-plan.json'])}")
    a(f"  timeout 60 chown -R root:root {rd}/inputs-staging")
    a(f"  if timeout 60 find {rd}/inputs-staging ! -type f ! -type d -print -quit | grep -q .; then echo 'inputs-staging "
      "holds a symlink or a non-regular entry; refusing' >&2; exit 3; fi")
    a(f"  install -m 0644 {rd}/inputs-staging/host-plan.json {ev}/host-plan.json")
    a("  # host-untouched: ONLY the installer's own targets, derived from the plan; bounded in entries, bytes and time")
    host_manifest = [*harness, "--host-manifest", None, "--plan", f"{ev}/host-plan.json"]
    a(f"  timeout 300 {_argv([*host_manifest[:-3], f'{ev}/host-before.json', *host_manifest[-2:]])}")
    a("  # FORBIDDEN-ON-HOST CANARY (Recovery D15 addition 2): guard-probe on the HOST must refuse with rc "
      f"{GUARD_PROBE_REFUSED_RC}; it is observe-only")
    canary = (f"  timeout 60 {_argv([*ENV_PREFIX, '/usr/bin/python3', '-I', f'{REPO_STAGE}/{LIVE_ENTRYPOINT_REL}', 'guard-probe'])} "
              f"{nonce_arg} {_argv(receipt('guard-probe'))} > {ev}/host-canary.json")
    for line in rc_step(canary, "host-canary.rc", f"= {GUARD_PROBE_REFUSED_RC}"):
        a(line)
    a("  # --- R1 image: per-run copy of the cached base; copy (never bind) inputs and wheelhouse in")
    a(f"  install -d -m 0700 {MACHINES_DIR}")
    a(f"  timeout 900 cp -a --reflink=auto {BASE_TREE} {md}")
    a(f"  install -d -m 0755 {md}{REPO_STAGE} {md}{M_INPUTS}")
    a(f"  install -d -m 0700 {md}{M_WHEELHOUSE} {md}{ATTESTATION_DIR}")
    a(f"  install -d -o 65534 -g 65534 -m 0700 {md}{M_WORK}")
    for name in fixture_input_names():
        a(f"  install -m 0644 {rd}/inputs-staging/out/{name} {md}{M_INPUTS}/{name}")
    a(f"  install -m 0644 {rd}/tracked-files {md}{M_INPUTS}/tracked-files")
    a(f"  timeout 300 cp --no-preserve=all -r {WHEELHOUSE_HOST}/. {md}{M_WHEELHOUSE}/")
    a(f"  printf 'FIXTURE-NOT-A-TOKEN-%s\\n' \"$(od -An -N16 -tx1 /dev/urandom | tr -d ' \\n')\" > {rd}/fixture-token")
    a(f"  install -m 0600 {rd}/fixture-token {md}{M_INPUTS}/bot-token")
    a(f"  install -m 0600 {rd}/nonce {md}{ATTESTATION_DIR}/nonce")
    a(f"  printf '{m}\\n' > {md}/etc/hostname")
    a(f"  printf '{fixture_machine_id(run_id)}\\n' > {md}/etc/machine-id")
    a(f"  chmod 0444 {md}/etc/machine-id")
    a(f"  test \"$(sha256sum {md}/etc/machine-id | cut -d' ' -f1)\" != \"$HOST_MACHINE_ID_SHA256\"")
    a(f"  printf '{PROBE_UNIT_TEXT}' > {md}/etc/systemd/system/{PROBE_UNIT}")
    a(f"  chmod 0644 {md}/etc/systemd/system/{PROBE_UNIT}")
    a("  # --- R2 boot: the PINNED nspawn argv (-U; ONE read-only bind; --private-network), bounded")
    a(f"  timeout 300 systemd-run --unit={m} --collect --property=Type=notify --property=NotifyAccess=all "
      f"--property=RuntimeMaxSec=5400 --property=TimeoutStartSec=240 -- {_argv(nspawn_argv(run_id))}")
    a(mstep(["/usr/bin/systemctl", "is-system-running", "--wait"], "boot-state.txt", 240) + " || true")
    a(f"  grep -qxE 'running|degraded' {ev}/boot-state.txt")
    a("  # --- P0: env (CF-1), mountinfo, systemctl real surface (CF-2), fixture preconditions")
    a(mstep(["/usr/bin/env"], "P0-env.txt"))
    a(mstep(["/usr/bin/cat", "/proc/self/mountinfo"], "P0-mountinfo.txt"))
    for case, unit in SYSTEMCTL_PROBE_CASES:
        if case == "enabled":
            a(mstep(["/usr/bin/systemctl", "enable", PROBE_UNIT], None))
        for line in rc_step(mstep(["/usr/bin/systemctl", "show", "--property=UnitFileState", "--value", "--", unit],
                                  f"P0-systemctl-{case}.txt"), f"P0-systemctl-{case}.rc", None):
            a(line)
        if case == "enabled":
            a(mstep(["/usr/bin/systemctl", "disable", PROBE_UNIT], None))
    a(mstep(["/usr/sbin/useradd", "--uid", "2000", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin",
             "rehearsal-operator"], None))
    for seat, uid in (("rehearsal-requester", 2001), ("rehearsal-approver", 2002)):
        a(mstep(["/usr/sbin/useradd", "--uid", str(uid), "--user-group", "--create-home", "--home-dir",
                 f"/home/{seat}", "--shell", "/usr/sbin/nologin", seat], None))
    a(mstep(["/usr/bin/install", "-d", "-m", "0755", "/home/post-office", "/home/post-office/mailbox",
             "/usr/local/libexec", M_CANARY, *(c["target"] for c in FAULT_CASES.values() if c["kind"] != "token destination"),
             M_CANARY + "/F3"], None))
    a(mstep(["/usr/bin/install", "-d", "-m", "0700", M_RECEIPTS], None))
    for name in ("F1/marker", "F2/marker", "F3/target"):
        a(mstep(["/usr/bin/install", "-m", "0644", "/dev/null", f"{M_CANARY}/{name}"], None))
    a("  # --- P1: the U1 plan from the staged commit, as nobody")
    a(mstep([*NOBODY_PREFIX, "/usr/bin/python3", "-I", f"{M_REPO}/tools/host_broker_install.py", "plan", *plan_args,
             "--out", f"{M_WORK}/plan.json"], None, 600))
    a(mstep(["/usr/bin/cat", f"{M_WORK}/plan.json"], "plan.json"))
    a(f"  PLAN_SHA256=\"$(timeout 60 {_argv([*harness, '--plan-digest', f'{ev}/plan.json'])})\"")
    a("  # --- R3 attestation: root writes EXACTLY the six string keys, 0600, after P1 (U4b interface)")
    att = ('{"host_machine_id_sha256":"%s","machine":"' + m + '","nonce_sha256":"%s","plan_sha256":"%s","run_id":"'
           + run_id + '","schema":"' + ATTESTATION_SCHEMA + '"}\\n')
    a(f"  printf '{att}' \"$HOST_MACHINE_ID_SHA256\" \"$NONCE_SHA256\" \"$PLAN_SHA256\" > {rd}/attestation.json")
    a(f"  timeout 60 machinectl copy-to {m} {rd}/attestation.json {ATTESTATION_DIR}/attestation.json")
    a(mstep(["/usr/bin/chown", "root:root", f"{ATTESTATION_DIR}/attestation.json"], None))
    a(mstep(["/usr/bin/chmod", "0600", f"{ATTESTATION_DIR}/attestation.json"], None))
    a(f"  install -m 0644 {rd}/attestation.json {ev}/attestation.json")
    a(live("guard-probe", receipt("guard-probe"), "machine-guard-probe.json", 120))
    a("  # --- F: fault cycles (plant, partial apply, canary untouched, rollback restores)")
    for case, spec in FAULT_CASES.items():
        a(f"  # {case}: a planted symlink at the {spec['kind']} {spec['plant']}")
        if case == "F1":
            a(mstep(["/usr/bin/install", "-d", "-m", "0755", "/etc/pantheon"], None))
        if case == "F2":
            a(mstep(["/usr/bin/rmdir", "/usr/local/libexec"], None))
        if case == "F3":
            ingress, _ = SERVICE_ACCOUNTS["ingress"]
            a(mstep(["/usr/bin/install", "-d", "-o", "root", "-g", ingress, "-m", "0750",
                     "/etc/pantheon/host-approval-telegram"], None))
            a(mstep(["/usr/bin/install", "-d", "-o", ingress, "-g", ingress, "-m", "0700",
                     "/etc/pantheon/host-approval-telegram/credentials"], None))
        a(mstep(["/usr/bin/ln", "-s", spec["target"], spec["plant"]], None))
        a(tree(f"{case}-S0.json"))
        for line in rc_step(live("apply", [*receipt(case), "--execute"],
                                 f"{case}-apply.out", 1800), f"{case}-apply.rc", "= 1"):
            a(line)
        a(mstep(["/usr/bin/cat", f"{M_RECEIPTS}/{case}.json"], f"{case}-receipt.json"))
        a(tree(f"{case}-S1.json"))
        a(live("rollback", receipt(case), f"{case}-rollback.out", 900))
        a(tree(f"{case}-S2.json"))
        a(mstep(["/usr/bin/getent", "passwd", *(n for n, _ in SERVICE_ACCOUNTS.values())], f"{case}-accounts.txt"))
        a(mstep(["/usr/bin/unlink", spec["plant"]], None))
        if case == "F2":
            a(mstep(["/usr/bin/install", "-d", "-m", "0755", "/usr/local/libexec"], None))
    a("  # --- P2-P9: the live apply")
    a(live("apply", [*receipt("main"), "--execute"], "apply.out", 1800))
    a(mstep(["/usr/bin/cat", f"{M_RECEIPTS}/main.json"], "main-receipt.json"))
    a("  # probe: producer privilege drop (P6). A probe of the SAME setpriv prefix as the P6 producer argv,")
    a("  # running cat /proc/self/status; NOT the producer itself. The checker requires CapBnd 0 and NoNewPrivs 1.")
    a(mstep(PRIVDROP_PROBE_ARGV, "probe-producer-privdrop.txt"))
    a("  # --- P9: daemons (approval, then broker)")
    for role in ("approval", "broker"):
        unit, bus, _ = DAEMONS[role]
        account = SERVICE_ACCOUNTS[role][0]
        a(mstep(["/usr/bin/systemctl", "start", unit], None, 120))
        a(mstep(["/usr/bin/systemctl", "show", "--property=ActiveState", "--property=MemoryDenyWriteExecute",
                 "--property=User", "--", unit], f"unit-{role}.txt"))
        a(mstep([*dbus_call, "GetConnectionUnixUser", "s", bus], f"busowner-{role}.txt"))
        a(mstep(["/usr/bin/pgrep", "-a", "-u", account], f"proc-{role}.txt"))
        a(mstep(["/usr/bin/find", "/proc", "-ignore_readdir_race", "-mindepth", "2", "-maxdepth", "2", "-name", "cwd",
                 "-user", account, "-printf", "%l\\n"], f"cwd-{role}.txt"))
    a("  # probe: launcher import closure (the daemons' interpreter flags, run as nobody; not the daemon itself)")
    a(mstep([*NOBODY_PREFIX, "/usr/bin/python3", "-I", "-c", LAUNCHER_PROBE_CODE], "launcher-imports.json"))
    a(mstep(["/usr/bin/journalctl", "--no-pager", "-o", "cat", "-u", DAEMONS["broker"][0]], "broker-journal.txt"))
    a("  # probe: the gate-32a root-side digest over the INSTALLED framework copy (CF-11)")
    a(mstep([*NOBODY_PREFIX, "/usr/bin/python3", "-I", "-c", GATE32A_PROBE_CODE], "gate32a-root.json", 120))
    a("  # ingress (B4): steps 1-3 then fail-closed at getMe (no network, fixture token); never serves")
    for line in rc_step(mstep(["/usr/bin/systemctl", "start", INGRESS_UNIT], None, 120), "ingress-start.rc", None):
        a(line)
    a(mstep(["/usr/bin/systemctl", "stop", INGRESS_UNIT], None, 120))
    a(mstep(["/usr/bin/systemctl", "show", "--property=MemoryDenyWriteExecute", "--property=User", "--", INGRESS_UNIT],
            "unit-ingress.txt"))
    a(mstep(["/usr/bin/journalctl", "--no-pager", "-o", "cat", "-u", INGRESS_UNIT], "ingress-journal.txt"))
    for line in rc_step(mstep([*dbus_call, "GetNameOwner", "s", INGRESS_BUS_NAME], None), "ingress-owner.rc", None):
        a(line)
    a("  # probe: MemoryDenyWriteExecute + PyGObject/libffi callback in a transient unit with the ingress unit's")
    a("  # sandbox properties (B4). It is a probe of the same sandbox, NOT the ingress serve.")
    props = [f"--property={k}={v}" for k, v in ingress_sandbox_properties(repo_root)]
    a(f"  timeout 120 {_argv(['/usr/bin/systemd-run', f'--machine={m}', '--pipe', '--wait', '--quiet', *props, '--', *ENV_PREFIX, '/usr/bin/python3', '-I', '-c', MDWE_PROBE_CODE])} > {ev}/mdwe-probe.txt")
    a("  # --- P9 health (CF-7): degraded with the seat executor not activatable, then ready (B5 move-aside, B7)")
    a(mstep(["/usr/bin/sha256sum", ACTIVATION_FILE], "activation-sha-before.txt"))
    a(mstep(["/usr/bin/mv", ACTIVATION_FILE, ACTIVATION_ASIDE], None))
    a(mstep([*dbus_call, "ReloadConfig"], None))
    a(mstep(["/usr/bin/systemctl", "restart", DAEMONS["broker"][0]], None, 120))
    a(f"  sleep {HEALTH_SETTLE_SECONDS}")
    a(mstep(health_get, "health-degraded.txt"))
    a(mstep(["/usr/bin/mv", ACTIVATION_ASIDE, ACTIVATION_FILE], None))
    a(mstep(["/usr/bin/sha256sum", ACTIVATION_FILE], "activation-sha-restored.txt"))
    a(f"  cmp -s <(cut -d' ' -f1 {ev}/activation-sha-before.txt) <(cut -d' ' -f1 {ev}/activation-sha-restored.txt)")
    a(mstep([*dbus_call, "ReloadConfig"], None))
    a(f"  sleep {HEALTH_SETTLE_SECONDS}")
    a(mstep(health_get, "health-ready.txt"))
    for line in rc_step(mstep([*dbus_call, "StartServiceByName", "su", EXECUTOR_BUS_NAME, "0"], "start-service.txt"),
                        "start-service.rc", None):
        a(line)
    a(mstep([*dbus_call, "GetConnectionUnixUser", "s", EXECUTOR_BUS_NAME], "executor-owner.txt") + " || true")
    a(live("verify", receipt("main"), "verify.out", 600))
    a("  # --- P12: identical rerun (CF-8) and the token scans (CF-10)")
    a(tree("rerun-S0.json"))
    a(live("apply", [*receipt("rerun"), "--execute"], "rerun.out", 1800))
    a(mstep(["/usr/bin/cat", f"{M_RECEIPTS}/rerun.json"], "rerun-receipt.json"))
    a(tree("rerun-S1.json"))
    a(mstep(["/usr/bin/journalctl", "--no-pager", "-o", "cat"], "journal-all.txt", 120))
    for line in rc_step(mstep(["/usr/bin/grep", "-rcaF", "--exclude=bot-token", "-f", f"{M_INPUTS}/bot-token",
                               *TOKEN_SCAN_DIRS], "token-scan-machine.txt", 300), "token-scan-machine.rc", "= 1"):
        a(line)
    for line in rc_step(f"  timeout 300 grep -rlaF -f {rd}/fixture-token {ev} > {ev}/token-scan-host.txt",
                        "token-scan-host.rc", "= 1"):
        a(line)
    a("  # --- R9 host-after manifest (teardown runs on EXIT)")
    a(f"  timeout 300 {_argv([*host_manifest[:-3], f'{ev}/host-after.json', *host_manifest[-2:]])}")
    a(f"  echo 'rehearsal run {run_id} finished; check: python3 -I tools/host_broker_rehearsal.py --check-evidence {ev}'")
    a("}")
    a("")
    a('case "${1:-}" in')
    a("  prerequisites) prerequisites ;;")
    a("  run) run ;;")
    a("  rollback-prerequisites) rollback_prerequisites ;;")
    a("  *) echo 'usage: THIS prerequisites | run | rollback-prerequisites' >&2; exit 64 ;;")
    a("esac")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# script lint
# ---------------------------------------------------------------------------

#: first words a code line may start with without a timeout (builtins and trivial local ops)
_UNTIMED_OK: Final = frozenset({"test", "install", "printf", "echo", "chmod", "if", ":", "head", "rm", "rmdir", "set",
                                "trap", "umask", "export", "case", "esac", "rc=0", "sleep", "cmp", "grep",
                                "NONCE_SHA256=", "HOST_MACHINE_ID_SHA256=", "PLAN_SHA256=", "SYSTEMD_VERSION=",
                                "PREREQ_TS=",
                                "prerequisites)", "run)",
                                "rollback-prerequisites)", "*)"})
_FUNC_RE: Final = re.compile(r"[a-z_]+\(\) \{")


def lint_script(text: str, run_id: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """Every rule the emitted script must keep; empty when it holds."""

    problems: list[str] = []
    lines = text.splitlines()
    if lines[:1] != ["#!/bin/bash"]:
        problems.append("shebang")
    for required in ("set -euo pipefail", "umask 077", "  trap teardown EXIT"):
        if required not in lines:
            problems.append(f"missing: {required}")
    rd = run_dir(run_id)
    md = f"{MACHINES_DIR}/{machine_name(run_id)}/"
    rm_ok = allowed_rm_targets(run_id)
    write_ok = (rd + "/", md, CACHE_DIR + "/", *PREREQ_RECORD_FILES)
    subs_ok = {"id -u", f"sha256sum {rd}/nonce | cut -d' ' -f1", "sha256sum /etc/machine-id | cut -d' ' -f1",
               f"sha256sum {md}etc/machine-id | cut -d' ' -f1", "dpkg-query -W -f='${Version}' systemd",
               "date -u +%Y%m%dT%H%M%SZ",
               "od -An -N16 -tx1 /dev/urandom | tr -d ' \\n'",
               f"timeout 60 {_argv([*ENV_PREFIX, '/usr/bin/python3', '-I', f'{REPO_STAGE}/{HARNESS_REL}', '--plan-digest', f'{rd}/evidence/plan.json'])}"}
    nspawn_lines = []
    block = list(prea_rollback_lines())
    starts = [i for i in range(len(lines)) if lines[i:i + len(block)] == block]
    #: L5: a removal line is KNOWN only as a line of the one pinned block, in place
    known_removal = frozenset(range(starts[0], starts[0] + len(block))) if len(starts) == 1 else frozenset()
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#") or _FUNC_RE.fullmatch(line) or line in ("}", ";;"):
            continue
        code = line.split("  # ", 1)[0]
        if re.search(r"\beval\b", code) or "`" in code:
            problems.append(f"{n}: eval or backtick")
        for sub in re.findall(r"\$\(((?:[^()']|'[^']*')*)\)", code):
            if sub not in subs_ok:
                problems.append(f"{n}: command substitution not allowlisted: {sub}")
        first = code.split()[0]
        for var in ("NONCE_SHA256=", "HOST_MACHINE_ID_SHA256=", "PLAN_SHA256=", "SYSTEMD_VERSION=", "PREREQ_TS="):
            if first.startswith(var):
                first = var
        if first not in _UNTIMED_OK and first != "timeout":
            problems.append(f"{n}: {first} is not under timeout")
        if first == "test" and "||" in code:
            after = code.split("||", 1)[1].strip()
            if not (after.startswith("timeout ") or after.startswith("{ echo")):
                problems.append(f"{n}: the || branch is not under timeout")
        unquoted = re.sub(r"'[^']*'", "''", code)
        for m_ in re.finditer(r"(?:^|\s)rm\s+((?:-[a-z-]+\s+)*)(\S+)", unquoted):
            if m_.group(2) not in rm_ok:
                problems.append(f"{n}: rm target {m_.group(2)} is not an allowed literal")
        for m_ in re.finditer(r"(?<![0-9&<(])>>?\s*(\S+)", unquoted):
            target = m_.group(1).rstrip(";}")
            if target in ("/dev/null", "&2"):
                continue
            if not target.startswith(write_ok):
                problems.append(f"{n}: write outside the allowed roots: {target}")
        if re.search(r"\bapt-get (install|-s install)\b", code):
            pinned = ('"systemd-container=$SYSTEMD_VERSION" debootstrap' in code
                      and "--no-install-recommends" in code and code.startswith("timeout "))
            if not pinned:
                problems.append(f"{n}: apt-get install must be --no-install-recommends with "
                                "systemd-container pinned to the installed systemd version")
            if "apt-get install" in code and "--no-remove" not in code:
                problems.append(f"{n}: the real apt-get install must carry --no-remove")
        for m_ in re.finditer(r"\bmv -n -- (\S+) (\S+)", code):
            if m_.group(1) not in PREREQ_RECORD_FILES or m_.group(2) != f'"{PREREQ_ARCHIVE}/$PREREQ_TS/";':
                problems.append(f"{n}: mv may only archive a prereq record file into {PREREQ_ARCHIVE}/<UTC-ts>/")
        if re.search(r"(?:^|\s)mv\s", code) and "mv -n -- " not in code and "/usr/bin/mv" not in code:
            problems.append(f"{n}: a host mv outside the prereq archive step")
        for prob in apt_install_problems(code, pinned=raw == prea_install_line()):
            problems.append(f"{n}: {prob}")
        if _REMOVAL_RE.search(code) and n - 1 not in known_removal:
            problems.append(f"{n}: unknown package-removal form (only the pinned PRE-a rollback lines may remove a "
                            "package)")
        if "/usr/bin/systemd-nspawn" in code and "test -x" not in code:
            nspawn_lines.append(code)
        for m_ in re.finditer(r"-I \S*tools/host_broker_rehearsal_live\.py (\S+)", unquoted):
            if m_.group(1) not in LIVE_VERBS:
                problems.append(f"{n}: {m_.group(1)} is not a U4b verb")
    problems.extend(lint_nspawn(nspawn_lines, run_id))
    if len(starts) != 1:
        problems.append("the PRE-a rollback must be the pinned simulate, subset-guard, then purge block, "
                        "exactly once")
    else:
        rb = lines.index("rollback_prerequisites() {") if "rollback_prerequisites() {" in lines else -1
        rb_end = lines.index("}", rb) if rb >= 0 and "}" in lines[rb:] else -1
        if not (rb >= 0 and rb < starts[0] and starts[0] + len(block) <= rb_end):
            problems.append("the PRE-a rollback block must sit inside rollback_prerequisites()")
    for path in (WHEELHOUSE_HOST, M_WHEELHOUSE):
        if under_trusted_root(path):
            problems.append(f"the wheelhouse {path} sits under a trusted root")
        for st in state_directory_roots(repo_root):
            if path == st or path.startswith(st + "/"):
                problems.append(f"the wheelhouse {path} sits under StateDirectory {st}")
    return problems


def lint_nspawn(nspawn_lines: Sequence[str], run_id: str) -> list[str]:
    """Recovery D15 addition 1: exactly one boot line; -U; no writable bind; exactly the named read-only bind."""

    if len(nspawn_lines) != 1:
        return [f"expected exactly one systemd-nspawn invocation, found {len(nspawn_lines)}"]
    argv = shlex.split(nspawn_lines[0])
    argv = argv[argv.index("/usr/bin/systemd-nspawn"):]
    problems = []
    if argv != nspawn_argv(run_id):
        problems.append("the nspawn argv differs from the pinned argv")
    if "-U" not in argv:
        problems.append("nspawn: -U (private users) is missing")
    for arg in argv:
        if any(arg.startswith(p) for p in NSPAWN_FORBIDDEN_PREFIXES):
            problems.append(f"nspawn: forbidden argument {arg}")
    binds = sorted(arg for arg in argv if arg.startswith("--bind-ro="))
    if binds != sorted(f"--bind-ro={b}" for b in READ_ONLY_BINDS):
        problems.append(f"nspawn: read-only binds {binds} != {list(READ_ONLY_BINDS)}")
    return problems


# ---------------------------------------------------------------------------
# --tree-manifest (read-only)
# ---------------------------------------------------------------------------


def _tree_entry(p: Path, shown: str) -> dict[str, Any]:
    st = os.lstat(p)
    out: dict[str, Any] = {"path": shown, "uid": st.st_uid, "gid": st.st_gid,
                           "mode": format(stat.S_IMODE(st.st_mode), "04o")}
    if stat.S_ISLNK(st.st_mode):
        out.update(type="symlink", target=os.readlink(p))
        out.pop("mode")
    elif stat.S_ISDIR(st.st_mode):
        out["type"] = "dir"
    elif stat.S_ISREG(st.st_mode):
        out["type"] = "file"
        if posixpath.basename(shown) in NO_HASH_BASENAMES:
            out["sha256"] = None
        else:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                h = hashlib.sha256()
                while chunk := os.read(fd, 1 << 16):
                    h.update(chunk)
            finally:
                os.close(fd)
            out["sha256"] = h.hexdigest()
    else:
        out["type"] = "other"
    return out


class TreeBoundExceeded(ValueError):
    """A tree manifest hit its entry, byte or time bound (the step fails; never silently partial)."""


class _Budget:
    def __init__(self) -> None:
        import time

        self._clock = time.monotonic
        self.deadline = self._clock() + TREE_MAX_SECONDS
        self.entries = 0
        self.bytes = 0

    def tick(self, size: int = 0) -> None:
        self.entries += 1
        self.bytes += size
        if self.entries > TREE_MAX_ENTRIES:
            raise TreeBoundExceeded(f"tree manifest: more than {TREE_MAX_ENTRIES} entries")
        if self.bytes > TREE_MAX_BYTES:
            raise TreeBoundExceeded(f"tree manifest: more than {TREE_MAX_BYTES} bytes hashed")
        if self._clock() > self.deadline:
            raise TreeBoundExceeded(f"tree manifest: more than {TREE_MAX_SECONDS} s")


def tree_manifest(roots: Iterable[str], *, base: Path | None = None, shallow: Iterable[str] = ()) -> dict[str, Any]:
    """Every entry under each root (no symlink followed; never a token hash), bounded in
    entries, hashed bytes and time. A ``shallow`` root records only its own entry. ``base``
    re-roots the walk (a fixture root); paths are always shown host-absolute."""

    budget = _Budget()
    entries = []
    shallow = frozenset(shallow)
    norm = sorted(set(roots) | shallow)
    for root in norm:
        if not root.startswith("/") or posixpath.normpath(root) != root or root == "/":
            raise ValueError(f"tree root {root!r} is not a normalized absolute path")
        real = Path(base, root.lstrip("/")) if base is not None else Path(root)
        if not os.path.lexists(real):
            entries.append({"path": root, "type": "absent"})
            continue
        entries.append(_tree_entry(real, root))
        budget.tick(os.lstat(real).st_size)
        if root in shallow:
            continue
        if real.is_dir() and not real.is_symlink():
            for dirpath, dirnames, filenames in os.walk(real, followlinks=False):
                dirnames.sort()
                for name in sorted(dirnames + filenames):
                    p = Path(dirpath, name)
                    entries.append(_tree_entry(p, root + "/" + p.relative_to(real).as_posix()))
                    budget.tick(os.lstat(p).st_size)
    entries.sort(key=lambda e: e["path"])
    doc: dict[str, Any] = {"schema": TREE_SCHEMA, "roots": norm, "entries": entries}
    if shallow:
        doc["shallow"] = sorted(shallow)
    return doc


def host_watch_spec(plan: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """HIGH-1: (recursive roots, shallow roots) of the host-untouched manifest, DERIVED from the
    plan: every path the installer writes, the enable links and the receipts dir. A dir entry that
    is an ancestor of another written path (e.g. /var/lib/pantheon, /etc/pantheon) is SHALLOW: only
    its own entry, never the live fleet state beside the installer's targets."""

    written = set()
    for e in plan["entries"]:
        path = e.get("path")
        if not path or e["kind"] in WATCH_SKIP_KINDS:
            continue
        written.add(path)
        if e["kind"] == "unit_enable":
            written.add(f"{WANTS_DIR}/{posixpath.basename(path)}")
    written.add(M_RECEIPTS)
    shallow = sorted(p for p in written if any(q != p and q.startswith(p + "/") for q in written))
    recursive = sorted(written - set(shallow))
    return recursive, shallow


def _account_rows(base: Path | None) -> dict[str, list[str]]:
    """The passwd/group rows of WATCH_ACCOUNTS (absent rows are simply missing)."""

    out = {}
    for table in ("passwd", "group"):
        path = Path(base, "etc", table) if base is not None else Path("/etc", table)
        rows = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out[table] = sorted(r for r in rows if r.split(":", 1)[0] in WATCH_ACCOUNTS)
    return out


def host_manifest(plan: Mapping[str, Any], *, base: Path | None = None) -> dict[str, Any]:
    """The host-untouched manifest: the plan-derived roots (bounded) plus the account rows."""

    from tools import host_broker_install as H

    if not H.verify_plan_digest(plan):
        raise ValueError("the plan digest does not verify")
    recursive, shallow = host_watch_spec(plan)
    tree = tree_manifest(recursive, base=base, shallow=shallow)
    return {"schema": HOST_TREE_SCHEMA, "plan_sha256": plan["plan_sha256"], "recursive": recursive,
            "shallow": shallow, "entries": tree["entries"], "accounts": _account_rows(base)}


def tree_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    a = {e["path"]: e for e in before.get("entries", [])}
    b = {e["path"]: e for e in after.get("entries", [])}
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


# ---------------------------------------------------------------------------
# --fixture-inputs (pure; FIXTURE values only)
# ---------------------------------------------------------------------------

FIXTURE_TEMPLATE_IDS: Final = ("tools/systemd/pantheon-persona-consume@.service",
                               "tools/systemd/pantheon-persona-consume@.timer",
                               "tools/systemd/pantheon-persona-quota@.service",
                               "tools/systemd/pantheon-persona-quota@.timer",
                               "tools/systemd/pantheon-mcp-allowlist-audit.service",
                               "tools/systemd/pantheon-mcp-allowlist-audit.timer")
FIXTURE_M1: Final = b"# FIXTURE registered manifest m1 for the nspawn rehearsal (bytes only; never parsed)\n"
FIXTURE_OPERATOR_UID: Final = 2000
FIXTURE_SEATS: Final = (("rehearsal-requester", 2001, "claude"), ("rehearsal-approver", 2002, "codex"))
FIXTURE_TELEGRAM_USER_ID: Final = 4242
FIXTURE_HUMAN_SUBJECT: Final = "fixture-human"


def guard_manifest() -> dict[str, Any]:
    """B6: a separate, clearly labelled FIXTURE substrate manifest (acl, owner_mode and an
    ancestor subject the T-2920 plan does not carry). It loads under every host-substrate rule."""

    from tools.germinate.host_substrate import families as F

    return {
        "schema": F.MANIFEST_SCHEMA, "status": "fixture", "note": GUARD_MANIFEST_LABEL,
        "installer_roots": [M_GUARD_ROOT],
        "paths": {
            f"{M_GUARD_ROOT}/acl-leaf": {"owner": "root", "group": "root", "mode": "0750",
                                         "acl": ["user::rwx", "group::r-x", "other::---"]},
            f"{M_GUARD_ROOT}/owner-mode-leaf": {"owner": "root", "group": "root", "mode": "0755"},
            f"{M_GUARD_ROOT}/ancestor/child": {"owner": "root", "group": "root", "mode": "0755"},
        },
    }


def fixture_inputs(repo_root: Path = REPO_ROOT) -> dict[str, bytes]:
    """The FIXTURE input files (name -> bytes); deterministic for a given commit."""

    from tools import host_broker_install as H
    from tools import host_broker_install_apply as A
    from tools.germinate.host_substrate import families as F

    manifest = json.loads((repo_root / "tools" / "host_broker_install.manifest.example.json").read_text("utf-8"))
    manifest["approvers"]["seat_ids"] = ["rehearsal-approver"]
    # the disposable rehearsal requester is a protocol-test seat (ORG-052 E37; section 8
    # clarification, #1665); the approver seat is never one (parse_manifest refuses it)
    manifest["plan_registration"] = {"protocol_test_seat_ids": [sid for sid, _uid, _h in FIXTURE_SEATS
                                                                 if sid not in manifest["approvers"]["seat_ids"]]}
    registry = {
        "schema": H.REGISTRY_SCHEMA,
        "seats": [{"seat_id": sid, "uid": uid, "persona": sid, "role": "fixture-seat", "instance": "rehearsal",
                   "account": sid, "harness": harness, "install_root": "/"} for sid, uid, harness in FIXTURE_SEATS],
        "manifests": [{"revision": "m1", "path": "/etc/pantheon/host-broker/manifests/m1.yaml",
                       "sha256": sha256_bytes(FIXTURE_M1), "framework_revision": "runtime",
                       "framework_sha256": H.framework_artifact_digest(repo_root, list(FIXTURE_TEMPLATE_IDS)),
                       "template_ids": list(FIXTURE_TEMPLATE_IDS)}],
    }
    grants = lambda *fams: [{"family": f, "seat_classes": ["persona_seat"], "max_ttl_seconds": 600}  # noqa: E731
                            for f in fams]
    approver_policy = {
        "schema": "pantheon-approval-policy/v1", "revision": "approver-policy-fixture",
        "seat_classes": {"rehearsal-requester": "persona_seat"},
        "subjects": {"rehearsal-approver": 2002, FIXTURE_HUMAN_SUBJECT: 2005},
        "family_categories": {"persona.install": "seat_apply_reversible", "persona.verify": "diagnostics_read"},
        "approvers": {
            "rehearsal-approver": {"decision_source": "agent", "grants": grants("persona.install", "persona.verify")},
            FIXTURE_HUMAN_SUBJECT: {"decision_source": "human", "grants": grants("persona.install")},
        },
        "revokers": ["rehearsal-approver"],
    }
    host_policy = {"schema": "pantheon-host-policy/v1", "revision": "host-policy-fixture", "grants": []}
    inputs = {
        "schema": A.INPUTS_SCHEMA,
        "operator_batch": {
            "1": {"token_source_path": f"{M_INPUTS}/bot-token", "operator_uid": FIXTURE_OPERATOR_UID},
            "2": {"telegram_user_id": FIXTURE_TELEGRAM_USER_ID, "human_subject": FIXTURE_HUMAN_SUBJECT},
            "3": {"approver_policy_source": f"{M_INPUTS}/approver-policy.json",
                  "broker_policy_source": f"{M_INPUTS}/host-policy.json"},
        },
        "broker": {"host_operation_families": ["persona.install", "persona.verify"], "plan_ttl_seconds": 3600},
        "registered_manifests": {"m1": f"{M_INPUTS}/m1.yaml"},
        "wheelhouse": M_WHEELHOUSE,
    }
    guard = guard_manifest()
    F.load_manifest(guard)
    label = {"fixture": True, "label": FIXTURE_LABEL, "evidence_class": EVIDENCE_CLASS}
    docs = {"manifest.json": manifest, "registry.json": registry, "approver-policy.json": approver_policy,
            "host-policy.json": host_policy, "inputs.json": inputs, "guard-manifest.json": guard, "FIXTURE.json": label}
    out = {name: (canonical(doc) + "\n").encode("ascii") for name, doc in docs.items()}
    out["m1.yaml"] = FIXTURE_M1
    assert tuple(sorted(out)) == fixture_input_names()
    return out


def _write_new(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def write_fixture_inputs(out: Path, repo_root: Path) -> None:
    files = fixture_inputs(repo_root)
    os.mkdir(out, 0o755)
    for name, data in sorted(files.items()):
        _write_new(out / name, data)


# ---------------------------------------------------------------------------
# the gate-evidence and carry-forward tables
# ---------------------------------------------------------------------------

_LIVE_FIRE: Final = "not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs"
GATE_TABLE: Final = {
    "evidenced": {
        "E35": "rehearsal tier, FIXTURE inputs: the bootstrap installs every account, executable, unit, bus policy, "
               "registry, journal root, approver policy and human allowlist on a disposable nspawn machine with a "
               "complete receipt, and the identical rerun is a no-op. The LIVE gate still needs orchestrator's supervised fire.",
        "E32a": "the broker's logged startup_record digest equals the root-side digest over the installed framework copy",
    },
    "partial": {
        "E32": "account, owner/mode, system-unit and D-Bus-policy actions converge and roll back from a registered "
               "manifest revision; groups, mailbox dir, linger and ACL convergence are not in the T-2920 plan",
        "E19": "broker and approval run as their unprivileged service uids; the executor runs as the seat uid only "
               "when StartServiceByName is admitted",
        "E7": "runtime bytes are root-owned and not seat-writable; adapter dependency import is not exercised",
        "E21": "the fixture token never appears in the evidence, the journal, receipts, units or root configs",
    },
    "not_evidenced_left_to_live_fire": {
        **{f"E{n}": _LIVE_FIRE for n in (1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 22, 23, 24,
                                        25, 27, 28, 29, 31, 33, 34, 36, 37, 38, 39, 40, 41)},
        "E26": "fault injection over the stuck states: the compensate hook is DISABLED for the first fire (Q4)",
        "E30": "the dispatch-intent fault proof: the compensate hook is DISABLED for the first fire (Q4)",
    },
    "carry_forward_partials": {
        "CF-5:ingress": "the ingress imports dbus/gi and connects the bus, passes the uid check and the token lease, "
                        "then fails closed at getMe (no network, fixture token; the Bot API base is a constant), so it "
                        "never owns its bus name or runs a GLib callback. A transient-unit MDWE+PyGObject/libffi probe "
                        "copies its sandbox properties. The full ingress serve is left to the supervised fire "
                        "(T-2924, E38)",
        "CF-3:acl+owner_mode": "the T-2920 plan has no acl.converge subject and every owner_mode path is preceded by "
                               "its dir entry, so the RootHelper acl/owner_mode leaf and ancestor guards are reachable "
                               "only with the B6 guard manifest, which U4b's verb set (apply, verify, rollback, "
                               "guard-probe, observe-probe) does not drive; the dir-leaf, ancestor and token-destination "
                               "symlinks are evidenced through the real plan",
    },
}

#: CF id -> (title, check ids)
CF_TABLE: Final = {
    "CF-1": ("LC_ALL=C on the operator runner", ("C-ENV",)),
    "CF-2": ("systemctl show UnitFileState parsing against real systemd", ("C-SYSTEMCTL",)),
    "CF-3": ("a real symlink through the real binaries", ("C-FAULT-F1", "C-FAULT-F2", "C-FAULT-F3")),
    "CF-4": ("the live runner passes cwd through", ("C-CWD",)),
    "CF-5": ("MemoryDenyWriteExecute with PyGObject/libffi under the real dist units",
             ("C-DAEMONS", "C-INGRESS", "C-MDWE-PROBE")),
    "CF-6": ("python3 -I launchers import only from the framework rev", ("C-LAUNCHER",)),
    "CF-7": ("degraded with an executor not activatable, then ready; a real activation", ("C-HEALTH",)),
    "CF-8": ("an identical rerun makes zero mutations", ("C-RERUN",)),
    "CF-9": ("rollback from a partial-failure receipt restores the pre-apply state", ("C-ROLLBACK",)),
    "CF-10": ("the token path: create, planted symlink refused, token never in evidence", ("C-FAULT-F3", "C-TOKEN-SCAN")),
    "CF-11": ("gate 32a: startup_record digest == root-side digest", ("C-GATE32A",)),
    "CF-12": ("the E35 rehearsal receipt labelled fixture; the gate table never over-claims", ("C-LABEL",)),
}
#: CF rows that are PARTIAL by ruling even when every check passes
CF_PARTIAL_BY_RULING: Final = {"CF-5": "CF-5:ingress", "CF-3": "CF-3:acl+owner_mode"}
GENERAL_CHECKS: Final = ("C-CLOSED-SET", "C-RUN", "C-PREREQ-STATE", "C-HOST-CANARY", "C-HOST-UNTOUCHED", "C-BOOT",
                         "C-MOUNTS", "C-PLAN", "C-ATTESTATION", "C-APPLY", "C-VERIFY", "C-PRIVDROP", "C-COMPENSATE",
                         "C-Q10-BACKUPS")


def _receipt_gates(green: bool) -> dict[str, Any]:
    """LOW-9: E35/E32a are listed as evidenced ONLY on a green run."""

    gates: dict[str, Any] = {k: dict(v) for k, v in GATE_TABLE.items()}
    if not green:
        gates["withheld_run_not_green"] = gates["evidenced"]
        gates["evidenced"] = {}
    return gates


def org052_gate_ids(repo_root: Path = REPO_ROOT) -> list[str]:
    """E<n> from ORG-052's frontmatter ``elements`` plus E32a (the ORG-082 amendment gate)."""

    import yaml

    text = (repo_root / "architecture/adr/ORG-052-harness-neutral-host-capability-broker.md").read_text("utf-8")
    front = yaml.safe_load(text.split("---", 2)[1])
    return sorted({e["id"] for e in front["elements"]} | {"E32a"})


# ---------------------------------------------------------------------------
# --check-evidence
# ---------------------------------------------------------------------------


_MOUNT_ESCAPE: Final = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def _ruled_ok(rec: Mapping[str, Any], e: Mapping[str, Any], anchors: Mapping[str, Mapping[str, Any]],
              devno_count: Mapping[str, int], pinned_flags: frozenset[str]) -> bool:
    """Recovery D15 W3 (T-5703): does a parsed line match MOUNT_RULED entry ``e`` exactly?"""

    def opt(r: Mapping[str, Any], key: str) -> list[str]:
        return [o[len(key) + 1:] for o in r["super"] if o.startswith(key + "=")]

    if (rec["point"], rec["fstype"]) != (e["mountpoint"], e["fstype"]) or rec["ro"] != e["ro"]:
        return False
    if e["root"] is not None and rec["root"] != e["root"]:
        return False
    if e["root_re"] is not None and not re.fullmatch(e["root_re"], rec["root"]):
        return False
    if {f for f in rec["flags"] if f in MOUNT_RULED_FLAG_SET} != set(e["flags"]) \
            or not all(o in rec["super"] for o in e["super"]):
        return False
    if e["userns"]:
        run = anchors.get("/run")
        if run is None or len(opt(run, "uid")) != 1 or len(opt(run, "gid")) != 1 \
                or opt(rec, "uid") != opt(run, "uid") or opt(rec, "gid") != opt(run, "gid"):
            return False
    if e["source"] is not None and (e["source"] not in anchors or rec["devno"] != anchors[e["source"]]["devno"]):
        return False
    if e["fresh"] and devno_count.get(rec["devno"]) != 1:
        return False
    if e["parent"] is not None and (e["parent"] not in anchors or rec["parent"] != anchors[e["parent"]]["id"]):
        return False
    return e["requires"] is None or e["requires"] in pinned_flags


def mount_refusals(text: str) -> list[str]:
    """The P0 mountinfo against the allowlist (U4b's rules, first match wins). Every line it
    flags is reported VERBATIM as ``attestation refused: unlisted mount <line>``. There is no
    parameter: the allowlist cannot be widened from here (Recovery's standing rule)."""

    if not text.endswith("\n"):
        return ["attestation refused: mountinfo is truncated (no final newline)"]
    out: list[str] = []
    seen_root = seen_repo = 0
    exact = dict(MOUNT_VIRTUAL_EXACT)
    recs: list[dict[str, Any]] = []
    for line in text[:-1].split("\n"):
        fields = line.split(" ")
        if "-" not in fields or fields.index("-") < 6 or len(fields) - fields.index("-") != 4:
            out.append(f"attestation refused: unparsable mountinfo line {line}")
            continue
        sep = fields.index("-")
        flags = fields[5].split(",")
        if flags[0] not in ("ro", "rw") or ("ro" in flags) == ("rw" in flags):
            out.append(f"attestation refused: unparsable mountinfo line {line}")
            continue
        recs.append({"line": line, "id": fields[0], "parent": fields[1], "devno": fields[2],
                     "root": _unescape(fields[3]), "point": _unescape(fields[4]), "flags": tuple(flags),
                     "ro": flags[0] == "ro", "fstype": fields[sep + 1], "super": tuple(fields[sep + 3].split(","))})
    # Recovery D15 W3 (T-5703): the anchors, the major:minor census and the pinned-argv tie
    anchors = {}
    for point, fstype in MOUNT_RULED_ANCHORS:
        at = [r for r in recs if r["point"] == point]
        if len(at) == 1 and at[0]["fstype"] == fstype:
            anchors[point] = at[0]
    devno_count: dict[str, int] = {}
    for r in recs:
        devno_count[r["devno"]] = devno_count.get(r["devno"], 0) + 1
    pinned_flags = frozenset(nspawn_argv("0" * 12))  # W3-3 holds ONLY while --private-network is pinned
    ruled_points = {e["mountpoint"] for e in MOUNT_RULED}
    used = {e["id"]: 0 for e in MOUNT_RULED}
    for r in recs:
        line, root, point, ro, fstype = r["line"], r["root"], r["point"], r["ro"], r["fstype"]
        if point == "/":
            seen_root += 1
            continue
        if point == REPO_STAGE:
            seen_repo += 1
            if not ro:
                out.append(UNLISTED_MOUNT + line + "  (the repo bind must be read-only)")
            continue
        if point in ruled_points:  # Recovery D15 W3 (T-5703): exact on the MOUNTPOINT, no fall-through
            hit = next((e for e in MOUNT_RULED if used[e["id"]] < e["max"]
                        and _ruled_ok(r, e, anchors, devno_count, pinned_flags)), None)
            if hit is None:
                out.append(UNLISTED_MOUNT + line)
            else:
                used[hit["id"]] += 1
            continue
        if point in exact:
            if (fstype in exact[point] and root == "/") or (ro and point.startswith(MOUNT_RO_MASK_PREFIXES)):
                continue
            out.append(UNLISTED_MOUNT + line)
            continue
        prefix_rule = next((ro_req for p, fstypes, ro_req in MOUNT_VIRTUAL_PREFIX
                            if point.startswith(p) and fstype in fstypes and root == "/"), None)
        if prefix_rule is not None:
            if prefix_rule and not ro:
                out.append(UNLISTED_MOUNT + line + "  (must be read-only)")
            continue
        if point.startswith(MOUNT_RO_MASK_PREFIXES):
            if not ro:
                out.append(UNLISTED_MOUNT + line + "  (a mask must be read-only)")
            continue
        if (point, fstype) == MOUNT_CONSOLE:
            continue
        out.append(UNLISTED_MOUNT + line)
    if seen_root != 1:
        out.append(f"attestation refused: {seen_root} mounts at / (exactly one)")
    if seen_repo != 1:
        out.append(f"attestation refused: {seen_repo} mounts at {REPO_STAGE} (exactly one read-only bind)")
    return out


class EvidenceError(ValueError):
    pass


#: LOW-8: the largest evidence file the checker reads
MAX_EVIDENCE_BYTES: Final = 256 << 20


class Evidence:
    def __init__(self, root: Path):
        if root.is_symlink() or not root.is_dir():
            raise EvidenceError(f"{root}: the evidence dir must be a real directory")
        self.root = root
        self.shas: dict[str, str] = {}
        self._cache: dict[str, bytes] = {}

    def raw(self, rel: str) -> bytes:
        if rel not in self._cache:
            fd = os.open(self.root / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise EvidenceError(f"{rel}: not a regular file")
                chunks, total = [], 0
                while chunk := os.read(fd, 1 << 20):
                    total += len(chunk)
                    if total > MAX_EVIDENCE_BYTES:
                        raise EvidenceError(f"{rel}: larger than {MAX_EVIDENCE_BYTES} bytes")
                    chunks.append(chunk)
            finally:
                os.close(fd)
            self._cache[rel] = b"".join(chunks)
            self.shas[rel] = sha256_bytes(self._cache[rel])
        return self._cache[rel]

    def text(self, rel: str) -> str:
        return self.raw(rel).decode("utf-8")

    def json(self, rel: str) -> Any:
        return json.loads(self.raw(rel))

    def rc(self, rel: str) -> int:
        value = self.text(rel).strip()
        if not value.isdigit():
            raise EvidenceError(f"{rel}: not an exit status")
        return int(value)


_MUTATING: Final = frozenset({"created", "replaced", "failed", "failed-unknown"})
_DONE: Final = frozenset({"created", "replaced"})


def _receipt_ok(receipt: Mapping[str, Any]) -> bool:
    from tools import host_broker_install_apply as A

    return isinstance(receipt, dict) and receipt.get("receipt_sha256") == A.receipt_digest(receipt)


def _kv(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _busctl_value(text: str) -> str:
    """``s "ready"`` / ``u 1901`` -> the value."""

    parts = text.strip().split(" ", 1)
    if len(parts) != 2:
        raise EvidenceError(f"busctl output {text.strip()!r}")
    return parts[1].strip().strip('"')


def check_evidence(root: Path, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Validate the evidence dir; return the REHEARSAL RECEIPT (``green`` only with no failure)."""

    from tools import host_broker_install as H
    from tools._organ_lifecycle import CommandResult
    from tools.germinate.host_substrate import ports as P

    ev = Evidence(root)
    ctx: dict[str, Any] = {}
    results: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}

    def closed_set() -> list[str]:
        found = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir() or p.is_symlink())
        exp = sorted(EVIDENCE_FILES)
        out = [f"unexpected evidence file {f}" for f in found if f not in exp]
        out += [f"missing evidence file {f}" for f in exp if f not in found]
        out += [f"symlink in evidence: {p.relative_to(root)}" for p in root.rglob("*") if p.is_symlink()]
        for f in exp:  # every expected file is hashed into the receipt, read or not
            if f in found and not (root / f).is_symlink():
                ev.raw(f)
        return out

    def run_doc() -> list[str]:
        doc = ev.json("run.json")
        ctx["run"] = doc
        out = []
        if doc.get("schema") != RUN_SCHEMA or doc.get("evidence_class") != EVIDENCE_CLASS:
            out.append("run.json: schema or evidence_class")
        if doc.get("live") is not False or doc.get("fixture") is not True:
            out.append("run.json: must be live=false, fixture=true")
        if not RUN_ID_RE.fullmatch(str(doc.get("run_id"))) or doc.get("machine") != machine_name(str(doc.get("run_id"))):
            out.append("run.json: run_id / machine")
        if not COMMIT_RE.fullmatch(str(doc.get("repo_commit"))):
            out.append("run.json: repo_commit")
        for k in ("nonce_sha256", "host_machine_id_sha256"):
            if not _HEX64.fullmatch(str(doc.get(k))):
                out.append(f"run.json: {k}")
        return out

    def prereq() -> list[str]:
        lines = ev.text("host-prestate.txt").splitlines()
        out = [] if lines[:1] == ["euid 0"] and lines[-1:] == ["attestation-dir absent"] else ["host-prestate.txt shape"]
        for pkg in PREREQ_PACKAGES:
            if not ({f"{pkg} present", f"{pkg} absent"} & set(lines)):
                out.append(f"host-prestate.txt: no pre-state for {pkg}")
        return out

    def canary() -> list[str]:
        """Recovery D15 addition 2: the host guard-probe REFUSED, and its evidence shows no construction."""

        out = []
        rc = ev.rc("host-canary.rc")
        if rc != GUARD_PROBE_REFUSED_RC:
            out.append(f"host canary: guard-probe on the host returned rc {rc}, not the refusal rc {GUARD_PROBE_REFUSED_RC}")
        doc = ev.json("host-canary.json")
        if not isinstance(doc, dict) or set(doc) != GUARD_PROBE_REFUSED_KEYS:
            out.append(f"host canary: the output keys must be exactly {sorted(GUARD_PROBE_REFUSED_KEYS)} (any other key "
                       "is construction or mutation evidence)")
            return out
        if doc.get("verb") != "guard-probe" or doc.get("verdict") != "refused":
            out.append(f"host canary: verdict {doc.get('verdict')!r} is not refused")
        reasons = doc.get("reasons")
        if not isinstance(reasons, list) or not reasons or not all(isinstance(x, str) and x for x in reasons):
            out.append("host canary: the refusal must list its refusing factors")
        elif not any(x.startswith(CONTAINER_REFUSAL_PREFIX) for x in reasons):
            out.append(f"host canary: the refusal does not name the container-file factor ({CONTAINER_REFUSAL_PREFIX.strip()})")
        return out

    def host_untouched() -> list[str]:
        """HIGH-1: only the installer's own targets (derived from the plan), plus the account rows."""

        plan = ev.json("host-plan.json")
        before, after = ev.json("host-before.json"), ev.json("host-after.json")
        out = []
        if not H.verify_plan_digest(plan):
            return ["host-plan.json: the plan digest does not verify"]
        recursive, shallow = host_watch_spec(plan)
        for name, doc in (("host-before.json", before), ("host-after.json", after)):
            if doc.get("schema") != HOST_TREE_SCHEMA or doc.get("plan_sha256") != plan["plan_sha256"] \
                    or doc.get("recursive") != recursive or doc.get("shallow") != shallow:
                out.append(f"{name}: not the manifest of the roots derived from host-plan.json")
        if out:
            return out
        out += [f"host touched: {p}" for p in tree_diff(before, after)]
        if before.get("accounts") != after.get("accounts"):
            out.append("host touched: the service or fixture account rows changed")
        if ctx.get("plan_sha256") and plan["plan_sha256"] != ctx["plan_sha256"]:
            out.append("host-plan.json: the host plan differs from the machine's P1 plan (same commit and inputs)")
        return out

    def boot() -> list[str]:
        return [] if ev.text("boot-state.txt").strip() in ("running", "degraded") else ["boot-state.txt"]

    def mounts() -> list[str]:
        """Recovery D15: the P0 snapshot against the fixed allowlist; every flagged line is quoted."""

        return mount_refusals(ev.text("P0-mountinfo.txt"))

    def env_check() -> list[str]:
        out = []
        if ev.text("P0-env.txt").splitlines() != list(C_ENV_LINES):
            out.append("CF-1: the in-machine operator environment is not exactly LC_ALL=C, PATH=/usr/bin:/bin")
        for r in (ctx.get("main") or ev.json("main-receipt.json")).get("entries", []):
            if r.get("kind") == "runtime" and r.get("action") in _DONE:
                if (r.get("observations") or {}).get("env") != list(C_ENV_LINES):
                    out.append(f"CF-1: {r['id']} ran without the C environment")
        return out

    def systemctl() -> list[str]:
        out = []
        expect: dict[str, Any] = {"disabled": None, "enabled": True, "absent": None}
        for case, unit in SYSTEMCTL_PROBE_CASES:
            stdout = ev.text(f"P0-systemctl-{case}.txt")
            rc = ev.rc(f"P0-systemctl-{case}.rc")
            try:
                got: Any = P.parse_observation(f"enabled:{unit}", [CommandResult(rc, stdout, "")])
            except P.ObservationError:
                got = "refused"
            want = expect.get(case, "refused")
            if got != want:
                out.append(f"CF-2: systemctl show for {case} ({unit}) parsed {got!r}, expected {want!r} "
                           f"(raw {stdout.strip()!r}, rc {rc})")
        return out

    def plan() -> list[str]:
        doc = ev.json("plan.json")
        out = []
        if not H.verify_plan_digest(doc):
            out.append("P1: the plan digest does not verify")
        if doc.get("live_actuation_enabled") is not False:
            out.append("P1: the plan records live_actuation_enabled not False")
        if doc.get("blocking_preconditions"):
            out.append(f"P1: the real plan has non-empty blocking_preconditions {doc['blocking_preconditions']} "
                       "(apply refuses before its first action; clear them at the pinned commit)")
        ctx["plan_sha256"] = doc.get("plan_sha256")
        ctx["framework_root"] = (doc.get("framework") or {}).get("root")
        return out

    def attestation() -> list[str]:
        out = []
        doc = ev.json("attestation.json")
        run = ctx.get("run") or {}
        if not isinstance(doc, dict) or tuple(sorted(doc)) != ATTESTATION_KEYS:
            return [f"attestation.json: keys must be exactly {list(ATTESTATION_KEYS)}"]
        for k in ("machine", "run_id", "nonce_sha256", "host_machine_id_sha256"):
            if doc[k] != run.get(k):
                out.append(f"attestation.json: {k} differs from run.json")
        if doc["plan_sha256"] != ctx.get("plan_sha256"):
            out.append("attestation.json: plan_sha256 is not the P1 plan's")
        guard = ev.json("machine-guard-probe.json")
        if not isinstance(guard, dict) or set(guard) != GUARD_PROBE_ATTESTED_KEYS or guard.get("verdict") != "attested" \
                or guard.get("verb") != "guard-probe" \
                or any(guard.get(k) != doc[k] for k in ("machine", "run_id", "nonce_sha256", "plan_sha256")):
            out.append("the in-machine guard-probe did not attest this machine, run, nonce and plan")
        if doc.get("schema") != ATTESTATION_SCHEMA:
            out.append(f"attestation.json: schema must be {ATTESTATION_SCHEMA}")
        return out

    def fault(case: str) -> list[str]:
        exp = FAULT_CASES[case]
        out = []
        s0, s1, s2 = (ev.json(f"{case}-{x}.json") for x in ("S0", "S1", "S2"))
        link = {e["path"]: e for e in s0.get("entries", [])}.get(exp["plant"]) or {}
        if link.get("type") != "symlink" or link.get("target") != exp["target"]:
            out.append(f"{case}: the planted symlink {exp['plant']} -> {exp['target']} is not in the pre-apply snapshot")
        canary = [p for p in tree_diff(s0, s1) if p == exp["target"] or p.startswith(exp["target"] + "/")]
        if canary or not any(e["path"] == exp["target"] for e in s0.get("entries", [])):
            out.append(f"{case}: the symlink target {exp['target']} was mutated (or is not recorded): {canary}")
        rc = ev.json(f"{case}-receipt.json")
        failed = rc.get("failed_entry") or {}
        if ev.rc(f"{case}-apply.rc") != 1 or rc.get("status") != "partial" or not _receipt_ok(rc):
            out.append(f"{case}: the apply must stop partial (rc 1) with a digest-valid receipt")
        if failed.get("id") != exp["failed_entry"] or exp["marker"] not in str(failed.get("error", "")):
            out.append(f"{case}: failed at {failed.get('id')!r} ({failed.get('error')!r}); expected "
                       f"{exp['failed_entry']!r} refusing with {exp['marker']!r}")
        if not any(r.get("action") in _DONE for r in rc.get("entries", [])):
            out.append(f"{case}: nothing mutated before the failure (not a partial-failure receipt)")
        if rc.get("plan_sha256") != ctx.get("plan_sha256"):
            out.append(f"{case}: plan_sha256 differs from P1's")
        ctx.setdefault("faults", {})[case] = (s0, s2)
        return out

    def rollback() -> list[str]:
        out = []
        names = sorted(n for n, _ in SERVICE_ACCOUNTS.values())
        for case in FAULT_CASES:
            s0, s2 = ctx.get("faults", {}).get(case) or (ev.json(f"{case}-S0.json"), ev.json(f"{case}-S2.json"))
            rb = json.loads(ev.text(f"{case}-rollback.out").strip().splitlines()[-1])
            if rb.get("errors"):
                out.append(f"{case}: rollback errors {rb['errors']}")
            for path in tree_diff(s0, s2):
                if path in ROLLBACK_RESIDUAL_FILES or any(path == d or path.startswith(d + "/")
                                                          for d in ROLLBACK_RESIDUAL_DIRS):
                    continue
                out.append(f"{case}: rollback left {path} changed (only sticky-account residuals may differ)")
            have = sorted(line.split(":", 1)[0] for line in ev.text(f"{case}-accounts.txt").splitlines() if line)
            if have != names:
                out.append(f"{case}: accounts are sticky; after rollback found {have}")
        return out

    def apply_check() -> list[str]:
        rc = ev.json("main-receipt.json")
        ctx["main"] = rc
        out = []
        status = json.loads(ev.text("apply.out").strip().splitlines()[-1])
        if status.get("status") != "complete" or rc.get("status") != "complete" or not _receipt_ok(rc):
            out.append("apply: a complete, digest-valid receipt is required")
        if status.get("receipt_sha256") != rc.get("receipt_sha256"):
            out.append("apply: the printed receipt sha differs from the receipt")
        if rc.get("mode") in (None, "fixture"):
            out.append(f"apply: receipt mode {rc.get('mode')!r}: the rehearsal must run the live code path")
        if rc.get("live_actuation_enabled") is not False:
            out.append("apply: the receipt records LIVE_ACTUATION_ENABLED not False")
        if rc.get("plan_sha256") != ctx.get("plan_sha256"):
            out.append("apply: plan_sha256 differs from P1's")
        return out

    def cwd_check() -> list[str]:
        fw = ctx.get("framework_root")
        out = []
        rows = [r for r in (ctx.get("main") or {}).get("entries", [])
                if r.get("kind") in ("runtime", "plan_registration") and r.get("action") in _DONE]
        if not rows:
            out.append("CF-4: no runtime build or producer run recorded")
        for r in rows:
            if (r.get("observations") or {}).get("cwd") != fw:
                out.append(f"CF-4: {r['id']} ran with cwd {(r.get('observations') or {}).get('cwd')!r}, not {fw!r}")
        return out

    def privdrop() -> list[str]:
        fields = {}
        for line in ev.text("probe-producer-privdrop.txt").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip()] = v.strip()
        out = []
        if fields.get("CapBnd") != "0000000000000000":
            out.append(f"producer privilege drop: CapBnd {fields.get('CapBnd')!r} != 0000000000000000")
        if fields.get("NoNewPrivs") != "1":
            out.append(f"producer privilege drop: NoNewPrivs {fields.get('NoNewPrivs')!r} != 1")
        if fields.get("Uid", "").split()[:1] != ["65534"]:
            out.append("producer privilege drop: the probe did not run as nobody")
        return out

    def daemons() -> list[str]:
        out = []
        for role, (unit, _bus, _exe) in DAEMONS.items():
            kv = _kv(ev.text(f"unit-{role}.txt"))
            account, uid = SERVICE_ACCOUNTS[role]
            if kv.get("ActiveState") != "active" or kv.get("MemoryDenyWriteExecute") != "yes" or kv.get("User") != account:
                out.append(f"CF-5: {unit}: {kv} (active, MemoryDenyWriteExecute=yes, User={account} required)")
            if _busctl_value(ev.text(f"busowner-{role}.txt")) != str(uid):
                out.append(f"CF-5: {unit} does not own its bus name as uid {uid}")
        return out

    def ingress() -> list[str]:
        out = []
        journal = ev.text("ingress-journal.txt")
        if INGRESS_GETME_FAILED not in journal:
            out.append("CF-5 ingress: the fail-closed exit at getMe is not in the journal")
        early = [m for m in INGRESS_EARLY_FAILURES if m in journal]
        if early:
            out.append(f"CF-5 ingress: a step before getMe failed: {early}")
        if ev.rc("ingress-start.rc") == 0:
            out.append("CF-5 ingress: the unit started; with no network it must fail closed")
        if ev.rc("ingress-owner.rc") == 0:
            out.append("CF-5 ingress: the ingress bus name has an owner")
        kv = _kv(ev.text("unit-ingress.txt"))
        if kv.get("MemoryDenyWriteExecute") != "yes" or kv.get("User") != SERVICE_ACCOUNTS["ingress"][0]:
            out.append("CF-5 ingress: the unit is not MemoryDenyWriteExecute=yes as the ingress account")
        return out

    def mdwe() -> list[str]:
        lines = ev.text("mdwe-probe.txt").splitlines()
        return [] if lines == ["imports_ok", "callback_fired"] else \
            [f"MDWE probe: dbus/gi import and a GLib (libffi) callback must succeed; got {lines}"]

    def launcher() -> list[str]:
        fw = ctx.get("framework_root")
        out = []
        for role, (_unit, _bus, exe) in DAEMONS.items():
            procs = [line.split() for line in ev.text(f"proc-{role}.txt").splitlines() if line.strip()]
            if len(procs) != 1 or procs[0][1:] != ["/usr/bin/python3", "-I", exe]:
                out.append(f"CF-6: {role} must be exactly one process '/usr/bin/python3 -I {exe}', found {procs}")
            cwds = ev.text(f"cwd-{role}.txt").split()
            if cwds != [fw]:
                out.append(f"CF-6: {role} cwd {cwds} != [{fw!r}]")
        doc = ev.json("launcher-imports.json")
        if doc.get("framework_root") != fw:
            out.append("CF-6: the import probe did not resolve 'active' to the plan's framework rev")
        files = doc.get("files") or []
        if not any(str(f).startswith(f"{fw}/tools/host_control/") for f in files):
            out.append("CF-6: the import probe loaded no framework module")
        for f in files:
            if not (str(f).startswith(f"{fw}/") or str(f).startswith(STDLIB_PREFIXES)):
                out.append(f"CF-6: a module loads from outside the framework rev and the system python: {f}")
        return out

    def gate32a() -> list[str]:
        from tools.germinate.host_substrate import gate32a as G
        from tools.host_control.runtime_table import gate32a_check

        lines = [ln for ln in ev.text("broker-journal.txt").splitlines() if "startup-record " in ln]
        if len(lines) != 1:
            return [f"CF-11: exactly one startup-record line is required, found {len(lines)}"]
        record = lines[0].split("startup-record ", 1)[1]
        root_side = ev.json("gate32a-root.json")
        out = []
        if root_side.get("framework_root") != ctx.get("framework_root"):
            out.append("CF-11: the root-side digest is not over the installed framework rev")
        projection = frozenset(json.loads(record).get("dispatch_table") or ())
        out += [f"CF-11: {f}" for f in gate32a_check(root_digest=str(root_side.get("root_digest")), startup_record=record,
                                                      expected_union=projection,
                                                      expected_digest=G.runtime_subset_digest(projection))]
        return out

    def health() -> list[str]:
        out = []
        if _busctl_value(ev.text("health-degraded.txt")) != "degraded":
            out.append("CF-7: the broker was not degraded with the seat executor not activatable")
        before = ev.text("activation-sha-before.txt").split()[:1]
        restored = ev.text("activation-sha-restored.txt").split()[:1]
        if not before or before != restored:
            out.append("CF-7: the restored activation file sha differs from the sha before the move-aside (B5)")
        planned = {r.get("path"): r.get("after_sha256") for r in (ctx.get("main") or {}).get("entries", [])
                   if r.get("kind") == "activation"}
        if not before or planned.get(ACTIVATION_FILE) != before[0]:
            out.append("CF-7: the activation file is not the bytes the apply installed")
        if _busctl_value(ev.text("health-ready.txt")) != "ready":
            out.append("CF-7: the broker is not ready once the executor is activatable again")
        if ev.rc("start-service.rc") == 0:
            owner = ev.text("executor-owner.txt").strip()
            if not owner or _busctl_value(owner) != str(SEAT_UID):
                out.append(f"CF-7: the activated executor name is not owned by the seat uid {SEAT_UID}")
            statuses["CF-7"] = "evidenced"
        else:
            statuses["CF-7"] = "partial"  # B7: StartServiceByName denied; activatable-only, not a failure
        ctx["activation"] = "activated" if statuses["CF-7"] == "evidenced" else "activatable-only"
        return out

    def verify_check() -> list[str]:
        rows = [json.loads(line) for line in ev.text("verify.out").splitlines() if line.strip()]
        bad = [r.get("id") for r in rows if r.get("ok") is not True]
        return [] if rows and not bad else [f"verify: drift in {bad[:5]}"]

    def rerun() -> list[str]:
        rc = ev.json("rerun-receipt.json")
        ctx["rerun"] = rc
        out = []
        if rc.get("status") != "noop" or not _receipt_ok(rc):
            out.append(f"CF-8: the rerun is not a no-op (status {rc.get('status')})")
        mutated = [r.get("id") for r in rc.get("entries", []) if r.get("action") in _MUTATING]
        if mutated:
            out.append(f"CF-8: the rerun mutated {mutated[:5]}")
        diff = tree_diff(ev.json("rerun-S0.json"), ev.json("rerun-S1.json"))
        if diff:
            out.append(f"CF-8: the tree changed across the rerun: {diff[:5]}")
        if rc.get("plan_sha256") != ctx.get("plan_sha256"):
            out.append("CF-8: plan_sha256 differs")
        return out

    def q10() -> list[str]:
        rc = ctx.get("rerun") or ev.json("rerun-receipt.json")
        dirs = {b.get("state_dir") for b in rc.get("backups") or []}
        need = {"/var/lib/pantheon-host-broker", "/var/lib/pantheon-host-approval"}
        return [] if need <= dirs else [f"Q10: the rerun did not back up {sorted(need - dirs)} first"]

    def token_scan() -> list[str]:
        out = []
        counts = [line.rsplit(":", 1) for line in ev.text("token-scan-machine.txt").splitlines() if line]
        if ev.rc("token-scan-machine.rc") != 1 or not counts or any(c[-1] != "0" for c in counts):
            out.append("CF-10: the fixture token appears in a machine path (or the scan did not run)")
        if ev.rc("token-scan-host.rc") != 1 or ev.text("token-scan-host.txt").strip():
            out.append("CF-10: the fixture token appears in the collected evidence")
        return out

    def compensate() -> list[str]:
        return [] if (ctx.get("run") or ev.json("run.json")).get("compensate_enabled") is False else \
            ["Q4: the compensate hook must stay disabled for the first fire"]

    def label() -> list[str]:
        out = []
        ids = set().union(*(set(v) for k, v in GATE_TABLE.items() if k != "carry_forward_partials"))
        if ids != set(org052_gate_ids(repo_root)):
            out.append("CF-12: the gate table is not exactly the ORG-052 gate set")
        if set(GATE_TABLE["evidenced"]) != {"E35", "E32a"}:
            out.append("CF-12: the gate table over-claims")
        return out

    checks: list[tuple[str, Callable[[], list[str]]]] = [
        ("C-CLOSED-SET", closed_set), ("C-RUN", run_doc), ("C-PREREQ-STATE", prereq), ("C-HOST-CANARY", canary),
        ("C-BOOT", boot), ("C-MOUNTS", mounts), ("C-PLAN", plan), ("C-HOST-UNTOUCHED", host_untouched),
        ("C-ATTESTATION", attestation), ("C-APPLY", apply_check), ("C-ENV", env_check), ("C-SYSTEMCTL", systemctl),
        ("C-FAULT-F1", lambda: fault("F1")), ("C-FAULT-F2", lambda: fault("F2")), ("C-FAULT-F3", lambda: fault("F3")),
        ("C-ROLLBACK", rollback), ("C-CWD", cwd_check), ("C-PRIVDROP", privdrop), ("C-DAEMONS", daemons),
        ("C-INGRESS", ingress), ("C-MDWE-PROBE", mdwe), ("C-LAUNCHER", launcher), ("C-GATE32A", gate32a),
        ("C-HEALTH", health), ("C-VERIFY", verify_check), ("C-RERUN", rerun), ("C-Q10-BACKUPS", q10),
        ("C-TOKEN-SCAN", token_scan), ("C-COMPENSATE", compensate), ("C-LABEL", label),
    ]
    for cid, fn in checks:
        try:
            results[cid] = fn()
        except FileNotFoundError as exc:
            results[cid] = [f"missing evidence file: {Path(str(exc.filename)).name}"]
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, UnicodeDecodeError, OSError) as exc:
            results[cid] = [f"{type(exc).__name__}: {exc}"]

    cf_rows = []
    for cf, (title, cids) in CF_TABLE.items():
        failed = [f for c in cids for f in results[c]]
        status = "failed" if failed else statuses.get(cf, "evidenced")
        if status != "failed" and cf in CF_PARTIAL_BY_RULING:
            status = "partial"
        cf_rows.append({"id": cf, "title": title, "checks": list(cids), "status": status,
                        **({"partial_reason": GATE_TABLE["carry_forward_partials"][CF_PARTIAL_BY_RULING[cf]]}
                           if status == "partial" and cf in CF_PARTIAL_BY_RULING else {}),
                        **({"activation": ctx.get("activation")} if cf == "CF-7" else {})})
    failures = [f"{cid}: {msg}" for cid, _ in checks for msg in results[cid]]
    run_meta = ctx.get("run") or {}
    receipt = {
        "schema": RECEIPT_SCHEMA, "evidence_class": EVIDENCE_CLASS, "fixture_inputs": True, "live": False,
        "label": FIXTURE_LABEL, "run_id": run_meta.get("run_id"), "machine": run_meta.get("machine"),
        "repo_commit": run_meta.get("repo_commit"), "plan_sha256": ctx.get("plan_sha256"),
        "prerequisites": [{k: p[k] for k in ("id", "command", "rollback", "rollback_condition")} for p in PREREQUISITES],
        "phases": [dict(p) for p in PHASES],
        "checks": [{"id": cid, "ok": not results[cid], "failures": results[cid]} for cid, _ in checks],
        "carry_forwards": cf_rows,
        "gates": _receipt_gates(not failures),
        "evidence_files": dict(sorted(ev.shas.items())),
        "failures": failures, "green": not failures,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical(receipt).encode("ascii"))
    return receipt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-commands", action="store_true", help="print the root command script (dry)")
    mode.add_argument("--check-evidence", type=Path, metavar="DIR", help="validate a run's evidence; print the receipt")
    mode.add_argument("--fixture-inputs", type=Path, metavar="OUT", help="write the FIXTURE inputs into a new dir")
    mode.add_argument("--tree-manifest", metavar="OUT", help="write a read-only tree manifest (new file, or - for stdout)")
    mode.add_argument("--plan-digest", type=Path, metavar="FILE", help="print a U1 plan's verified plan_sha256")
    mode.add_argument("--host-manifest", type=Path, metavar="OUT",
                      help="write the plan-derived host-untouched manifest (read-only walk; new file)")
    ap.add_argument("--plan", type=Path, help="--host-manifest: the U1 plan")
    ap.add_argument("--run-id")
    ap.add_argument("--repo-commit")
    ap.add_argument("--repo", help="--emit-commands: orchestrator's checkout; --fixture-inputs: the staged tree")
    ap.add_argument("--root", action="append", default=[], help="--tree-manifest root (repeatable)")
    args = ap.parse_args(argv)
    try:
        if args.emit_commands:
            sys.stdout.write(emit_commands(args.run_id or "", args.repo_commit or "", args.repo or ""))
            return 0
        if args.fixture_inputs is not None:
            write_fixture_inputs(args.fixture_inputs, Path(args.repo) if args.repo else REPO_ROOT)
            return 0
        if args.tree_manifest is not None:
            data = (canonical(tree_manifest(args.root)) + "\n").encode("ascii")
            if args.tree_manifest == "-":
                sys.stdout.buffer.write(data)
            else:
                _write_new(Path(args.tree_manifest), data)
            return 0
        if args.host_manifest is not None:
            if args.plan is None:
                raise ValueError("--host-manifest needs --plan")
            doc = host_manifest(json.loads(args.plan.read_bytes()))
            _write_new(args.host_manifest, (canonical(doc) + "\n").encode("ascii"))
            return 0
        if args.plan_digest is not None:
            from tools import host_broker_install as H

            doc = json.loads(args.plan_digest.read_bytes())
            if not H.verify_plan_digest(doc):
                raise ValueError("the plan digest does not verify")
            print(doc["plan_sha256"])
            return 0
        receipt = check_evidence(args.check_evidence)
        sys.stdout.write(canonical(receipt) + "\n")
        return 0 if receipt["green"] else 1
    except (ValueError, OSError) as exc:
        print(f"host_broker_rehearsal: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
