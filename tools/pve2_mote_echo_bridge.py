#!/usr/bin/env python3
"""T-6220: render, health-check and lint the dark pve2 Mote/Echo mosquitto bridge.

The bridge is stock mosquitto (Debian 2.0.21) in bridge mode, so persistent QoS-1 sessions,
redelivery, keepalive and bounded reconnect come from mosquitto's tested code, not from us.
This tool only:

  render  fill hosts/pve2/mote-echo-bridge/bridge.conf.template from the external credential
          env file (owned by T-5852's engineering owner, handed over by systemd LoadCredential=).
          It fails closed: missing file, missing or extra key, whitespace in a value, the
          placeholder host, or a non-loopback Mote listener without a password file all refuse
          (exit 2) and write nothing.
  health  read the bridge's retained connection-state notification on the loopback health
          listener; exit 0 only when it reads "1" (connected), 1 otherwise.
  lint    check the template still carries exactly the approved route (exit 0/1).

Stdlib only; it runs on pve2 under /usr/bin/python3 -I.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

#: The approved route (TG 5885): direction, topic. QoS 1 each, no wildcards.
ROUTE = (
    ("out", "echo/input/raw_pcm"),
    ("in", "echo/home/tap/tts_raw"),
    ("in", "echo/home/tap/tts"),
)
STATE_TOPIC = "pantheon/bridge/echo-upstream/state"
HEALTH_PORT = 18839
PLACEHOLDER_HOST = "UPSTREAM_BROKER_FROM_T_5852"

REQUIRED = ("UPSTREAM_HOST", "UPSTREAM_PORT", "UPSTREAM_TLS", "UPSTREAM_USERNAME", "UPSTREAM_PASSWORD")
OPTIONAL = ("UPSTREAM_CAFILE", "LOCAL_BIND", "LOCAL_PORT", "LOCAL_PASSWORD_FILE")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,250}$")
CRED_RE = re.compile(r"^[!-~]{1,256}$")  # printable ASCII, no whitespace


class Refused(Exception):
    """A typed render refusal; the message names the offending key, never its value."""


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_]+", key):
            raise Refused(f"line {n}: not KEY=VALUE")
        if key not in REQUIRED + OPTIONAL:
            raise Refused(f"line {n}: unknown key {key}")
        if key in env:
            raise Refused(f"line {n}: duplicate key {key}")
        env[key] = value
    return env


def _port(env: dict[str, str], key: str, default: str | None = None) -> int:
    value = env.get(key, default)
    if value is None or not value.isdigit() or not 1 <= int(value) <= 65535:
        raise Refused(f"{key} must be a port 1-65535")
    return int(value)


def _path(env: dict[str, str], key: str) -> str:
    value = env[key]
    if not PATH_RE.fullmatch(value) or ".." in value.split("/"):
        raise Refused(f"{key} must be a plain absolute path")
    return value


def substitutions(env: dict[str, str], *, conf_dir: str, state_dir: str) -> dict[str, str]:
    missing = [k for k in REQUIRED if k not in env]
    if missing:
        raise Refused(f"missing key(s): {', '.join(missing)}")
    host = env["UPSTREAM_HOST"]
    if host == PLACEHOLDER_HOST:
        raise Refused("UPSTREAM_HOST is still the placeholder; T-5852 must supply the endpoint")
    if not HOST_RE.fullmatch(host):
        raise Refused("UPSTREAM_HOST is not a hostname or IP")
    for key in ("UPSTREAM_USERNAME", "UPSTREAM_PASSWORD"):
        if not CRED_RE.fullmatch(env[key]) or env[key] == "CHANGE_ME":
            raise Refused(f"{key} must be set, printable and without whitespace")
    tls = env["UPSTREAM_TLS"]
    if tls not in ("true", "false"):
        raise Refused("UPSTREAM_TLS must be true or false")
    if tls == "true":
        ca = _path(env, "UPSTREAM_CAFILE") if "UPSTREAM_CAFILE" in env else None
        tls_block = (f"bridge_cafile {ca}" if ca else "bridge_capath /etc/ssl/certs") + "\nbridge_insecure false"
    else:
        if "UPSTREAM_CAFILE" in env:
            raise Refused("UPSTREAM_CAFILE given with UPSTREAM_TLS=false")
        tls_block = "# upstream TLS off (UPSTREAM_TLS=false)"
    bind = env.get("LOCAL_BIND", "127.0.0.1")
    try:
        bind_ip = ipaddress.ip_address(bind)
    except ValueError:
        raise Refused("LOCAL_BIND must be an IP address") from None
    local_port = _port(env, "LOCAL_PORT", "1883")
    if local_port == HEALTH_PORT:
        raise Refused(f"LOCAL_PORT must differ from the health port {HEALTH_PORT}")
    if "LOCAL_PASSWORD_FILE" in env:
        local_auth = f"allow_anonymous false\npassword_file {_path(env, 'LOCAL_PASSWORD_FILE')}"
    elif bind_ip.is_loopback:
        local_auth = "allow_anonymous true"
    else:
        raise Refused("a non-loopback LOCAL_BIND requires LOCAL_PASSWORD_FILE")
    return {
        "@STATE_DIR@": state_dir,
        "@CONF_DIR@": conf_dir,
        "@HEALTH_PORT@": str(HEALTH_PORT),
        "@LOCAL_PORT@": str(local_port),
        "@LOCAL_BIND@": str(bind_ip),
        "@LOCAL_AUTH@": local_auth,
        "@UPSTREAM_HOST@": host,
        "@UPSTREAM_PORT@": str(_port(env, "UPSTREAM_PORT")),
        "@UPSTREAM_USERNAME@": env["UPSTREAM_USERNAME"],
        "@UPSTREAM_PASSWORD@": env["UPSTREAM_PASSWORD"],
        "@UPSTREAM_TLS@": tls_block,
    }


def render_text(template: str, env_text: str, *, conf_dir: str, state_dir: str) -> str:
    subs = substitutions(parse_env(env_text), conf_dir=conf_dir, state_dir=state_dir)
    leftover = sorted(set(re.findall(r"@[A-Z_]+@", template)) - set(subs))
    if leftover:
        raise Refused(f"unfilled template marker(s): {', '.join(leftover)}")
    # One pass over the TEMPLATE only, so marker-like text inside a value stays literal.
    return re.sub(r"@[A-Z_]+@", lambda m: subs[m.group(0)], template)


def lint_text(template: str) -> list[str]:
    """Problems with the template's route; empty means it carries exactly the approved route."""
    problems = []
    topics = [ln.split() for ln in template.splitlines() if ln.strip().startswith("topic ")]
    seen = [(t[2], t[1]) for t in topics if len(t) >= 3]
    if sorted(seen) != sorted(ROUTE):
        problems.append(f"topic lines {seen} != approved route {list(ROUTE)}")
    for t in topics:
        if len(t) < 4 or t[3] != "1":
            problems.append(f"{' '.join(t)}: QoS must be 1")
        if any(ch in t[1] for ch in "#+"):
            problems.append(f"{' '.join(t)}: wildcard")
    lines = {ln.strip() for ln in template.splitlines()}
    for required in ("cleansession false", "persistence true", "notifications true",
                     "notifications_local_only true", "bridge_protocol_version mqttv311",
                     "keepalive_interval 30", "restart_timeout 2 30", "per_listener_settings true",
                     f"notification_topic {STATE_TOPIC}"):
        if required not in lines:
            problems.append(f"missing: {required}")
    if template.count("connection ") != 1:
        problems.append("exactly one bridge connection expected")
    return problems


