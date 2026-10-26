#!/usr/bin/env python3
"""pantheon_oom_killer — the fleet's single early-OOM killer (T-5227, ORG-104 rules 1+2).

WHY. 2026-10-19 (T-5222): a runaway probe inherited a -850 oom_score_adj and outranked ~550
tiny processes, so the kernel and earlyoom killed the wrong things and the control plane fell
over. Priority-by-inheritance is the bug. This daemon replaces earlyoom and the oomd policy.

THE RULES.
  * PROTECT A LIST, NOT A LINEAGE. Protected = EXACT PIDs, recomputed every <=10 s: each seat's
    tmux server, the process(es) directly in that seat's named tmux session pane(s) (pane_pid
    plus the tty foreground-group leader), and the seat account's `systemd --user` and
    `dbus-daemon`. Children inherit nothing. oom_score_adj is never read.
  * KILL THE BIGGEST UNPROTECTED PROCESS (RSS+swap) when MemAvailable AND SwapFree are both low.
  * SESSION HARD LINE. A protected session that itself exceeds the hard line loses protection.
  * A KILL MEANS A HOLD. The victim's seat is put in seat-maintenance (re-armed until
    `--release`), told by urgent mailbox message, and while held every UNPROTECTED process of
    that seat over HELD_LINE is killed immediately, regardless of host pressure.

Every /proc read and side effect goes through `Env`, so tests use fake process tables.
Stdlib-only apart from the repo's own agent_role_map / notify_policy / mailbox (lazy imports).

HOLDS FOLLOW ANCESTRY ONLY. A seat is held only when the victim's ancestry reaches a protected
seat session (or that seat's tmux server). A victim that merely runs under a seat's uid (a
systemd --user service, e.g. the echo daemon under `claude`) is recorded with
attribution="uid:<account>", seat=null, and NO hold.

NEVER FAIL OPEN. If the seat list cannot be resolved (repo import fails AND no seats.json cache)
every non-root uid's tmux servers, pane trees and systemd --user/dbus are protected instead
(`emergency_protected`). After 3 consecutive failed ticks the loop runs `degraded_step`, which
kills under pressure using the last good protected set (or the emergency set).

TRUST EDGE (accepted, E2): the daemon runs as root and imports agent_role_map, notify_policy and
mailbox from the repo checkout (PANTHEON_CONFIG_DIR, default /home/claude/pantheon-config), which
is writable by the `claude` account. Whoever can write that checkout gets root execution in this
daemon. Accepted under the fleet's sudo-intentionally-unhardened stance; recorded, not mitigated.

FOLLOW-UP (T-3399): the trigger is MemAvailable AND SwapFree only. PSI full is recorded in every
kill record but not used as a trigger; calibrate a PSI gate from that data before adding one.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pantheon-oom-killer")

# --- tunables (constants, overridable by CLI) --------------------------------------------
LOOP_SECONDS = 1.0
# Live test 2026-10-19 17:32Z (T-5227): a 1 GB/s balloon drove SwapFree to 0% while the
# kernel still reported MemAvailable at 18-20%, so a 10%/10% trigger never fired and the
# kernel OOM killer acted first. MemAvailable overstates what is reclaimable under
# pressure; fire while there is still swap headroom.
MEM_AVAIL_PCT = 25.0          # trigger: MemAvailable below this ...
SWAP_FREE_PCT = 20.0          # ... AND SwapFree below this (skipped when no swap exists)
DEBOUNCE_TICKS = 2            # consecutive over-threshold ticks before a kill
URGENT_SWAP_PCT = 10.0        # over the line AND SwapFree below this: kill on the first tick
FAST_LOOP_SECONDS = 0.5       # poll interval while MemAvailable is below PRESSURED_MEM_PCT
PRESSURED_MEM_PCT = 30.0      # (live test: swap drained ~450 MB/s, ~2 s of margin at 20%)
COOLDOWN_SECONDS = 5.0        # after a kill, let freed memory become visible
PROTECT_REFRESH_SECONDS = 10.0
GRACE_SECONDS = 3.0           # SIGTERM -> SIGKILL
GRACE_POLL = 0.25
SOFT_LINE_KB = 1536 * 1024    # session warning
HARD_LINE_KB = 3 * 1024 * 1024  # session loses protection
SESSION_STARTUP_WINDOW = 30.0  # seconds: descendants started this soon after the pane process are session
try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):
    CLK_TCK = 100
HELD_LINE_KB = 1024 * 1024    # per-process line for a held seat's unprotected processes
WARN_INTERVAL = 600.0
HELD_NOTIFY_INTERVAL = 300.0
HOLD_MINUTES = 240            # notify_policy.MAINTENANCE_MAX_MINUTES
REARM_SECONDS = 600.0         # re-arm well inside the 240-minute registry expiry
STATE_DIR = Path("/var/lib/pantheon/oom")
MAX_RECORD_LINES = 5000
RECORD_TRIM_SLACK = 500       # trim only when this far over the cap: amortized, not per-append
HELD_LOOP_MAX = 3             # same cmdline killed this many times under the held line ...
HELD_LOOP_WINDOW = 600.0      # ... within this many seconds => stop killing it, record once
DEGRADED_AFTER_FAILS = 3
SENDER = "pantheon-oom-killer"
DEFAULT_REPO = "/home/claude/pantheon-config"


# --- data -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    comm: str
    cmdline: str
    rss_kb: int
    swap_kb: int
    start: int            # /proc/<pid>/stat starttime: pid-reuse guard
    tpgid: int = -1
    tty_nr: int = 0

    @property
    def size_kb(self) -> int:
        return self.rss_kb + self.swap_kb


@dataclass(frozen=True)
class Pane:
    server_pid: int
    session: str
    pane_pid: int


@dataclass
class SeatConfig:
    """account -> persona, plus the instance.yaml tmux session names (templated or literal)."""
    accounts: dict[str, str] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)

    def names_for(self, persona: str) -> set[str]:
        out: set[str] = set()
        for name in self.sessions:
            name = name.replace("{persona}", persona)
            if "{" not in name:
                out.add(name)
        return out


@dataclass
class Entry:
    start: int
    persona: str | None
    kind: str                       # server|pane|foreground|plumbing|failsafe-*
    sessions: set = field(default_factory=set)


@dataclass
class Protected:
    pids: dict[int, Entry] = field(default_factory=dict)       # currently protected
    owner: dict[int, str] = field(default_factory=dict)        # every seat-session pid -> persona
    sessions: dict[tuple, dict] = field(default_factory=dict)  # (persona, session) -> {kb,..}
    failsafe: bool = False

    def active(self, procs: dict[int, Proc]) -> set[int]:
        """Cached protection, dropping pids that vanished or were reused (amendment A)."""
        return {pid for pid, e in self.pids.items() if pid in procs and procs[pid].start == e.start}


class Env:
    """Real /proc, tmux, kill, registry and mailbox access. Tests subclass and fake it all."""

    def __init__(self, repo: str | None = None, state_dir: Path = STATE_DIR):
        self.repo = repo or os.environ.get("PANTHEON_CONFIG_DIR") or DEFAULT_REPO
        self.state_dir = Path(state_dir)

    # -- clocks
    def mono(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- /proc
    def meminfo(self) -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, rest = line.partition(":")
                v = rest.split()
                if v and v[0].isdigit():
                    out[k] = int(v[0])
        except OSError:
            pass
        return out

    def psi_full_avg10(self) -> float | None:
        try:
            for line in Path("/proc/pressure/memory").read_text().splitlines():
                if line.startswith("full"):
                    for tok in line.split():
                        if tok.startswith("avg10="):
                            return float(tok[6:])
        except (OSError, ValueError):
            pass
        return None

    def read_procs(self, uids: set[int] | None = None) -> dict[int, Proc]:
        out: dict[int, Proc] = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            return out
        for n in names:
            if not n.isdigit():
                continue
            try:
                if uids is not None and os.stat(f"/proc/{n}").st_uid not in uids:
                    continue
                p = self._read_one(int(n))
            except (OSError, ValueError, IndexError, UnicodeDecodeError):
                continue                       # process vanished mid-read: skip
            if p is not None:
                out[p.pid] = p
        return out

    @staticmethod
    def _read_one(pid: int) -> Proc | None:
        base = f"/proc/{pid}"
        with open(f"{base}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
        rest = stat[stat.rindex(")") + 2:].split()
        ppid, tty_nr, tpgid, start = int(rest[1]), int(rest[4]), int(rest[5]), int(rest[19])
        uid = name = None
        rss = swap = 0
        with open(f"{base}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Name:"):
                    name = line.split(None, 1)[1].strip()
                elif line.startswith("Uid:"):
                    uid = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    swap = int(line.split()[1])
        if uid is None or name is None:
            return None
        try:
            with open(f"{base}/cmdline", "rb") as fh:
                cmd = fh.read(2048).replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmd = ""
        return Proc(pid, ppid, uid, name, cmd or f"[{name}]", rss, swap, start, tpgid, tty_nr)

    # -- tmux
    def tmux_panes(self, uid: int) -> list[Pane] | None:
        """All panes of every tmux server owned by `uid`; None when tmux could not be asked."""
        panes: list[Pane] = []
        socks = sorted(glob.glob(f"/tmp/tmux-{uid}/*"))
        if not socks:
            return []
        asked = False
        for sock in socks:
            try:
                r = subprocess.run(
                    ["tmux", "-S", sock, "list-panes", "-a", "-F",
                     "#{pid}\t#{pane_pid}\t#{session_name}"],
                    capture_output=True, text=True, timeout=2)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode != 0:
                continue
            asked = True
            for line in r.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    panes.append(Pane(int(parts[0]), parts[2], int(parts[1])))
        return panes if asked else None

    # -- seats
    def _tools_on_path(self) -> None:
        tools = str(Path(self.repo) / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)

    def load_seats(self) -> SeatConfig:
        cache = self.state_dir / "seats.json"
        try:
            self._tools_on_path()
            import agent_role_map as arm
            doc = arm.load_instance_document(Path(self.repo) / "instance.yaml")
            cfg = SeatConfig(dict(arm.account_persona_map()), sorted(_walk_sessions(doc)))
            if cfg.accounts:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"accounts": cfg.accounts, "sessions": cfg.sessions}))
                except OSError:
                    pass
                return cfg
        except Exception as exc:  # noqa: BLE001 — any loader fault falls back to the cache
            log.warning("seat load failed (%s); using cache", exc)
        try:
            d = json.loads(cache.read_text())
            return SeatConfig(dict(d.get("accounts", {})), list(d.get("sessions", [])))
        except (OSError, ValueError):
            return SeatConfig()

    def uid_of(self, account: str) -> int | None:
        try:
            return pwd.getpwnam(account).pw_uid
        except KeyError:
            return None

    # -- side effects
    def send_signal(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def start_of(self, pid: int) -> int | None:
        try:
            p = self._read_one(pid)
        except (OSError, ValueError, IndexError):
            return None
        return p.start if p else None

    def write_record(self, rec: dict) -> None:
        """Append one JSONL line; trim oldest-first only when SLACK lines over the cap."""
        path = self.state_dir / "kills.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if getattr(self, "_rec_lines", None) is None:
                try:
                    self._rec_lines = len(path.read_text().splitlines())
                except OSError:
                    self._rec_lines = 0
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            self._rec_lines += 1
            if self._rec_lines > MAX_RECORD_LINES + RECORD_TRIM_SLACK:
                lines = path.read_text().splitlines()[-MAX_RECORD_LINES:]
                path.write_text("\n".join(lines) + "\n")
                self._rec_lines = len(lines)
        except OSError as exc:
            log.error("kill record write failed: %s", exc)

    def _holds_path(self) -> Path:
        return self.state_dir / "holds.json"

    def held(self) -> dict[str, dict]:
        try:
            d = json.loads(self._holds_path().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_holds(self, d: dict) -> None:
        self._holds_path().parent.mkdir(parents=True, exist_ok=True)
        self._holds_path().write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")

    def _notify_policy(self):
        self._tools_on_path()
        import notify_policy
        return notify_policy

    def place_hold(self, persona: str, reason: str) -> None:
        """Seat-maintenance entry (240 min cap) + our own record so the loop re-arms it."""
        try:
            self._notify_policy().enter_maintenance(persona, minutes=HOLD_MINUTES, by=SENDER,
                                                    reason=reason)
        except Exception as exc:  # noqa: BLE001
            log.error("enter_maintenance(%s) failed: %s", persona, exc)
        try:
            d = self.held()
            d[persona] = {"since": self.now_iso(), "reason": reason}
            self._save_holds(d)
        except OSError as exc:
            log.error("holds.json write failed: %s", exc)

    def rearm_hold(self, persona: str) -> None:
        try:
            self._notify_policy().enter_maintenance(persona, minutes=HOLD_MINUTES, by=SENDER,
                                                    reason="oom hold re-arm (unreleased)")
        except Exception as exc:  # noqa: BLE001
            log.error("hold re-arm(%s) failed: %s", persona, exc)

    def release_hold(self, persona: str) -> bool:
        """True if a hold was recorded and is now cleared, False if none was recorded.

        Raises if the hold can't be cleared (e.g. holds.json is root-owned and the caller
        isn't root): a release that didn't happen must never read as one that did. On
        2026-10-22 a non-root --release printed "released" and exited 0 while the hold stayed."""
        d = self.held()
        had = d.pop(persona, None) is not None
        if had:
            self._save_holds(d)                         # a failed write is a failed release
        try:
            self._notify_policy().exit_maintenance(persona)
        except Exception as exc:  # noqa: BLE001 — the hold itself is cleared; maintenance is secondary
            log.error("release(%s): hold cleared, but exit_maintenance failed: %s", persona, exc)
        return had

    def notify(self, persona: str, subject: str, body: str) -> None:
        try:
            self._tools_on_path()
            import agent_role_map as arm
            import mailbox
            mailbox.send(f"{arm.current_mailbox_hub()}/{persona}", {"text": body}, SENDER,
                         subject=subject, severity="urgent")
        except Exception as exc:  # noqa: BLE001 — a failed notice never blocks the kill path
            log.error("mailbox notify(%s) failed: %s", persona, exc)


def _walk_sessions(node, out: set | None = None) -> set:
    """Collect tmux session names from `tmux_session: x` and `tmux: {session: x}` anywhere."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "tmux_session" and isinstance(v, str):
                out.add(v)
            elif k == "tmux" and isinstance(v, dict) and isinstance(v.get("session"), str):
                out.add(v["session"])
            _walk_sessions(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_sessions(v, out)
    return out


# --- pure logic -----------------------------------------------------------------------------
def is_over(mem: dict[str, int], mem_pct: float = MEM_AVAIL_PCT, swap_pct: float = SWAP_FREE_PCT) -> bool:
    total = mem.get("MemTotal", 0)
    if total <= 0:
        return False
    if mem.get("MemAvailable", total) * 100.0 / total >= mem_pct:
        return False
    stot = mem.get("SwapTotal", 0)
    if stot <= 0:
        return True
    return mem.get("SwapFree", stot) * 100.0 / stot < swap_pct


def emergency_protected(procs: dict[int, Proc], hard_kb: int = HARD_LINE_KB,
                        startup_window: float = SESSION_STARTUP_WINDOW) -> Protected:
    """No seat list: protect, from /proc alone (no tmux call, no config), every non-root uid's
    tmux servers, their pane trees (startup-window rule), and systemd --user / dbus-daemon."""
    prot = Protected(failsafe=True)
    window = int(startup_window * CLK_TCK)
    children: dict[int, list[int]] = {}
    for q in procs.values():
        children.setdefault(q.ppid, []).append(q.pid)

    def add(p: Proc, kind: str) -> None:
        prot.pids.setdefault(p.pid, Entry(p.start, None, kind))

    def add_tree(root: Proc) -> None:
        add(root, "emergency-pane")
        stack, seen = [root.pid], {root.pid}
        while stack:
            for cid in children.get(stack.pop(), []):
                c = procs.get(cid)
                if c and cid not in seen and 0 <= c.start - root.start <= window:
                    seen.add(cid)
                    add(c, "emergency-startup")
                    stack.append(cid)

    for p in procs.values():
        if p.uid < 1000:
            continue
        if p.comm == "dbus-daemon" or (p.comm == "systemd" and p.ppid == 1):
            add(p, "emergency-plumbing")
        elif p.comm.startswith("tmux"):
            add(p, "emergency-server")
            for cid in children.get(p.pid, []):
                pane = procs[cid]
                add_tree(pane)
                if pane.tpgid > 0 and pane.tpgid in procs:
                    add_tree(procs[pane.tpgid])
    return prot


def compute_protected(procs: dict[int, Proc], cfg: SeatConfig, env: Env,
                      hard_kb: int = HARD_LINE_KB,
                      startup_window: float = SESSION_STARTUP_WINDOW) -> Protected:
    seat_uids: dict[int, str] = {}
    for account, persona in cfg.accounts.items():
        uid = env.uid_of(account)
        if uid is not None:
            seat_uids[uid] = persona
    if not seat_uids:
        log.error("seat list EMPTY (manifest and cache both unusable): FAIL-SAFE protecting "
                  "every non-root uid's tmux servers, pane trees and systemd --user/dbus")
        return emergency_protected(procs, hard_kb, startup_window)
    prot = Protected()
    cand: dict[int, Entry] = {}

    def add(pid: int, persona, kind: str, session=None) -> None:
        p = procs.get(pid)
        if p is None:
            return
        e = cand.setdefault(pid, Entry(p.start, persona, kind))
        if session is not None:
            e.sessions.add(session)
            if persona:
                prot.owner[pid] = persona

    children: dict[int, list[int]] = {}
    for q in procs.values():
        children.setdefault(q.ppid, []).append(q.pid)
    window_ticks = int(startup_window * CLK_TCK)

    def add_session_tree(root: int, persona, kind: str, session) -> None:
        """The root plus every descendant that started within the startup window of the ROOT's
        start time: launcher chains (wrapper -> real binary) and startup children (MCP servers)
        are part of the session; tool calls and tasks, which start later, are not."""
        r = procs.get(root)
        if r is None:
            return
        add(root, persona, kind, session)
        stack, seen = [root], {root}
        while stack:
            for cid in children.get(stack.pop(), []):
                c = procs.get(cid)
                if c is None or cid in seen or not 0 <= c.start - r.start <= window_ticks:
                    continue
                seen.add(cid)
                add(cid, persona, "startup", session)
                stack.append(cid)

    def fg_leader(pane_pid: int) -> int | None:
        p = procs.get(pane_pid)
        if p and p.tpgid > 0 and p.tpgid in procs:
            return p.tpgid
        return None

    for uid, persona in seat_uids.items():
        # core plumbing: exact comm + uid (systemd --user is parented to init)
        for p in procs.values():
            if p.uid == uid and (p.comm == "dbus-daemon" or (p.comm == "systemd" and p.ppid == 1)):
                add(p.pid, persona, "plumbing")
        panes = env.tmux_panes(uid)
        if not panes and any(p.uid == uid and p.comm.startswith("tmux") for p in procs.values()):
            panes = None      # a tmux server exists but we could not list it (odd socket path)
        if panes is None:
            # FAIL-SAFE (amendment C): tmux unaskable -> protect every tmux server of the seat
            # account, its direct pane children, and their startup trees.
            prot.failsafe = True
            for p in procs.values():
                if p.uid == uid and p.comm.startswith("tmux"):
                    add(p.pid, persona, "failsafe-server")
                    for c in procs.values():
                        if c.ppid == p.pid:
                            add_session_tree(c.pid, persona, "failsafe-pane", (persona, "?"))
                            fg = fg_leader(c.pid)
                            if fg:
                                add_session_tree(fg, persona, "failsafe-foreground", (persona, "?"))
            continue
        live = {pane.session for pane in panes}
        wanted = cfg.names_for(persona)
        if not (wanted & live):
            # The manifest names none of this account's live sessions (e.g. codex-session is
            # only prose in instance.yaml): protect ALL of its sessions, fail-safe toward
            # protection. A seat is never left naked because the manifest is incomplete.
            wanted = live
            if live:
                prot.failsafe = True
        for pane in panes:
            if pane.session not in wanted:
                continue                    # workers' own sessions are NOT protected
            key = (persona, pane.session)
            add(pane.server_pid, persona, "server", key)
            add_session_tree(pane.pane_pid, persona, "pane", key)
            fg = fg_leader(pane.pane_pid)
            if fg:
                add_session_tree(fg, persona, "foreground", key)

    # session footprints and the hard line
    for pid, e in cand.items():
        for key in e.sessions:
            s = prot.sessions.setdefault(key, {"kb": 0, "pids": set()})
            s["kb"] += procs[pid].size_kb
            s["pids"].add(pid)
    for s in prot.sessions.values():
        s["over_hard"] = s["kb"] > hard_kb
    for pid, e in cand.items():
        if e.sessions and all(prot.sessions[k]["over_hard"] for k in e.sessions):
            continue                        # every session using this pid is over the line
        prot.pids[pid] = e
    return prot


def eligible(p: Proc, self_pid: int) -> bool:
    if p.pid <= 2 or p.pid == self_pid or p.ppid == 2 or p.size_kb <= 0:
        return False
    return not (p.uid == 0 and p.ppid == 1)     # system services: kernel OOM is the last line


def select_victim(procs: dict[int, Proc], protected: set[int], self_pid: int) -> Proc | None:
    best = None
    for p in procs.values():
        if p.pid in protected or not eligible(p, self_pid):
            continue
        if best is None or (p.size_kb, p.pid) > (best.size_kb, best.pid):
            best = p
    return best


def attribute(victim: Proc, procs: dict[int, Proc], prot: Protected,
              uid_account: dict[int, str]) -> tuple[str | None, str, list[dict]]:
    """Seat = ancestry only. A uid match is recorded as "uid:<account>" but yields NO seat."""
    chain: list[dict] = []
    seat = None
    cur, seen = victim, set()
    while cur is not None and cur.pid not in seen and len(chain) < 64:
        seen.add(cur.pid)
        chain.append({"pid": cur.pid, "comm": cur.comm})
        if seat is None and cur.pid in prot.owner:
            seat = prot.owner[cur.pid]
        cur = procs.get(cur.ppid)
    if seat is not None:
        return seat, "ancestry", chain
    if victim.uid in uid_account:
        return None, f"uid:{uid_account[victim.uid]}", chain
    return None, "none", chain


# --- the daemon -----------------------------------------------------------------------------
class Killer:
    def __init__(self, env: Env, *, mem_pct=MEM_AVAIL_PCT, swap_pct=SWAP_FREE_PCT,
                 soft_kb=SOFT_LINE_KB, hard_kb=HARD_LINE_KB, held_kb=HELD_LINE_KB,
                 debounce=DEBOUNCE_TICKS, self_pid=None):
        self.env, self.mem_pct, self.swap_pct = env, mem_pct, swap_pct
        self.soft_kb, self.hard_kb, self.held_kb = soft_kb, hard_kb, held_kb
        self.debounce = debounce
        self.pressured = False
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        self.prot = Protected()
        self.cfg = SeatConfig()
        self.have_prot = False
        self.last_refresh = -1e9
        self.last_cfg = -1e9
        self.over_ticks = 0
        self.cooldown_until = 0.0
        self.last_warn: dict = {}
        self.last_held_notify: dict = {}
        self.last_rearm: dict = {}
        self.kills: list[dict] = []
        self.fails = 0
        self.held_line_hist: dict[str, list[float]] = {}
        self.held_line_looped: dict[str, float] = {}

    def uid_account(self) -> dict[int, str]:
        out = {}
        for account in self.cfg.accounts:
            uid = self.env.uid_of(account)
            if uid is not None:
                out[uid] = account
        return out

    def uid_persona(self) -> dict[int, str]:
        out = {}
        for account, persona in self.cfg.accounts.items():
            uid = self.env.uid_of(account)
            if uid is not None:
                out[uid] = persona
        return out

    def refresh(self, procs: dict[int, Proc], now: float) -> None:
        if now - self.last_cfg >= 60 or not self.cfg.accounts:
            self.cfg = self.env.load_seats()
            self.last_cfg = now
        self.prot = compute_protected(procs, self.cfg, self.env, self.hard_kb)
        self.have_prot = True
        self.last_refresh = now
        for key, s in self.prot.sessions.items():
            if s["over_hard"]:
                log.error("session %s/%s at %d MB exceeds hard line: protection withdrawn",
                          key[0], key[1], s["kb"] // 1024)
            elif s["kb"] > self.soft_kb and now - self.last_warn.get(key, -1e9) >= WARN_INTERVAL:
                self.last_warn[key] = now
                log.warning("session %s/%s at %d MB exceeds soft line", key[0], key[1], s["kb"] // 1024)

    def step(self) -> list[dict]:
        """One evaluation tick. Returns the kill records produced."""
        now = self.env.mono()
        mem = self.env.meminfo()
        over = is_over(mem, self.mem_pct, self.swap_pct)
        total = mem.get("MemTotal", 0) or 1
        self.pressured = mem.get("MemAvailable", total) * 100.0 / total < PRESSURED_MEM_PCT
        stot = mem.get("SwapTotal", 0)
        urgent = over and stot > 0 and mem.get("SwapFree", stot) * 100.0 / stot < URGENT_SWAP_PCT
        crossed = over and self.over_ticks == 0
        self.over_ticks = self.over_ticks + 1 if over else 0
        held = self.env.held()
        if not self.cfg.accounts and (held or over):
            self.cfg = self.env.load_seats()
            self.last_cfg = now
        uid_persona = self.uid_persona()
        held_uids = {u for u, p in uid_persona.items() if p in held}
        need_refresh = crossed or not self.have_prot or now - self.last_refresh >= PROTECT_REFRESH_SECONDS
        if not (over or held_uids or need_refresh):
            return []
        # Amendment A: tmux is only forked on refresh; between refreshes the cached pids are
        # reused, minus any whose start-time changed. A held-only tick scans just held uids.
        procs = self.env.read_procs(None if (over or need_refresh) else held_uids)
        if need_refresh:
            self.refresh(procs, now)
        prot_now = self.prot.active(procs)
        for persona in held:
            if now - self.last_rearm.get(persona, -1e9) >= REARM_SECONDS:
                self.last_rearm[persona] = now
                if persona in self.env.held():      # N2: re-read: --release may have raced us
                    self.env.rearm_hold(persona)
        out: list[dict] = []
        killed: set[int] = set()
        for p in sorted(procs.values(), key=lambda q: (-q.size_kb, q.pid)):
            if (p.uid in held_uids and p.pid not in prot_now and eligible(p, self.self_pid)
                    and p.size_kb > self.held_kb):
                killed.add(p.pid)
                if self._held_line_loop(p, uid_persona, now):
                    continue
                out.append(self._kill(p, procs, "held-seat-line", mem, uid_persona, now, held))
        if over and (self.over_ticks >= self.debounce or urgent) and now >= self.cooldown_until:
            victim = select_victim({k: v for k, v in procs.items() if k not in killed},
                                   prot_now, self.self_pid)
            if victim is not None:
                out.append(self._kill(victim, procs, "pressure", mem, uid_persona, now, held))
                self.cooldown_until = now + COOLDOWN_SECONDS
        return out

    def tick(self) -> None:
        """One loop iteration that outlives any fault. After DEGRADED_AFTER_FAILS consecutive
        failures, run degraded_step so the box is never left without OOM handling."""
        try:
            self.step()
            self.fails = 0
            return
        except Exception:  # noqa: BLE001 — the killer must outlive any single bad tick
            self.fails += 1
            log.exception("tick failed (%d consecutive)", self.fails)
        if self.fails >= DEGRADED_AFTER_FAILS:
            try:
                self.degraded_step()
            except Exception:  # noqa: BLE001
                log.exception("degraded step failed")

    def degraded_step(self) -> list[dict]:
        """Pressure-only kill using the last good protected set, else the emergency set.
        Runs with no tmux call and no config; logs at ERROR every time. Never fails open."""
        now = self.env.mono()
        mem = self.env.meminfo()
        if not is_over(mem, self.mem_pct, self.swap_pct):
            return []
        procs = self.env.read_procs()
        if self.have_prot and self.prot.pids:
            protected = self.prot.active(procs)
            src = "last-good"
        else:
            self.prot = emergency_protected(procs, self.hard_kb)
            protected = set(self.prot.pids)
            src = "emergency"
        log.error("DEGRADED kill path (%s protected set, %d pids)", src, len(protected))
        if now < self.cooldown_until:
            return []
        victim = select_victim(procs, protected, self.self_pid)
        if victim is None:
            return []
        self.cooldown_until = now + COOLDOWN_SECONDS
        return [self._kill(victim, procs, "pressure-degraded", mem, self.uid_persona(), now, {})]

    def _held_line_loop(self, v: Proc, uid_persona, now: float) -> bool:
        """N9: a cmdline killed HELD_LOOP_MAX times in the window is respawning (e.g. a user
        service systemd restarts). Stop killing it; record "held-line-loop" once per window."""
        key = v.cmdline[:512]
        hist = [t for t in self.held_line_hist.get(key, []) if now - t < HELD_LOOP_WINDOW]
        if len(hist) < HELD_LOOP_MAX:
            hist.append(now)
            self.held_line_hist[key] = hist
            return False
        self.held_line_hist[key] = hist
        if now - self.held_line_looped.get(key, -1e9) >= HELD_LOOP_WINDOW:
            self.held_line_looped[key] = now
            rec = {"ts": self.env.now_iso(), "reason": "held-line-loop", "pid": v.pid,
                   "comm": v.comm, "cmdline": key, "uid": v.uid, "seat": uid_persona.get(v.uid),
                   "attribution": "uid", "signal": None,
                   "note": f"{HELD_LOOP_MAX} kills in {int(HELD_LOOP_WINDOW)}s; no longer killing"}
            self.env.write_record(rec)
            self.kills.append(rec)
            log.error("held-line loop: %r killed %d times in %ds; stopped killing it",
                      key[:120], HELD_LOOP_MAX, int(HELD_LOOP_WINDOW))
        return True

    def _kill(self, v: Proc, procs, reason, mem, uid_persona, now, held) -> dict:
        seat, how, chain = attribute(v, procs, self.prot, self.uid_account())
        sig = self.terminate(v)
        rec = {
            "ts": self.env.now_iso(), "reason": reason, "pid": v.pid, "comm": v.comm,
            "cmdline": v.cmdline[:512], "uid": v.uid, "rss_kb": v.rss_kb, "swap_kb": v.swap_kb,
            "seat": seat, "attribution": how, "ancestry": chain, "signal": sig,
            "mem_available_kb": mem.get("MemAvailable"), "swap_free_kb": mem.get("SwapFree"),
            "psi_full_avg10": self.env.psi_full_avg10(), "protected_count": len(self.prot.pids),
        }
        self.env.write_record(rec)
        self.kills.append(rec)
        log.error("killed pid %d (%s, %d MB) reason=%s seat=%s sig=%s",
                  v.pid, v.comm, v.size_kb // 1024, reason, seat, sig)
        if reason.startswith("pressure") and seat:
            self.env.place_hold(seat, f"oom kill of pid {v.pid} {v.comm}")
            self._tell(seat, v, reason)
        elif reason == "held-seat-line":
            # the seat is already held (selection is by its uid); tell it why, rate-limited
            target = seat or uid_persona.get(v.uid)
            if target and now - self.last_held_notify.get(target, -1e9) >= HELD_NOTIFY_INTERVAL:
                self.last_held_notify[target] = now
                self._tell(target, v, reason)
        return rec

    def _tell(self, seat: str, v: Proc, reason: str) -> None:
        body = (f"The OOM killer terminated pid {v.pid}: {v.cmdline[:300]!r} ({v.size_kb // 1024} MB, "
                f"reason={reason}). Your seat is on HOLD: no new launches or resumed work, and any "
                f"process of yours over {self.held_kb // 1024} MB is killed immediately, until a "
                "system orchestrator or the operator releases it (pantheon_oom_killer.py --release).")
        try:
            self.env.notify(seat, f"OOM kill: {v.comm} (pid {v.pid}); seat on hold", body)
        except Exception as exc:  # noqa: BLE001 — a failed notice never blocks the kill path
            log.error("notify(%s) failed: %s", seat, exc)

    def terminate(self, v: Proc) -> str:
        """SIGTERM, grace, SIGKILL. Start-time checked first so a reused pid is never hit."""
        if self.env.start_of(v.pid) != v.start:
            return "gone"
        try:
            self.env.send_signal(v.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            log.error("no permission to signal pid %d", v.pid)
            return "failed"
        waited = 0.0
        while waited < GRACE_SECONDS:
            self.env.sleep(GRACE_POLL)
            waited += GRACE_POLL
            if self.env.start_of(v.pid) != v.start:
                return "TERM"
        try:
            self.env.send_signal(v.pid, signal.SIGKILL)
        except ProcessLookupError:
            return "TERM"
        except PermissionError:
            return "failed"
        return "KILL"


# --- CLI ------------------------------------------------------------------------------------
def describe(env: Env, k: Killer, out=print) -> None:
    """Dry-run report: protected set, pressure, and what WOULD be killed. No side effects."""
    mem = env.meminfo()
    procs = env.read_procs()
    k.refresh(procs, env.mono())
    total = mem.get("MemTotal", 1)
    swap = (mem.get("SwapFree", 0) * 100 / mem["SwapTotal"]) if mem.get("SwapTotal") else 100.0
    out(f"pressure: MemAvailable {mem.get('MemAvailable', 0) * 100 / total:.1f}% "
        f"(trigger <{k.mem_pct}%), SwapFree {swap:.1f}% (trigger <{k.swap_pct}%), "
        f"triggered={is_over(mem, k.mem_pct, k.swap_pct)}")
    out(f"seats resolved: {len(k.cfg.accounts)}; failsafe={k.prot.failsafe}")
    out(f"protected pids ({len(k.prot.pids)}):")
    for pid, e in sorted(k.prot.pids.items()):
        p = procs.get(pid)
        out(f"  {pid:>7} {e.persona or '-':<10} {e.kind:<10} {(p.comm if p else '?'):<16} "
            f"{(p.size_kb // 1024) if p else 0} MB")
    for (persona, sess), s in sorted(k.prot.sessions.items()):
        flag = "OVER HARD LINE (unprotected)" if s["over_hard"] else (
            "over soft line" if s["kb"] > k.soft_kb else "ok")
        out(f"  session {persona}/{sess}: {s['kb'] // 1024} MB {flag}")
    v = select_victim(procs, k.prot.active(procs), os.getpid())
    out("would kill if triggered: " + (
        f"pid {v.pid} {v.comm} {v.size_kb // 1024} MB cmd={v.cmdline[:120]!r}" if v else "nothing"))
    for persona in sorted(env.held()):
        out(f"held seat {persona}: its unprotected processes over {k.held_kb // 1024} MB would be killed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print protected set + would-kill; kill nothing")
    ap.add_argument("--once", action="store_true", help="single evaluation (real side effects)")
    ap.add_argument("--release", metavar="PERSONA", help="clear a seat's OOM hold and exit")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--state-dir", default=str(STATE_DIR))
    ap.add_argument("--interval", type=float, default=LOOP_SECONDS)
    ap.add_argument("--mem-pct", type=float, default=MEM_AVAIL_PCT)
    ap.add_argument("--swap-pct", type=float, default=SWAP_FREE_PCT)
    ap.add_argument("--soft-line-mb", type=int, default=SOFT_LINE_KB // 1024)
    ap.add_argument("--hard-line-mb", type=int, default=HARD_LINE_KB // 1024)
    ap.add_argument("--held-line-mb", type=int, default=HELD_LINE_KB // 1024)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(levelname)s %(message)s")
    env = Env(a.repo, Path(a.state_dir))
    if a.release:
        try:
            had = env.release_hold(a.release)
        except Exception as exc:  # noqa: BLE001
            print(f"NOT released: {a.release}'s hold is still in place ({exc}); run as root", file=sys.stderr)
            return 1
        print("released" if had else "no hold recorded (maintenance cleared anyway)")
        return 0
    k = Killer(env, mem_pct=a.mem_pct, swap_pct=a.swap_pct, soft_kb=a.soft_line_mb * 1024,
               hard_kb=a.hard_line_mb * 1024, held_kb=a.held_line_mb * 1024,
               debounce=1 if a.once else DEBOUNCE_TICKS)
    if a.dry_run:
        describe(env, k)
        return 0
    if a.once:
        k.step()
        return 0
    while True:
        k.tick()
        env.sleep(min(a.interval, FAST_LOOP_SECONDS) if k.pressured else a.interval)


if __name__ == "__main__":
    sys.exit(main())
