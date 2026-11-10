#!/usr/bin/env python3
"""Tests for tools/host_broker_rehearsal_live.py (T-2920 U4b, the attested rehearsal layer).

NOTHING here actuates. The attestation decision is the pure ``_evaluate`` over
CAPTURED fact bytes; the runner gate is ``ArgvAllowlist.classify`` (no subprocess);
the live primitives run against a tmp directory; ``main`` runs only on refusal paths
(this host refuses by construction: the real-path test below). No test constructs an
OperatorRunner, LiveHost, RehearsalSubstratePort or RehearsalContext except to prove a
constructor refuses. The ONE RehearsalAttestation any test obtains is through ``load``
itself, fed captured GOOD facts, in the canary's positive control (guard-probe only,
with every live constructor stubbed to fail).
"""

from __future__ import annotations

import ast
import copy
import grp
import hashlib
import inspect
import io
import json
import os
import posixpath
import pwd
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools import host_broker_install_apply as A  # noqa: E402
from tools import host_broker_rehearsal_live as L  # noqa: E402
from tools import test_host_broker_install as T  # noqa: E402
from tools import test_host_broker_install_apply as AT  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.host_substrate import ports as P  # noqa: E402
from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS  # noqa: E402

SOURCE_PATH = Path(L.__file__)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# ---------------------------------------------------------------------------
# captured facts: a GOOD set and single-factor corruptions
# ---------------------------------------------------------------------------

#: SYNTHESIZED; replace with a sanitized real capture after the first rehearsal (U4a's
#: P0 captures the real /proc/self/mountinfo into the evidence). Shape: systemd-nspawn
#: -U -D <dir> --bind-ro=/srv/rehearsal/repo (no --bind=, no --volatile), with nspawn's
#: documented API mounts and read-only masks.
GOOD_MOUNTINFO = (
    "1000 900 8:1 /var/lib/machines/rh-1 / rw,relatime shared:1 - ext4 /dev/vda1 rw\n"
    "1001 1000 0:51 / /tmp rw,nosuid,nodev shared:2 - tmpfs tmpfs rw\n"
    "1002 1000 0:52 / /dev rw,nosuid shared:3 - tmpfs tmpfs rw,size=4096k,mode=755\n"
    "1003 1002 0:53 / /dev/shm rw,nosuid,nodev - tmpfs tmpfs rw\n"
    "1004 1002 0:54 / /dev/pts rw,nosuid,noexec - devpts devpts rw,gid=5,mode=620,ptmxmode=666\n"
    "1005 1002 0:54 /0 /dev/console rw,nosuid,noexec - devpts devpts rw,gid=5,mode=620\n"
    "1006 1002 0:55 / /dev/mqueue rw,nosuid,nodev,noexec - mqueue mqueue rw\n"
    "1007 1000 0:56 / /run rw,nosuid,nodev - tmpfs tmpfs rw,mode=755\n"
    "1008 1007 0:56 /host /run/host ro,nosuid,nodev,noexec - tmpfs tmpfs rw,mode=755,uid=524288,gid=524288\n"
    "1009 1008 8:1 /usr/lib/os-release /run/host/os-release ro,nosuid,nodev - ext4 /dev/vda1 rw\n"
    "1010 1000 0:58 / /proc rw,nosuid,nodev,noexec - proc proc rw\n"
    "1011 1010 0:58 /sys /proc/sys ro,nosuid,nodev,noexec - proc proc rw\n"
    "1012 1010 0:58 /sysrq-trigger /proc/sysrq-trigger ro,nosuid,nodev,noexec - proc proc rw\n"
    "1013 1010 0:52 /null /proc/kcore ro,nosuid - tmpfs tmpfs rw\n"
    "1014 1011 0:56 /.#proc-sys-kernel-random-boot-id0123456789abcdef//deleted /proc/sys/kernel/random/boot_id "
    "ro,nosuid,nodev,noexec - tmpfs tmpfs rw,mode=755,uid=524288,gid=524288\n"
    "1015 1000 0:60 / /sys ro,nosuid,nodev,noexec - tmpfs tmpfs rw,size=4096k,mode=555,uid=524288,gid=524288\n"
    "1016 1015 0:61 / /sys/fs/cgroup rw,nosuid,nodev,noexec - cgroup2 cgroup2 rw,nsdelegate\n"
    "1017 1000 8:1 /home/op/pantheon-config /srv/rehearsal/repo ro,relatime - ext4 /dev/vda1 rw\n"
)
#: Recovery D15 W3 (2026-11-01, T-5703): the /run/host (1008), boot_id (1014) and /sys (1015) lines above are in
#: the RULED nspawn 255 shapes (a bind of the /run tmpfs; the //deleted boot_id file), with the
#: synthetic userns base below. The earlier synthesized forms (a fresh tmpfs at /run/host; a
#: ``/boot_id`` ro mask without noexec) now REFUSE (RuledMountsW3Test pins it).
SYNTH_ID_MAP = b"         0     524288      65536\n"

HOST_MACHINE_ID = b"11111111111111111111111111111111\n"
MACHINE_ID = b"22222222222222222222222222222222\n"
NONCE = b"rehearsal-nonce-bytes\n"
NONCE_SHA = hashlib.sha256(NONCE).hexdigest()
PLAN_SHA = "c" * 64
ATT_DOC = {"schema": L.ATTESTATION_SCHEMA, "machine": "rh-1", "run_id": "run-20261027-1", "nonce_sha256": NONCE_SHA,
           "host_machine_id_sha256": hashlib.sha256(HOST_MACHINE_ID).hexdigest(), "plan_sha256": PLAN_SHA}


def ff(data: bytes, uid: int = 0, mode: int = 0o644, regular: bool = True) -> L.FileFact:
    return L.FileFact(data, uid, mode, regular)


def att_fact(**changes) -> L.FileFact:
    doc = {**ATT_DOC, **changes}
    doc = {k: v for k, v in doc.items() if v is not None}
    return ff(json.dumps(doc).encode(), 0, 0o600)


GOOD = L.Facts(container=ff(b"systemd-nspawn\n"), pid1_environ=ff(b"container=systemd-nspawn\0TERM=xterm\0"),
               attestation=att_fact(), nonce=ff(NONCE, 0, 0o600), machine_id=ff(MACHINE_ID), hostname=ff(b"rh-1\n"),
               mountinfo=ff(GOOD_MOUNTINFO.encode(), 0, 0o444), euid=0, environ=dict(L.REQUIRED_ENV),
               uid_map=ff(SYNTH_ID_MAP, 0, 0o444), gid_map=ff(SYNTH_ID_MAP, 0, 0o444))


def mi(text: str) -> L.FileFact:
    return ff(text.encode(), 0, 0o444)


#: (name, facts, argv nonce, plan sha) -- each corrupts EXACTLY one factor
CORRUPTIONS = [
    ("container wrong", replace(GOOD, container=ff(b"docker\n")), NONCE_SHA, PLAN_SHA),
    ("container missing", replace(GOOD, container=None), NONCE_SHA, PLAN_SHA),
    ("container no newline", replace(GOOD, container=ff(b"systemd-nspawn")), NONCE_SHA, PLAN_SHA),
    ("pid1 no record", replace(GOOD, pid1_environ=ff(b"TERM=xterm\0")), NONCE_SHA, PLAN_SHA),
    ("pid1 record as substring", replace(GOOD, pid1_environ=ff(b"Xcontainer=systemd-nspawn\0")), NONCE_SHA, PLAN_SHA),
    ("pid1 unreadable", replace(GOOD, pid1_environ=None), NONCE_SHA, PLAN_SHA),
    ("attestation missing", replace(GOOD, attestation=None), NONCE_SHA, PLAN_SHA),
    ("attestation not root", replace(GOOD, attestation=replace(att_fact(), uid=1000)), NONCE_SHA, PLAN_SHA),
    ("attestation mode 0644", replace(GOOD, attestation=replace(att_fact(), mode=0o644)), NONCE_SHA, PLAN_SHA),
    ("attestation not regular", replace(GOOD, attestation=replace(att_fact(), regular=False)), NONCE_SHA, PLAN_SHA),
    ("attestation extra key", replace(GOOD, attestation=att_fact(extra="x")), NONCE_SHA, PLAN_SHA),
    ("attestation no plan_sha256", replace(GOOD, attestation=att_fact(plan_sha256=None)), NONCE_SHA, PLAN_SHA),
    ("attestation wrong schema", replace(GOOD, attestation=att_fact(schema="x/v1")), NONCE_SHA, PLAN_SHA),
    ("attestation not json", replace(GOOD, attestation=ff(b"{", 0, 0o600)), NONCE_SHA, PLAN_SHA),
    ("attestation bad machine", replace(GOOD, attestation=att_fact(machine="Bad_Name")), NONCE_SHA, PLAN_SHA),
    ("attestation bad run_id", replace(GOOD, attestation=att_fact(run_id="../x")), NONCE_SHA, PLAN_SHA),
    ("nonce file differs", replace(GOOD, nonce=ff(b"other nonce", 0, 0o600)), NONCE_SHA, PLAN_SHA),
    ("nonce file missing", replace(GOOD, nonce=None), NONCE_SHA, PLAN_SHA),
    ("argv nonce differs", GOOD, "d" * 64, PLAN_SHA),
    ("argv nonce malformed", GOOD, "not-a-sha", PLAN_SHA),
    ("machine-id is the host's", replace(GOOD, machine_id=ff(HOST_MACHINE_ID)), NONCE_SHA, PLAN_SHA),
    ("machine-id missing", replace(GOOD, machine_id=None), NONCE_SHA, PLAN_SHA),
    ("hostname differs", replace(GOOD, hostname=ff(b"rh-2\n")), NONCE_SHA, PLAN_SHA),
    ("hostname missing", replace(GOOD, hostname=None), NONCE_SHA, PLAN_SHA),
    ("plan differs", GOOD, NONCE_SHA, "e" * 64),
    ("plan not rebuilt", GOOD, NONCE_SHA, None),
    ("euid not 0", replace(GOOD, euid=1000), NONCE_SHA, PLAN_SHA),
    ("environment extra var", replace(GOOD, environ={**L.REQUIRED_ENV, "PYTHONPATH": "/x"}), NONCE_SHA, PLAN_SHA),
    ("environment missing LC_ALL", replace(GOOD, environ={"PATH": "/usr/bin:/bin"}), NONCE_SHA, PLAN_SHA),
    ("mountinfo rw repo", replace(GOOD, mountinfo=mi(GOOD_MOUNTINFO.replace(
        "/srv/rehearsal/repo ro,relatime", "/srv/rehearsal/repo rw,relatime"))), NONCE_SHA, PLAN_SHA),
    ("mountinfo unreadable", replace(GOOD, mountinfo=None), NONCE_SHA, PLAN_SHA),
]


