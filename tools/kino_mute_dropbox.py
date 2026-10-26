#!/usr/bin/env python3
"""kino_mute_dropbox.py — kino's Health-section mute toggle without a privilege edge (T-5397).

kino (a tailnet-facing service, not a seat) must be able to list, mute and unmute a
persona's alerts (ORG-077 Health Plane, T-2979) without holding any way to run code as
Recovery. So kino never touches the mute registry: it drops a request file, and this tool,
running as Recovery from a systemd .path unit, validates it and applies it through
tools/notify_policy.py's writers (strict since T-5396: they refuse a registry they
cannot read instead of wiping it). The operator delegated the grant (TG 12232, 2026-10-23).

Layout (`init` creates it, idempotent; base defaults to /var/lib/pantheon/kino-mute):
  <base>/               claude-fallback, 2750 (setgid pantheon-shared), ACL u:kino:r-x
  <base>/requests/      ACL u:kino:rwx, default u:claude-fallback:rw (kino drops files)
  <base>/status.json    written by this tool; default ACL u:kino:r on <base>

REQUEST CONTRACT (kino writes <base>/requests/<id>.json: write a dotfile, then rename;
a dotfile is kino's in-progress temp: left alone while fresh, deleted once older than
60 s, an abandoned write; anything else is rejected):
  {"id": "<[A-Za-z0-9][A-Za-z0-9._-]{0,63}, equals the file stem>",
   "op": "list" | "mute" | "unmute",
   "persona": "<^[a-z0-9][a-z0-9_-]{0,63}$, required for mute/unmute; must be a persona
               the health plane publishes (/run/pantheon-health/<persona>.json)>",
   "reason": "<optional, printable, <= 200 chars; mute only>",
   "requested_by": "<optional, [A-Za-z0-9._@-]{1,64}; recorded as by=kino:<it>>"}
  Max 4096 bytes, regular file only, created mode 0644 (a 0600 file masks the ACL that
  lets Recovery read it, and is then refused as unreadable). Every request file is deleted
  once processed; at most 20 per run, the rest on the next trigger.
  status.json is also refreshed every 5 min (pantheon-kino-mute.timer, `apply --refresh`),
  so a mute made outside kino shows within 5 min; "updated" is the snapshot's time.
  The .path unit fires while requests/ is NON-EMPTY, so apply leaves it empty whenever
  it can (a directory is rmdir'd, a fresh dotfile is waited out). Only an entry Recovery
  cannot remove (a non-empty kino-made directory) keeps it firing, until systemd's start
  limit fails the unit and OnFailure pages Recovery. That is the intended loud stop.

STATUS CONTRACT (<base>/status.json, replaced atomically after every run):
  {"schema": "pantheon-kino-mute-status/v1", "updated": "<iso Z>",
   "registry_ok": true | false,
   "muted": {"<persona>": {"since": "...", "by": "...", "reason": "..."}},
   "results": [{"id": "...", "op": "...", "persona": "..."|null, "ok": true|false,
                "result": "muted"|"already muted"|"unmuted"|"was not muted"|"listed"|null,
                "error": "<why it was refused>"|null, "at": "<iso Z>"}, ... newest first, max 50]}
  kino matches its request by id. "registry_ok": false means the registry could not be
  read: nothing was written, and muted is empty (unknown, not "none").

CLI
  kino_mute_dropbox.py init  [--base DIR] [--kino-user kino]
  kino_mute_dropbox.py apply [--base DIR] [--health-dir /run/pantheon-health] [--registry P]
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import notify_policy  # noqa: E402

DEFAULT_BASE = Path("/var/lib/pantheon/kino-mute")
DEFAULT_HEALTH_DIR = Path("/run/pantheon-health")
SCHEMA = "pantheon-kino-mute-status/v1"
OPS = ("list", "mute", "unmute")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REQUESTER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
MAX_BYTES = 4096
MAX_REASON = 200
MAX_PER_RUN = 20
STALE_TMP_SECS = 60
KEEP_RESULTS = 50


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


HEALTH_FILE_MAX_BYTES = 64 * 1024


def known_personas(health_dir: Path) -> set[str]:
    """Personas the health plane publishes; only these may be muted from kino.

    A file counts only if it is a per-persona record: its JSON `persona` field equals
    its stem (what kino's feed B checks). The directory also holds non-persona files
    whose names pass PERSONA_RE, e.g. the T-5395 aggregate fleet-health.json, so the
    name alone is not enough. Unreadable or malformed files are simply not personas.
    """
    try:
        paths = list(health_dir.glob("*.json"))
    except OSError:
        return set()
    out = set()
    for p in paths:
        stem = p.stem
        if not PERSONA_RE.match(stem):
            continue
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                # Bounded: a larger file truncates here and then fails to parse.
                raw = os.read(fd, HEALTH_FILE_MAX_BYTES)
            finally:
                os.close(fd)
            doc = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("persona") == stem:
            out.add(stem)
    return out


def read_request(path: Path) -> dict:
    """Open without following links, insist on a small regular file, parse JSON."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > MAX_BYTES:
            raise ValueError(f"larger than {MAX_BYTES} bytes")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


def validate(doc: dict, stem: str, personas: set[str]) -> tuple[dict, Optional[str]]:
    """(normalized request, error or None)."""
    rid = doc.get("id")
    req = {"id": rid if isinstance(rid, str) and ID_RE.match(rid) else stem,
           "op": doc.get("op"), "persona": doc.get("persona")}
    if not isinstance(rid, str) or not ID_RE.match(rid):
        return req, "bad id"
    if rid != stem:
        return req, "id does not match the file name"
    if doc.get("op") not in OPS:
        return req, f"op must be one of {', '.join(OPS)}"
    if doc["op"] == "list":
        req["persona"] = None
        return req, None
    persona = doc.get("persona")
    if not isinstance(persona, str) or not PERSONA_RE.match(persona):
        return req, "bad persona"
    if persona not in personas:
        return req, "persona is not published by the health plane"
    reason = doc.get("reason", "")
    if not isinstance(reason, str) or len(reason) > MAX_REASON or not reason.isprintable():
        return req, f"reason must be printable text of at most {MAX_REASON} chars"
    by = doc.get("requested_by", "kino")
    if not isinstance(by, str) or not REQUESTER_RE.match(by):
        return req, "bad requested_by"
    req.update(reason=reason, requested_by=by)
    return req, None


def apply_one(req: dict, registry=None) -> dict:
    out = {"id": req["id"], "op": req["op"], "persona": req.get("persona"),
           "ok": True, "result": None, "error": None, "at": _now()}
    try:
        if req["op"] == "list":
            out["result"] = "listed"
        elif req["op"] == "mute":
            newly = notify_policy.mute(req["persona"], by=f"kino:{req['requested_by']}",
                                       reason=req["reason"], registry=registry)
            out["result"] = "muted" if newly else "already muted"
        else:
            was = notify_policy.unmute(req["persona"], registry=registry)
            out["result"] = "unmuted" if was else "was not muted"
    except notify_policy.RegistryUnreadable as exc:
        out.update(ok=False, error=f"registry unreadable, nothing written: {exc}")
    return out


def _load_status(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(base: Path, results: list[dict], registry) -> bool:
    """Rewrite status.json atomically; returns registry_ok."""
    status_path = base / "status.json"
    prev = _load_status(status_path).get("results") or []
    try:
        notify_policy._load_strict(notify_policy._registry_path(registry), "muted",
                                   notify_policy.SCHEMA)
        registry_ok, muted = True, notify_policy.list_muted(registry)
    except notify_policy.RegistryUnreadable:
        registry_ok, muted = False, {}
    doc = {"schema": SCHEMA, "updated": _now(), "registry_ok": registry_ok, "muted": muted,
           "results": (list(reversed(results)) + list(prev))[:KEEP_RESULTS]}
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o640)   # with the base dir's default ACL: kino reads it
        os.replace(tmp, status_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return registry_ok


def _remove(path: Path, log) -> None:
    """Delete a processed entry without following it; rmdir a directory."""
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.rmdir(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # e.g. a non-empty directory: refuse it, keep the run going
        log(f"[kino_mute_dropbox] {path.name}: cannot remove: {exc.__class__.__name__}")


def _settle_temps(reqdir: Path, log, sleep, clock) -> None:
    """Leave no dotfile behind to re-fire the DirectoryNotEmpty watch: wait a fresh
    one out (kino renames it within milliseconds; a renamed request re-fires the watch
    itself), delete one older than STALE_TMP_SECS as an abandoned write."""
    deadline = clock() + STALE_TMP_SECS + 1
    while True:
        fresh = False
        try:
            names = [n for n in os.listdir(reqdir) if n.startswith(".")]
        except FileNotFoundError:
            return
        for n in names:
            p = reqdir / n
            try:
                age = clock() - os.lstat(p).st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_TMP_SECS:
                log(f"[kino_mute_dropbox] {n}: abandoned temp ({int(age)}s old), deleted")
                _remove(p, log)
            else:
                fresh = True
        if not fresh or clock() >= deadline:
            return
        sleep(1)


def apply(base: Path = DEFAULT_BASE, *, health_dir: Path = DEFAULT_HEALTH_DIR,
          registry=None, log=print, sleep=time.sleep, clock=time.time,
          refresh: bool = False) -> list[dict]:
    """Process up to MAX_PER_RUN request files (oldest name first), delete each, and
    rewrite status.json. Returns the results (also logged one line each). refresh=True
    rewrites status.json even with no requests, so a mute made elsewhere (the
    notify_policy CLI, cap_mute_driver) reaches kino; an unreadable registry is then
    reported as a "registry unreadable" result so the unit fails."""
    reqdir = base / "requests"
    personas = known_personas(health_dir)
    try:
        names = sorted(n for n in os.listdir(reqdir) if not n.startswith("."))
    except FileNotFoundError:
        names = []
    results = []
    for name in names[:MAX_PER_RUN]:
        path = reqdir / name
        stem = name[:-5] if name.endswith(".json") else name
        try:
            if not name.endswith(".json"):
                raise ValueError("not a .json request")
            doc = read_request(path)
            req, err = validate(doc, stem, personas)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            req, err = {"id": stem if ID_RE.match(stem) else "?", "op": None, "persona": None}, \
                f"unreadable request: {exc.__class__.__name__}: {str(exc)[:120]}"
        if err:
            res = {"id": req["id"], "op": req.get("op") if req.get("op") in OPS else None,
                   "persona": req.get("persona") if isinstance(req.get("persona"), str)
                   and PERSONA_RE.match(req["persona"]) else None,
                   "ok": False, "result": None, "error": err, "at": _now()}
        else:
            res = apply_one(req, registry)
        results.append(res)
        log(f"[kino_mute_dropbox] {res['id']}: op={res['op']} persona={res['persona']} "
            f"ok={res['ok']} result={res['result']} error={res['error']}")
        _remove(path, log)
    _settle_temps(reqdir, log, sleep, clock)
    if results or refresh or not (base / "status.json").exists():
        ok = _write_status(base, results, registry)
        if refresh and not ok and not any(
                (r.get("error") or "").startswith("registry unreadable") for r in results):
            log("[kino_mute_dropbox] refresh: registry unreadable, status says registry_ok=false")
            return results + [{"id": "refresh", "op": None, "persona": None, "ok": False,
                               "result": None, "error": "registry unreadable (refresh)",
                               "at": _now()}]
    return results


def init(base: Path = DEFAULT_BASE, kino_user: str = "kino", recovery_user: Optional[str] = None,
         run=subprocess.run) -> None:
    """Create the dropbox and its ACLs. Idempotent; needs setfacl."""
    recovery_user = recovery_user or os.environ.get("USER", "claude-fallback")
    (base / "requests").mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o2750)
    # Owner-only by mode; kino gets in by ACL. No group bits: other pantheon-shared
    # accounts must not be able to drop requests.
    os.chmod(base / "requests", 0o2700)
    acls = [
        [str(base), f"u:{kino_user}:r-x", f"d:u:{kino_user}:r"],
        [str(base / "requests"), f"u:{kino_user}:rwx", f"d:u:{recovery_user}:rw"],
    ]
    for path, *entries in acls:
        for e in entries:
            run(["setfacl", "-m", e, path], check=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--base", type=Path, default=DEFAULT_BASE)
    i.add_argument("--kino-user", default="kino")
    a_ = sub.add_parser("apply")
    a_.add_argument("--base", type=Path, default=DEFAULT_BASE)
    a_.add_argument("--health-dir", type=Path, default=DEFAULT_HEALTH_DIR)
    a_.add_argument("--registry", default=None)
    a_.add_argument("--refresh", action="store_true",
                    help="rewrite status.json even with no requests (the timer's fire)")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        init(a.base, a.kino_user)
        return 0
    results = apply(a.base, health_dir=a.health_dir, registry=a.registry, refresh=a.refresh)
    # A refused request is kino's to read in status.json, not a page. Only an unreadable
    # registry (a real fault on our side) fails the unit, so OnFailure reaches Recovery.
    broken = [r for r in results if (r.get("error") or "").startswith("registry unreadable")]
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
