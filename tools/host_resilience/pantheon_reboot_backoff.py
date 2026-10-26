#!/usr/bin/env python3
"""Reboot-loop backoff and boot alert for the host self-reboot config (T-5237).

Two boot-time subcommands, run by two systemd units:

  backoff  (pantheon-reboot-backoff.service, early, before multi-user)
      Append this boot (epoch, boot id) to boots.log, count boots in the last 60 minutes (N,
      including this one) and back the panic delay off so a reboot loop slows down:
          N<=1: 10s   N=2: 60s   N=3: 300s   N>=4: HOLD
      HOLD = disarm the runtime watchdog for THIS boot only (a drop-in under /run, which is a
      tmpfs and so vanishes on the next boot by itself; nothing persistent is ever disabled),
      daemon-reexec, set kernel.panic=0 (a panic leaves the box up and visible instead of looping),
      write the LOOP_HOLD marker, log loudly, and send one urgent mail to role recovery (best effort).
      If the hold cannot be fully applied the panic delay falls back to 1800s, never to nothing.
      A clean MANUAL reboot also counts as a boot: 4 manual reboots within an hour is rare, and the
      only effect is a longer panic delay, plus a watchdog hold for that one boot.

  rearm    (manual, as root) end a hold WITHOUT a reboot: remove the /run hold drop-in,
           daemon-reexec, kernel.panic=10, verify PID 1 holds /dev/watchdog again, remove LOOP_HOLD.
           Idempotent; a no-op on a box that is not held.

  count    read-only: print N for install.sh --check.

  alert    (pantheon-boot-alert.service, after network-online)
      Send the operator one Telegram message on EVERY boot, directly through the Bot API, so the news
      reaches him even when every seat is down. Bounded retries; never blocks boot; exits 0 even
      on failure and says so loudly. Credentials come from /etc/pantheon/boot-alert.env (0600) and
      reach curl on stdin as a `-K -` config, never in argv, so they cannot show up in `ps`.

FAIL-OPEN toward normal behaviour: an unreadable or malformed boots.log counts as N=1, a
malformed line is skipped, and any failure here leaves the stock panic delay (10s) in place.

Test hooks: HR_INSTALL_ROOT prefixes every path (incl. /proc and /run), HR_NOW overrides the
clock (epoch), HR_BOOT_ID the boot id, HR_RETRY_SCALE multiplies alert retry delays (0 in tests),
PANTHEON_CONFIG_DIR locates the repo checkout for the best-effort mailbox import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

WINDOW_S = 3600
MAX_LOG_LINES = 200
LADDER = {1: 10, 2: 60, 3: 300}  # N -> panic delay seconds; N>=HOLD_AT is the hold
HOLD_AT = 4
INSTALLED = "/usr/local/sbin/pantheon_reboot_backoff.py"
REARM_HINT = f"re-arm with: sudo python3 {INSTALLED} rearm (or reboot)"
FALLBACK_PANIC_S = 1800  # used only if the hold itself cannot be applied
RETRY_DELAYS_S = (5, 15, 30, 60)  # 5 tries, ~110s of waiting in total
SENDER = "pantheon-reboot-backoff"
ROOT = os.environ.get("HR_INSTALL_ROOT", "")


def p(path: str) -> Path:
    return Path(ROOT + path)


STATE_DIR = p("/var/lib/pantheon/host-resilience")
BOOTS_LOG = STATE_DIR / "boots.log"
STATE_JSON = STATE_DIR / "state.json"
MARKER = STATE_DIR / "LOOP_HOLD"
HOLD_DROPIN = p("/run/systemd/system.conf.d/90-pantheon-watchdog-hold.conf")
PANIC = p("/proc/sys/kernel/panic")
ROOT_PATH = Path(ROOT or "/")
ALERT_ENV = p("/etc/pantheon/boot-alert.env")


def crit(msg: str) -> None:
    print(f"<2>{msg}", file=sys.stderr, flush=True)  # journal priority: crit


def info(msg: str) -> None:
    print(f"<6>{msg}", flush=True)


def now() -> int:
    return int(os.environ.get("HR_NOW") or time.time())


def boot_id() -> str:
    env = os.environ.get("HR_BOOT_ID")
    if env:
        return env
    try:
        return p("/proc/sys/kernel/random/boot_id").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def delay_for(n: int) -> int | None:
    """Panic delay for N boots in the window; None means HOLD."""
    if n >= HOLD_AT:
        return None
    return LADDER[max(n, 1)]


def parse_boots(text: str, at: int) -> list[tuple[int, str]]:
    """Valid '<epoch> <bootid>' lines inside the window; malformed lines are skipped."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        ts = int(parts[0])
        if at - WINDOW_S <= ts <= at + 60:
            out.append((ts, parts[1]))
    return out


