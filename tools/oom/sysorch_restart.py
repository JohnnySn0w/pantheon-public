#!/usr/bin/env python3
"""tools/oom/sysorch_restart.py — ORG-104 rule 3: only system orchestrators restart
themselves (T-5226).

WHY: on 2026-10-19 the kernel OOM SIGKILLed user@1000/1001/1002. user@.service has
no Restart=, so the managers stayed dead. The runtime controllers above them
(Restart=always, RestartSec=5s, 20 starts per 10 min, no back-off) restarted into
the ongoing OOM and exhausted their start limit (01:23Z). Nothing inside the fleet
could bring the recovery seats back. The operator had to (T-5222).

WHAT: for each system orchestrator in instance.yaml (roles head-system-orchestrator
and system-orchestrator-N; today recovery on claude-fallback and watchman on
codex-fallback), and ONLY those (the operator TG 11998):
  * root drop-in on user@<uid>.service: Restart=on-failure with exponential back-off,
    a bounded start limit, and a memory gate in ExecStartPre;
  * user-level drop-in on pantheon-runtime-controller@<persona>.service: the same
    back-off and gate, and no start limit, so the recovery seat never gives up;
  * both carry ConditionPathExists=!/var/lib/pantheon/oom-hold/<persona>, so a system
    orchestrator held down on purpose is not restarted.
The system orchestrators then restore every other seat; that is not this tool.

The gate holds a start while the box is still out of memory (MemAvailable, SwapFree,
and memory PSI "full"), releases it as soon as memory is clear, and exits
EXIT_TIMEOUT after GATE_MAX_WAIT_S, so systemd's back-off takes over. It fails OPEN on
an unreadable or malformed /proc: a probe fault must never block a recovery seat.

Usage:
  sysorch_restart.py gate [--max-wait S]          # ExecStartPre
  sysorch_restart.py install [--apply] [--root D]  # dry-run unless --apply
  sysorch_restart.py uninstall [--apply] [--root D]
  sysorch_restart.py check [--root D]             # rc 0 = installed and current
Install/uninstall need root (they write /etc and other accounts' homes). After
install/uninstall: systemctl daemon-reload, plus a daemon-reload in each seat's
user manager.
"""
from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTANCE_YAML = REPO / "instance.yaml"
GATE_INSTALL_PATH = Path("/usr/local/sbin/pantheon_sysorch_restart.py")
HOLD_DIR = Path("/var/lib/pantheon/oom-hold")
HOLD_GROUP = "pantheon-health"
DROPIN_NAME_ROOT = "50-pantheon-sysorch-restart.conf"
DROPIN_NAME_CTRL = "50-pantheon-sysorch-backoff.conf"
SYSORCH_ROLE = re.compile(r"^(head-)?system-orchestrator(-\d+)?$")

# Gate thresholds. The box is "clear" when all of these hold.
MIN_AVAIL_PCT = 20.0      # MemAvailable / MemTotal
MIN_SWAP_FREE_PCT = 10.0  # SwapFree / SwapTotal (ignored when there is no swap)
MAX_FULL_AVG10 = 5.0      # /proc/pressure/memory "full avg10"
GATE_MAX_WAIT_S = 240
GATE_INTERVAL_S = 5
START_TIMEOUT_S = 360     # TimeoutStartSec; must exceed the gate's max wait
EXIT_TIMEOUT = 75         # EX_TEMPFAIL: still out of memory; let back-off retry
EXIT_UNREADABLE = 77      # EX_NOPERM: check could not read some files; run it as root


def _log(msg: str) -> None:
    print(f"[sysorch_restart] {msg}", file=sys.stderr, flush=True)


# --- gate --------------------------------------------------------------------

def _read_proc(root: Path = Path("/proc")) -> tuple[str, str]:
    return (root / "meminfo").read_text(), (root / "pressure/memory").read_text()