class EvaluateTest(unittest.TestCase):
    def test_the_good_facts_pass(self) -> None:
        reasons, doc = L._evaluate(GOOD, NONCE_SHA, PLAN_SHA)
        self.assertEqual(reasons, [])
        self.assertEqual(doc, ATT_DOC)

    def test_every_single_factor_refuses(self) -> None:
        self.assertGreaterEqual(len(CORRUPTIONS), 25)
        for name, facts, nonce, plan in CORRUPTIONS:
            with self.subTest(factor=name):
                reasons, doc = L._evaluate(facts, nonce, plan)
                self.assertTrue(reasons, name)
                self.assertIsNone(doc)

    def test_a_trailing_newline_is_refused_by_every_validator(self) -> None:
        """U4a review MED: ``$`` with ``.match`` matches before a final newline; every
        validator is ``fullmatch`` over an anchorless pattern."""

        cases = [("--nonce-sha256", GOOD, NONCE_SHA + "\n"),
                 ("attestation machine", replace(GOOD, attestation=att_fact(machine="rh-1\n")), NONCE_SHA),
                 ("attestation run_id", replace(GOOD, attestation=att_fact(run_id="run-20261027-1\n")), NONCE_SHA),
                 ("attestation nonce_sha256", replace(GOOD, attestation=att_fact(nonce_sha256=NONCE_SHA + "\n")),
                  NONCE_SHA),
                 ("hostname", replace(GOOD, hostname=ff(b"rh-1\n\n")), NONCE_SHA)]
        for name, facts, nonce in cases:
            with self.subTest(field=name):
                reasons, doc = L._evaluate(facts, nonce, PLAN_SHA)
                self.assertTrue(reasons, name)
                self.assertIsNone(doc)
        self.assertEqual(L._evaluate(GOOD, NONCE_SHA + "\n", PLAN_SHA + "\n")[1], None)
        # --receipt
        self.assertIsNotNone(L._RECEIPT_RE.fullmatch(AT.RECEIPT))
        self.assertIsNone(L._RECEIPT_RE.fullmatch(AT.RECEIPT + "\n"))
        main = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        self.assertIn("_RECEIPT_RE.fullmatch(args.receipt)", ast.unparse(main))
        for rx in (L._HEX64, L._MACHINE_RE, L._RUN_ID_RE, L._RECEIPT_RE):
            self.assertFalse(rx.pattern.startswith("^") or rx.pattern.endswith("$"), rx.pattern)
        # no .match anywhere in the module: only fullmatch
        matches = [n for n in ast.walk(TREE) if isinstance(n, ast.Attribute) and n.attr == "match"]
        self.assertEqual(matches, [])
        # the gate's slot templates too
        gate = L.ArgvAllowlist.observe_only(plan_and_manifest()[1])
        with self.assertRaises(L.ArgvRefused):
            gate.classify([P.GETENT, "passwd\n"], None)
        account = sorted(plan_and_manifest()[1].section("accounts"))[0]
        self.assertFalse(gate.classify([P.GETENT, "passwd", account], None))
        with self.assertRaises(L.ArgvRefused):
            gate.classify([P.GETENT, "passwd", account + "\n"], None)
        with self.assertRaises(L.MountinfoError):
            L.parse_mountinfo("2000 1000 0:1\n / /x rw - tmpfs tmpfs rw\n")

    def test_the_attestation_schema_keys_are_exact(self) -> None:
        self.assertEqual(L.ATTESTATION_KEYS, {"schema", "machine", "run_id", "nonce_sha256", "host_machine_id_sha256",
                                              "plan_sha256"})
        self.assertEqual(L.REQUIRED_ENV, {"LC_ALL": "C", "PATH": "/usr/bin:/bin"})

    def test_recheck_rereads_container_machine_id_and_mountinfo(self) -> None:
        mid = hashlib.sha256(MACHINE_ID).hexdigest()
        self.assertEqual(L._recheck_reasons(GOOD.container, GOOD.machine_id, GOOD.mountinfo, mid,
                                            GOOD.uid_map, GOOD.gid_map), [])
        # Recovery D15 W3: the mount factor in recheck reads the id maps too (absent = the ruled mounts refuse)
        self.assertTrue(L._recheck_reasons(GOOD.container, GOOD.machine_id, GOOD.mountinfo, mid))
        self.assertTrue(L._recheck_reasons(ff(b"x\n"), GOOD.machine_id, GOOD.mountinfo, mid))
        self.assertTrue(L._recheck_reasons(GOOD.container, ff(HOST_MACHINE_ID), GOOD.mountinfo, mid))
        self.assertTrue(L._recheck_reasons(GOOD.container, None, GOOD.mountinfo, mid))
        self.assertTrue(L._recheck_reasons(GOOD.container, GOOD.machine_id,
                                           mi(GOOD_MOUNTINFO + "2000 1000 8:1 /home /mnt rw - ext4 /dev/vda1 rw\n"), mid))

    def test_recheck_runs_before_every_runner_call_and_every_mutation(self) -> None:
        run = ast.dump(_method(_class("OperatorRunner"), "run").body[0])
        self.assertIn("recheck", run)
        mutate = _method(_class("LiveHost"), "_mutate")
        self.assertIn("recheck", ast.dump(mutate.body[0]))
        self.assertIn("recheck", ast.dump(_method(_class("LiveHost"), "precheck")))
        host = _class("LiveHost")
        for name in ("mkdir", "write_file", "chown", "chmod", "unlink", "rmdir", "rename", "symlink_replace",
                     "remove_tree_contained"):
            with self.subTest(method=name):
                self.assertIn("hook=self._mutate", ast.unparse(_method(host, name)))
        self.assertIn("self._mutate(", ast.unparse(_method(host, "fchown")))

    def test_the_real_path_load_refuses_on_this_host(self) -> None:
        """D5 real-path bar: load() against THIS host's real files refuses by construction."""

        with self.assertRaises(L.AttestationRefused) as cm:
            L.RehearsalAttestation.load(NONCE_SHA, PLAN_SHA)
        joined = " ".join(cm.exception.reasons)
        self.assertIn(L.CONTAINER_FILE, joined)
        self.assertIn("environment", joined)

    def test_the_constructor_refuses_without_the_module_key(self) -> None:
        for key in (None, object(), "key"):
            with self.assertRaises(L.AttestationRefused):
                L.RehearsalAttestation(key, ATT_DOC, "0" * 64)


# ---------------------------------------------------------------------------
# (a) mountinfo
# ---------------------------------------------------------------------------


class MountinfoTest(unittest.TestCase):
    def test_the_good_sample_passes(self) -> None:
        mounts = L.parse_mountinfo(GOOD_MOUNTINFO)
        self.assertEqual(len(mounts), GOOD_MOUNTINFO.count("\n"))
        self.assertEqual(L.mount_refusals(mounts, (524288, 524288)), [])

    def _refused(self, text: str) -> list[str]:
        return L._mount_factor(mi(text), GOOD.uid_map, GOOD.gid_map)

    def test_a_writable_repo_bind_refuses(self) -> None:
        self.assertTrue(self._refused(GOOD_MOUNTINFO.replace("/srv/rehearsal/repo ro,", "/srv/rehearsal/repo rw,")))

    def test_an_extra_rw_mount_refuses(self) -> None:
        self.assertTrue(self._refused(GOOD_MOUNTINFO + "2000 1000 0:70 / /mnt/scratch rw - tmpfs tmpfs rw\n"))
        self.assertTrue(self._refused(GOOD_MOUNTINFO + "2000 1000 8:1 / /var/lib/x rw - ext4 /dev/vda1 rw\n"))

    def test_a_writable_bind_of_a_host_path_refuses(self) -> None:
        for line in ("2000 1000 8:1 /home /home rw,relatime - ext4 /dev/vda1 rw\n",
                     "2000 1007 0:20 /user/1000 /run/user/1000 rw - tmpfs tmpfs rw\n",  # a host tmpfs bind (root != /)
                     "2000 1000 8:1 /etc /srv/rehearsal/inputs rw - ext4 /dev/vda1 rw\n",
                     "2000 1015 0:61 /x /sys/fs/cgroup/x rw - cgroup2 cgroup2 rw\n"):
            with self.subTest(line=line):
                self.assertTrue(self._refused(GOOD_MOUNTINFO + line))

    def test_a_shadowing_mount_over_the_repo_or_root_refuses(self) -> None:
        self.assertTrue(self._refused(GOOD_MOUNTINFO + "2000 1017 0:70 / /srv/rehearsal/repo rw - tmpfs tmpfs rw\n"))
        self.assertTrue(self._refused(GOOD_MOUNTINFO + "2000 1000 0:70 / / rw - tmpfs tmpfs rw\n"))
        self.assertTrue(self._refused(GOOD_MOUNTINFO.replace("/srv/rehearsal/repo ro", "/srv/rehearsal/other ro")))

    def test_a_writable_mask_refuses(self) -> None:
        self.assertTrue(self._refused(GOOD_MOUNTINFO.replace("/proc/sys ro,", "/proc/sys rw,")))
        self.assertTrue(self._refused(GOOD_MOUNTINFO.replace(" /sys ro,nosuid", " /sys rw,nosuid")))

    def test_malformed_samples_refuse(self) -> None:
        for text in ("", "garbage\n", GOOD_MOUNTINFO[:-1], GOOD_MOUNTINFO + "1 2 3\n",
                     GOOD_MOUNTINFO + "2000 1000 0:1 / /x rw shared:1 tmpfs tmpfs rw\n",
                     GOOD_MOUNTINFO + "2000 1000 0:1 / /x nosuid - tmpfs tmpfs rw\n",
                     GOOD_MOUNTINFO + "2000 1000 0:1 / /x ro,rw - tmpfs tmpfs rw\n",
                     GOOD_MOUNTINFO + "x 1000 0:1 / /x rw - tmpfs tmpfs rw\n",
                     GOOD_MOUNTINFO + "2000 1000 0:1 / relative rw - tmpfs tmpfs rw\n"):
            with self.subTest(text=text[-60:]):
                self.assertTrue(self._refused(text))
        self.assertTrue(L._mount_factor(ff(b"\xff\xfe\n")))

    def test_a_devtmpfs_dev_is_a_host_bind_and_refuses(self) -> None:
        """Review F1: nspawn always mounts a tmpfs at /dev; devtmpfs there is the host's."""

        self.assertEqual(L.VIRTUAL_EXACT["/dev"], frozenset({"tmpfs"}))
        good_dev = "1002 1000 0:52 / /dev rw,nosuid shared:3 - tmpfs tmpfs rw,size=4096k,mode=755\n"
        self.assertIn(good_dev, GOOD_MOUNTINFO)
        probe = "1002 1000 0:5 / /dev rw - devtmpfs udev rw\n"  # the reviewer's probe (this host's own /dev)
        reasons = self._refused(GOOD_MOUNTINFO.replace(good_dev, probe))
        self.assertIn("attestation refused: unlisted mount " + probe[:-1], reasons)

    def test_an_unlisted_mount_is_refused_in_recoveries_exact_words_with_the_raw_line(self) -> None:
        for line in ("2000 1000 8:1 /home /home rw,relatime shared:9 - ext4 /dev/vda1 rw,errors=remount-ro",
                     "2001 1000 0:70 / /mnt/scratch rw - tmpfs tmpfs rw",
                     "2002 1000 8:1 /srv/a\\040b /srv/a\\040b rw - ext4 /dev/vda1 rw",
                     "2003 1015 0:61 /x /sys/fs/cgroup/x rw - cgroup2 cgroup2 rw"):
            with self.subTest(line=line):
                reasons = self._refused(GOOD_MOUNTINFO + line + "\n")
                self.assertEqual(reasons, [f"attestation refused: unlisted mount {line}"])
        # a listed mountpoint in an unlisted (rw) form gets the same words, raw line verbatim
        for good, bad in (("1017 1000 8:1 /home/op/pantheon-config /srv/rehearsal/repo ro,relatime - ext4 /dev/vda1 rw",
                           "1017 1000 8:1 /home/op/pantheon-config /srv/rehearsal/repo rw,relatime - ext4 /dev/vda1 rw"),
                          ("1011 1010 0:58 /sys /proc/sys ro,nosuid,nodev,noexec - proc proc rw",
                           "1011 1010 0:58 /sys /proc/sys rw,nosuid,nodev,noexec - proc proc rw")):
            with self.subTest(line=bad):
                self.assertIn(good + "\n", GOOD_MOUNTINFO)
                reasons = self._refused(GOOD_MOUNTINFO.replace(good + "\n", bad + "\n"))
                self.assertEqual(reasons, [f"attestation refused: unlisted mount {bad}"])
        self.assertEqual(L.UNLISTED_MOUNT, "attestation refused: unlisted mount ")

    def test_octal_escapes_are_decoded_before_matching(self) -> None:
        (m,) = L.parse_mountinfo("2000 1000 0:1 / /srv/rehearsal\\040repo ro - ext4 /dev/vda1 rw\n")
        self.assertEqual(m.mountpoint, "/srv/rehearsal repo")


# ---------------------------------------------------------------------------
# (a') Recovery D15 W3 (2026-11-01, T-5703): the REAL systemd-nspawn 255 mounts
# ---------------------------------------------------------------------------

#: CAPTURED, not synthesized: rehearsal 1 (run 1990856d106c) evidence, copied byte-for-byte from
#: /var/lib/pantheon/rehearsal-evidence/1990856d106c/ (P0-mountinfo.txt is the FULL real
#: /proc/self/mountinfo; machine-guard-probe.json holds the 7 lines the old allowlist refused).
#: uid_map / gid_map are the kernel's format for that run's base (1708523520 appears in every
#: tmpfs superblock of the capture); P0 did not capture them.
REAL_DIR = Path(__file__).resolve().parent / "fixtures" / "host_broker_rehearsal" / "1990856d106c"
REAL_MOUNTINFO = (REAL_DIR / "P0-mountinfo.txt").read_text(encoding="utf-8")
REAL_PROBE = json.loads((REAL_DIR / "machine-guard-probe.json").read_text(encoding="utf-8"))
REAL_UID_MAP = (REAL_DIR / "uid_map").read_bytes()
REAL_GID_MAP = (REAL_DIR / "gid_map").read_bytes()
REAL = replace(GOOD, mountinfo=mi(REAL_MOUNTINFO), uid_map=ff(REAL_UID_MAP, 0, 0o444),
               gid_map=ff(REAL_GID_MAP, 0, 0o444))