def health(port: int = HEALTH_PORT, timeout: int = 5, *, run=subprocess.run) -> tuple[int, str]:
    """(exit code, detail). 0 only when the retained state notification reads '1'."""
    argv = ["/usr/bin/mosquitto_sub", "-h", "127.0.0.1", "-p", str(port), "-t", STATE_TOPIC,
            "-C", "1", "-W", str(timeout), "-q", "0"]
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"bridge health UNKNOWN: {type(exc).__name__}"
    state = (r.stdout or "").strip()
    if r.returncode == 0 and state == "1":
        return 0, "bridge echo-upstream connected"
    if state == "0":
        return 1, "bridge echo-upstream DISCONNECTED (state 0)"
    return 1, f"bridge health UNKNOWN (rc {r.returncode}, no state read): {(r.stderr or '').strip()[:200]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--template", required=True)
    r.add_argument("--env", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--conf-dir", required=True, help="where mote.acl and health.acl live")
    r.add_argument("--state-dir", required=True, help="mosquitto persistence directory")
    h = sub.add_parser("health")
    h.add_argument("--port", type=int, default=HEALTH_PORT)
    h.add_argument("--timeout", type=int, default=5)
    li = sub.add_parser("lint")
    li.add_argument("--template", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "lint":
        problems = lint_text(Path(a.template).read_text(encoding="utf-8"))
        for p in problems:
            print(f"lint: {p}", file=sys.stderr)
        return 1 if problems else 0
    if a.cmd == "health":
        rc, detail = health(a.port, a.timeout)
        print(detail, file=sys.stderr if rc else sys.stdout)
        return rc
    try:
        env_text = Path(a.env).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"render refused: credential file unreadable ({type(exc).__name__})", file=sys.stderr)
        return 2
    try:
        text = render_text(Path(a.template).read_text(encoding="utf-8"), env_text,
                           conf_dir=a.conf_dir, state_dir=a.state_dir)
    except Refused as exc:
        print(f"render refused: {exc}", file=sys.stderr)
        return 2
    out = Path(a.out)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