def _parse(meminfo: str, psi: str) -> tuple[float, float | None, float]:
    kv = {}
    for line in meminfo.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            kv[m.group(1)] = int(m.group(2))
    avail_pct = 100.0 * kv["MemAvailable"] / kv["MemTotal"]
    swap_pct = (100.0 * kv["SwapFree"] / kv["SwapTotal"]) if kv.get("SwapTotal") else None
    m = re.search(r"^full avg10=([\d.]+)", psi, re.M)
    if not m:
        raise ValueError("no 'full avg10' in pressure/memory")
    return avail_pct, swap_pct, float(m.group(1))


def _clear(avail_pct: float, swap_pct: float | None, full10: float) -> bool:
    return (avail_pct >= MIN_AVAIL_PCT and full10 <= MAX_FULL_AVG10
            and (swap_pct is None or swap_pct >= MIN_SWAP_FREE_PCT))


def gate(reader=_read_proc, max_wait: float = GATE_MAX_WAIT_S, interval: float = GATE_INTERVAL_S,
         sleep=time.sleep, clock=time.monotonic) -> int:
    deadline = clock() + max_wait
    while True:
        try:
            avail, swap, full10 = _parse(*reader())
        except (OSError, KeyError, ValueError, ZeroDivisionError) as e:
            _log(f"gate: cannot read memory state ({e}); failing open")
            return 0
        if _clear(avail, swap, full10):
            return 0
        if clock() >= deadline:
            _log(f"gate: still out of memory after {max_wait:.0f}s "
                 f"(avail {avail:.1f}%, swap free {swap}, full avg10 {full10}); deferring to back-off")
            return EXIT_TIMEOUT
        sleep(interval)


# --- seats -------------------------------------------------------------------

@dataclass(frozen=True)
class Seat:
    persona: str
    account: str
    uid: int
    gid: int
    home: Path


def load_instance(path: Path = INSTANCE_YAML) -> dict:
    import yaml  # deferred: the gate path must not depend on PyYAML
    return yaml.safe_load(path.read_text())


def system_orchestrators(instance: dict, pwlookup=pwd.getpwnam) -> list[Seat]:
    out = []
    for role, spec in (instance.get("roles") or {}).items():
        if not SYSORCH_ROLE.match(role) or not isinstance(spec, dict):
            continue
        try:
            pw = pwlookup(spec["account"])
        except KeyError:
            _log(f"skip {role}: account {spec.get('account')!r} not on this host")
            continue
        out.append(Seat(spec["persona"], spec["account"], pw.pw_uid, pw.pw_gid, Path(pw.pw_dir)))
    return out


# --- render / install --------------------------------------------------------

@dataclass(frozen=True)
class File:
    path: Path
    content: str
    owner: Seat | None  # None = root


_GATE_CMD = f"/usr/bin/python3 {GATE_INSTALL_PATH} gate"


def _root_dropin(seat: Seat) -> str:
    return f"""# ORG-104 rule 3 (T-5226): only system orchestrators restart themselves.
# Installed by tools/oom/sysorch_restart.py; do not hand-edit (check/uninstall own it).
[Unit]
ConditionPathExists=!{HOLD_DIR}/{seat.persona}
StartLimitIntervalSec=6h
StartLimitBurst=12

[Service]
Restart=on-failure
RestartSec=30s
RestartSteps=6
RestartMaxDelaySec=15min
TimeoutStartSec={START_TIMEOUT_S}
ExecStartPre=+{_GATE_CMD}
"""


def _ctrl_dropin(seat: Seat) -> str:
    return f"""# ORG-104 rule 3 (T-5226): back-off + memory gate for a system orchestrator's session.
# Installed by tools/oom/sysorch_restart.py; do not hand-edit (check/uninstall own it).
[Unit]
ConditionPathExists=!{HOLD_DIR}/{seat.persona}
StartLimitIntervalSec=0

[Service]
RestartSteps=6
RestartMaxDelaySec=10min
TimeoutStartSec={START_TIMEOUT_S}
ExecStartPre={_GATE_CMD}
"""


