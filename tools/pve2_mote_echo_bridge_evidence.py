#!/usr/bin/env python3
"""T-6220 live evidence for the mosquitto bridge, against DISPOSABLE local brokers on pve2.

Run on pve2 as the mosquitto user (never touches the live Echo broker, firewall or laptop):

    runuser -u mosquitto -- python3 -I tools/pve2_mote_echo_bridge_evidence.py --repo <checkout>

It renders the SHIPPED template (the same render path the unit uses), starts a throwaway
upstream broker U and the bridge B on loopback high ports, and checks Specialist's acceptance
conditions (T-6220 log):
  a  QoS-1 raw_pcm local->upstream, every sequence number, in order
  b  both downlink topics upstream->local
  c  U stopped mid-stream and restarted: zero loss, duplicates counted and reported
  d  bounded reconnect, from B's log timestamps
  e  health exits non-zero while U is down and 0 once reconnected
  f  HALF-OPEN partition (U frozen by SIGSTOP past keepalive x 1.5): detected, zero loss after SIGCONT
  +  route boundary: an unapproved topic is not carried in either direction
Prints a JSON report; exit 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

U_PORT, LOCAL_PORT = 28831, 28830
PAD = "x" * 3200  # ~100 ms of 16 kHz s16 mono PCM per message


def wait_for(pred, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--keep", action="store_true", help="keep the temp dir for inspection")
    ap.add_argument("--control-small-queue", action="store_true",
                    help="NEGATIVE CONTROL: cap the bridge queue at 50 so the outage MUST lose messages "
                         "(proves check c can fail); expect exit 1")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    sys.path.insert(0, str(repo / "tools"))
    import pve2_mote_echo_bridge as b  # noqa: E402

    d = Path(tempfile.mkdtemp(prefix="t6220-evidence-"))
    procs: dict[str, subprocess.Popen] = {}
    report: dict = {"checks": {}}

    def start(name, argv, log):
        procs[name] = subprocess.Popen(argv, stdout=open(log, "a"), stderr=subprocess.STDOUT)
        return procs[name]

    def stop(name, sig=signal.SIGTERM):
        p = procs.pop(name, None)
        if p and p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def pub(port, topic, payload_lines):
        r = subprocess.run(["mosquitto_pub", "-h", "127.0.0.1", "-p", str(port), "-q", "1",
                            "-t", topic, "-l"], input="\n".join(payload_lines) + "\n",
                           text=True, capture_output=True, timeout=60)
        return r.returncode

    try:
        conf = d / "conf"
        state = d / "state"
        ustate = d / "ustate"
        for p in (conf, state, ustate):
            p.mkdir(mode=0o700)
        src = repo / "hosts/pve2/mote-echo-bridge"
        shutil.copy(src / "mote.acl", conf)
        shutil.copy(src / "health.acl", conf)
        env = (f"UPSTREAM_HOST=127.0.0.1\nUPSTREAM_PORT={U_PORT}\nUPSTREAM_TLS=false\n"
               "UPSTREAM_USERNAME=evidence-user\nUPSTREAM_PASSWORD=evidence-pass\n"
               f"LOCAL_BIND=127.0.0.1\nLOCAL_PORT={LOCAL_PORT}\n")
        bridge_conf = d / "bridge.conf"
        bridge_conf.write_text(b.render_text((src / "bridge.conf.template").read_text(encoding="utf-8"), env,
                                             conf_dir=str(conf), state_dir=str(state)))
        report["lint_rendered"] = b.lint_text(bridge_conf.read_text())
        if a.control_small_queue:
            bridge_conf.write_text(bridge_conf.read_text().replace("max_queued_messages 50000", "max_queued_messages 50"))
            report["control"] = "small-queue (expect c_zero_loss_across_outage false)"
        (d / "upstream.conf").write_text(
            f"listener {U_PORT} 127.0.0.1\nallow_anonymous true\npersistence true\n"
            f"persistence_location {ustate}/\nmax_queued_messages 50000\n")

        start("U", ["mosquitto", "-c", str(d / "upstream.conf")], d / "u.log")
        time.sleep(0.5)
        start("B", ["mosquitto", "-c", str(bridge_conf)], d / "b.log")
        up = wait_for(lambda: b.health()[0] == 0, 30)
        report["checks"]["bridge_connects"] = up

        # Subscribers: persistent QoS-1 on U for raw_pcm; catch-all on U and on B for the boundary.
        s_up = d / "s_up.txt"
        start("S_up", ["mosquitto_sub", "-h", "127.0.0.1", "-p", str(U_PORT), "-i", "evid-up", "-c",
                       "-q", "1", "-t", "echo/input/raw_pcm"], s_up)
        s_all_u = d / "s_all_u.txt"
        start("S_all_u", ["mosquitto_sub", "-h", "127.0.0.1", "-p", str(U_PORT), "-q", "1", "-v",
                          "-t", "echo/#"], s_all_u)
        s_local = d / "s_local.txt"
        start("S_local", ["mosquitto_sub", "-h", "127.0.0.1", "-p", str(LOCAL_PORT), "-q", "1", "-v",
                          "-t", "echo/home/tap/tts_raw", "-t", "echo/home/tap/tts"], s_local)
        time.sleep(1.5)

        # a: 300 QoS-1 PCM-sized messages, in order.
        n1 = 300
        rc_a = pub(LOCAL_PORT, "echo/input/raw_pcm", [f"seq={i:06d} {PAD}" for i in range(n1)])
        wait_for(lambda: len(lines(s_up)) >= n1, 30)
        got = [ln.split()[0] for ln in lines(s_up)]
        report["checks"]["a_qos1_pcm_in_order"] = rc_a == 0 and got == [f"seq={i:06d}" for i in range(n1)]

        # b: both downlinks, plus a boundary probe that must NOT arrive locally.
        pub(U_PORT, "echo/home/tap/tts_raw", ["tts-raw-1"])
        pub(U_PORT, "echo/home/tap/tts", ["tts-1"])
        pub(U_PORT, "echo/home/tap/other", ["must-not-cross-down"])
        wait_for(lambda: len(lines(s_local)) >= 2, 15)
        time.sleep(1.0)
        local = lines(s_local)
        report["checks"]["b_both_downlinks"] = ("echo/home/tap/tts_raw tts-raw-1" in local
                                                and "echo/home/tap/tts tts-1" in local)

        # boundary uplink: the Mote listener's ACL refuses an unapproved topic outright.
        pub(LOCAL_PORT, "echo/other", ["must-not-cross-up"])
        time.sleep(1.5)
        all_u = " ".join(lines(s_all_u))
        report["checks"]["boundary_not_carried"] = ("must-not-cross-up" not in all_u
                                                    and "must-not-cross-down" not in " ".join(lines(s_local)))

        # c/d/e: stop U mid-stream, publish through the outage, restart U.
        n2 = 500
        seqs = [f"seq={i:06d} {PAD}" for i in range(n1, n1 + n2)]
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs[:100])
        stop("U")
        t_down = time.time()
        down_seen = wait_for(lambda: b.health()[0] == 1, 20)
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs[100:400])
        time.sleep(8)
        report["checks"]["e_health_fails_while_down"] = down_seen
        start("U", ["mosquitto", "-c", str(d / "upstream.conf")], d / "u.log")
        t_up = time.time()
        back = wait_for(lambda: b.health()[0] == 0, 45)
        t_back = time.time()
        report["checks"]["e_health_recovers"] = back
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs[400:])
        want = {f"seq={i:06d}" for i in range(n1 + n2)}
        wait_for(lambda: want <= {ln.split()[0] for ln in lines(s_up) if ln}, 60)
        got_all = [ln.split()[0] for ln in lines(s_up) if ln]
        missing = sorted(want - set(got_all))
        dups = len(got_all) - len(set(got_all))
        report["outage"] = {"published": n1 + n2, "received": len(got_all), "missing": len(missing),
                            "duplicates": dups, "first_missing": missing[:5],
                            "down_s": round(t_up - t_down, 1), "reconnect_after_restart_s": round(t_back - t_up, 1)}
        report["checks"]["c_zero_loss_across_outage"] = not missing

        # d: reconnect attempts while U was down, from B's own log.
        attempts = []
        for ln in lines(d / "b.log"):
            m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d): Connecting bridge \(step 1\) echo-upstream", ln)
            if m:
                attempts.append(time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))
        gaps = [round(y - x) for x, y in zip(attempts, attempts[1:])]
        report["reconnect_gaps_s"] = gaps
        report["checks"]["d_bounded_reconnect"] = len(attempts) >= 2 and all(g <= 31 for g in gaps)

        # f (Specialist #1753 note 1): HALF-OPEN partition. SIGSTOP U so its TCP stays open but silent,
        # past keepalive 30 x 1.5, while publishing; in-flight QoS-1 must be replayed after SIGCONT.
        n3 = 300
        base = n1 + n2
        seqs3 = [f"seq={i:06d} {PAD}" for i in range(base, base + n3)]
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs3[:100])
        procs["U"].send_signal(signal.SIGSTOP)
        t_freeze = time.time()
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs3[100:200])
        stale = wait_for(lambda: b.health()[0] == 1, 75, step=1.0)
        t_stale = time.time()
        pub(LOCAL_PORT, "echo/input/raw_pcm", seqs3[200:])
        time.sleep(5)
        procs["U"].send_signal(signal.SIGCONT)
        t_thaw = time.time()
        back3 = wait_for(lambda: b.health()[0] == 0, 90, step=1.0)
        want3 = {f"seq={i:06d}" for i in range(base, base + n3)}
        wait_for(lambda: want3 <= {ln.split()[0] for ln in lines(s_up) if ln}, 90)
        got3 = [ln.split()[0] for ln in lines(s_up) if ln and ln.split()[0] in want3]
        missing3 = sorted(want3 - set(got3))
        report["half_open"] = {"published": n3, "received": len(got3), "missing": len(missing3),
                               "duplicates": len(got3) - len(set(got3)),
                               "frozen_s": round(t_thaw - t_freeze, 1),
                               "keepalive_detect_s": round(t_stale - t_freeze, 1) if stale else None,
                               "first_missing": missing3[:5]}
        report["checks"]["f_half_open_detected"] = stale
        report["checks"]["f_zero_loss_half_open"] = back3 and not missing3
    finally:
        for name in list(procs):
            stop(name)
        report["tempdir"] = str(d) if a.keep else "removed"
        if not a.keep:
            shutil.rmtree(d, ignore_errors=True)

    report["pass"] = all(report["checks"].values()) and not report.get("lint_rendered")
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