def record_boot(at: int, bid: str) -> int:
    """Append this boot (once per boot id), trim oldest-first, return N. Fail-open: N=1."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            lines = BOOTS_LOG.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            lines = []
        if not any(len(ln.split()) == 2 and ln.split()[1] == bid for ln in lines):
            lines.append(f"{at} {bid}")
        lines = lines[-MAX_LOG_LINES:]
        tmp = BOOTS_LOG.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, BOOTS_LOG)
        return max(1, len(parse_boots("\n".join(lines), at)))
    except OSError as exc:
        crit(f"reboot-backoff: cannot update {BOOTS_LOG}: {exc}; treating as N=1 (fail-open)")
        return 1


def count_boots(at: int) -> int:
    """Read-only N for the current window (used by the alert and --check fallbacks)."""
    try:
        return max(1, len(parse_boots(BOOTS_LOG.read_text(errors="replace"), at)))
    except OSError:
        return 1


def set_panic(seconds: int) -> bool:
    try:
        PANIC.write_text(f"{seconds}\n")
        return True
    except OSError as exc:
        crit(f"reboot-backoff: cannot write {PANIC}: {exc}")
        return False


def apply_hold(n: int, at: int) -> bool:
    """Disarm the watchdog for this boot only and stop auto-reboot. True if fully applied."""
    ok = True
    try:
        HOLD_DROPIN.parent.mkdir(parents=True, exist_ok=True)
        HOLD_DROPIN.write_text(
            "# pantheon-reboot-backoff: reboot loop hold, THIS BOOT ONLY (/run is tmpfs).\n"
            "# Sorts after 10-pantheon-watchdog.conf so it wins.\n[Manager]\nRuntimeWatchdogSec=0\n")
        r = subprocess.run(["systemctl", "daemon-reexec"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            crit(f"reboot-backoff: daemon-reexec failed rc={r.returncode}")
            ok = False
    except (OSError, subprocess.SubprocessError) as exc:
        crit(f"reboot-backoff: watchdog disarm failed: {exc}")
        ok = False
    if not set_panic(0):
        ok = False
    try:
        MARKER.write_text(f"ts={at}\nboots_in_hour={n}\n")
    except OSError as exc:
        crit(f"reboot-backoff: cannot write {MARKER}: {exc}")
    return ok


def send_mail(n: int) -> None:
    """One urgent mail to role recovery. Best effort: a failure here never blocks anything."""
    try:
        repo = os.environ.get("PANTHEON_CONFIG_DIR", "/home/claude/pantheon-config")
        sys.path.insert(0, str(Path(repo) / "tools"))
        import agent_role_map as arm  # noqa: PLC0415
        import mailbox  # noqa: PLC0415
        body = (f"REBOOT LOOP: {n} boots in the last hour. Auto-reboot (kernel.panic=0) and the "
                "runtime watchdog are DISABLED for this boot; the box is staying up for inspection. "
                f"After fixing, {REARM_HINT}.")
        mailbox.send(f"{arm.current_mailbox_hub()}/recovery", {"text": body}, SENDER,
                     subject="REBOOT LOOP hold on the VPS", severity="urgent")
    except Exception as exc:  # noqa: BLE001 — paging must never block the boot path
        crit(f"reboot-backoff: mail to recovery failed (non-fatal): {exc}")


def backoff() -> int:
    at, bid = now(), boot_id()
    n = record_boot(at, bid)
    delay = delay_for(n)
    hold = delay is None
    if hold:
        crit(f"reboot-backoff: REBOOT LOOP: {n} boots in the last hour; disarming the watchdog "
             f"for this boot and setting kernel.panic=0; {REARM_HINT}")
        if not apply_hold(n, at):
            set_panic(FALLBACK_PANIC_S)
            delay = FALLBACK_PANIC_S
            crit(f"reboot-backoff: hold not fully applied; kernel.panic falls back to {delay}s")
        else:
            delay = 0
    else:
        set_panic(delay)
        try:
            MARKER.unlink()  # the loop is over (fewer than HOLD_AT boots in the hour)
        except FileNotFoundError:
            pass
        except OSError as exc:
            crit(f"reboot-backoff: cannot clear {MARKER}: {exc}")
        info(f"reboot-backoff: {n} boot(s) in the last hour; kernel.panic={delay}s")
    write_state(at, n, delay, hold)  # BEFORE the mail: the alert unit reads it
    if hold:
        send_mail(n)
    return 0


def write_state(at: int, n: int, panic: int, hold: bool) -> None:
    try:
        STATE_JSON.write_text(json.dumps({"ts": at, "n": n, "panic": panic, "hold": hold}))
    except OSError as exc:
        crit(f"reboot-backoff: cannot write {STATE_JSON}: {exc}")


def pid1_holds_watchdog() -> bool:
    try:
        for fd in (ROOT_PATH / "proc/1/fd").iterdir():
            try:
                if os.readlink(fd).endswith(("/dev/watchdog", "/dev/watchdog0")):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def rearm() -> int:
    """End a hold without a reboot. Idempotent; a no-op on a box that is not held."""
    if not HOLD_DROPIN.exists() and not MARKER.exists():
        info("reboot-backoff rearm: not held; nothing to do")
        return 0
    try:
        HOLD_DROPIN.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        crit(f"reboot-backoff rearm: cannot remove {HOLD_DROPIN}: {exc}; marker kept")
        return 1
    r = subprocess.run(["systemctl", "daemon-reexec"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        crit(f"reboot-backoff rearm: daemon-reexec failed rc={r.returncode}; marker kept")
        return 1
    if not set_panic(10):
        crit("reboot-backoff rearm: could not restore kernel.panic=10; marker kept")
        return 1
    deadline = time.time() + float(os.environ.get("HR_WAIT_SECS", "10"))
    while not pid1_holds_watchdog():
        if time.time() >= deadline:
            crit("reboot-backoff rearm: PID 1 does NOT hold the watchdog device; marker kept, "
                 "watchdog NOT re-armed (a reboot will re-arm it)")
            return 1
        time.sleep(1)
    try:
        MARKER.unlink()
    except FileNotFoundError:
        pass
    try:
        st = json.loads(STATE_JSON.read_text())
        write_state(int(st["ts"]), int(st["n"]), 10, False)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    info("reboot-backoff rearm: watchdog re-armed, kernel.panic=10, hold cleared")
    return 0


# --- alert ----------------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def alert_text() -> str:
    at = now()
    try:
        st = json.loads(STATE_JSON.read_text())
        n, panic, hold = int(st["n"]), int(st["panic"]), bool(st["hold"])
    except (OSError, ValueError, KeyError, TypeError):
        n = count_boots(at)
        try:
            panic = int(PANIC.read_text().strip())
        except (OSError, ValueError):
            panic = -1
        hold = MARKER.exists()
    ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(at))
    if hold:
        return (f"REBOOT LOOP: {n} boots in 1h. Auto-reboot and watchdog DISABLED for this boot; "
                f"box staying up for inspection. To {REARM_HINT}.")
    return f"VPS rebooted at {ts}. Reboots in last hour: {n}. Panic delay now {panic}s."


def curl_config(token: str, chat: str, text: str) -> str:
    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return (f'url = "https://api.telegram.org/bot{token}/sendMessage"\n'
            f'data-urlencode = "chat_id={q(chat)}"\n'
            f'data-urlencode = "text={q(text)}"\n')


def alert() -> int:
    try:
        env = load_env(ALERT_ENV)
        token, chat = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]
        if not token or not chat:
            raise KeyError("empty")
    except (OSError, KeyError) as exc:
        crit(f"boot-alert: NOT SENT, no usable credentials in {ALERT_ENV} ({type(exc).__name__})")
        return 0
    text = alert_text()
    scale = float(os.environ.get("HR_RETRY_SCALE", "1"))
    tries = len(RETRY_DELAYS_S) + 1
    for i in range(tries):
        try:
            r = subprocess.run(["curl", "-sS", "--fail", "--max-time", "20", "-K", "-"],
                               input=curl_config(token, chat, text), capture_output=True, text=True,
                               timeout=40)
            err = (r.stderr or "").replace(token, "<token>").strip()
            if r.returncode == 0:
                info(f"boot-alert: sent on try {i + 1}: {text}")
                return 0
            crit(f"boot-alert: try {i + 1}/{tries} failed rc={r.returncode}: {err}")
        except (OSError, subprocess.SubprocessError) as exc:
            crit(f"boot-alert: try {i + 1}/{tries} errored: {str(exc).replace(token, '<token>')}")
        if i < tries - 1:
            time.sleep(RETRY_DELAYS_S[i] * scale)
    crit(f"boot-alert: GAVE UP after {tries} tries; the operator was NOT alerted: {text}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "backoff":
            return backoff()
        if cmd == "alert":
            return alert()
        if cmd == "rearm":
            return rearm()
        if cmd == "count":  # read-only, for install.sh --check
            print(count_boots(now()))
            return 0
    except Exception as exc:  # noqa: BLE001 — never fail the boot
        crit(f"{cmd}: unexpected error (ignored, exit 0): {type(exc).__name__}: {exc}")
        return 0
    print("usage: pantheon_reboot_backoff.py backoff|alert|rearm|count", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