def render(seats: list[Seat], root: Path) -> list[File]:
    files = []
    for s in seats:
        files.append(File(root / f"etc/systemd/system/user@{s.uid}.service.d" / DROPIN_NAME_ROOT,
                          _root_dropin(s), None))
        rel_home = s.home.relative_to("/")
        files.append(File(root / rel_home / ".config/systemd/user"
                          / f"pantheon-runtime-controller@{s.persona}.service.d" / DROPIN_NAME_CTRL,
                          _ctrl_dropin(s), s))
    return files


def install(seats: list[Seat], root: Path, apply: bool, chown: bool = True,
            gate_src: Path = Path(__file__)) -> None:
    gate_dst = root / GATE_INSTALL_PATH.relative_to("/")
    hold = root / HOLD_DIR.relative_to("/")
    for f in render(seats, root):
        print(f"{'WRITE' if apply else 'DRY'} {f.path}")
        if apply:
            f.path.parent.mkdir(parents=True, exist_ok=True)
            f.path.write_text(f.content)
            if chown and f.owner:
                for p in (f.path, f.path.parent):
                    os.chown(p, f.owner.uid, f.owner.gid)
    print(f"{'WRITE' if apply else 'DRY'} {gate_dst}  (gate)")
    print(f"{'MKDIR' if apply else 'DRY'} {hold}  (2775 root:{HOLD_GROUP})")
    if apply:
        gate_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(gate_src, gate_dst)
        os.chmod(gate_dst, 0o755)
        hold.mkdir(parents=True, exist_ok=True)
        if chown:
            import grp
            os.chown(hold, 0, grp.getgrnam(HOLD_GROUP).gr_gid)
            os.chmod(hold, 0o2775)
    if not apply:
        print("dry-run: nothing written (pass --apply)")


def uninstall(seats: list[Seat], root: Path, apply: bool) -> None:
    for f in render(seats, root):
        print(f"{'REMOVE' if apply else 'DRY'} {f.path}")
        if apply and f.path.exists():
            f.path.unlink()
            try:
                f.path.parent.rmdir()
            except OSError:
                pass  # other drop-ins live there
    # The gate binary and the hold dir are left in place: removing a hold dir could
    # silently release a deliberate hold.
    if not apply:
        print("dry-run: nothing removed (pass --apply)")


def check(seats: list[Seat], root: Path) -> int:
    """Exit 0 when everything is installed as rendered, 1 on anything missing or
    drifted. T-5230: a file this account cannot read (another seat's drop-in in a
    0750 home) is UNREADABLE, not missing; when that is the only problem, exit
    EXIT_UNREADABLE so a non-root run is not mistaken for a broken install."""
    bad = unreadable = 0
    for f in render(seats, root):
        try:
            ok = f.path.read_text() == f.content
        except FileNotFoundError:
            print(f"MISSING {f.path}")
            bad += 1
            continue
        except PermissionError:
            print(f"UNREADABLE {f.path}")
            unreadable += 1
            continue
        except OSError as e:
            print(f"ERROR {f.path}: {e}")
            bad += 1
            continue
        if not ok:
            print(f"DRIFTED {f.path}")
            bad += 1
    if not (root / GATE_INSTALL_PATH.relative_to("/")).exists():
        print(f"MISSING {GATE_INSTALL_PATH}")
        bad += 1
    print(f"check: {len(seats)} system orchestrator(s), {bad} problem(s), "
          f"{unreadable} unreadable")
    if unreadable:
        print("check: some files could not be read by this account; run as root to verify them")
    if bad or not seats:
        return 1
    return EXIT_UNREADABLE if unreadable else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--max-wait", type=float, default=GATE_MAX_WAIT_S)
    for name in ("install", "uninstall", "check"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=Path("/"))
        p.add_argument("--instance", type=Path, default=INSTANCE_YAML)
        if name != "check":
            p.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "gate":
        return gate(max_wait=a.max_wait)
    seats = system_orchestrators(load_instance(a.instance))
    if a.cmd == "install":
        install(seats, a.root, a.apply, chown=(a.root == Path("/")))
        return 0
    if a.cmd == "uninstall":
        uninstall(seats, a.root, a.apply)
        return 0
    return check(seats, a.root)


if __name__ == "__main__":
    sys.exit(main())