def real_line(mount_id: int) -> str:
    (line,) = [ln for ln in REAL_MOUNTINFO.splitlines() if ln.startswith(f"{mount_id} ")]
    return line


class RuledMountsW3Test(unittest.TestCase):
    """Recovery D15 W3: EXACTLY the 7 refused nspawn 255 mounts, each pinned on every attribute."""

    def _reasons(self, text: str, uid_map: bytes = REAL_UID_MAP, gid_map: bytes = REAL_GID_MAP) -> list[str]:
        return L._mount_factor(mi(text), ff(uid_map, 0, 0o444), ff(gid_map, 0, 0o444))

    def _swap(self, mount_id: int, old: str, new: str) -> tuple[str, str]:
        line = real_line(mount_id)
        self.assertIn(old, line)
        bad = line.replace(old, new, 1)
        return REAL_MOUNTINFO.replace(line + "\n", bad + "\n"), bad

    def assertOnlyRefuses(self, text: str, *lines: str, **maps: bytes) -> None:
        self.assertEqual(self._reasons(text, **maps), [L.UNLISTED_MOUNT + ln for ln in lines])

    def test_the_real_capture_passes_end_to_end(self) -> None:
        self.assertEqual(self._reasons(REAL_MOUNTINFO), [])
        reasons, doc = L._evaluate(REAL, NONCE_SHA, PLAN_SHA)
        self.assertEqual(reasons, [])
        self.assertEqual(doc, ATT_DOC)
        mid = hashlib.sha256(MACHINE_ID).hexdigest()
        self.assertEqual(L._recheck_reasons(REAL.container, REAL.machine_id, REAL.mountinfo, mid,
                                            REAL.uid_map, REAL.gid_map), [])

    def test_the_seven_probe_refusals_are_real_lines_and_now_pass(self) -> None:
        self.assertEqual(REAL_PROBE["verdict"], "refused")
        quoted = [r[len(L.UNLISTED_MOUNT):] for r in REAL_PROBE["reasons"]]
        self.assertEqual(len(quoted), 7)
        self.assertTrue(all(r.startswith(L.UNLISTED_MOUNT) for r in REAL_PROBE["reasons"]))
        for line in quoted:
            self.assertIn(line + "\n", REAL_MOUNTINFO)
        self.assertEqual(sorted(int(q.split()[0]) for q in quoted), [174, 178, 188, 191, 192, 202, 203])

    def test_the_cross_cutting_userns_base_is_read_from_the_id_maps(self) -> None:
        tmpfs = [real_line(i) for i in (178, 188, 172, 174, 191, 192)]
        other = b"         0 1708589056      65536\n"
        self.assertOnlyRefuses(REAL_MOUNTINFO, *sorted(tmpfs, key=REAL_MOUNTINFO.index), uid_map=other)
        self.assertOnlyRefuses(REAL_MOUNTINFO, *sorted(tmpfs, key=REAL_MOUNTINFO.index), gid_map=other)
        self.assertEqual(L._mount_factor(mi(REAL_MOUNTINFO)), [L.UNLISTED_MOUNT + ln
                                                               for ln in sorted(tmpfs, key=REAL_MOUNTINFO.index)])
        self.assertNotIn("1708523520", "".join(str(e) for e in L.RULED_MOUNTS))  # never a literal

    def test_parse_id_map(self) -> None:
        self.assertEqual(L.parse_id_map(REAL_UID_MAP), 1708523520)
        self.assertEqual(L.parse_id_map(b"0 524288 65536\n"), 524288)
        for bad in (None, b"", b"0 524288 65536", b"0 524288 65536\n0 1 1\n", b"1 524288 65536\n",
                    b"0 0 4294967295\n", b"0 524288 0\n", b"0 524288\n", b"0 -5 65536\n", b"\xff\n",
                    b"0 4294967295 65536\n"):
            with self.subTest(data=bad):
                self.assertIsNone(L.parse_id_map(bad))

    def test_item1_sys(self) -> None:
        for old, new in (("uid=1708523520", "uid=1708523521"), ("gid=1708523520", "gid=0"),
                         ("/sys ro,", "/sys rw,"), ("size=4096k", "size=8192k"), ("mode=555", "mode=755"),
                         ("nodev,noexec,relatime", "nodev,relatime"), (" / /sys ", " /x /sys ")):
            with self.subTest(change=new):
                self.assertOnlyRefuses(*self._swap(178, old, new))

    def test_item2_run_host(self) -> None:
        for old, new in (("188 187 0:74", "188 187 0:99"), ("/run/host ro,", "/run/host rw,"),
                         ("uid=1708523520", "uid=1708523521"), (" /host /run/host", " / /run/host"),
                         ("nodev,noexec shared", "nodev shared")):
            with self.subTest(change=new):
                self.assertOnlyRefuses(*self._swap(188, old, new))

    def test_item2_the_old_fresh_tmpfs_run_host_now_refuses(self) -> None:
        old = "1008 1007 0:57 / /run/host ro,nosuid,nodev - tmpfs tmpfs rw,mode=755"
        text = GOOD_MOUNTINFO.replace(GOOD_MOUNTINFO.splitlines()[8] + "\n", old + "\n")
        self.assertEqual(L._mount_factor(mi(text), GOOD.uid_map, GOOD.gid_map), [L.UNLISTED_MOUNT + old])
        self.assertNotIn("/run/host", L.VIRTUAL_EXACT)

    def test_item3_proc_sys_net(self) -> None:
        for mid, old, new in ((202, "202 196 0:78", "202 196 0:99"), (203, "203 195 0:78", "203 195 0:99"),
                              (202, "/proc/sys/net rw,", "/proc/sys/net ro,"), (203, "/sys/net /proc", "/sys /proc"),
                              (203, "nodev,noexec", "nodev"), (202, "202 196 ", "202 176 ")):
            with self.subTest(line=mid, change=new):
                self.assertOnlyRefuses(*self._swap(mid, old, new))

    def test_item3_a_third_proc_sys_net_line_refuses(self) -> None:
        for parent in (195, 196):
            third = f"999 {parent} 0:78 /sys/net /proc/sys/net rw,nosuid,nodev,noexec,relatime shared:493 - proc proc rw"
            with self.subTest(parent=parent):
                self.assertOnlyRefuses(REAL_MOUNTINFO + third + "\n", third)

    def test_item3_is_accepted_only_while_the_pinned_argv_carries_private_network(self) -> None:
        self.assertIn("--private-network", L.NSPAWN_PINNED_FLAGS)
        dropped = tuple(f for f in L.NSPAWN_PINNED_FLAGS if f != "--private-network")
        with mock.patch.object(L, "NSPAWN_PINNED_FLAGS", dropped):
            self.assertOnlyRefuses(REAL_MOUNTINFO, real_line(202), real_line(203))
            self.assertTrue(L._evaluate(REAL, NONCE_SHA, PLAN_SHA)[0])

    def test_item4_boot_id_both_lines(self) -> None:
        for mid, old, new in (
                (172, "ida6c5390f447ec4de//deleted", "ida6c5390f447ec4d//deleted"),
                (174, "ida6c5390f447ec4de//deleted", "idA6C5390F447EC4DE//deleted"),
                (174, "ida6c5390f447ec4de//deleted", "ida6c5390f447ec4de"),
                (172, "nodev,noexec shared", "nodev shared"),        # (a) ro: noexec is required
                (174, "rw,nosuid,nodev shared", "rw,nosuid,nodev,noexec shared"),  # (b) rw: exactly nosuid,nodev
                (172, "boot_id ro,", "boot_id rw,"),
                (172, "172 196 0:74", "172 195 0:74"),               # (a) parent must be /proc/sys
                (174, "174 195 0:74", "174 196 0:74"),               # (b) parent must be /proc
                (174, "174 195 0:74", "174 195 0:73"),               # source must be the /run tmpfs
                (172, "uid=1708523520", "uid=1708523521")):
            with self.subTest(line=mid, change=new):
                self.assertOnlyRefuses(*self._swap(mid, old, new))

    def test_item4_the_old_boot_id_mask_now_refuses(self) -> None:
        old = "1014 1010 0:59 /boot_id /proc/sys/kernel/random/boot_id ro,nosuid,nodev - tmpfs tmpfs rw"
        new_lines = GOOD_MOUNTINFO.splitlines()
        i = next(n for n, ln in enumerate(new_lines) if ln.startswith("1014 "))
        new_lines[i] = old
        text = "\n".join(new_lines) + "\n"
        self.assertEqual(L._mount_factor(mi(text), GOOD.uid_map, GOOD.gid_map), [L.UNLISTED_MOUNT + old])

    def test_item5_kmsg(self) -> None:
        for old, new in (("kmsga1e5a99e17967672//deleted", "kmsga1e5a99e1796767//deleted"),
                         ("/.#proc-kmsg", "/.#proc-kcore"), ("/proc/kmsg rw,", "/proc/kmsg ro,"),
                         ("191 195 0:74", "191 195 0:73"), ("191 195 ", "191 196 "),
                         ("uid=1708523520", "uid=0"), ("nosuid,nodev shared", "nosuid shared")):
            with self.subTest(change=new):
                self.assertOnlyRefuses(*self._swap(191, old, new))
        # kmsg on /run/lock's major:minor: kmsg is off /run AND /run/lock is no longer fresh
        text, bad = self._swap(191, "191 195 0:74", "191 195 0:76")
        self.assertOnlyRefuses(text, bad, real_line(192))

    def test_item6_run_lock(self) -> None:
        for old, new in (("192 187 0:76", "192 187 0:74"),           # sharing /run's source
                         ("192 187 0:76", "192 187 0:73"),           # sharing any other mount's source
                         ("size=5120k", "size=6144k"), ("192 187 ", "192 176 "),
                         ("uid=1708523520", "uid=1708523521"), ("/run/lock rw,", "/run/lock ro,"),
                         ("nodev,noexec,relatime", "nodev,relatime"), (" / /run/lock", " /lock /run/lock")):
            with self.subTest(change=new):
                text, bad = self._swap(192, old, new)
                reasons = self._reasons(text)
                self.assertIn(L.UNLISTED_MOUNT + bad, reasons)
                # a shared source also un-freshens the OTHER mount only if it is ruled fresh; none is
                self.assertEqual(reasons, [L.UNLISTED_MOUNT + bad])

    def test_no_entry_is_reused_and_nothing_else_widens(self) -> None:
        second_lock = "998 187 0:97 / /run/lock rw,nosuid,nodev,noexec,relatime shared:322 - tmpfs tmpfs " \
                      "rw,size=5120k,uid=1708523520,gid=1708523520,inode64"
        self.assertOnlyRefuses(REAL_MOUNTINFO + second_lock + "\n", second_lock)
        for line in ("997 187 0:96 / /run/other rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=5120k,"
                     "uid=1708523520,gid=1708523520",
                     "996 195 0:74 /.#proc-kmsga1e5a99e17967672//deleted /proc/kcore rw,nosuid,nodev - tmpfs tmpfs "
                     "rw,uid=1708523520,gid=1708523520"):
            with self.subTest(line=line):
                self.assertOnlyRefuses(REAL_MOUNTINFO + line + "\n", line)
        # two mounts at /run make the anchor ambiguous: every /run-anchored ruled line refuses
        extra_run = "995 176 0:95 / /run rw,nosuid,nodev - tmpfs tmpfs rw,uid=1708523520,gid=1708523520"
        reasons = self._reasons(REAL_MOUNTINFO + extra_run + "\n")
        for mid in (188, 172, 174, 191, 192):
            self.assertIn(L.UNLISTED_MOUNT + real_line(mid), reasons)

    def test_f2_any_fstype_at_a_ruled_mountpoint_must_match_that_mountpoints_entries(self) -> None:
        """Coordinator F2 ruling (fail-closed tightening): keyed on the mountpoint, not (mountpoint, fstype)."""

        for line in ("990 195 253:1 /x /proc/kmsg rw,nosuid,nodev - ext4 /dev/vda1 rw",
                     "990 195 253:1 /x /proc/kmsg ro,nosuid,nodev - ext4 /dev/vda1 rw",
                     "990 176 0:79 / /sys ro,nosuid,nodev,noexec - sysfs sysfs ro",
                     "990 196 0:74 / /proc/sys/net ro,nosuid,nodev,noexec - tmpfs tmpfs rw,uid=1708523520,gid=1708523520",
                     "990 196 0:78 /sys/net /proc/sys/kernel/random/boot_id ro,nosuid,nodev,noexec - proc proc rw",
                     "990 187 0:74 /lock /run/lock ro,nosuid,nodev,noexec - tmpfs tmpfs rw,uid=1708523520,gid=1708523520"):
            with self.subTest(line=line):
                self.assertOnlyRefuses(REAL_MOUNTINFO + line + "\n", line)
        # mason ruling: real nspawn 255 mounts /sys as tmpfs (sysfs only on the submounts); no sysfs entry at /sys
        self.assertNotIn("/sys", L.VIRTUAL_EXACT)
        self.assertEqual({m.fstype for m in L.parse_mountinfo(REAL_MOUNTINFO) if m.mountpoint == "/sys"}, {"tmpfs"})
        self.assertIn("sysfs", {m.fstype for m in L.parse_mountinfo(REAL_MOUNTINFO) if m.mountpoint.startswith("/sys/")})

    def test_the_ruled_table_is_exactly_recoveries_items(self) -> None:
        self.assertEqual([e["id"] for e in L.RULED_MOUNTS],
                         ["W3-1", "W3-2", "W3-3a", "W3-3b", "W3-4a", "W3-4b", "W3-5", "W3-6"])
        self.assertEqual({(e["mountpoint"], e["fstype"]) for e in L.RULED_MOUNTS},
                         {("/sys", "tmpfs"), ("/run/host", "tmpfs"), ("/proc/sys/net", "proc"),
                          ("/proc/sys/kernel/random/boot_id", "tmpfs"), ("/proc/kmsg", "tmpfs"),
                          ("/run/lock", "tmpfs")})
        for e in L.RULED_MOUNTS:
            with self.subTest(entry=e["id"]):
                self.assertEqual(e["max"], 1)
                self.assertEqual(e["userns"], e["fstype"] == "tmpfs")
                self.assertTrue((e["root"] is None) != (e["root_re"] is None))


