#!/usr/bin/env python3
"""pve2_backup_freshness.py — alert when pve2's or boxon's nightly backups go stale or fail (T-4998, T-5301).

Scope: each host's OWN guests (pve2 and boxon are standalone since the 07-26
decluster); each host is asked for its guest list and checked against its dump dir.

pve2 runs two root jobs Recovery set up after the 10-14 sdb incident (T-4992):
  - Proxmox job `recovery-nightly-archive` (02:30 local): vzdump of every guest to the
    `archive` storage (/mnt/archive/dump), keep 7 daily / 4 weekly;
  - pve2-gitdata-backup.timer (03:40 local): Forgejo's /mnt/git-data (a CT200 bind
    mount vzdump skips) to /mnt/archive/git-data, keep 7.
boxon runs Proxmox job `recovery-nightly-pve2` (01:30 local, T-5301): vzdump of every
guest to the `pve2-archive` NFS storage (pve2's /mnt/archive/boxon, mounted on boxon
at /mnt/pve/pve2-archive), keep 7 daily / 4 weekly. It is read through boxon's mount,
so a dropped mount shows up as missing dumps, not as a pass.
None of these can alert anyone on its own. Like backup_staleness_check.py, the honest
signal is FRESHNESS: every failure mode (job error, timer dead, disk full, host
down) shows up as a missing or failed newest artifact.

One ssh call per host gathers facts; evaluate() is pure. A problem exits 1 and the
unit's OnFailure routes it to Recovery. A host unreachable is ALSO exit 1 (fail-closed):
"could not look" must never read as "backups fine".

Checks, per non-template guest: its newest dump is younger than --max-age-hours
and its .log has 'Finished Backup of' and no 'ERROR:'. And the newest git-data
archive (pve2 only) is younger than --max-age-hours.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import tailnet_self  # noqa: E402  (T-5385, T-5390)

# (ssh host, dump dir on that host, git-data dir or None)
TARGETS = (
    ("pve2", "/mnt/archive/dump", "/mnt/archive/git-data"),
    ("boxon", "/mnt/pve/pve2-archive/dump", None),
)
# T-5390: guests are WEEKLY since T-5356 (the operator TG 8727: pve2 'sun 02:30', boxon
# 'sun 01:30'), so a guest's newest dump may be up to 7 days old just before Sunday's
# run; +6h covers the run itself. The git-data copy is still daily (03:40 local).
DEFAULT_MAX_AGE_HOURS = 7 * 24 + 6.0
DEFAULT_GITDATA_MAX_AGE_HOURS = 30.0
SSH_TIMEOUT_S = 90

# Runs on the host as root (fed to `python3 -` on stdin, after a DUMP_DIR/GIT_DIR
# preamble from remote_script()). Prints one JSON object. Read-only.
REMOTE = r'''
import glob, json, os, re, subprocess
guests = json.loads(subprocess.run(
    ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
    capture_output=True, text=True, check=True).stdout)
dumps = {}
for p in glob.glob(os.path.join(DUMP_DIR, "vzdump-*-*-*.zst")):
    m = re.match(r"vzdump-(qemu|lxc)-(\d+)-", os.path.basename(p))
    if not m:
        continue
    vmid = int(m.group(2)); mt = os.path.getmtime(p)
    if vmid not in dumps or mt > dumps[vmid]["mtime"]:
        log = re.sub(r"\.(vma|tar)\.zst$", ".log", p)
        try:
            txt = open(log, errors="replace").read()
        except OSError:
            txt = None
        dumps[vmid] = {"path": p, "mtime": mt,
                       "log_finished": None if txt is None else ("Finished Backup of" in txt),
                       "log_error": None if txt is None else ("ERROR:" in txt)}
git = sorted(glob.glob(os.path.join(GIT_DIR, "git-data-*.tar.zst")), key=os.path.getmtime) if GIT_DIR else []
print(json.dumps({
    "now": __import__("time").time(),
    # Only this node's guests: pve2's /etc/pve still lists boxon's guests from the
    # old cluster (status unknown), and they must not count as missing here.
    "guests": [{"vmid": g["vmid"], "name": g.get("name"), "template": g.get("template", 0)}
               for g in guests if g.get("node") == os.uname().nodename],
    "dumps": {str(k): v for k, v in dumps.items()},
    "gitdata_mtime": os.path.getmtime(git[-1]) if git else None,
}))
'''


def remote_script(dump_dir: str, git_dir: str | None) -> str:
    return f"DUMP_DIR = {dump_dir!r}\nGIT_DIR = {git_dir!r}\n" + REMOTE


def gather(host: str, dump_dir: str, git_dir: str | None, *, run=None) -> dict:
    run = subprocess.run if run is None else run  # resolved per call, so tests can patch it
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
             f"timeout {SSH_TIMEOUT_S - 10} python3 -"],
            input=remote_script(dump_dir, git_dir), capture_output=True, text=True, timeout=SSH_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host} exited {r.returncode}: {(r.stderr or '').strip()[:200]}")
    return json.loads(r.stdout)


def evaluate(facts: dict, *, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
             gitdata_max_age_hours: float = DEFAULT_GITDATA_MAX_AGE_HOURS,
             check_gitdata: bool = True) -> list[str]:
    """Problems, one line each; [] when every backup is fresh and clean."""
    now, limit = facts["now"], gitdata_max_age_hours * 3600
    problems = []
    for g in facts["guests"]:
        if g.get("template"):
            continue
        vmid, name = g["vmid"], g.get("name") or "?"
        d = facts["dumps"].get(str(vmid))
        if d is None:
            problems.append(f"guest {vmid} ({name}): no dump in archive at all")
            continue
        age = (now - d["mtime"]) / 3600
        if age > max_age_hours:
            problems.append(f"guest {vmid} ({name}): newest dump is {age:.0f}h old "
                            f"(limit {max_age_hours:g}h)")
        if d.get("log_finished") is None:
            problems.append(f"guest {vmid} ({name}): newest dump has no log")
        elif d.get("log_error") or not d.get("log_finished"):
            problems.append(f"guest {vmid} ({name}): newest dump log shows "
                            f"{'ERROR' if d.get('log_error') else 'no Finished Backup line'}")
    gm = facts.get("gitdata_mtime")
    if not check_gitdata:
        pass
    elif gm is None:
        problems.append("git-data: no archive at all in /mnt/archive/git-data")
    elif now - gm > limit:
        problems.append(f"git-data: newest archive is {(now - gm) / 3600:.0f}h old "
                        f"(limit {gitdata_max_age_hours:g}h)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                    help="guest dump age limit (weekly job, T-5390)")
    ap.add_argument("--gitdata-max-age-hours", type=float, default=DEFAULT_GITDATA_MAX_AGE_HOURS)
    a = ap.parse_args(argv)
    failed = False
    for host, dump_dir, git_dir in TARGETS:
        try:
            facts = gather(host, dump_dir, git_dir)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            off = tailnet_self.off_tailnet()
            if off:  # T-5390: this host is off the tailnet; nothing is known about the backups
                tailnet_self.report_off_tailnet("pve2-backup-freshness", off, detail=str(exc))
                continue
            print(f"pve2_backup_freshness: FAIL could not read backups on {host}: {exc}",
                  file=sys.stderr)
            failed = True
            continue
        tailnet_self.note_reachable()
        problems = evaluate(facts, max_age_hours=a.max_age_hours,
                            gitdata_max_age_hours=a.gitdata_max_age_hours,
                            check_gitdata=git_dir is not None)
        n = sum(1 for g in facts["guests"] if not g.get("template"))
        for p in problems:
            print(f"pve2_backup_freshness: FAIL {host}: {p}", file=sys.stderr)
        if problems:
            failed = True
        else:
            what = "guest(s) + git-data" if git_dir else "guest(s)"
            print(f"pve2_backup_freshness: {host}: OK {n} {what} fresh "
                  f"(guests <{a.max_age_hours:g}h, git-data <{a.gitdata_max_age_hours:g}h, logs clean) at {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