# ---------------------------------------------------------------------------
# (b) the runner gate
# ---------------------------------------------------------------------------

RCD = AT.RECEIPT + ".d"
_PLAN: dict = {}


def plan_and_manifest():
    if not _PLAN:
        with mock.patch.object(H, "_literal", AT._literal_allowlist_landed):
            doc, _staging = AT.build_plan()
        _PLAN["plan"] = doc
        _PLAN["fmanifest"] = F.load_manifest(A.fragment_from_plan(doc))
    return _PLAN["plan"], _PLAN["fmanifest"]


def family_argvs(plan, fmanifest):
    out = []
    for e in plan["entries"]:
        src = e["source"]
        if src.get("route") != "family":
            continue
        r = F.resolve(src["family"], dict(src["params"]), fmanifest)
        staged = posixpath.join(RCD, "substrate-staging", src["staging"]) if "staging" in src else None
        if r.family in ("sudoers_fragment.ensure",) and staged is None:
            staged = posixpath.join(RCD, "substrate-staging", "sudoers/x")
        out += P.apply_argv(r, staged_source=staged)
        if r.family == "account.ensure":
            out += P.rollback_argv(r, None)
    return out


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan, cls.fmanifest = plan_and_manifest()
        cls.wheelhouse = AT.inputs_doc()["wheelhouse"]
        cls.allow = L.ArgvAllowlist.for_plan(cls.plan, cls.fmanifest, cls.wheelhouse, RCD)
        cls.fw = cls.plan["framework"]["root"]

    def ok(self, argv, cwd=None) -> bool:
        return self.allow.classify(argv, cwd)

    def refused(self, argv, cwd=None, allow=None) -> None:
        with self.assertRaises(L.ArgvRefused, msg=argv):
            (allow or self.allow).classify(argv, cwd)

    def test_allowed_binaries_are_derived_from_the_ruled_tables(self) -> None:
        derived = L.allowed_binaries()
        expected = {P.GETENT, P.STAT, P.SHA256SUM, P.GETFACL, P.SYSTEMCTL, P.VISUDO, P.UNLINK, P.GROUPADD, P.USERADD,
                    P.USERMOD, P.USERDEL, P.GROUPDEL, P.GPASSWD, P.SETFACL, P.CHOWN, P.CHMOD, P.INSTALL, P.MV,
                    P.SUDO, P.LOGINCTL, A.DPKG_QUERY, H.ENV, H.PYTHON3, H.SETPRIV}
        self.assertEqual(derived, expected)
        # every OBSERVE_ARGV_TABLE binary is in it (the derivation's source, not a parallel list)
        self.assertTrue({row[0][0] for row in P.OBSERVE_ARGV_TABLE} <= derived)

    def test_every_family_argv_of_this_plan_is_accepted_as_mutating(self) -> None:
        argvs = family_argvs(self.plan, self.fmanifest)
        self.assertGreater(len(argvs), 20)
        for argv in argvs:
            with self.subTest(argv=argv):
                self.assertTrue(self.ok(argv))

    def test_every_observation_of_this_plan_is_accepted_as_read_only(self) -> None:
        subjects = sorted(P.manifest_subjects(self.fmanifest))
        for s in subjects:
            for argv in P.observe_argv(s):
                with self.subTest(argv=argv):
                    self.assertFalse(self.ok(argv))
            kind, key = s.split(":", 1)
            if kind in ("acl", "owner_mode", "dir"):
                for anc in L._ancestors(key):
                    self.assertFalse(self.ok(P.path_stat_argv(anc)[0]))
        for argv in P.snapshot_argv():
            self.assertFalse(self.ok(argv))
        for pkg in A.VERIFY_PACKAGES:
            self.assertFalse(self.ok([A.DPKG_QUERY, "-W", "-f", A.DPKG_FORMAT, pkg]))
        self.assertFalse(self.ok(list(A.PYYAML_PROBE)))
        self.assertTrue(self.ok(P.visudo_argv(RCD + "/substrate-staging/sudoers/x")[0]) is False)

    def test_one_token_mutations_of_accepted_argv_are_refused(self) -> None:
        for argv in family_argvs(self.plan, self.fmanifest):
            with self.subTest(argv=argv):
                self.refused(argv + ["/etc/shadow"])
                self.refused(argv[:-1] + ["/etc/shadow"])
                self.refused(argv, cwd="/")

    def test_env_python3_and_setpriv_need_the_full_ruled_argv(self) -> None:
        build = ["/usr/bin/env", "-i", *A.BUILD_ENV, H.PYTHON3, "-I", posixpath.join(self.fw, H.BUILD_RUNTIME_REL),
                 "--root", "/", "--wheelhouse", self.wheelhouse, "--python", H.PYTHON3, "--adapter"]
        for key in H.RUNTIME_ADAPTERS:
            self.assertTrue(self.ok(build + [key], cwd=self.fw))
            self.refused(build + [key])  # no cwd
            self.refused(build + [key], cwd="/tmp")
        self.refused(build + ["other"], cwd=self.fw)
        bad_wheels = [a if a != self.wheelhouse else "/tmp/wheels" for a in build] + ["host"]
        self.refused(bad_wheels, cwd=self.fw)
        other_fw = [a.replace(self.fw, "/tmp/fw") for a in build] + ["host"]
        self.refused(other_fw, cwd="/tmp/fw")
        for argv in (["/usr/bin/env", "/bin/sh"], ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/bin/sh", "-c", "id"],
                     [H.PYTHON3, "-c", "print(1)"], [H.PYTHON3, "-I", "-c", "import os; os.system('id')"],
                     [H.PYTHON3, "-I", posixpath.join(self.fw, "tools/host_plan_producer.py")],
                     [H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--", "/bin/sh"],
                     ["/bin/sh", "-c", "true"], ["/usr/bin/curl", "https://example.invalid"], []):
            with self.subTest(argv=argv):
                self.refused(argv)

    def test_setpriv_is_the_ruled_amended_template_bound_to_each_plan_entry(self) -> None:
        self.assertIs(L.SETPRIV_TEMPLATE_RULED, True)
        regs = [e for e in self.plan["entries"] if e["kind"] == "plan_registration"]
        self.assertTrue(regs)
        ruled = [H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--no-new-privs", "--inh-caps=-all",
                 "--bounding-set=-all", "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/usr/bin/python3", "-I"]
        for e in regs:
            argv = list(e["source"]["producer_argv"])
            self.assertEqual(argv[:13], ruled)
            self.assertEqual(argv[13:17], [posixpath.join(self.fw, "tools/host_plan_producer.py"), "--framework-root",
                                           self.fw, "--seat-record"])
            self.assertTrue(self.ok(argv, cwd=self.fw))
            self.refused(argv)  # cwd is part of the template
            self.refused(AT.OLD_SETPRIV_PREFIX + argv[8:], cwd=self.fw)
            # a seat record of another seat (the plan names one protocol-test seat, #1665)
            other_record = {**json.loads(argv[17]), "seat_id": "some-other-seat"}
            swapped = argv[:17] + [H.canonical(other_record)] + argv[18:]
            self.assertNotEqual(swapped, argv)
            self.refused(swapped, cwd=self.fw)
            self.refused(argv[:21] + ["/tmp/elsewhere.json"], cwd=self.fw)
            self.refused(argv[:4] + argv[5:], cwd=self.fw)  # a hardening flag dropped
        with mock.patch.object(L, "SETPRIV_TEMPLATE_RULED", False):
            self.refused(list(regs[0]["source"]["producer_argv"]), cwd=self.fw)

    def test_a_plan_whose_producer_argv_is_not_the_ruled_form_cannot_build_a_gate(self) -> None:
        import copy

        plan = copy.deepcopy(self.plan)
        e = next(x for x in plan["entries"] if x["kind"] == "plan_registration")
        e["source"]["producer_argv"] = AT.OLD_SETPRIV_PREFIX + e["source"]["producer_argv"][8:]
        with self.assertRaises(L.ArgvRefused):
            L.ArgvAllowlist.for_plan(plan, self.fmanifest, self.wheelhouse, RCD)

    def test_paths_names_and_sources_are_bound_to_the_plan(self) -> None:
        for argv in ([P.CHMOD, "00644", "/etc/shadow"], [P.CHOWN, "-h", "root:root", "/etc/passwd"],
                     [P.UNLINK, "/etc/sudoers"], [P.USERDEL, "root"], [P.GPASSWD, "--add", "root", "sudo"],
                     [P.INSTALL, "-o", "root", "-g", "root", "-m", "0644", "/tmp/evil", "/etc/systemd/system/x.service"],
                     [P.SYSTEMCTL, "enable", "ssh.service"], [P.LOGINCTL, "enable-linger", "root"],
                     [P.SYSTEMCTL, "start", "x.service"]):
            with self.subTest(argv=argv):
                self.refused(argv)

    def test_every_slot_validator_refuses_a_valid_value_plus_a_trailing_newline(self) -> None:
        """U4a review MED-3 carried to U4b: each slot check, fed "<valid>" then "<valid>\\n"."""

        slots = L.ArgvAllowlist._manifest_slots(self.fmanifest)
        _chown, leaf = self._leaf()
        a = self.fmanifest.section("accounts")
        account = sorted(a)[0]
        unit = posixpath.basename(next(iter(self.fmanifest.section("units").values()))["path"])
        valid = {"A": account, "G": account, L.P_OBSERVE: posixpath.dirname(leaf), L.P_MUTATE: leaf, "U": unit,
                 L.P_SHELL: a[account]["shell"],
                 "N": "root", "ID": str(a[account]["uid"]), "MODE": "0755", "ACL": "user::rwx,group::r-x",
                 "PKG": sorted(A.VERIFY_PACKAGES)[0]}
        self.assertEqual(set(valid), set(slots), "a new slot needs a trailing-newline case here")
        for name, value in valid.items():
            with self.subTest(slot=name):
                self.assertTrue(slots[name](value), value)
                self.assertFalse(slots[name](value + "\n"), value)
        # the attestation's other two sha fields
        for k in ("host_machine_id_sha256", "plan_sha256"):
            with self.subTest(field=k):
                reasons, doc = L._evaluate(replace(GOOD, attestation=att_fact(**{k: ATT_DOC[k] + "\n"})), NONCE_SHA,
                                           PLAN_SHA)
                self.assertIn(f"{L.ATTESTATION_FILE}: {k} is not a sha256", reasons)
                self.assertIsNone(doc)
        # the rollback preimage validators (names, modes)
        att = mock.Mock(machine="rh-1", run_id="run-1", nonce_sha256="a" * 64, plan_sha256="b" * 64)
        doc = {"mode": "rehearsal-nspawn", "rehearsal": {"machine": "rh-1", "run_id": "run-1", "nonce_sha256": "a" * 64},
               "plan_sha256": "b" * 64,
               "entries": [{"family": {"subject": "owner_mode:/srv/x",
                                       "preimage": {"owner": "daemon\n", "group": "adm\n", "mode": "2750\n"}}}]}
        self.assertEqual(L.rollback_preimages_of(doc, att), {"/srv/x": {"names": set(), "modes": set(), "acls": set()}})
        # U3a's validators on the rehearsal path (apply.py): the producer shape, the
        # credential owner, the registered-manifest revision keys
        producer = next(list(e["source"]["producer_argv"]) for e in self.plan["entries"]
                        if e["source"].get("producer_argv"))
        self.assertTrue(A.producer_argv_shape_ok(producer))
        bad = list(producer)
        bad[1] += "\n"
        self.assertFalse(A.producer_argv_shape_ok(bad))
        src = ast.unparse(ast.parse(Path(A.__file__).read_text()))
        for rx in ("_ACCOUNT_RE", "H._REVISION_RE"):
            with self.subTest(rx=rx):
                self.assertNotIn(f"{rx}.match(", src)
                self.assertIn(f"{rx}.fullmatch(", src)

    def _leaf(self) -> tuple[list[str], str]:
        """A plan chown argv (owner_mode.set) and its leaf path."""

        chown = next(a for a in family_argvs(self.plan, self.fmanifest) if a[0] == P.CHOWN)
        return chown, chown[-1]

    def test_review_f3_mutating_paths_are_leaf_subjects_only(self) -> None:
        stage = RCD + "/substrate-staging/units/x.service"
        for argv in ([P.CHMOD, "0777", "/"], [P.CHMOD, "00777", "/"], [P.CHOWN, "-h", "root:root", "/"],
                     [P.CHOWN, "-h", "root:root", "/etc"], [P.CHMOD, "00755", "/etc"],
                     [P.INSTALL, "-o", "root", "-g", "root", "-m", "0644", stage, "/etc"],
                     [P.INSTALL, "-d", "-o", "root", "-g", "root", "-m", "0755", "/etc"],
                     [P.SETFACL, "--physical", "--set", "user::rwx", "/etc"], [P.UNLINK, "/etc"],
                     [P.UNLINK, "/etc.pantheon-tmp"]):
            with self.subTest(argv=argv):
                self.refused(argv)
        _chown, leaf = self._leaf()
        anc = posixpath.dirname(leaf)
        self.assertFalse(self.ok(P.path_stat_argv(anc)[0]))  # ancestors stay observable (row-4 walk)
        self.refused([P.CHOWN, "-h", "root:root", anc])

    def test_review_f3_owner_names_are_manifest_or_root_and_preimage_in_rollback(self) -> None:
        chown, leaf = self._leaf()
        self.assertTrue(self.ok(chown))
        self.assertTrue(self.ok([P.CHOWN, "-h", "root:root", leaf]))
        stranger = [P.CHOWN, "-h", "daemon:daemon", leaf]
        self.assertNotIn("daemon", set(self.fmanifest.section("accounts")) | set(self.fmanifest.section("groups")))
        self.refused(stranger)
        rb = L.ArgvAllowlist.for_plan(self.plan, self.fmanifest, self.wheelhouse, RCD,
                                      rollback_preimages={leaf: {"names": {"daemon"}, "modes": set()}})
        self.assertTrue(rb.classify(stranger, None))
        other = next(a for a in family_argvs(self.plan, self.fmanifest) if a[0] == P.CHOWN and a[-1] != leaf)[-1]
        self.refused([P.CHOWN, "-h", "daemon:daemon", other], allow=rb)  # the preimage name is per path
        self.refused([P.INSTALL, "-d", "-o", "daemon", "-g", "daemon", "-m", "0755", leaf])

    def test_review_f3_special_mode_bits_need_the_plans_exact_mode(self) -> None:
        _chown, leaf = self._leaf()
        for mode in ("04755", "02755", "01777", "06755", "07777"):
            with self.subTest(mode=mode):
                self.refused([P.CHMOD, mode, leaf])
        self.assertTrue(self.ok([P.CHMOD, "00750", leaf]))
        # this plan names no special mode; a gate whose plan DID name 2750 for the leaf
        # accepts exactly that mode on exactly that path
        other = next(a for a in family_argvs(self.plan, self.fmanifest) if a[0] == P.CHOWN and a[-1] != leaf)[-1]
        planned = L.ArgvAllowlist(L.ruled_templates(), L.ArgvAllowlist._manifest_slots(self.fmanifest), {},
                                  names={"root"}, modes={leaf: {0o2750}})
        self.assertTrue(planned.classify([P.CHMOD, "02750", leaf], None))
        self.assertTrue(planned.classify([P.CHMOD, "00750", leaf], None))
        for argv in ([P.CHMOD, "04750", leaf], [P.CHMOD, "02755", leaf], [P.CHMOD, "02750", other]):
            with self.subTest(argv=argv):
                self.refused(argv, allow=planned)
        rb = L.ArgvAllowlist.for_plan(self.plan, self.fmanifest, self.wheelhouse, RCD,
                                      rollback_preimages={leaf: {"names": set(), "modes": {0o4755}}})
        self.assertTrue(rb.classify([P.CHMOD, "04755", leaf], None))
        self.refused([P.CHMOD, "02755", leaf], allow=rb)

    def test_review_f3_rollback_preimages_come_only_from_this_attested_receipt(self) -> None:
        att = mock.Mock(machine="rh-1", run_id="run-1", nonce_sha256="a" * 64, plan_sha256="b" * 64)
        ident = {"machine": "rh-1", "run_id": "run-1", "nonce_sha256": "a" * 64}
        entry = {"family": {"subject": "owner_mode:/srv/x", "preimage": {"owner": "daemon", "group": "adm",
                                                                         "mode": "2750"}}}
        doc = {"mode": "rehearsal-nspawn", "rehearsal": ident, "plan_sha256": "b" * 64,
               "entries": [entry, {"family": {"subject": "file:/x", "preimage": None}}, {"id": "no-family"}]}
        self.assertEqual(L.rollback_preimages_of(doc, att), {"/srv/x": {"names": {"daemon", "adm"}, "modes": {0o2750}, "acls": set()}})
        for bad in ({**doc, "mode": "fixture"}, {**doc, "mode": "live"}, {**doc, "rehearsal": {**ident, "run_id": "x"}},
                    {**doc, "plan_sha256": "c" * 64}, {k: v for k, v in doc.items() if k != "rehearsal"}):
            with self.subTest(bad=bad.get("mode")):
                self.assertEqual(L.rollback_preimages_of(bad, att), {})

    # --- delta review L1 / L4 and Recovery's D15 g/o-write rule ---------------------------

    def test_delta_l1_a_login_shell_is_only_valid_in_useradds_shell_position(self) -> None:
        a = self.fmanifest.section("accounts")
        shells = {spec["shell"] for spec in a.values()}
        slots = L.ArgvAllowlist._manifest_slots(self.fmanifest)
        for shell in sorted(shells):
            with self.subTest(shell=shell):
                self.assertFalse(slots[L.P_MUTATE](shell))
                self.assertTrue(slots[L.P_SHELL](shell))
                stage = RCD + "/substrate-staging/units/x.service"
                for argv in ([P.UNLINK, shell], [P.CHMOD, "00755", shell], [P.CHOWN, "-h", "root:root", shell],
                             [P.INSTALL, "-o", "root", "-g", "root", "-m", "0755", stage, shell],
                             [P.SETFACL, "--physical", "--set", "user::rwx", shell]):
                    self.refused(argv)
        useradds = [x for x in family_argvs(self.plan, self.fmanifest) if x[0] == P.USERADD]
        self.assertTrue(useradds)
        for argv in useradds:
            with self.subTest(argv=argv):
                self.assertIn(argv[argv.index("--shell") + 1], shells)
                self.assertTrue(self.ok(argv))
        home = next(iter(a.values()))["home"]
        self.assertTrue(L.ArgvAllowlist._manifest_slots(self.fmanifest)[L.P_MUTATE](home))  # homes stay
        # the shell slot appears in exactly one template position: useradd --shell
        sh = [(t.origin, i) for t in L.ruled_templates() for i, tok in enumerate(t.tokens)
              if not isinstance(tok, str) and L.P_SHELL in tok[1]]
        self.assertEqual(len(sh), 1, sh)
        t = next(t for t in L.ruled_templates() if t.origin == sh[0][0] and t.binary == P.USERADD)
        self.assertEqual(t.tokens[sh[0][1] - 1], "--shell")

    def test_recovery_d15_group_other_write_modes_need_the_plans_exact_mode(self) -> None:
        _chown, leaf = self._leaf()
        for mode in ("0777", "00777", "0664", "00664", "0666", "0775", "0757", "0602"):
            with self.subTest(mode=mode):
                self.refused([P.CHMOD, mode, leaf])
        self.assertTrue(self.ok([P.CHMOD, "00750", leaf]))
        planned = L.ArgvAllowlist(L.ruled_templates(), L.ArgvAllowlist._manifest_slots(self.fmanifest), {},
                                  names={"root"}, modes={leaf: {0o660}})
        self.assertTrue(planned.classify([P.CHMOD, "00660", leaf], None))
        self.refused([P.CHMOD, "00664", leaf], allow=planned)
        # a preimage g/o-write mode: accepted in rollback only
        rb = L.ArgvAllowlist.for_plan(self.plan, self.fmanifest, self.wheelhouse, RCD,
                                      rollback_preimages={leaf: {"names": set(), "modes": {0o664}}})
        self.assertTrue(rb.classify([P.CHMOD, "00664", leaf], None))
        self.refused([P.CHMOD, "00664", leaf])
        self.refused([P.CHMOD, "00666", leaf], allow=rb)

    def test_recovery_d15_every_real_plan_mode_still_passes_the_for_plan_gate(self) -> None:
        argvs = family_argvs(self.plan, self.fmanifest)
        moded = [x for x in argvs if x[0] in (P.CHMOD, P.INSTALL)]
        self.assertTrue(moded)
        for argv in argvs:
            with self.subTest(argv=argv):
                self.assertTrue(self.ok(argv))

    def _with_mailbox(self, mode: str):
        """L4 fixture: the real plan plus ONE mailbox_dir.ensure entry with ``mode``."""

        frag = copy.deepcopy(A.fragment_from_plan(self.plan))
        account = sorted(frag["accounts"])[0]
        group = sorted(frag["groups"])[0] if frag.get("groups") else account
        path = "/etc/pantheon/host-broker/mbx-l4"
        frag.setdefault("mailbox_dirs", {})["mbx-l4"] = {"path": path, "owner": account, "group": group, "mode": mode}
        fm = F.load_manifest(frag)
        plan = copy.deepcopy(self.plan)
        plan["entries"].append({"id": "mbx-l4", "kind": "owner_mode",
                                "source": {"route": "family", "family": "mailbox_dir.ensure",
                                           "params": {"mailbox": "mbx-l4"}}})
        return plan, fm, path

    def test_delta_l4_a_planned_setgid_mailbox_dir_passes_through_for_plan(self) -> None:
        plan, fm, path = self._with_mailbox("2770")
        gate = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD)
        r = F.resolve("mailbox_dir.ensure", {"mailbox": "mbx-l4"}, fm)
        planned = P.apply_argv(r)
        chmods = [x for x in planned if x[0] in (P.CHMOD, P.INSTALL) and any("2770" in t for t in x)]
        self.assertTrue(chmods, planned)
        for argv in planned:
            with self.subTest(argv=argv):
                self.assertTrue(gate.classify(argv, None))
        _chown, other = self._leaf()
        self.assertNotEqual(other, path)
        for mode in ("02770", "2770"):
            with self.subTest(unplanned=mode):
                self.refused([P.CHMOD, mode, other], allow=gate)
        # the same path at a DIFFERENT special / g-write mode still refuses
        self.refused([P.CHMOD, "02777", path], allow=gate)
        self.refused([P.CHMOD, "00770", path], allow=gate)

    # --- delta review 2: M1 (ACL values bound to their path) --------------------------

    def _with_acl(self):
        """M1 fixture: the real plan plus ONE acl.converge path (T-5658/T-5665-valid)."""

        frag = copy.deepcopy(A.fragment_from_plan(self.plan))
        account = sorted(frag["accounts"])[0]
        path = "/etc/pantheon/host-broker/acl-m1"
        frag["paths"][path] = {"owner": "root", "group": "root", "mode": "0750",
                               "acl": ["user::rwx", "group::r-x", "other::---", f"user:{account}:r-x", "mask::r-x"]}
        fm = F.load_manifest(frag)
        plan = copy.deepcopy(self.plan)
        plan["entries"].append({"id": "acl-m1", "kind": "owner_mode",
                                "source": {"route": "family", "family": "acl.converge", "params": {"path": path}}})
        return plan, fm, path

    def test_delta_m1_the_reviewers_world_write_probe_refuses(self) -> None:
        probe = [P.SETFACL, "--physical", "--set", "user::rwx,group::rwx,other::rwx", "/etc/pantheon"]
        self.assertTrue(L.ArgvAllowlist._manifest_slots(self.fmanifest)[L.P_MUTATE]("/etc/pantheon"))
        self.refused(probe)
        plan, fm, path = self._with_acl()
        gate = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD)
        self.refused(probe, allow=gate)
        self.refused(probe[:-1] + [path], allow=gate)

    def test_delta_m1_only_the_plans_canonical_acl_passes_and_recoveries_probes_refuse(self) -> None:
        plan, fm, path = self._with_acl()
        gate = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD)
        (planned,) = P.apply_argv(F.resolve("acl.converge", {"path": path}, fm))
        self.assertEqual(planned[:3], [P.SETFACL, "--physical", "--set"])
        self.assertTrue(gate.classify(planned, None))
        account = sorted(fm.section("accounts"))[0]
        # Recovery D15 (ACL slot): every one of these refuses
        for value in ("user::rwx,group::rwx,other::rwx",  # the reviewer's probe
                      planned[3].replace("mask::r-x", "mask::rwx"),  # mask::rwx
                      planned[3] + ",user:nobody:rwx",  # an unplanned named entry
                      planned[3] + ",default:other::rwx",  # an unplanned default entry
                      planned[3].replace("other::---", "other::r--"),  # not write, but not the plan's
                      planned[3] + ",other::rwx",  # a second other:: entry (T-5665: not canonical)
                      f"user::rwx,group::r-x,other::---,user:{account}:rwx,mask::rwx",
                      planned[3] + ",u:nobody:rwx"):  # a short-form tag (not an allowlisted shape)
            with self.subTest(value=value):
                self.refused([P.SETFACL, "--physical", "--set", value, path], allow=gate)
        # a canonically-equal REORDERING of the planned ACL is accepted (Recovery D15)
        reordered = ",".join(reversed(planned[3].split(",")))
        self.assertNotEqual(reordered, planned[3])
        self.assertTrue(gate.classify([P.SETFACL, "--physical", "--set", reordered, path], None))
        _chown, other_path = self._leaf()
        self.refused([P.SETFACL, "--physical", "--set", planned[3], other_path], allow=gate)  # per path

    def test_recovery_d15_acl_canonical_is_the_existing_families_rule(self) -> None:
        """Parity: the canonical form IS F.resolve's sorted order, and a value the T-5665
        manifest check refuses (two entries for one key) is not canonical here either."""

        plan, fm, path = self._with_acl()
        spec = fm.section("paths")[path]
        r = F.resolve("acl.converge", {"path": path}, fm)
        self.assertEqual(L.acl_canonical(",".join(spec["acl"])), tuple(r.after["entries"]))
        dup = spec["acl"] + ["other::rwx"]
        frag = copy.deepcopy(A.fragment_from_plan(self.plan))
        frag["paths"][path] = {**spec, "acl": dup}
        with self.assertRaises(F.ManifestError):
            F.load_manifest(frag)
        self.assertIsNone(L.acl_canonical(",".join(dup)))
        self.assertIsNone(L.acl_canonical(""))
        self.assertIsNone(L.acl_canonical("user::rwx\n"))

    def test_delta_m1_every_real_plan_setfacl_passes_and_the_real_plan_has_none(self) -> None:
        setfacls = [x for x in family_argvs(self.plan, self.fmanifest) if x[0] == P.SETFACL]
        self.assertEqual(setfacls, [])  # the real plan names no ACL (rehearsal 2 brings the mini-manifest)
        plan, fm, path = self._with_acl()
        gate = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD)
        for argv in family_argvs(plan, fm):
            with self.subTest(argv=argv):
                self.assertTrue(gate.classify(argv, None))

    def test_delta_m1_a_preimage_acl_is_accepted_in_rollback_only(self) -> None:
        plan, fm, path = self._with_acl()
        pre = "group::r-x,mask::r-x,other::r--,user::rwx"
        apply_gate = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD)
        rb = L.ArgvAllowlist.for_plan(plan, fm, self.wheelhouse, RCD,
                                      rollback_preimages={path: {"names": set(), "modes": set(), "acls": {pre}}})
        argv = [P.SETFACL, "--physical", "--set", pre, path]
        self.refused(argv, allow=apply_gate)
        self.assertTrue(rb.classify(argv, None))
        _chown, other_path = self._leaf()
        self.refused([P.SETFACL, "--physical", "--set", pre, other_path], allow=rb)
        # rollback_preimages_of records the exact joined value from an acl: preimage
        att = mock.Mock(machine="rh-1", run_id="run-1", nonce_sha256="a" * 64, plan_sha256="b" * 64)
        doc = {"mode": "rehearsal-nspawn", "rehearsal": {"machine": "rh-1", "run_id": "run-1", "nonce_sha256": "a" * 64},
               "plan_sha256": "b" * 64,
               "entries": [{"family": {"subject": f"acl:{path}", "preimage": {"entries": pre.split(",")}}},
                           {"family": {"subject": "acl:/x", "preimage": {"entries": ["user::rwx\n"]}}}]}
        got = L.rollback_preimages_of(doc, att)
        self.assertEqual(got[path]["acls"], {pre})
        self.assertEqual(got["/x"]["acls"], set())

    def test_n1_with_no_single_mutated_path_any_acl_refuses(self) -> None:
        self.assertFalse(hasattr(L, "acl_grants_write"))
        gate = L.ArgvAllowlist([], {}, {}, acls={"/p": {"user::rwx,group::r-x,other::---"}})
        for values in ({"ACL": ["user::rwx,group::r-x,other::---"]},  # even a planned value, with no path
                       {"ACL": ["user::rwx,group::r-x,other::---"], L.P_MUTATE: ["/p", "/q"]}):
            with self.subTest(values=values):
                self.assertFalse(gate._context_ok(values))
        self.assertTrue(gate._context_ok({"ACL": ["user::rwx,group::r-x,other::---"], L.P_MUTATE: ["/p"]}))

    def test_n1_every_setfacl_template_carries_exactly_one_mutated_path(self) -> None:
        setfacls = [t for t in L.ruled_templates() if t.binary == P.SETFACL]
        self.assertTrue(setfacls)
        for t in setfacls:
            with self.subTest(origin=t.origin):
                slots = [n for tok in t.tokens if not isinstance(tok, str) for n in tok[1]]
                self.assertEqual(slots.count(L.P_MUTATE), 1, slots)
                self.assertNotIn(L.P_OBSERVE, slots)

    def test_the_observe_only_gate_refuses_every_dark_and_mutating_row(self) -> None:
        obs = L.ArgvAllowlist.observe_only(self.fmanifest)
        for s in sorted(P.manifest_subjects(self.fmanifest)):
            for argv in P.observe_argv(s):
                self.assertFalse(obs.classify(argv, None))
        for argv in [*P.snapshot_argv(), P.visudo_argv(RCD + "/substrate-staging/x")[0], list(A.PYYAML_PROBE),
                     *family_argvs(self.plan, self.fmanifest)[:5]]:
            with self.subTest(argv=argv):
                self.refused(argv, allow=obs)
        self.assertTrue(all(not t.mutating for t in L.ruled_templates(observe_only=True)))


# ---------------------------------------------------------------------------
# OperatorRunner pins
# ---------------------------------------------------------------------------


def _class(name: str) -> ast.ClassDef:
    return next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == name)


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


class RunnerPinTest(unittest.TestCase):
    def test_the_run_signature(self) -> None:
        sig = inspect.signature(L.OperatorRunner.run)
        params = list(sig.parameters.values())
        self.assertEqual([p.name for p in params], ["self", "argv", "cwd"])
        self.assertEqual(params[1].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertEqual(params[2].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(params[2].default)
        self.assertFalse([p for p in params if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)])

    def test_the_one_subprocess_call_is_run_of_the_kwargs_helper(self) -> None:
        calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual(len(calls), 1)
        (call,) = calls
        self.assertEqual(call.func.attr, "run")
        self.assertEqual(call.args, [])
        self.assertEqual(len(call.keywords), 1)
        kw = call.keywords[0]
        self.assertIsNone(kw.arg)  # **
        self.assertEqual(ast.unparse(kw.value), "_subprocess_kwargs(argv, cwd)")
        run = _method(_class("OperatorRunner"), "run")
        self.assertIn(call, list(ast.walk(run)))
        for node in ast.walk(TREE):  # no other process-spawning surface
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                self.assertFalse(node.attr.startswith(("exec", "spawn", "system", "popen", "fork")), node.attr)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                self.assertIn(node.attr, ("run", "DEVNULL", "TimeoutExpired"))

    def test_the_subprocess_kwargs(self) -> None:
        kw = L._subprocess_kwargs(("/usr/bin/getent", "passwd"), None)
        self.assertEqual(kw, {"args": ["/usr/bin/getent", "passwd"], "cwd": None, "env": {"LC_ALL": "C",
                              "PATH": "/usr/bin:/bin"}, "stdin": L.subprocess.DEVNULL, "capture_output": True,
                              "text": True, "errors": "replace", "check": False, "close_fds": True, "shell": False,
                              "timeout": 300})
        self.assertIsInstance(kw["args"], list)
        self.assertEqual(L._subprocess_kwargs(["/usr/bin/env", "-i"], "/fw")["timeout"], 1800)
        self.assertEqual(L._subprocess_kwargs(["/usr/bin/env", "-i"], "/fw")["cwd"], "/fw")
        env = L._subprocess_kwargs(["x"], None)["env"]
        env["X"] = "1"
        self.assertEqual(L._subprocess_kwargs(["x"], None)["env"], L.REQUIRED_ENV)  # a fresh copy each time


# ---------------------------------------------------------------------------
# live primitives (tmp root) and constructors
# ---------------------------------------------------------------------------

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


class PrimitivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        os.mkdir(os.path.join(self.root, "etc"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_mkdir_chown_chmod_rename_and_observe(self) -> None:
        r = self.root
        L._p_mkdir(r, "/etc/d", ME, MY_GROUP, "0750")
        L._p_write_file(r, "/etc/d/f", b"bytes", ME, MY_GROUP, "0640")
        obs = L._p_observe(r, "/etc/d/f")
        self.assertEqual((obs["type"], obs["owner"], obs["mode"], obs["sha256"]),
                         ("file", ME, "0640", hashlib.sha256(b"bytes").hexdigest()))
        self.assertEqual(L._p_observe(r, "/etc/d")["mode"], "0750")
        L._p_chmod(r, "/etc/d/f", "0600")
        L._p_chown(r, "/etc/d/f", ME, MY_GROUP)
        L._p_rename(r, "/etc/d/f", "/etc/d/g")
        self.assertEqual(L._p_read_bytes(r, "/etc/d/g"), b"bytes")
        L._p_symlink_replace(r, "/etc/active", "d")
        self.assertEqual(L._p_observe(r, "/etc/active")["link_target"], "d")
        L._p_unlink(r, "/etc/active", allow_leaf_symlink=True)
        L._p_remove_tree_contained(r, "/etc/d", "/etc")
        self.assertFalse(os.path.lexists(os.path.join(r, "etc/d")))
        hooked = []
        L._p_mkdir(r, "/etc/h", ME, MY_GROUP, "0700", hook=lambda op, p: hooked.append((op, p)))
        self.assertEqual(hooked, [("mkdir", "/etc/h")])

    def test_ancestor_and_leaf_symlinks_are_refused_for_every_write(self) -> None:
        r = self.root
        outside = os.path.join(r, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(r, "etc/link"))
        with open(os.path.join(outside, "t"), "wb") as f:
            f.write(b"x")
        os.symlink(os.path.join(outside, "t"), os.path.join(r, "etc/leaf"))
        anc = [lambda: L._p_write_file(r, "/etc/link/x", b"", ME, MY_GROUP, "0644"),
               lambda: L._p_mkdir(r, "/etc/link/x", ME, MY_GROUP, "0755"),
               lambda: L._p_chmod(r, "/etc/link/t", "0777"), lambda: L._p_chown(r, "/etc/link/t", ME, MY_GROUP),
               lambda: L._p_unlink(r, "/etc/link/t"), lambda: L._p_read_bytes(r, "/etc/link/t")]
        for i, fn in enumerate(anc):
            with self.subTest(ancestor=i), self.assertRaises(A.ApplyRefused):
                fn()
        leaf = [lambda: L._p_write_file(r, "/etc/leaf", b"", ME, MY_GROUP, "0644"),
                lambda: L._p_chmod(r, "/etc/leaf", "0777"), lambda: L._p_chown(r, "/etc/leaf", ME, MY_GROUP),
                lambda: L._p_unlink(r, "/etc/leaf"), lambda: L._p_rmdir(r, "/etc/leaf"),
                lambda: L._p_rename(r, "/etc/leaf", "/etc/new"), lambda: L._p_mkdir(r, "/etc/leaf", ME, MY_GROUP, "0755")]
        for i, fn in enumerate(leaf):
            with self.subTest(leaf=i), self.assertRaises(A.EntryFailed):
                fn()
        self.assertEqual(os.stat(os.path.join(outside, "t")).st_mode & 0o777 != 0o777, True)
        with self.assertRaises(L.LiveRefused):
            L._p_remove_tree_contained(r, "/etc/link", "/etc")

    def test_every_primitive_at_root_slash_without_the_key_refuses(self) -> None:
        """mason Q3 pin: no primitive reaches the real '/' without the attested LiveHost."""

        calls = {
            "_p_path": lambda root: L._p_path(root, "/etc/hostname"),
            "_p_wpath": lambda root: L._p_wpath(root, "/etc/hostname"),
            "_p_read_bytes": lambda root: L._p_read_bytes(root, "/etc/hostname"),
            "_p_observe": lambda root: L._p_observe(root, "/etc/hostname"),
            "_p_mkdir": lambda root: L._p_mkdir(root, "/tmp/u4b-never", ME, MY_GROUP, "0700"),
            "_p_write_file": lambda root: L._p_write_file(root, "/tmp/u4b-never", b"", ME, MY_GROUP, "0600"),
            "_p_chown": lambda root: L._p_chown(root, "/tmp", ME, MY_GROUP),
            "_p_chmod": lambda root: L._p_chmod(root, "/tmp", "0777"),
            "_p_unlink": lambda root: L._p_unlink(root, "/tmp/u4b-never"),
            "_p_rmdir": lambda root: L._p_rmdir(root, "/tmp/u4b-never"),
            "_p_rename": lambda root: L._p_rename(root, "/tmp/u4b-a", "/tmp/u4b-b"),
            "_p_symlink_replace": lambda root: L._p_symlink_replace(root, "/tmp/u4b-never", "x"),
            "_p_remove_tree_contained": lambda root: L._p_remove_tree_contained(root, "/tmp/u4b-never", "/tmp"),
        }
        self.assertEqual(set(calls), set(L.PRIMITIVES))
        link = os.path.join(self.root, "to-root")
        os.symlink("/", link)
        for name, call in calls.items():
            for root in ("/", link, "/./", "//"):
                with self.subTest(primitive=name, root=root), self.assertRaises(L.LiveRefused):
                    call(root)
        self.assertFalse(os.path.lexists("/tmp/u4b-never"))

    def test_the_constructors_refuse_without_a_loaded_attestation(self) -> None:
        fake = mock.Mock()
        with self.assertRaises(L.AttestationRefused):
            L.LiveHost(fake)
        with self.assertRaises(L.AttestationRefused):
            L.OperatorRunner(fake, L.ArgvAllowlist([], {}, {}))
        with self.assertRaises(L.AttestationRefused):
            L.RehearsalSubstratePort(fake, fake, fake, {})
        with self.assertRaises(L.AttestationRefused):
            L.RehearsalContext(fake, fake, fake, fake)


# ---------------------------------------------------------------------------
# construction sites, observe-only verbs, closure, mixin
# ---------------------------------------------------------------------------

LIVE_CLASSES = ("OperatorRunner", "LiveHost", "RehearsalSubstratePort", "RehearsalContext")


def _calls_named(tree: ast.AST, names) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names]


class ConstructionSiteTest(unittest.TestCase):
    def test_the_live_classes_are_constructed_only_in_main_after_load(self) -> None:
        main = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        load_calls = [n for n in ast.walk(main) if isinstance(n, ast.Call) and ast.unparse(n.func)
                      == "RehearsalAttestation.load"]
        self.assertEqual(len(load_calls), 1)
        load_line = load_calls[0].lineno
        everywhere = _calls_named(TREE, LIVE_CLASSES)
        in_main = _calls_named(main, LIVE_CLASSES)
        self.assertEqual(sorted(id(c) for c in everywhere), sorted(id(c) for c in in_main))
        self.assertEqual(sorted({c.func.id for c in in_main}), sorted(LIVE_CLASSES))
        self.assertTrue(all(c.lineno > load_line for c in in_main))
        # the attestation itself: only inside load (with the key)
        att = _calls_named(TREE, ("RehearsalAttestation",))
        load = _method(_class("RehearsalAttestation"), "load")
        self.assertEqual(len(att), 1)
        self.assertIn(att[0], list(ast.walk(load)))
        self.assertEqual(ast.unparse(att[0].args[0]), "_KEY")
        # the refusal return precedes every construction
        refused = [n for n in ast.walk(main) if isinstance(n, ast.Return) and ast.unparse(n.value) == "3"]
        self.assertTrue(refused and all(r.lineno < min(c.lineno for c in in_main) for r in refused))

    def test_no_other_tools_module_constructs_them(self) -> None:
        root = H.REPO_ROOT / "tools"
        names = (*LIVE_CLASSES, "RehearsalAttestation")
        for path in root.rglob("*.py"):
            if path == SOURCE_PATH or path.name.startswith("test_") or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not any(n in text for n in names):
                continue
            with self.subTest(path=str(path.relative_to(H.REPO_ROOT))):
                self.assertEqual(_calls_named(ast.parse(text), names), [])

    def test_the_probes_are_observe_only(self) -> None:
        main = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        branches = {ast.unparse(n.test): n for n in ast.walk(main) if isinstance(n, ast.If)}
        guard = branches["args.verb == 'guard-probe'"]
        self.assertEqual(_calls_named(guard, (*LIVE_CLASSES, "ArgvAllowlist")), [])
        self.assertIsInstance(guard.body[-1], ast.Return)
        observe = branches["args.verb == 'observe-probe'"]
        self.assertIsInstance(observe.body[-1], ast.Return)
        called = {ast.unparse(n.func) for n in ast.walk(observe) if isinstance(n, ast.Call)}
        self.assertEqual(called, {"OperatorRunner", "ArgvAllowlist.observe_only", "RehearsalSubstratePort", "sorted",
                                  "P.manifest_subjects", "_emit", "probe.get", "str"})
        # both probes sit BEFORE any LiveHost / context construction (and the for_plan gate)
        first_live = min(c.lineno for c in _calls_named(main, ("LiveHost", "RehearsalContext")))
        self.assertLess(observe.end_lineno, first_live)
        self.assertLess(guard.end_lineno, first_live)
        self.assertEqual(set(L.OBSERVE_ONLY_VERBS), {"guard-probe", "observe-probe"})


class ClosureExclusionTest(unittest.TestCase):
    def test_absent_from_the_framework_code_artifacts_and_the_closure(self) -> None:
        rel = "tools/host_broker_rehearsal_live.py"
        self.assertNotIn(rel, FRAMEWORK_CODE_ARTIFACTS)
        py_files, _external = H.import_closure(H.REPO_ROOT, H.CLOSURE_ENTRY_MODULES)
        self.assertNotIn(rel, py_files)
        self.assertNotIn(rel, T.closure().files)
        self.assertNotIn("tools/host_broker_install_apply.py", py_files)  # the seam's lazy import stays out too


#: Source-segment sha of HostSubstratePort.__init__ and sha of its docstring BEFORE
#: the mixin refactor (main at 842573604); the refactor must keep both byte-identical.
HOST_PORT_INIT_SOURCE_SHA = "34d4766a2755dba8732126563d86f4ead4448111e5d5635f2607b9f156a5b2f1"
HOST_PORT_DOC_SHA = "b2e0b55f6c1435f279388438b439ec6fe532cddb7f48818c3997538b80d78e6d"


def _source_segment_sha(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise AssertionError("AST node has no recoverable source segment")
    return hashlib.sha256(segment.encode()).hexdigest()


class MixinTest(unittest.TestCase):
    PORTS = Path(P.__file__)

    def test_the_host_port_constructor_and_dark_gate_are_byte_identical(self) -> None:
        source = self.PORTS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HostSubstratePort")
        init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual(_source_segment_sha(source, init), HOST_PORT_INIT_SOURCE_SHA)
        self.assertEqual(hashlib.sha256(ast.get_docstring(cls, clean=False).encode()).hexdigest(), HOST_PORT_DOC_SHA)

    def test_the_constructor_source_pin_is_ast_dump_independent(self) -> None:
        source = "def guarded():\n    return 'dark'"
        node = ast.parse(source).body[0]
        expected = hashlib.sha256(source.encode()).hexdigest()
        with mock.patch.object(ast, "dump", side_effect=AssertionError("version-specific AST serialization used")):
            self.assertEqual(_source_segment_sha(source, node), expected)

        mutated = source.replace("'dark'", "'live'")
        self.assertNotEqual(_source_segment_sha(mutated, ast.parse(mutated).body[0]), expected)
        with self.assertRaisesRegex(AssertionError, "no recoverable source segment"):
            _source_segment_sha(source, ast.Pass())

    def test_the_host_port_still_refuses_while_dark(self) -> None:
        self.assertIs(P.LIVE_ACTUATION_ENABLED, False)
        self.assertIs(H.LIVE_ACTUATION_ENABLED, False)
        with self.assertRaises(P.LiveActuationDisabled):
            P.HostSubstratePort(mock.Mock(), mock.Mock(), {})
        with self.assertRaises(P.LiveActuationDisabled):
            P.HostSubstratePort(None)

    def test_the_mixin_cannot_be_used_on_its_own(self) -> None:
        with self.assertRaises(P.LiveActuationDisabled):
            P._LiveSubstrateOps()
        self.assertNotIn("__init__", vars(P._LiveSubstrateOps))

    def test_exactly_two_subclasses(self) -> None:
        self.assertEqual({c.__name__ for c in P._LiveSubstrateOps.__subclasses__()},
                         {"HostSubstratePort", "RehearsalSubstratePort"})
        found = set()
        for path in (H.REPO_ROOT / "tools").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "_LiveSubstrateOps" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ClassDef) and any(ast.unparse(b).endswith("_LiveSubstrateOps") for b in node.bases):
                    found.add(node.name)
        self.assertEqual(found, {"HostSubstratePort", "RehearsalSubstratePort"})

    def test_the_rehearsal_port_overrides_only_init(self) -> None:
        self.assertEqual([n.name for n in _class("RehearsalSubstratePort").body if isinstance(n, ast.FunctionDef)],
                         ["__init__"])


# ---------------------------------------------------------------------------
# (d) the FORBIDDEN-ON-HOST canary
# ---------------------------------------------------------------------------

ARGS_TAIL = ["--nonce-sha256", NONCE_SHA, "--plan", "/nonexistent/p", "--manifest", "/nonexistent/m",
             "--registry", "/nonexistent/r", "--tracked-files", "/nonexistent/t", "--inputs", "/nonexistent/i",
             "--receipt", AT.RECEIPT]


class _TmpHost:
    """A read-only stand-in for LiveHost's receipt surface, rooted at a temp dir."""

    def __init__(self, root: str):
        self.root, self.pins = root, []

    def path(self, p: str) -> Path:
        return Path(self.root + p)

    def exists(self, p: str) -> bool:
        return os.path.lexists(self.path(p))

    def pin(self, p: str) -> None:
        self.pins.append(p)

    def read_bytes(self, p: str) -> bytes:
        return self.path(p).read_bytes()


def _att():
    return mock.Mock(machine="rh-1", run_id="run-1", nonce_sha256="a" * 64, plan_sha256="b" * 64)


#: delta-review L3: each malformed shape -> (receipt bytes or None, journal bytes or None)
def _malformed():
    entries5 = {"schema": A.RECEIPT_SCHEMA, "entries": 5}
    entries5["receipt_sha256"] = A.receipt_digest(entries5)
    return {"non-dict receipt": (b"[1, 2]", None),
            "non-json receipt": (b"{not json", None),
            "non-iterable entries": (json.dumps(entries5).encode(), None),
            "empty journal": (None, b""),
            "keyless journal head": (None, b"{}\n"),
            "non-dict journal head": (None, b"[1]\n"),
            "non-json journal": (None, b"nope\n"),
            "non-ascii journal": (None, b"\xff\n"),
            "neither file": (None, None)}


class ReceiptTest(unittest.TestCase):
    """Delta-review L2 (owner / mode / double read) and L3 (malformed receipts refuse)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="u4b-receipt-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _plant(self, receipt: bytes | None, journal: bytes | None) -> _TmpHost:
        host = _TmpHost(self.tmp)
        base = host.path(AT.RECEIPT)
        (base.parent / (base.name + ".d")).mkdir(parents=True, exist_ok=True)
        if receipt is not None:
            base.write_bytes(receipt)
        if journal is not None:
            host.path(posixpath.join(AT.RECEIPT + ".d", "progress.jsonl")).write_bytes(journal)
        return host

    def test_l3_each_malformed_shape_raises_a_receipt_error_never_a_crash(self) -> None:
        for name, (receipt, journal) in _malformed().items():
            with self.subTest(shape=name):
                host = self._plant(receipt, journal)
                with self.assertRaises(L.RECEIPT_ERRORS):
                    L.rollback_receipt_preimages(host, AT.RECEIPT, _att())
                for f in (host.path(AT.RECEIPT), host.path(AT.RECEIPT + ".d/progress.jsonl")):
                    if f.exists():
                        f.unlink()

    def test_l2_both_files_are_pinned_before_the_load(self) -> None:
        good = {"schema": A.RECEIPT_SCHEMA, "entries": [], "mode": "rehearsal-nspawn"}
        good["receipt_sha256"] = A.receipt_digest(good)
        host = self._plant(json.dumps(good).encode(), b"{}\n")
        self.assertEqual(L.rollback_receipt_preimages(host, AT.RECEIPT, _att()), {})
        self.assertEqual(host.pins, [AT.RECEIPT, AT.RECEIPT + ".d/progress.jsonl"])

    def test_l2_the_stat_rule_matches_attestation_json(self) -> None:
        def st(mode, uid=0, gid=0):
            return os.stat_result((mode, 0, 0, 1, uid, gid, 10, 0, 0, 0))

        self.assertIsNone(L.receipt_file_refusal(st(0o100600)))
        self.assertIsNone(L.receipt_file_refusal(st(0o100644)))
        for bad in (st(0o100620), st(0o100602), st(0o100666), st(0o100600, uid=1007), st(0o100600, gid=1007),
                    st(0o040700), st(0o120777)):
            with self.subTest(st=bad):
                self.assertIsNotNone(L.receipt_file_refusal(bad))

    def test_l2_pin_refuses_a_non_root_file_and_a_symlink_on_the_real_fs(self) -> None:
        f = Path(self.tmp) / "r.json"
        f.write_bytes(b"{}")
        link = Path(self.tmp) / "link.json"
        link.symlink_to(f)
        for p in (f, link, Path(self.tmp) / "absent.json"):
            with self.subTest(p=p.name):
                with self.assertRaises(A.ApplyRefused):
                    L._read_root_owned(str(p))

    def test_l2_a_pinned_receipt_must_read_back_the_same_bytes(self) -> None:
        f = Path(self.tmp) / "r.json"
        f.write_bytes(b"first")
        ns = SimpleNamespace(_pins={})  # the real methods, bound to a namespace: no LiveHost is constructed
        for meth in ("path", "_check_absent", "read_bytes", "pin"):
            setattr(ns, meth, getattr(L.LiveHost, meth).__get__(ns))
        with mock.patch.object(L, "receipt_file_refusal", lambda _st: None):
            ns.pin(str(f))
        self.assertEqual(ns._pins, {str(f): L.sha256_bytes(b"first")})
        self.assertEqual(L.LiveHost.read_bytes(ns, str(f)), b"first")
        f.write_bytes(b"swapped")
        with self.assertRaises(A.ApplyRefused):
            L.LiveHost.read_bytes(ns, str(f))
        other = Path(self.tmp) / "unpinned"
        other.write_bytes(b"x")
        self.assertEqual(L.LiveHost.read_bytes(ns, str(other)), b"x")  # unpinned paths read normally

    def test_l5_a_path_pinned_absent_that_appears_later_refuses(self) -> None:
        """Only the journal exists at pin time; the receipt appears before the load."""

        receipt = self.tmp + "/r.json"
        journal = self.tmp + "/r.json.d/progress.jsonl"
        Path(journal).parent.mkdir()
        Path(journal).write_bytes(b"{}\n")
        ns = SimpleNamespace(_pins={})
        for meth in ("path", "_check_absent", "exists", "read_bytes", "pin"):
            setattr(ns, meth, getattr(L.LiveHost, meth).__get__(ns))
        with mock.patch.object(L, "receipt_file_refusal", lambda _st: None):
            for p_ in (receipt, journal):
                ns.pin(p_)
        self.assertIsNone(ns._pins[receipt])
        self.assertIsNotNone(ns._pins[journal])
        self.assertFalse(ns.exists(receipt))  # still absent: fine
        Path(receipt).write_bytes(b"{}")
        for call in (lambda: ns.exists(receipt), lambda: ns.path(receipt).is_file(), lambda: ns.read_bytes(receipt),
                     lambda: A._load_receipt(ns, receipt)):
            with self.assertRaises(A.ApplyRefused):
                call()
        # N2: the write-path and observe accessors refuse it too
        for meth in ("wpath", "observe"):
            setattr(ns, meth, getattr(L.LiveHost, meth).__get__(ns))
        with self.assertRaises(A.ApplyRefused):
            ns.wpath(receipt)
        with self.assertRaises(A.ApplyRefused):
            ns.observe(receipt)
        # the journal itself still reads (its pin matches)
        self.assertEqual(ns.read_bytes(journal), b"{}\n")

    def test_l3_main_emits_the_json_refusal_rc_2_for_each_malformed_shape(self) -> None:
        plan, _fm = plan_and_manifest()
        facts = replace(GOOD, attestation=att_fact(plan_sha256=plan["plan_sha256"]))
        rebuilt = L.Rebuilt(b"", b"", b"", b"", b"", plan=plan, inputs=None)  # type: ignore[arg-type]
        boom = mock.Mock(side_effect=AssertionError("for_plan reached"))
        for name, (receipt, journal) in _malformed().items():
            with self.subTest(shape=name):
                host = self._plant(receipt, journal)
                patches = [mock.patch.object(L, "_read_facts", lambda: facts),
                           mock.patch.object(L, "_rebuild", lambda args: rebuilt),
                           mock.patch.object(L.os, "getcwd", lambda: L.REPO_BIND),
                           mock.patch.object(L, "LiveHost", lambda att: host),
                           mock.patch.object(L.ArgvAllowlist, "for_plan", boom),
                           mock.patch.object(A, "rollback", boom)]
                for p_ in patches:
                    p_.start()
                try:
                    out = io.StringIO()
                    with redirect_stdout(out):
                        rc = L.main(["rollback", *[(plan["plan_sha256"] if a == "x" else a) for a in ARGS_TAIL]])
                finally:
                    for p_ in reversed(patches):
                        p_.stop()
                self.assertEqual(rc, 2, out.getvalue())
                doc = json.loads(out.getvalue())
                self.assertEqual((doc["verb"], doc["verdict"]), ("rollback", "refused"))
                self.assertTrue(doc["reasons"][0].startswith("receipt: "))
                boom.assert_not_called()
                for f in (host.path(AT.RECEIPT), host.path(AT.RECEIPT + ".d/progress.jsonl")):
                    if f.exists():
                        f.unlink()


class CanaryTest(unittest.TestCase):
    def _stubs(self):
        hits: list[str] = []

        def stub(name):
            def boom(*_a, **_k):
                hits.append(name)
                raise AssertionError(f"{name} was reached")
            return boom

        patches = [mock.patch.object(L, name, stub(name)) for name in LIVE_CLASSES]
        patches += [mock.patch.object(L.ArgvAllowlist, "for_plan", stub("ArgvAllowlist.for_plan")),
                    mock.patch.object(L.ArgvAllowlist, "observe_only", stub("ArgvAllowlist.observe_only")),
                    mock.patch.object(A, "apply", stub("apply")), mock.patch.object(A, "verify", stub("verify")),
                    mock.patch.object(A, "rollback", stub("rollback")),
                    mock.patch.object(L.os, "getcwd", lambda: L.REPO_BIND)]
        return hits, patches

    def _main(self, verb, facts=None, plan_sha=PLAN_SHA):
        hits, patches = self._stubs()
        rebuilt = L.Rebuilt(b"", b"", b"", b"", b"", plan={"plan_sha256": plan_sha}, inputs=None)  # type: ignore[arg-type]
        if facts is not None:
            patches += [mock.patch.object(L, "_read_facts", lambda: facts),
                        mock.patch.object(L, "_rebuild", lambda args: rebuilt)]
        for p in patches:
            p.start()
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = L.main([verb, *ARGS_TAIL])
        finally:
            for p in reversed(patches):
                p.stop()
        return rc, hits, out.getvalue()

    def test_every_verb_refuses_on_this_real_host_before_any_construction(self) -> None:
        for verb in L.VERBS:
            with self.subTest(verb=verb):
                rc, hits, out = self._main(verb)
                self.assertEqual(rc, 3)
                self.assertEqual(hits, [])
                self.assertEqual(json.loads(out)["verdict"], "refused")

    def test_every_verb_refuses_with_each_single_factor_wrong(self) -> None:
        for verb in L.VERBS:
            for name, facts, nonce, plan in CORRUPTIONS:
                if nonce != NONCE_SHA:  # the argv-nonce factor: corrupt the argv instead
                    continue
                with self.subTest(verb=verb, factor=name):
                    rc, hits, _out = self._main(verb, facts, plan)
                    self.assertEqual(rc, 3, name)
                    self.assertEqual(hits, [], name)

    def test_a_wrong_argv_nonce_refuses_every_verb(self) -> None:
        global ARGS_TAIL
        saved = ARGS_TAIL
        try:
            ARGS_TAIL = [("d" * 64 if a == NONCE_SHA else a) for a in saved]
            for verb in L.VERBS:
                rc, hits, _out = self._main(verb, GOOD)
                self.assertEqual((rc, hits), (3, []))
        finally:
            ARGS_TAIL = saved

    def test_positive_control_good_facts_reach_only_the_guard_probe_verdict(self) -> None:
        """The harness itself is live: with every factor good, guard-probe attests and
        constructs nothing (and the stubs WOULD catch a construction)."""

        rc, hits, out = self._main("guard-probe", GOOD)
        self.assertEqual(rc, 0)
        self.assertEqual(hits, [])
        self.assertEqual(json.loads(out)["verdict"], "attested")
        self.assertEqual(json.loads(out)["plan_sha256"], PLAN_SHA)


if __name__ == "__main__":
    unittest.main()
