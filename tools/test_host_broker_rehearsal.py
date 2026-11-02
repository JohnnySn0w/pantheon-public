#!/usr/bin/env python3
"""Tests for tools/host_broker_rehearsal.py (T-2920 U4a: the nspawn rehearsal harness, DARK).

Nothing here runs as root, creates a machine or touches a host path. The emitted
root script is pinned by a golden and linted (Recovery D15 addition 1: the nspawn argv
has -U and exactly one read-only bind). The evidence checker is exercised against
a GOOD FIXTURE evidence dir and against one mutation per check id.

The good evidence is a FIXTURE: the receipts, rollbacks, snapshots and verify rows
come from the REAL U3a code run under a fixture root (FixtureHost +
FixtureCommandRunner) with the harness's fixture inputs; the machine-only
observations (mountinfo, systemctl, busctl, journal, /proc) are canned text in the
shapes the pinned argv print. It is replaced by sanitized real capture after
orchestrator's first nspawn run. The main receipt's mode is rewritten from "fixture" to
"rehearsal-nspawn" (digest recomputed), which only the U4b live path can produce.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools import host_broker_install_apply as A  # noqa: E402
from tools import host_broker_rehearsal as R  # noqa: E402
from tools import test_host_broker_install as T  # noqa: E402
from tools import test_host_broker_install_apply as TA  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.host_substrate import ports as P  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "host_broker_rehearsal.commands.golden.sh"
GOLDEN_ARGS = ("0123456789ab", "1" * 40, "/home/orchestrator/pantheon-config")
RUN_ID = GOLDEN_ARGS[0]
NONCE = "a" * 64
HOST_MID = "b" * 64
FIXTURE_TOKEN = b"FIXTURE-NOT-A-TOKEN-0123456789abcdef0123456789abcdef\n"
NOW = "2026-10-27T12:00:00Z"


def emitted() -> str:
    return R.emit_commands(*GOLDEN_ARGS)


# ---------------------------------------------------------------------------
# 1. the emitted script: golden, lint, nspawn argv, prerequisites, wheelhouse
# ---------------------------------------------------------------------------


class ScriptTest(unittest.TestCase):
    def test_deterministic_and_pinned_by_the_golden(self) -> None:
        self.assertEqual(emitted(), emitted())
        self.assertEqual(emitted(), GOLDEN.read_text(encoding="utf-8"),
                         "regenerate: python3 -I tools/host_broker_rehearsal.py --emit-commands --run-id 0123456789ab "
                         "--repo-commit 1111111111111111111111111111111111111111 --repo /home/orchestrator/pantheon-config")

    def test_debootstrap_reads_universe_for_python3_venv(self) -> None:
        # T-5702: python3-venv is in universe; a main-only debootstrap fails on the first real run
        self.assertIn("python3-venv", R.DEBOOTSTRAP_INCLUDE)
        self.assertIn("--components=main,universe", emitted())

    def test_cli_emits_the_same_bytes(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.main(["--emit-commands", "--run-id", GOLDEN_ARGS[0], "--repo-commit", GOLDEN_ARGS[1],
                         "--repo", GOLDEN_ARGS[2]])
        self.assertEqual((rc, buf.getvalue()), (0, emitted()))

    def test_emit_refuses_unsafe_arguments(self) -> None:
        for args in (("XYZ", "1" * 40, "/r"), (RUN_ID, "abc", "/r"), (RUN_ID, "1" * 40, "rel"),
                     (RUN_ID, "1" * 40, "/r;x"), (RUN_ID, "1" * 40, "/a/../b")):
            with self.assertRaises(ValueError):
                R.emit_commands(*args)

    def test_lint_is_clean(self) -> None:
        self.assertEqual(R.lint_script(emitted(), RUN_ID), [])

    def test_nspawn_argv_is_pinned(self) -> None:
        self.assertEqual(R.nspawn_argv(RUN_ID), [
            "/usr/bin/systemd-nspawn", "--quiet", "--boot", "--notify-ready=yes", "-U", "--private-network",
            "--machine=pantheon-rehearsal-0123456789ab",
            "--directory=/var/lib/machines/pantheon-rehearsal-0123456789ab",
            "--bind-ro=/srv/rehearsal/repo"])
        self.assertEqual(R.READ_ONLY_BINDS, ("/srv/rehearsal/repo",))

    def _lint_with(self, old: str, new: str) -> list[str]:
        text = emitted()
        self.assertIn(old, text)
        return R.lint_script(text.replace(old, new, 1), RUN_ID)

    def test_lint_fails_on_a_writable_bind(self) -> None:
        problems = self._lint_with("--bind-ro=/srv/rehearsal/repo", "--bind=/srv/rehearsal/repo")
        self.assertTrue(any("--bind=" in p for p in problems), problems)

    def test_lint_fails_on_a_missing_private_users_flag(self) -> None:
        problems = self._lint_with(" -U --private-network", " --private-network")
        self.assertTrue(any("-U" in p for p in problems), problems)

    def test_lint_fails_on_an_extra_read_only_bind(self) -> None:
        problems = self._lint_with("--bind-ro=/srv/rehearsal/repo",
                                   "--bind-ro=/srv/rehearsal/repo --bind-ro=/var/cache/pantheon-rehearsal/wheelhouse")
        self.assertTrue(any("read-only binds" in p for p in problems), problems)

    def test_lint_fails_on_volatile_or_a_second_nspawn(self) -> None:
        self.assertTrue(self._lint_with("--quiet --boot", "--quiet --volatile=yes --boot"))
        text = emitted().replace("  sleep 35\n", "  timeout 9 /usr/bin/systemd-nspawn --quiet\n", 1)
        self.assertTrue(any("exactly one" in p for p in R.lint_script(text, RUN_ID)))

    def test_lint_fails_on_a_non_literal_rm_target(self) -> None:
        problems = self._lint_with("rm -f /var/lib/pantheon-rehearsal/0123456789ab/nonce",
                                   "rm -f /var/lib/pantheon-rehearsal/0123456789ab/*")
        self.assertTrue(any("rm target" in p for p in problems), problems)
        self.assertTrue(any("rm target" in p for p in self._lint_with(
            "timeout 300 rm -rf --one-file-system /var/cache/pantheon-rehearsal/wheelhouse",
            "timeout 300 rm -rf --one-file-system /var/cache/pantheon-rehearsal")))

    def test_lint_fails_on_an_untimed_command_and_a_stray_write(self) -> None:
        self.assertTrue(any("not under timeout" in p for p in self._lint_with(
            "  timeout 900 cp -a --reflink=auto", "  cp -a --reflink=auto")))
        self.assertTrue(any("write outside" in p for p in self._lint_with(
            "> /var/lib/pantheon-rehearsal/0123456789ab/evidence/P0-env.txt", "> /etc/pantheon/x")))

    def test_lint_fails_on_a_verb_u4b_does_not_have(self) -> None:
        problems = self._lint_with("host_broker_rehearsal_live.py guard-probe", "host_broker_rehearsal_live.py shell")
        self.assertTrue(any("not a U4b verb" in p for p in problems), problems)

    def test_every_live_call_is_attested_and_runs_in_the_machine_except_the_host_canary(self) -> None:
        live = [ln for ln in emitted().splitlines() if "host_broker_rehearsal_live.py " in ln and "test -f" not in ln
                and not ln.lstrip().startswith("#")]
        self.assertGreaterEqual(len(live), 10)
        for ln in live:
            self.assertIn('--nonce-sha256 "$NONCE_SHA256"', ln)
        host = [ln for ln in live if "--machine=" not in ln]
        self.assertEqual(len(host), 1, host)
        self.assertIn("guard-probe", host[0])
        self.assertIn("/evidence/host-canary.json || rc=$?", host[0])

    def test_every_live_call_carries_u4bs_required_flags_and_cwd(self) -> None:
        live = [ln for ln in emitted().splitlines() if "host_broker_rehearsal_live.py " in ln and "test -f" not in ln
                and not ln.lstrip().startswith("#")]
        for ln in live:
            for flag in ("--plan ", "--manifest ", "--registry ", "--tracked-files ", "--inputs ",
                         "--receipt /var/lib/pantheon-install-receipts/"):
                self.assertIn(flag, ln)
            if "--machine=" in ln:
                self.assertIn(f"--working-directory={R.LIVE_CWD} ", ln)
        self.assertIn("KNOWN FIRST-RUN RISK", emitted())
        lines = emitted().splitlines()
        mountinfo = next(i for i, ln in enumerate(lines) if "/proc/self/mountinfo" in ln)
        first_machine_live = next(i for i, ln in enumerate(lines) if "--machine=" in ln and "rehearsal_live.py" in ln)
        self.assertLess(mountinfo, first_machine_live)

    def test_entrypoint_absence_aborts_before_the_machine_exists(self) -> None:
        lines = emitted().splitlines()
        guard = next(i for i, ln in enumerate(lines) if "test -f /srv/rehearsal/repo/tools/host_broker_rehearsal_live.py" in ln)
        copy_ = next(i for i, ln in enumerate(lines) if "cp -a --reflink=auto" in ln)
        self.assertLess(guard, copy_)
        self.assertIn("exit 3", lines[guard])

    def test_attestation_is_copied_in_after_the_plan_with_exactly_six_keys(self) -> None:
        lines = emitted().splitlines()
        plan = next(i for i, ln in enumerate(lines) if "host_broker_install.py plan" in ln and "--machine=" in ln)
        write = next(i for i, ln in enumerate(lines)
                     if "printf" in ln and ln.endswith("> /var/lib/pantheon-rehearsal/0123456789ab/attestation.json"))
        copy_in = next(i for i, ln in enumerate(lines) if "machinectl copy-to" in ln)
        first_live = next(i for i, ln in enumerate(lines) if "--machine=" in ln and "host_broker_rehearsal_live.py" in ln)
        self.assertTrue(plan < write < copy_in < first_live)
        body = lines[write].split("printf '", 1)[1].split("\\n'", 1)[0]
        doc = json.loads(body.replace("%s", "x"))
        self.assertEqual(tuple(sorted(doc)), R.ATTESTATION_KEYS)
        self.assertEqual(doc["schema"], R.ATTESTATION_SCHEMA)
        perms = " ".join(lines[copy_in + 1:copy_in + 3])
        self.assertIn("/usr/bin/chown root:root /etc/pantheon-rehearsal/attestation.json", perms)
        self.assertIn("/usr/bin/chmod 0600 /etc/pantheon-rehearsal/attestation.json", perms)

    def test_prerequisites_block_and_rollbacks(self) -> None:
        text = emitted()
        head = text.split("prerequisites() {", 1)[0]
        for pre in R.PREREQUISITES:
            self.assertIn(f"#     command:  {' '.join(pre['command'])}", head)
            self.assertIn(f"#     ROLLBACK: {' '.join(pre['rollback'])}", head)
        self.assertIn("# After a PARTIAL prerequisites failure, run rollback-prerequisites before re-running", head)
        body = text.split("prerequisites() {", 1)[1].split("\n}\n", 1)[0].splitlines()
        idx = {k: next(i for i, ln in enumerate(body) if k in ln) for k in (
            "dpkg-query -W -f='${Status}\\n' systemd-container", "SYSTEMD_VERSION=\"$(dpkg-query -W -f='${Version}' systemd)\"",
            "apt-get -s install --no-install-recommends", "Inst [^ ]+ \\[|Remv ", "prereq-new-packages.txt || true",
            "timeout 900 apt-get install -y")}
        order = [idx[k] for k in ("dpkg-query -W -f='${Status}\\n' systemd-container",
                                  "SYSTEMD_VERSION=\"$(dpkg-query -W -f='${Version}' systemd)\"",
                                  "apt-get -s install --no-install-recommends", "Inst [^ ]+ \\[|Remv ",
                                  "prereq-new-packages.txt || true", "timeout 900 apt-get install -y")]
        self.assertEqual(order, sorted(order), "pre-state, version, simulation, upgrade refusal, list, then install")
        install = body[idx["timeout 900 apt-get install -y"]]
        self.assertEqual(install.strip(), 'timeout 900 apt-get install -y --no-install-recommends --no-remove '
                                          '"systemd-container=$SYSTEMD_VERSION" debootstrap  # PRE-a')
        self.assertEqual(sum("apt-get install" in ln for ln in text.splitlines() if not ln.startswith("#")), 1)
        rb = text.split("rollback_prerequisites() {", 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(sum("apt-get purge" in ln for ln in text.splitlines() if not ln.startswith("#")), 1)
        self.assertNotIn("purge -y --no-remove", text)
        sim = rb.index("apt-get -s purge >")
        guard = rb.index("comm -23 /var/lib/pantheon-rehearsal/prereq-purge-set.txt")
        purge = rb.index("apt-get purge -y; fi")
        self.assertTrue(sim < guard < purge, "simulate, subset-guard, then purge")
        archive = rb.index('PREREQ_TS="$(date -u +%Y%m%dT%H%M%SZ)"')
        self.assertLess(purge, archive, "the list is purged before it is archived")
        self.assertIn("printf '%s\\n' \"$PREREQ_TS\" | grep -qxE '[0-9]{8}T[0-9]{6}Z'", rb)
        self.assertIn('test ! -e "/var/lib/pantheon-rehearsal/prereq-archive/$PREREQ_TS"', rb)
        for f in R.PREREQ_RECORD_FILES:
            self.assertIn(f'if test -e {f}; then timeout 60 mv -n -- {f} '
                          f'"/var/lib/pantheon-rehearsal/prereq-archive/$PREREQ_TS/"; fi', rb)
            self.assertIn(f"test ! -e {f}", rb)
            self.assertNotIn(f"rm -f {f}", text)
            self.assertNotIn(f, R.allowed_rm_targets(RUN_ID))
        self.assertIn("rm -rf --one-file-system /var/cache/pantheon-rehearsal/base-noble", rb)
        self.assertIn("rm -rf --one-file-system /var/cache/pantheon-rehearsal/wheelhouse", rb)
        pip = R.PREREQUISITES[2]["command"]
        for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps"):
            self.assertIn(flag, pip)
        # the run never executes a prerequisite
        run = text.split("run() {", 1)[1]
        self.assertNotIn("apt-get", run)
        self.assertNotIn("debootstrap --variant", run)
        self.assertNotIn("pip download", run)
        self.assertIn("  *) echo 'usage: THIS prerequisites | run | rollback-prerequisites' >&2; exit 64 ;;", text)

    def test_lint_fails_on_an_unpinned_install_or_a_cascading_purge(self) -> None:
        self.assertTrue(self._lint_with('"systemd-container=$SYSTEMD_VERSION" debootstrap  # PRE-a',
                                        "systemd-container debootstrap  # PRE-a"))
        purge = "timeout 900 xargs -r -a /var/lib/pantheon-rehearsal/prereq-purge-set.txt apt-get purge -y; fi"
        self.assertTrue(any("unknown package-removal form" in p for p in self._lint_with(
            purge, purge.replace("purge -y; fi", "purge -y --no-remove; fi"))))
        self.assertTrue(any("unknown package-removal form" in p for p in self._lint_with(
            purge, purge.replace("prereq-purge-set.txt", "prereq-new-packages.txt"))))
        self.assertTrue(self._lint_with("timeout 900 apt-get install -y --no-install-recommends",
                                        "timeout 900 apt-get install -y"))
        self.assertTrue(any("--no-remove" in p for p in self._lint_with(
            "--no-install-recommends --no-remove \"systemd-container", "--no-install-recommends \"systemd-container")))

    # canned apt-get -s purge text only: nothing here runs apt, dpkg or any emitted line
    _SIM_OK = "Purg systemd-container [255.4-1ubuntu8.18]\nPurg debootstrap [1.0.134ubuntu1]\n"
    _REC = "systemd-container\ndebootstrap\n"

    def test_rollback_purges_the_verified_set(self) -> None:
        self.assertEqual(R.prea_rollback_decision(self._SIM_OK, self._REC), "purge")
        self.assertEqual(R.prea_rollback_decision(self._SIM_OK, "debootstrap\nsystemd-container\n"), "purge",
                         "order-insensitive (both sides are sort -u)")
        noisy = "NOTE: This is only a simulation!\n" + self._SIM_OK + "Inst something-else (1 noble)\n"
        self.assertEqual(R.prea_rollback_decision(noisy, self._REC), "purge", "only Purg/Remv lines count")

    def test_rollback_refuses_on_an_extra_cascaded_removal(self) -> None:
        extra = self._SIM_OK + "Remv libnss-mymachines [255.4-1ubuntu8.18]\n"
        self.assertEqual(R.prea_rollback_decision(extra, self._REC), "refuse")
        self.assertEqual(R.prea_rollback_decision("Remv libc6 [2.39]\n", self._REC), "refuse",
                         "an extra removal refuses even when nothing recorded would be purged")

    def test_partial_or_failed_install_never_refuses(self) -> None:
        # prerequisites recorded the Inst list, then the install failed (network, a held dpkg lock):
        # the purge simulation finds nothing installed, so the rollback proceeds and archives the records
        inst = "Inst systemd-container (255.4-1ubuntu8.18 Ubuntu:24.04/noble-updates [amd64])\nInst debootstrap (1.0.134ubuntu1 Ubuntu:24.04/noble [all])\n"
        recorded = "".join(ln.split(" ")[1] + "\n" for ln in inst.splitlines())
        self.assertEqual(recorded, self._REC)
        self.assertEqual(R.prea_rollback_decision("NOTE: This is only a simulation!\n", recorded), "skip")
        self.assertEqual(R.prea_rollback_decision("", recorded), "skip")
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1.0.134ubuntu1]\n", recorded), "purge",
                         "a smaller set (a partial install) proceeds")

    def test_recorded_names_dpkg_never_knew_cannot_fail_the_simulation(self) -> None:
        # a recorded package that never installed and left the apt index: dpkg-query drops it, the
        # simulation runs over the rest, and the subset check stays against the FULL recorded list
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1]\n", self._REC, known="debootstrap\n"),
                         "purge")
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1]\n", self._REC, known=""), "skip",
                         "dpkg knows none of them: the simulation file is truncated empty, nothing purged")
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1]\nRemv libc6 [2.39]\n", self._REC,
                                                  known="debootstrap\n"), "refuse")
        block = self._BLOCK_LITERAL
        self.assertLess(block.index("dpkg-query -W"), block.index("apt-get -s purge"))
        self.assertIn("|| test $? = 123; fi", block, "only xargs's 123 (dpkg-query rc 1) is tolerated, never a timeout")
        self.assertIn("comm -23 /var/lib/pantheon-rehearsal/prereq-purge-set.txt "
                      "/var/lib/pantheon-rehearsal/prereq-new-packages.sorted.txt", block)

    # canned dpkg-query -W -f='${Package} ${db:Status-Status}\n' dpkg <recorded...> output only
    def test_verify_all_purged_archives(self) -> None:
        self.assertEqual(R.prea_rollback_verify("dpkg installed\n", self._REC), "archive",
                         "purged and dropped from the dpkg database: only the sentinel line")
        self.assertEqual(R.prea_rollback_verify(
            "dpkg installed\nsystemd-container not-installed\ndebootstrap not-installed\n", self._REC), "archive")

    def test_verify_one_still_installed_refuses_and_does_not_archive(self) -> None:
        status = "dpkg installed\nsystemd-container not-installed\ndebootstrap installed\n"
        self.assertEqual(R.prea_rollback_verify(status, self._REC), "refuse-left")
        for leftover in ("config-files", "half-installed", "unpacked", "half-configured"):
            with self.subTest(status=leftover):
                self.assertEqual(R.prea_rollback_verify(f"dpkg installed\ndebootstrap {leftover}\n", self._REC),
                                 "refuse-left", "anything but not-installed is a leftover")
        rb = emitted().split("rollback_prerequisites() {", 1)[1].split("\n}\n", 1)[0]
        left = rb.index("then timeout 60 xargs -a /var/lib/pantheon-rehearsal/prereq-verify-left.txt echo "
                        "'PRE-a rollback incomplete: still installed:' >&2; exit 3; fi")
        self.assertLess(rb.index("apt-get purge -y; fi"), left, "the verify follows the purge")
        self.assertLess(left, rb.index('PREREQ_TS="$(date -u +%Y%m%dT%H%M%SZ)"'), "and refuses BEFORE any archive")
        self.assertLess(left, rb.index(" mv -n -- "))

    def test_verify_never_installed_names_archive(self) -> None:
        # dpkg-query prints nothing for a name it does not know (rc 1, xargs 123, tolerated)
        self.assertEqual(R.prea_rollback_verify("dpkg installed\n", "never-was\nalso-never\n"), "archive")

    def test_verify_db_unreadable_refuses(self) -> None:
        # dpkg-query rc 2 also becomes xargs 123; the missing sentinel line is what refuses
        self.assertEqual(R.prea_rollback_verify("", self._REC), "refuse-db")
        self.assertEqual(R.prea_rollback_verify("debootstrap not-installed\n", self._REC), "refuse-db")
        self.assertEqual(R.prea_rollback_verify("dpkg half-configured\n", self._REC), "refuse-db")
        rb = emitted().split("rollback_prerequisites() {", 1)[1].split("\n}\n", 1)[0]
        db = rb.index("! grep -qx 'dpkg installed' /var/lib/pantheon-rehearsal/prereq-verify-status.txt; then echo "
                      "'PRE-a rollback verify: dpkg-query could not read the dpkg database; refusing' >&2; exit 3; fi")
        self.assertLess(db, rb.index('PREREQ_TS="$(date -u +%Y%m%dT%H%M%SZ)"'))
        self.assertEqual(R.prea_rollback_verify("", ""), "archive", "an empty recorded list has nothing to verify")

    def test_rollback_with_an_empty_list_is_a_no_op(self) -> None:
        self.assertEqual(R.prea_rollback_decision("Remv everything [1]\n", ""), "skip")

    def test_mirror_treats_carriage_return_as_the_shell_does(self) -> None:
        # grep/sort/comm split on \n only: "debootstrap\r" is a different name from "debootstrap"
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1]\n", "debootstrap\r\n"), "refuse")
        self.assertEqual(R.prea_rollback_decision("Purg debootstrap [1]\r\n", "debootstrap\n"), "purge",
                         "cut -f2 stops at the space, so a trailing \\r on the Purg line is dropped")
        self.assertEqual(R._shell_lines("a\rb\nc"), ["a\rb", "c"])

    #: L3: the WHOLE guard block as a literal, so any drift (cmp X X, a changed comm argument) goes red
    _BLOCK_LITERAL = (
        "  # ROLLBACK PRE-a (begin): simulate the purge, refuse a removal not on the recorded list, purge the rest\n"
        "  # window: apt state could change between the purge simulation and the purge; it needs root, lasts "
        "seconds, and the rollback runs once under orchestrator's supervision\n"
        "  # simulate over the recorded names dpkg knows; the subset check below stays against the FULL list\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 xargs -a "
        "/var/lib/pantheon-rehearsal/prereq-new-packages.txt dpkg-query -W '-f=${Package}\\n' > "
        "/var/lib/pantheon-rehearsal/prereq-purge-known.txt || test $? = 123; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then : > "
        "/var/lib/pantheon-rehearsal/prereq-purge-simulation.txt; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s "
        "/var/lib/pantheon-rehearsal/prereq-purge-known.txt; then timeout 120 xargs -r -a "
        "/var/lib/pantheon-rehearsal/prereq-purge-known.txt apt-get -s purge > "
        "/var/lib/pantheon-rehearsal/prereq-purge-simulation.txt; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then grep -E '^(Purg|Remv) ' "
        "/var/lib/pantheon-rehearsal/prereq-purge-simulation.txt | cut -d' ' -f2 | sort -u > "
        "/var/lib/pantheon-rehearsal/prereq-purge-set.txt || true; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then sort -u "
        "/var/lib/pantheon-rehearsal/prereq-new-packages.txt > "
        "/var/lib/pantheon-rehearsal/prereq-new-packages.sorted.txt; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 comm -23 "
        "/var/lib/pantheon-rehearsal/prereq-purge-set.txt /var/lib/pantheon-rehearsal/prereq-new-packages.sorted.txt "
        "> /var/lib/pantheon-rehearsal/prereq-purge-extra.txt; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s "
        "/var/lib/pantheon-rehearsal/prereq-purge-extra.txt; then timeout 60 xargs -a "
        "/var/lib/pantheon-rehearsal/prereq-purge-extra.txt echo 'PRE-a rollback would remove packages not in the "
        "recorded list:' >&2; exit 3; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s "
        "/var/lib/pantheon-rehearsal/prereq-purge-set.txt; then timeout 900 xargs -r -a "
        "/var/lib/pantheon-rehearsal/prereq-purge-set.txt apt-get purge -y; fi\n"
        "  # verify BEFORE archiving: no recorded package may still be present (anything but not-installed); "
        "dpkg is the sentinel\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 xargs -a "
        "/var/lib/pantheon-rehearsal/prereq-new-packages.txt dpkg-query -W '-f=${Package} ${db:Status-Status}\\n' "
        "dpkg > /var/lib/pantheon-rehearsal/prereq-verify-status.txt || test $? = 123; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && ! grep -qx 'dpkg installed' "
        "/var/lib/pantheon-rehearsal/prereq-verify-status.txt; then echo 'PRE-a rollback verify: dpkg-query could "
        "not read the dpkg database; refusing' >&2; exit 3; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then grep -v '^dpkg ' "
        "/var/lib/pantheon-rehearsal/prereq-verify-status.txt | grep -v ' not-installed$' | cut -d' ' -f1 > "
        "/var/lib/pantheon-rehearsal/prereq-verify-left.txt || true; fi\n"
        "  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s "
        "/var/lib/pantheon-rehearsal/prereq-verify-left.txt; then timeout 60 xargs -a "
        "/var/lib/pantheon-rehearsal/prereq-verify-left.txt echo 'PRE-a rollback incomplete: still installed:' >&2; "
        "exit 3; fi\n"
        "  # ROLLBACK PRE-a (end)\n")

    def test_the_emitted_rollback_is_the_literal_guarded_block(self) -> None:
        text = emitted()
        self.assertEqual("\n".join(R.prea_rollback_lines()) + "\n", self._BLOCK_LITERAL)
        self.assertEqual(text.count(self._BLOCK_LITERAL), 1)
        rb = text.split("rollback_prerequisites() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn(self._BLOCK_LITERAL, rb + "\n")

    def test_header_documents_the_subset_rule_and_the_window(self) -> None:
        head = emitted().split("prerequisites() {", 1)[0]
        self.assertIn("NOT on the recorded list refuses (never a cascade)", head)
        self.assertIn("window: apt state could change between the purge simulation and the purge; it needs root, "
                      "lasts seconds, and the rollback runs once under orchestrator's supervision", head)

    def test_lint_refuses_a_purge_without_the_subset_guard(self) -> None:
        lines = R.prea_rollback_lines()
        self.assertTrue(any("PRE-a rollback" in p for p in self._lint_with(lines[9] + "\n", "")))
        self.assertTrue(any("PRE-a rollback" in p for p in self._lint_with(
            lines[8], lines[8].replace("comm -23 /var/lib/pantheon-rehearsal/prereq-purge-set.txt",
                                       "comm -13 /var/lib/pantheon-rehearsal/prereq-purge-set.txt"))))
        sim, purge = lines[5], lines[10]
        swapped = emitted().replace(sim, "@@SIM@@").replace(purge, sim).replace("@@SIM@@", purge)
        self.assertTrue(any("PRE-a rollback" in p for p in R.lint_script(swapped, RUN_ID)))
        extra = emitted().replace("  # ROLLBACK PRE-a (end)\n", "  # ROLLBACK PRE-a (end)\n" + purge + "\n")
        self.assertTrue(any("unknown package-removal form" in p for p in R.lint_script(extra, RUN_ID)),
                        "a pinned purge line copied outside the block is unknown")

    def test_lint_refuses_the_block_outside_rollback_prerequisites(self) -> None:
        text = emitted()
        block = self._BLOCK_LITERAL
        self.assertEqual(R.lint_script(text, RUN_ID), [])
        moved = text.replace(block, "").replace("prerequisites() {\n", "prerequisites() {\n" + block, 1)
        self.assertTrue(moved.index(block) < moved.index("rollback_prerequisites() {"))
        self.assertIn("the PRE-a rollback block must sit inside rollback_prerequisites()",
                      R.lint_script(moved, RUN_ID))

    def test_lint_classifies_every_other_install_or_upgrade_form_as_unknown(self) -> None:
        anchor = "  rm -f /var/cache/pantheon-rehearsal/requirements.lock\n"
        for form, msg in (("timeout 900 apt-get -y install systemd-", "unknown package-install form"),
                          ("timeout 900 apt-get -y install systemd-", "ending in - removes a package"),
                          ("timeout 900 apt install -y pkg-", "ending in - removes a package"),
                          ("timeout 900 apt install -y pkg", "unknown package-install form"),
                          ("timeout 900 apt-get dist-upgrade -y", "unknown package-install form"),
                          ("timeout 900 apt full-upgrade -y", "unknown package-install form"),
                          ("timeout 900 apt-get upgrade -y", "unknown package-install form"),
                          ("timeout 900 aptitude -y install pkg", "unknown package-install form"),
                          ("timeout 900 apt-get -o Dpkg::Options::=--force-confnew install pkg",
                           "unknown package-install form"),
                          ("timeout 9 true; timeout 900 /usr/bin/apt-get install -y pkg", "unknown package-install form"),
                          ("timeout 60 apt-get -s install systemd-", "ending in - removes a package")):
            with self.subTest(form=form, msg=msg):
                self.assertTrue(any(msg in p for p in self._lint_with(anchor, anchor + "  " + form + "\n")))
        for quoted in ('timeout 900 "apt-get" install x', "timeout 900 'apt' -y install x",
                       'timeout 900 "/usr/bin/apt-get" dist-upgrade -y'):
            with self.subTest(quoted=quoted):
                self.assertTrue(any("unknown package-install form" in p
                                    for p in self._lint_with(anchor, anchor + "  " + quoted + "\n")))
        for quoted in ('timeout 900 "apt-get" purge -y x', "timeout 900 'apt-get' remove x",
                       'timeout 900 apt-get "purge" x', 'timeout 900 "dpkg" -P x'):
            with self.subTest(quoted_removal=quoted):
                self.assertTrue(any("unknown package-removal form" in p
                                    for p in self._lint_with(anchor, anchor + "  " + quoted + "\n")))
        pinned = R.prea_install_line()
        self.assertTrue(any("ending in - removes a package" in p for p in self._lint_with(
            pinned, pinned.replace(" debootstrap  # PRE-a", " debootstrap systemd-  # PRE-a"))),
            "a trailing - refuses even on the pinned-line shape")
        self.assertTrue(any("unknown package-install form" in p for p in self._lint_with(
            pinned, pinned.replace("  # PRE-a", " extra-pkg  # PRE-a"))))
        for benign in ("timeout 60 apt-get -s install --no-install-recommends debootstrap",
                       "timeout 60 dpkg-query -W debootstrap", "timeout 60 apt-cache policy debootstrap"):
            with self.subTest(benign=benign):
                self.assertFalse(any("package-install" in p or "ending in -" in p
                                     for p in self._lint_with(anchor, anchor + "  " + benign + "\n")))
        self.assertEqual(R.lint_script(emitted(), RUN_ID), [])

    def test_lint_classifies_every_other_removal_form_as_unknown(self) -> None:
        anchor = "  rm -f /var/cache/pantheon-rehearsal/requirements.lock\n"
        for form in ("timeout 900 apt-get remove -y debootstrap", "timeout 900 apt-get autoremove -y",
                     "timeout 900 apt-get autopurge -y", "timeout 900 apt remove -y debootstrap",
                     "timeout 900 apt purge -y debootstrap", "timeout 900 aptitude purge -y debootstrap",
                     "timeout 900 dpkg --purge debootstrap", "timeout 900 dpkg --remove debootstrap",
                     "timeout 900 dpkg -r debootstrap", "timeout 900 dpkg -P debootstrap",
                     "timeout 900 dpkg -rP debootstrap", "timeout 900 apt-get purge -y debootstrap",
                     "timeout 900 xargs -r -a /var/lib/pantheon-rehearsal/prereq-new-packages.txt apt-get purge -y"):
            with self.subTest(form=form):
                self.assertTrue(any("unknown package-removal form" in p
                                    for p in self._lint_with(anchor, anchor + "  " + form + "\n")))
        for benign in ("timeout 60 dpkg-query -W debootstrap", "timeout 60 dpkg -l debootstrap",
                       "timeout 60 apt-get -s install --no-install-recommends debootstrap"):
            with self.subTest(benign=benign):
                self.assertFalse(any("unknown package-removal form" in p
                                     for p in self._lint_with(anchor, anchor + "  " + benign + "\n")))

    def test_lint_refuses_deleting_or_misplacing_the_prereq_record(self) -> None:
        self.assertTrue(any("rm target" in p for p in self._lint_with(
            "  test ! -e /var/lib/pantheon-rehearsal/prereq-verify-left.txt\n}",
            "  rm -f /var/lib/pantheon-rehearsal/prereq-prestate.txt\n}")))
        self.assertTrue(any("mv may only archive" in p for p in self._lint_with(
            '"/var/lib/pantheon-rehearsal/prereq-archive/$PREREQ_TS/"; fi', '/tmp/; fi')))
        self.assertTrue(any("command substitution" in p for p in self._lint_with(
            '"$(date -u +%Y%m%dT%H%M%SZ)"', '"$(date)"')))

    def test_trailing_newline_or_unsafe_args_are_refused(self) -> None:
        good = list(GOLDEN_ARGS)
        for i in range(3):
            for bad in (good[i] + "\n", good[i] + "\nx"):
                args = list(good)
                args[i] = bad
                with self.subTest(arg=i, value=bad), self.assertRaises(ValueError):
                    R.emit_commands(*args)
        self.assertIsNone(R.RUN_ID_RE.fullmatch("0123456789ab\n"))
        self.assertIsNone(R.COMMIT_RE.fullmatch("1" * 40 + "\n"))

    def test_no_host_write_into_the_running_machine_tree(self) -> None:
        lines = emitted().splitlines()
        boot = next(i for i, ln in enumerate(lines) if "/usr/bin/systemd-nspawn --quiet" in ln)
        md = "/var/lib/machines/pantheon-rehearsal-0123456789ab"
        for ln in lines[boot + 1:]:
            if ln.lstrip().startswith(("#", "timeout 60 machinectl poweroff", "timeout 300 machinectl remove", "test ! -e")):
                continue
            if "--machine=" in ln or "machinectl copy-to" in ln:
                continue
            self.assertNotIn(md, ln, ln)
        self.assertIn("  timeout 60 machinectl copy-to pantheon-rehearsal-0123456789ab "
                      "/var/lib/pantheon-rehearsal/0123456789ab/attestation.json "
                      "/etc/pantheon-rehearsal/attestation.json", lines)
        scan = next(ln for ln in lines if "token-scan-host.txt" in ln and "grep" in ln)
        self.assertIn("-f /var/lib/pantheon-rehearsal/0123456789ab/fixture-token ", scan)

    def test_staging_is_refused_with_a_symlink_or_special_entry_before_any_copy(self) -> None:
        lines = emitted().splitlines()
        chown = lines.index("  timeout 60 chown -R root:root /var/lib/pantheon-rehearsal/0123456789ab/inputs-staging")
        check = next(i for i, ln in enumerate(lines) if "find /var/lib/pantheon-rehearsal/0123456789ab/inputs-staging "
                     "! -type f ! -type d -print -quit | grep -q .; then" in ln)
        first_use = min(i for i, ln in enumerate(lines) if "inputs-staging/out/" in ln and "install -m" in ln)
        self.assertLess(check, first_use)
        self.assertEqual(chown, check - 1, "root takes ownership BEFORE the find (no find-to-install TOCTOU)")
        self.assertIn("exit 3", lines[check])

    def test_fixed_machine_id_that_differs_from_the_host(self) -> None:
        lines = emitted().splitlines()
        mid = R.fixture_machine_id(RUN_ID)
        self.assertRegex(mid, r"\A[0-9a-f]{32}\Z")
        i = lines.index(f"  printf '{mid}\\n' > /var/lib/machines/pantheon-rehearsal-0123456789ab/etc/machine-id")
        self.assertIn('!= "$HOST_MACHINE_ID_SHA256"', lines[i + 2])
        self.assertNotIn("  : > /var/lib/machines/pantheon-rehearsal-0123456789ab/etc/machine-id", lines)

    def test_boot_unit_collects_and_teardown_reports_a_leftover_machine(self) -> None:
        text = emitted()
        boot = next(ln for ln in text.splitlines() if "/usr/bin/systemd-nspawn --quiet" in ln)
        self.assertIn("systemd-run --unit=pantheon-rehearsal-0123456789ab --collect ", boot)
        td = text.split("teardown() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("machinectl remove pantheon-rehearsal-0123456789ab || echo 'LEFTOVER MACHINE", td)
        self.assertIn("test ! -e /var/lib/machines/pantheon-rehearsal-0123456789ab || { echo 'LEFTOVER MACHINE", td)

    def test_wheelhouse_is_outside_every_trusted_root(self) -> None:
        for path in (R.WHEELHOUSE_HOST, R.M_WHEELHOUSE):
            self.assertFalse(R.under_trusted_root(path), path)
            for st in R.state_directory_roots():
                self.assertFalse(path == st or path.startswith(st + "/"), (path, st))
        self.assertEqual(R.state_directory_roots(), ["/var/lib/pantheon-host-approval",
                                                     "/var/lib/pantheon-host-approval-telegram",
                                                     "/var/lib/pantheon-host-broker", "/var/lib/pantheon/health-signals"])
        inputs = json.loads(R.fixture_inputs()["inputs.json"])
        self.assertEqual(inputs["wheelhouse"], R.M_WHEELHOUSE)
        with mock.patch.object(R, "M_WHEELHOUSE", "/usr/local/lib/pantheon/wheelhouse"):
            self.assertTrue(any("trusted root" in p for p in R.lint_script(emitted(), RUN_ID)))
        with mock.patch.object(R, "WHEELHOUSE_HOST", "/var/lib/pantheon-host-broker/wh"):
            self.assertTrue(any("wheelhouse" in p for p in R.lint_script(emitted(), RUN_ID)))

    def test_privdrop_probe_is_the_amended_producer_prefix(self) -> None:
        self.assertEqual(R.PRODUCER_SETPRIV_PREFIX[-3:], ("--no-new-privs", "--inh-caps=-all", "--bounding-set=-all"))
        self.assertEqual(R.PRODUCER_SETPRIV_PREFIX[:4],
                         (H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups"))
        line = next(ln for ln in emitted().splitlines() if "probe-producer-privdrop.txt" in ln)
        self.assertIn(" ".join(R.PRIVDROP_PROBE_ARGV), line)
        idx = emitted().splitlines().index(line)
        self.assertIn("probe: producer privilege drop", emitted().splitlines()[idx - 2])

    def test_mdwe_probe_copies_every_ingress_sandbox_property(self) -> None:
        text = (H.REPO_ROOT / R.INGRESS_UNIT_REL).read_text()
        service = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
        keys = [ln.split("=", 1)[0] for ln in service.splitlines() if "=" in ln and not ln.startswith("#")]
        not_sandbox = {"Type", "BusName", "ExecStart", "Restart", "RestartSec", "StateDirectory", "StateDirectoryMode"}
        self.assertEqual(sorted(R.MDWE_PROBE_PROPERTIES), sorted(set(keys) - not_sandbox))
        line = next(ln for ln in emitted().splitlines() if "mdwe-probe.txt" in ln)
        for k, v in R.ingress_sandbox_properties():
            self.assertIn(f"--property={k}={v}" if " " not in v else f"'--property={k}={v}'", line)

    def test_health_settle_exceeds_the_driver_refresh(self) -> None:
        from tools.host_control.driver import HEALTH_REFRESH_SECONDS

        self.assertGreater(R.HEALTH_SETTLE_SECONDS, HEALTH_REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# 2. dark pins and the fixture inputs
# ---------------------------------------------------------------------------


class HostWatchTest(unittest.TestCase):
    """HIGH-1: the host-untouched manifest covers ONLY the installer's own targets, from the plan."""

    WRITE_KINDS_SKIPPED = R.WATCH_SKIP_KINDS

    def test_every_written_plan_path_is_under_a_watch_root(self) -> None:
        plan = _CHECKED.plan
        recursive, shallow = R.host_watch_spec(plan)
        for e in plan["entries"]:
            path = e.get("path")
            if not path or e["kind"] in self.WRITE_KINDS_SKIPPED and e["kind"] != "framework_file":
                continue
            covered = path in shallow or any(path == r or path.startswith(r + "/") for r in recursive)
            self.assertTrue(covered, (e["id"], path))
        for e in plan["entries"]:
            if e["kind"] == "unit_enable":
                self.assertIn(f"{R.WANTS_DIR}/{os.path.basename(e['path'])}", recursive)
        self.assertIn(R.M_RECEIPTS, recursive)

    def test_never_watches_live_fleet_state_recursively(self) -> None:
        recursive, shallow = R.host_watch_spec(_CHECKED.plan)
        for live in ("/var/lib/pantheon", "/var/lib/pantheon/home", "/etc/pantheon", "/usr/local/libexec",
                     "/etc/systemd/system", "/etc/dbus-1/system.d", "/usr/share/dbus-1/system-services",
                     "/home/post-office/mailbox", "/home", "/etc"):
            self.assertNotIn(live, recursive)
        self.assertIn("/var/lib/pantheon", shallow)
        self.assertIn("/etc/pantheon", shallow)
        for r in recursive:
            ok = (r.startswith(("/var/lib/pantheon/home/", "/var/lib/pantheon-host-", "/etc/pantheon/",
                                "/usr/local/lib/pantheon/", "/usr/local/libexec/pantheon-", "/run/pantheon-host-install",
                                "/etc/dbus-1/system.d/org.pantheon.", "/usr/share/dbus-1/system-services/org.pantheon.",
                                "/etc/systemd/system/pantheon-host-", f"{R.WANTS_DIR}/pantheon-host-"))
                  or r == R.M_RECEIPTS)
            self.assertTrue(ok, r)

    def test_every_dist_unit_is_wanted_by_multi_user(self) -> None:
        for unit in sorted((H.REPO_ROOT / R.DIST_REL).glob("pantheon-host-*.service")):
            text = unit.read_text()
            if "[Install]" in text:
                self.assertIn("WantedBy=multi-user.target", text.split("[Install]", 1)[1], unit.name)

    def test_tree_manifest_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a").mkdir()
            for i in range(5):
                (base / "a" / f"f{i}").write_bytes(b"x" * 10)
            with mock.patch.object(R, "TREE_MAX_ENTRIES", 3):
                with self.assertRaises(R.TreeBoundExceeded):
                    R.tree_manifest(["/a"], base=base)
            with mock.patch.object(R, "TREE_MAX_BYTES", 15):
                with self.assertRaises(R.TreeBoundExceeded):
                    R.tree_manifest(["/a"], base=base)
            with mock.patch.object(R, "TREE_MAX_SECONDS", -1):
                with self.assertRaises(R.TreeBoundExceeded):
                    R.tree_manifest(["/a"], base=base)
            self.assertEqual(len(R.tree_manifest(["/a"], base=base, shallow=["/a"])["entries"]), 1)

    def test_host_manifest_records_account_rows_and_refuses_a_bad_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "etc").mkdir()
            (base / "etc/passwd").write_text("root:x:0:0::/root:/bin/sh\npantheon-hostbroker:x:1901:1901::/h:/x\n")
            (base / "etc/group").write_text("root:x:0:\n")
            doc = R.host_manifest(_CHECKED.plan, base=base)
            self.assertEqual(doc["accounts"], {"passwd": ["pantheon-hostbroker:x:1901:1901::/h:/x"], "group": []})
            self.assertEqual(doc["plan_sha256"], _CHECKED.plan["plan_sha256"])
            bad = dict(_CHECKED.plan, schema="tampered")
            with self.assertRaises(ValueError):
                R.host_manifest(bad, base=base)


class DarkPinTest(unittest.TestCase):
    def test_harness_constructs_and_imports_no_actuator(self) -> None:
        src = Path(R.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported |= {f"{node.module}.{a.name}" for a in node.names}
        for banned in ("subprocess", "socket", "urllib", "http", "shutil", "pty", "multiprocessing", "dbus", "gi",
                       "tools.host_broker_rehearsal_live"):
            self.assertFalse(any(i == banned or i.startswith(banned + ".") for i in imported), banned)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("SubprocessCommandRunner", "FixtureCommandRunner", "FixtureHost", "HostSubstratePort",
                       "RootHelper", "system", "popen", "execv", "execve", "fork"):
            self.assertNotIn(banned, names)

    def test_dark_literals_stay_false(self) -> None:
        self.assertIs(H.LIVE_ACTUATION_ENABLED, False)
        self.assertIs(P.LIVE_ACTUATION_ENABLED, False)
        tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
        touched = [n for n in ast.walk(tree) if (isinstance(n, ast.Name) and n.id == "LIVE_ACTUATION_ENABLED")
                   or (isinstance(n, ast.Attribute) and n.attr == "LIVE_ACTUATION_ENABLED")]
        self.assertEqual(touched, [], "the harness must never read or write a dark literal")


class FixtureInputsTest(unittest.TestCase):
    def test_deterministic_and_parse_under_the_real_parsers(self) -> None:
        a, b = R.fixture_inputs(), R.fixture_inputs()
        self.assertEqual(a, b)
        self.assertEqual(tuple(sorted(a)), R.fixture_input_names())
        H.parse_manifest(a["manifest.json"])
        H.parse_registry(a["registry.json"])
        inputs = A.parse_inputs(a["inputs.json"])
        self.assertEqual(inputs.operator_uid, R.FIXTURE_OPERATOR_UID)
        self.assertEqual(json.loads(a["FIXTURE.json"])["fixture"], True)

    def test_values_are_fixture_shaped_never_tokenish(self) -> None:
        doc = json.loads(R.fixture_inputs()["inputs.json"])
        flat: list[str] = []

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            else:
                flat.append(str(v))

        walk(doc["operator_batch"])
        for value in flat:
            self.assertIsNone(H._TOKENISH.search(value), value)
        self.assertTrue(doc["operator_batch"]["1"]["token_source_path"].startswith(R.M_INPUTS + "/"))

    def test_guard_manifest_loads_under_every_host_substrate_rule_and_is_labelled(self) -> None:
        doc = R.guard_manifest()
        self.assertEqual(doc["note"], R.GUARD_MANIFEST_LABEL)
        F.load_manifest(doc)
        broken = copy.deepcopy(doc)
        broken["paths"][f"{R.M_GUARD_ROOT}/acl-leaf"]["acl"] = ["user::rwx", "group::r-x"]  # T-5510 base entries
        with self.assertRaises(F.ManifestError):
            F.load_manifest(broken)
        broken = copy.deepcopy(doc)
        broken["paths"][f"{R.M_GUARD_ROOT}/acl-leaf"]["acl"] = ["user::rwx", "group::rwx", "other::---"]  # T-5658
        with self.assertRaises(F.ManifestError):
            F.load_manifest(broken)

    def test_write_fixture_inputs_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "out")
            R.write_fixture_inputs(out, H.REPO_ROOT)
            self.assertEqual(sorted(os.listdir(out)), sorted(R.fixture_input_names()))
            with self.assertRaises(FileExistsError):
                R.write_fixture_inputs(out, H.REPO_ROOT)


# ---------------------------------------------------------------------------
# 3. the gate and carry-forward tables
# ---------------------------------------------------------------------------


class TablesTest(unittest.TestCase):
    def test_gate_table_is_exactly_org052_and_never_over_claims(self) -> None:
        g = R.GATE_TABLE
        sections = [set(g[k]) for k in ("evidenced", "partial", "not_evidenced_left_to_live_fire")]
        self.assertEqual(sum(len(s) for s in sections), len(set().union(*sections)), "sections overlap")
        self.assertEqual(set().union(*sections), set(R.org052_gate_ids()))
        self.assertEqual(len(R.org052_gate_ids()), 42)
        self.assertEqual(set(g["evidenced"]), {"E35", "E32a"})
        self.assertEqual(set(g["partial"]), {"E32", "E19", "E7", "E21"})
        self.assertEqual(set(g["not_evidenced_left_to_live_fire"]),
                         {f"E{n}" for n in range(1, 42) if n not in (7, 19, 21, 32, 35)})
        self.assertIn("FIXTURE", g["evidenced"]["E35"])
        self.assertIn("supervised fire", g["evidenced"]["E35"])
        self.assertEqual(set(g["carry_forward_partials"]), {"CF-5:ingress", "CF-3:acl+owner_mode"})
        self.assertIn("left to the supervised fire", g["carry_forward_partials"]["CF-5:ingress"])

    def test_cf_table_covers_cf1_to_cf12_and_names_real_checks(self) -> None:
        self.assertEqual(list(R.CF_TABLE), [f"CF-{n}" for n in range(1, 13)])
        receipt = _CHECKED.receipt
        ids = {c["id"] for c in receipt["checks"]}
        for _cf, (_title, cids) in R.CF_TABLE.items():
            self.assertTrue(cids)
            self.assertTrue(set(cids) <= ids, cids)
        self.assertTrue(set(R.GENERAL_CHECKS) <= ids)
        self.assertEqual(ids, set(R.GENERAL_CHECKS) | {c for _, cs in R.CF_TABLE.values() for c in cs})

    def test_every_check_has_a_failure_mode_test(self) -> None:
        ids = {c["id"] for c in _CHECKED.receipt["checks"]}
        self.assertEqual(ids, set(FAILURE_MODES))

    def test_phase_list_mirrors_the_runbook(self) -> None:
        ids = [p["id"] for p in R.PHASES]
        self.assertEqual([i for i in ids if i.startswith("P")], ["P0", "P1", "P2-P9", "P6", "P9", "P10", "P11", "P12"])
        plan_phases = {e["phase"] for e in _CHECKED.plan["entries"]}
        self.assertEqual(plan_phases, {"P0", "P2", "P4", "P5", "P6", "P7", "P8", "P9"})
        self.assertTrue(all(R.PHASES[ids.index("P2-P9")]["what"]))


# ---------------------------------------------------------------------------
# 4. the GOOD FIXTURE evidence (real U3a receipts; canned machine observations)
# ---------------------------------------------------------------------------


def _w(d: Path, name: str, data: str | bytes) -> None:
    (d / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _make_fixture_root(tmp: Path, inputs: dict[str, bytes]) -> Path:
    root = tmp / "root"
    root.mkdir()
    for rel in ("etc", "etc/systemd/system", "etc/dbus-1/system.d", "usr/share/dbus-1/system-services", "usr/local/lib",
                "usr/local/libexec", "var/lib", "run", "home/post-office/mailbox", "var/lib/pantheon-install-receipts",
                "var/lib/pantheon-rehearsal-inputs/wheelhouse", "srv/rehearsal-canary/F1", "srv/rehearsal-canary/F2",
                "srv/rehearsal-canary/F3"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for d in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        os.chmod(d, 0o755)
    (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n"
                                     "rehearsal-operator:x:2000:2000::/nonexistent:/usr/sbin/nologin\n"
                                     "rehearsal-requester:x:2001:2001::/home/rehearsal-requester:/usr/sbin/nologin\n"
                                     "rehearsal-approver:x:2002:2002::/home/rehearsal-approver:/usr/sbin/nologin\n")
    (root / "etc/group").write_text("root:x:0:\nrehearsal-operator:x:2000:\nrehearsal-requester:x:2001:\n"
                                    "rehearsal-approver:x:2002:\n")
    (root / "etc/shadow").write_text("root:*:20000:0:99999:7:::\n")
    inp = root / R.M_INPUTS.lstrip("/")
    for name, data in inputs.items():
        (inp / name).write_bytes(data)
    (inp / "bot-token").write_bytes(FIXTURE_TOKEN)
    os.chmod(inp / "bot-token", 0o600)
    (inp / "wheelhouse/fixture-0.0-py3-none-any.whl").write_bytes(TA.WHEEL)
    for marker in ("F1/marker", "F2/marker", "F3/target"):
        (root / "srv/rehearsal-canary" / marker).write_bytes(b"")
    meta = root / A.FIXTURE_META
    meta.mkdir()
    (meta / "facts.json").write_text(json.dumps({
        "dpkg": {"python3": "3.12.3-0", "python3-dbus": "1.3.2-5", "python3-gi": "3.48.2-1",
                 "python3-yaml": TA.PYYAML_OK}, "pyyaml": TA.PYYAML_OK}))
    return root


class _Checked:
    """Builds the good fixture evidence once (the real U3a code under a fixture root)."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="u4a-evidence-"))
        self.good = self.tmp / "evidence"
        with mock.patch.object(H, "_literal", TA._literal_allowlist_landed), \
                mock.patch.object(A, "lock_pinned_hashes", TA._pinned_plus_fixture_wheel):
            self._build()
        self.receipt = R.check_evidence(self.good)

    def _build(self) -> None:
        inputs = R.fixture_inputs()
        manifest_raw, registry_raw = inputs["manifest.json"], inputs["registry.json"]
        plan, _staging = H.build_plan(H.parse_manifest(manifest_raw), H.parse_registry(registry_raw), H.REPO_ROOT,
                                      closure=T.closure())
        self.plan = plan
        plan_raw = H.plan_bytes(plan)
        fw = plan["framework"]["root"]
        root = _make_fixture_root(self.tmp, inputs)
        ev = self.good
        ev.mkdir()
        rid, machine = RUN_ID, R.machine_name(RUN_ID)
        factory = TA.runner_factory()

        def run_apply(name: str) -> A.ApplyOutcome:
            return A.apply(plan_raw=plan_raw, manifest_raw=manifest_raw, registry_raw=registry_raw, tracked_raw=None,
                           inputs_raw=inputs["inputs.json"], root=root, receipt=f"{R.M_RECEIPTS}/{name}.json",
                           execute=True, runner_factory=factory, closure=T.closure(), now=lambda: NOW)

        def snap() -> str:
            return R.canonical(R.tree_manifest(R.MACHINE_SNAPSHOT_ROOTS, base=root)) + "\n"

        def status_line(outcome: A.ApplyOutcome, name: str) -> str:
            rc = outcome.receipt or {}
            return R.canonical({"status": rc.get("status"), "receipt": f"{R.M_RECEIPTS}/{name}.json",
                                "receipt_sha256": rc.get("receipt_sha256"), "failed_entry": rc.get("failed_entry")}) + "\n"

        # --- host side and P0 (canned shapes of the pinned argv)
        _w(ev, "run.json", R.canonical({"compensate_enabled": False, "evidence_class": R.EVIDENCE_CLASS, "fixture": True,
                                        "host_machine_id_sha256": HOST_MID, "live": False, "machine": machine,
                                        "nonce_sha256": NONCE, "repo_commit": "1" * 40, "run_id": rid,
                                        "schema": R.RUN_SCHEMA}) + "\n")
        _w(ev, "host-prestate.txt", "euid 0\nsystemd-container present\ndebootstrap present\nattestation-dir absent\n")
        empty_host = self.tmp / "host"
        empty_host.mkdir()
        host_tree = R.canonical(R.host_manifest(plan, base=empty_host)) + "\n"
        _w(ev, "host-plan.json", plan_raw)
        _w(ev, "host-before.json", host_tree)
        _w(ev, "host-after.json", host_tree)
        _w(ev, "host-canary.json", R.canonical({"verb": "guard-probe", "verdict": "refused", "reasons": [
            "/run/systemd/container: absent (not a systemd-nspawn machine)", "/proc/1/environ: no container=systemd-nspawn",
            "/etc/pantheon-rehearsal/attestation.json: absent", "euid is not 0"]}) + "\n")
        _w(ev, "host-canary.rc", "3\n")
        _w(ev, "boot-state.txt", "running\n")
        _w(ev, "P0-env.txt", "LC_ALL=C\nPATH=/usr/bin:/bin\n")
        _w(ev, "P0-mountinfo.txt",
           "100 99 0:50 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
           "101 100 0:51 / /proc rw,nosuid,nodev,noexec - proc proc rw\n"
           "102 100 0:52 / /sys ro,nosuid,nodev,noexec - tmpfs tmpfs rw,size=4096k,mode=555,uid=524288,gid=524288\n"
           "103 100 0:53 / /dev rw,nosuid - tmpfs tmpfs rw,mode=755\n"
           "104 100 0:54 / /run rw,nosuid,nodev - tmpfs tmpfs rw,mode=755,uid=524288,gid=524288\n"
           "105 100 8:1 /srv/rehearsal/repo /srv/rehearsal/repo ro,nosuid,nodev,idmapped - ext4 /dev/sda1 rw\n"
           "106 100 0:55 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw\n")
        for case, out in (("disabled", "disabled\n"), ("enabled", "enabled\n"), ("absent", "\n"),
                          ("static-refused", "static\n")):
            _w(ev, f"P0-systemctl-{case}.txt", out)
            _w(ev, f"P0-systemctl-{case}.rc", "0\n")
        _w(ev, "plan.json", plan_raw)
        att = {"host_machine_id_sha256": HOST_MID, "machine": machine, "nonce_sha256": NONCE,
               "plan_sha256": plan["plan_sha256"], "run_id": rid, "schema": R.ATTESTATION_SCHEMA}
        _w(ev, "attestation.json", R.canonical(att) + "\n")
        _w(ev, "machine-guard-probe.json", R.canonical({"verb": "guard-probe", "verdict": "attested", "machine": machine,
                                                        "nonce_sha256": NONCE, "run_id": rid,
                                                        "plan_sha256": plan["plan_sha256"]}) + "\n")

        # --- F1-F3 on the SAME fixture root, in the script's order (accounts stay sticky)
        host = A.FixtureHost(root)
        for case, spec in R.FAULT_CASES.items():
            plant = root / spec["plant"].lstrip("/")
            if case == "F1":
                if not host.exists("/etc/pantheon"):
                    host.mkdir("/etc/pantheon", "root", "root", "0755")
            if case == "F2":
                os.rmdir(plant)
            if case == "F3":
                ingress = R.SERVICE_ACCOUNTS["ingress"][0]
                if not host.exists("/etc/pantheon/host-approval-telegram"):
                    host.mkdir("/etc/pantheon/host-approval-telegram", "root", ingress, "0750")
                if not host.exists("/etc/pantheon/host-approval-telegram/credentials"):
                    host.mkdir("/etc/pantheon/host-approval-telegram/credentials", ingress, ingress, "0700")
            os.symlink(spec["target"], plant)
            _w(ev, f"{case}-S0.json", snap())
            outcome = run_apply(case)
            _w(ev, f"{case}-apply.out", status_line(outcome, case))
            _w(ev, f"{case}-apply.rc", f"{outcome.rc}\n")
            _w(ev, f"{case}-receipt.json", R.canonical(outcome.receipt) + "\n")
            _w(ev, f"{case}-S1.json", snap())
            _rc, out = A.rollback(receipt=f"{R.M_RECEIPTS}/{case}.json", root=root, runner_factory=factory,
                                  now=lambda: NOW)
            _w(ev, f"{case}-rollback.out", R.canonical({"actions": len(out["actions"]), "errors": out["errors"],
                                                        "residuals": len(out["residuals"])}) + "\n")
            _w(ev, f"{case}-S2.json", snap())
            passwd = (root / "etc/passwd").read_text().splitlines()
            _w(ev, f"{case}-accounts.txt", "".join(ln + "\n" for ln in passwd
                                                   if ln.split(":", 1)[0] in {n for n, _ in R.SERVICE_ACCOUNTS.values()}))
            os.unlink(plant)
            if case == "F2":
                os.mkdir(plant, 0o755)
        # --- the main apply, verify and the rerun
        main = run_apply("main")
        receipt = copy.deepcopy(main.receipt)
        receipt["mode"] = "rehearsal-nspawn"  # FIXTURE rewrite: only the U4b live path produces this mode
        receipt["receipt_sha256"] = A.receipt_digest(receipt)
        _w(ev, "apply.out", R.canonical({"status": "complete", "receipt": f"{R.M_RECEIPTS}/main.json",
                                         "receipt_sha256": receipt["receipt_sha256"], "failed_entry": None}) + "\n")
        _w(ev, "main-receipt.json", R.canonical(receipt) + "\n")
        rc, report = A.verify(plan_raw=plan_raw, root=root, receipt_raw=(root / R.M_RECEIPTS.lstrip("/") / "main.json")
                              .read_bytes(), runner_factory=factory)
        assert rc == 0, [r for r in report if not r["ok"]][:3]
        _w(ev, "verify.out", "".join(R.canonical(r) + "\n" for r in report))
        for state in ("/var/lib/pantheon-host-broker", "/var/lib/pantheon-host-approval"):  # systemd StateDirectory
            os.mkdir(root / state.lstrip("/"), 0o700)
        _w(ev, "rerun-S0.json", snap())
        rerun = run_apply("rerun")
        _w(ev, "rerun.out", status_line(rerun, "rerun"))
        _w(ev, "rerun-receipt.json", R.canonical(rerun.receipt) + "\n")
        _w(ev, "rerun-S1.json", snap())

        # --- P6 and P9 machine observations (canned shapes; the gate-32a digests are REAL)
        _w(ev, "probe-producer-privdrop.txt", "Name:\tcat\nUid:\t65534\t65534\t65534\t65534\nNoNewPrivs:\t1\n"
                                              "CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        for role, (_unit, _bus, exe) in R.DAEMONS.items():
            account, uid = R.SERVICE_ACCOUNTS[role]
            _w(ev, f"unit-{role}.txt", f"ActiveState=active\nMemoryDenyWriteExecute=yes\nUser={account}\n")
            _w(ev, f"busowner-{role}.txt", f"u {uid}\n")
            _w(ev, f"proc-{role}.txt", f"{4000 + uid} /usr/bin/python3 -I {exe}\n")
            _w(ev, f"cwd-{role}.txt", fw + "\n")
        _w(ev, "launcher-imports.json", R.canonical({"framework_root": fw, "files": [
            f"{fw}/tools/host_control/__init__.py", f"{fw}/tools/host_control/dbus_service.py",
            "/usr/lib/python3.12/json/__init__.py", "/usr/lib/python3/dist-packages/dbus/__init__.py"]}) + "\n")
        from tools.host_control.runtime_table import load_checkout_op_registry, load_daemon_runtime

        installed = root / fw.lstrip("/")
        runtime = load_daemon_runtime(host_operation_families=["persona.install", "persona.verify"],
                                      registry_loader=lambda: load_checkout_op_registry(installed), geteuid=lambda: 1901)
        _w(ev, "broker-journal.txt", f"pantheon-host-broker: starting\nstartup-record {runtime.startup_record()}\n")
        _w(ev, "gate32a-root.json", R.canonical({"framework_root": fw,
                                                 "root_digest": load_checkout_op_registry(installed).digest}) + "\n")
        _w(ev, "ingress-start.rc", "1\n")
        _w(ev, "ingress-journal.txt", "Starting pantheon-host-approval-telegram-ingress.service...\n"
                                      f"{R.INGRESS_GETME_FAILED}\n")
        _w(ev, "unit-ingress.txt", f"MemoryDenyWriteExecute=yes\nUser={R.SERVICE_ACCOUNTS['ingress'][0]}\n")
        _w(ev, "ingress-owner.rc", "1\n")
        _w(ev, "mdwe-probe.txt", "imports_ok\ncallback_fired\n")
        act = next(r for r in receipt["entries"] if r.get("kind") == "activation" and r["path"] == R.ACTIVATION_FILE)
        _w(ev, "activation-sha-before.txt", f"{act['after_sha256']}  {R.ACTIVATION_FILE}\n")
        _w(ev, "activation-sha-restored.txt", f"{act['after_sha256']}  {R.ACTIVATION_FILE}\n")
        _w(ev, "health-degraded.txt", 's "degraded"\n')
        _w(ev, "health-ready.txt", 's "ready"\n')
        _w(ev, "start-service.txt", "u 1\n")
        _w(ev, "start-service.rc", "0\n")
        _w(ev, "executor-owner.txt", f"u {R.SEAT_UID}\n")
        _w(ev, "journal-all.txt", "systemd[1]: Started pantheon-host-broker.service.\n")
        _w(ev, "token-scan-machine.txt", "".join(f"{d}/x:0\n" for d in R.TOKEN_SCAN_DIRS))
        _w(ev, "token-scan-machine.rc", "1\n")
        _w(ev, "token-scan-host.txt", "")
        _w(ev, "token-scan-host.rc", "1\n")

    def copy(self) -> Path:
        dest = Path(tempfile.mkdtemp(prefix="u4a-mut-", dir=self.tmp))
        shutil.rmtree(dest)
        shutil.copytree(self.good, dest)
        return dest


_CHECKED: _Checked = None  # type: ignore[assignment]


def setUpModule() -> None:
    global _CHECKED
    _CHECKED = _Checked()


def tearDownModule() -> None:
    shutil.rmtree(_CHECKED.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. the evidence checker: green on the good fixture; one failure per check id
# ---------------------------------------------------------------------------


def _edit_json(name: str, fn: Callable[[dict], None], *, redigest: bool = False) -> Callable[[Path], None]:
    def mutate(d: Path) -> None:
        doc = json.loads((d / name).read_text())
        fn(doc)
        if redigest:
            doc["receipt_sha256"] = A.receipt_digest(doc)
        (d / name).write_text(R.canonical(doc) + "\n")
    return mutate


def _write(name: str, text: str) -> Callable[[Path], None]:
    return lambda d: (d / name).write_text(text)


def _replace(name: str, old: str, new: str) -> Callable[[Path], None]:
    def mutate(d: Path) -> None:
        text = (d / name).read_text()
        assert old in text, (name, old)
        (d / name).write_text(text.replace(old, new))
    return mutate


def _entry_edit(kind: str, fn: Callable[[dict], None]) -> Callable[[dict], None]:
    def edit(doc: dict) -> None:
        for r in doc["entries"]:
            if r.get("kind") == kind and r.get("action") in ("created", "replaced"):
                fn(r)
                return
        raise AssertionError(kind)
    return edit


def _snap_add(name: str, path: str) -> Callable[[dict], None]:
    def edit(doc: dict) -> None:
        doc["entries"].append({"path": path, "type": "file", "uid": 0, "gid": 0, "mode": "0644", "sha256": "0" * 64})
    return edit


def _canary_mutated(doc: dict) -> None:
    for e in doc["entries"]:
        if e["path"] == "/srv/rehearsal-canary/F1/marker":
            e["sha256"] = "f" * 64


def _set(key, value):
    return lambda doc: doc.__setitem__(key, value)


FAILURE_MODES: dict[str, list[Callable[[Path], None]]] = {
    "C-CLOSED-SET": [_write("extra.txt", "x"), lambda d: (d / "mdwe-probe.txt").unlink()],
    "C-RUN": [_edit_json("run.json", _set("live", True)), _edit_json("run.json", _set("evidence_class", "live"))],
    "C-PREREQ-STATE": [_write("host-prestate.txt", "euid 0\nattestation-dir absent\n")],
    "C-HOST-CANARY": [_write("host-canary.rc", "0\n"),
                      _edit_json("host-canary.json", _set("reasons", ["euid is not 0"])),
                      _edit_json("host-canary.json", _set("constructed", {"host": True})),
                      _edit_json("host-canary.json", _set("verdict", "attested")),
                      _edit_json("host-canary.json", _set("reasons", []))],
    "C-HOST-UNTOUCHED": [_edit_json("host-after.json", _snap_add("x", "/etc/pantheon/host-broker/x")),
                         _edit_json("host-after.json", lambda doc: doc["accounts"]["passwd"].append(
                             "pantheon-hostbroker:x:1901:1901::/h:/x")),
                         _edit_json("host-before.json", lambda doc: doc["recursive"].append("/var/lib/pantheon")),
                         _edit_json("host-plan.json", _set("schema", "tampered"))],
    "C-BOOT": [_write("boot-state.txt", "starting\n")],
    "C-MOUNTS": [_replace("P0-mountinfo.txt", "/srv/rehearsal/repo ro,", "/srv/rehearsal/repo rw,"),
                 lambda d: (d / "P0-mountinfo.txt").write_text(
                     (d / "P0-mountinfo.txt").read_text() + "107 100 8:1 /x /var/lib/pantheon-rehearsal-inputs rw - ext4 /dev/sda1 rw\n"),
                 lambda d: (d / "P0-mountinfo.txt").write_text(
                     (d / "P0-mountinfo.txt").read_text() + "108 104 0:60 / /run/lock rw,nosuid - tmpfs tmpfs rw\n")],
    "C-PLAN": [_edit_json("plan.json", _set("schema", "tampered"))],
    "C-ATTESTATION": [_edit_json("attestation.json", _set("extra", "x")),
                      _edit_json("attestation.json", _set("schema", "other/v1")),
                      _edit_json("attestation.json", _set("plan_sha256", "0" * 64)),
                      _edit_json("machine-guard-probe.json", _set("verdict", "refused"))],
    "C-APPLY": [_edit_json("main-receipt.json", _set("mode", "fixture"), redigest=True),
                _edit_json("main-receipt.json", _set("status", "partial"))],
    "C-ENV": [_write("P0-env.txt", "LC_ALL=C\nPATH=/usr/bin:/bin\nLANG=en_US.UTF-8\n"),
              _edit_json("main-receipt.json", _entry_edit("runtime", lambda r: r["observations"].__setitem__(
                  "env", ["PATH=/usr/bin:/bin"])), redigest=True)],
    "C-SYSTEMCTL": [_write("P0-systemctl-enabled.txt", "disabled\n"),
                    _write("P0-systemctl-static-refused.txt", "disabled\n")],
    "C-FAULT-F1": [_edit_json("F1-S1.json", _canary_mutated)],
    "C-FAULT-F2": [_edit_json("F2-receipt.json", lambda doc: doc["failed_entry"].__setitem__("id", "runtime:host"),
                              redigest=True)],
    "C-FAULT-F3": [_write("F3-apply.rc", "0\n")],
    "C-ROLLBACK": [_edit_json("F2-S2.json", _snap_add("x", "/etc/dbus-1/system.d/leftover.conf")),
                   _replace("F1-accounts.txt", "pantheon-hostmonitor", "someone-else")],
    "C-CWD": [_edit_json("main-receipt.json", _entry_edit("runtime", lambda r: r["observations"].__setitem__(
        "cwd", "/")), redigest=True)],
    "C-PRIVDROP": [_replace("probe-producer-privdrop.txt", "CapBnd:\t0000000000000000", "CapBnd:\t000001ffffffffff"),
                   _replace("probe-producer-privdrop.txt", "NoNewPrivs:\t1", "NoNewPrivs:\t0")],
    "C-DAEMONS": [_replace("unit-broker.txt", "MemoryDenyWriteExecute=yes", "MemoryDenyWriteExecute=no"),
                  _write("busowner-approval.txt", "u 0\n")],
    "C-INGRESS": [_write("ingress-journal.txt", "telegram-ingress event=api_error detail=wrong_uid\n"),
                  _write("ingress-owner.rc", "0\n")],
    "C-MDWE-PROBE": [_write("mdwe-probe.txt", "imports_ok\ncallback_missing\n")],
    "C-LAUNCHER": [_edit_json("launcher-imports.json", lambda doc: doc["files"].append("/home/x/tools/evil.py")),
                   _write("proc-broker.txt", "1 /usr/bin/python3 /usr/local/libexec/pantheon-host-broker\n")],
    "C-GATE32A": [_edit_json("gate32a-root.json", _set("root_digest", "0" * 64))],
    "C-HEALTH": [_write("activation-sha-restored.txt", "0" * 64 + "  x\n"), _write("health-degraded.txt", 's "ready"\n')],
    "C-VERIFY": [_replace("verify.out", '"ok":true', '"ok":false')],
    "C-RERUN": [_edit_json("rerun-receipt.json", _set("status", "complete"), redigest=True),
                _edit_json("rerun-S1.json", _snap_add("x", "/etc/pantheon/new"))],
    "C-Q10-BACKUPS": [_edit_json("rerun-receipt.json", _set("backups", []), redigest=True)],
    "C-TOKEN-SCAN": [_write("token-scan-machine.txt", "/etc/pantheon/x:1\n"), _write("token-scan-host.rc", "0\n")],
    "C-COMPENSATE": [_edit_json("run.json", _set("compensate_enabled", True))],
    "C-LABEL": [lambda d: None],  # needs a table patch; see test_label_check_fails_on_an_over_claim
}


class CheckEvidenceTest(unittest.TestCase):
    def test_good_fixture_is_green(self) -> None:
        r = _CHECKED.receipt
        self.assertEqual(r["failures"], [])
        self.assertTrue(r["green"])
        self.assertEqual((r["evidence_class"], r["live"], r["fixture_inputs"]), (R.EVIDENCE_CLASS, False, True))
        status = {c["id"]: c["status"] for c in r["carry_forwards"]}
        self.assertEqual(status, {**{f"CF-{n}": "evidenced" for n in range(1, 13)}, "CF-3": "partial", "CF-5": "partial"})
        cf7 = next(c for c in r["carry_forwards"] if c["id"] == "CF-7")
        self.assertEqual(cf7["activation"], "activated")
        self.assertEqual(sorted(r["evidence_files"]), sorted(R.EVIDENCE_FILES))
        self.assertEqual([p["id"] for p in r["prerequisites"]], ["PRE-a", "PRE-b", "PRE-c"])

    def test_receipt_is_deterministic_and_self_digested(self) -> None:
        again = R.check_evidence(_CHECKED.good)
        self.assertEqual(again, _CHECKED.receipt)
        body = {k: v for k, v in again.items() if k != "receipt_sha256"}
        self.assertEqual(again["receipt_sha256"], hashlib.sha256(R.canonical(body).encode()).hexdigest())

    def test_the_fault_receipts_are_real_partial_failures(self) -> None:
        for case, spec in R.FAULT_CASES.items():
            rc = json.loads((_CHECKED.good / f"{case}-receipt.json").read_text())
            self.assertEqual(rc["failed_entry"]["id"], spec["failed_entry"], rc["failed_entry"])

    def test_each_failure_mode_fails_its_check(self) -> None:
        for cid, mutations in FAILURE_MODES.items():
            if cid == "C-LABEL":
                continue
            for i, mutate in enumerate(mutations):
                with self.subTest(check=cid, mode=i):
                    d = _CHECKED.copy()
                    mutate(d)
                    r = R.check_evidence(d)
                    row = next(c for c in r["checks"] if c["id"] == cid)
                    self.assertFalse(row["ok"], f"{cid} mode {i} did not fail")
                    self.assertFalse(r["green"])
                    shutil.rmtree(d)

    def test_blocking_preconditions_name_the_real_condition(self) -> None:
        d = _CHECKED.copy()
        doc = json.loads((d / "plan.json").read_text())
        doc["blocking_preconditions"] = ["mcp-allowlist"]
        body = {k: v for k, v in doc.items() if k != "plan_sha256"}
        doc["plan_sha256"] = H.sha256_bytes(H.canonical(body).encode("ascii"))
        self.assertTrue(H.verify_plan_digest(doc))
        (d / "plan.json").write_text(R.canonical(doc) + "\n")
        r = R.check_evidence(d)
        shutil.rmtree(d)
        self.assertFalse(next(c for c in r["checks"] if c["id"] == "C-PLAN")["ok"])
        self.assertTrue(any("the real plan has non-empty blocking_preconditions ['mcp-allowlist']" in f
                            for f in r["failures"]), r["failures"])
        self.assertFalse(any("MCP allowlist edit" in f for f in r["failures"]))

    def test_a_fifo_or_an_oversized_file_is_refused_without_hanging(self) -> None:
        d = _CHECKED.copy()
        (d / "boot-state.txt").unlink()
        os.mkfifo(d / "boot-state.txt")
        r = R.check_evidence(d)
        self.assertFalse(next(c for c in r["checks"] if c["id"] == "C-BOOT")["ok"])
        shutil.rmtree(d)
        with mock.patch.object(R, "MAX_EVIDENCE_BYTES", 100):
            r = R.check_evidence(_CHECKED.good)
        self.assertFalse(r["green"])
        self.assertTrue(any("larger than 100 bytes" in f for f in r["failures"]))

    def test_gates_are_evidenced_only_on_a_green_run(self) -> None:
        self.assertEqual(set(_CHECKED.receipt["gates"]["evidenced"]), {"E35", "E32a"})
        self.assertNotIn("withheld_run_not_green", _CHECKED.receipt["gates"])
        d = _CHECKED.copy()
        (d / "boot-state.txt").write_text("x\n")
        r = R.check_evidence(d)
        self.assertEqual(r["gates"]["evidenced"], {})
        self.assertEqual(set(r["gates"]["withheld_run_not_green"]), {"E35", "E32a"})
        shutil.rmtree(d)

    def test_rollback_residual_is_only_the_sticky_homes(self) -> None:
        self.assertEqual(R.ROLLBACK_RESIDUAL_DIRS, ("/var/lib/pantheon/home",))
        d = _CHECKED.copy()
        _edit_json("F1-S2.json", _snap_add("x", "/var/lib/pantheon/stray"))(d)
        self.assertFalse(next(c for c in R.check_evidence(d)["checks"] if c["id"] == "C-ROLLBACK")["ok"])
        shutil.rmtree(d)

    def test_label_check_fails_on_an_over_claim(self) -> None:
        table = copy.deepcopy(R.GATE_TABLE)
        table["evidenced"]["E37"] = table["not_evidenced_left_to_live_fire"].pop("E37")
        with mock.patch.object(R, "GATE_TABLE", table):
            r = R.check_evidence(_CHECKED.good)
        self.assertFalse(next(c for c in r["checks"] if c["id"] == "C-LABEL")["ok"])

    def test_b7_denied_activation_is_partial_not_a_failure(self) -> None:
        d = _CHECKED.copy()
        (d / "start-service.rc").write_text("1\n")
        (d / "executor-owner.txt").write_text("")
        r = R.check_evidence(d)
        self.assertTrue(r["green"], r["failures"])
        cf7 = next(c for c in r["carry_forwards"] if c["id"] == "CF-7")
        self.assertEqual((cf7["status"], cf7["activation"]), ("partial", "activatable-only"))
        shutil.rmtree(d)

    def test_activated_by_the_wrong_uid_fails(self) -> None:
        d = _CHECKED.copy()
        (d / "executor-owner.txt").write_text("u 0\n")
        self.assertFalse(R.check_evidence(d)["green"])
        shutil.rmtree(d)

    def test_cli_rc(self) -> None:
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(R.main(["--check-evidence", str(_CHECKED.good)]), 0)
        self.assertEqual(json.loads(buf.getvalue())["receipt_sha256"], _CHECKED.receipt["receipt_sha256"])
        d = _CHECKED.copy()
        (d / "boot-state.txt").write_text("x\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(["--check-evidence", str(d)]), 1)
        shutil.rmtree(d)
        self.assertEqual(R.main(["--check-evidence", str(_CHECKED.tmp / "missing")]), 2)

    def test_plan_digest_cli(self) -> None:
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(R.main(["--plan-digest", str(_CHECKED.good / "plan.json")]), 0)
        self.assertEqual(buf.getvalue().strip(), _CHECKED.plan["plan_sha256"])
        d = _CHECKED.copy()
        _edit_json("plan.json", _set("schema", "x"))(d)
        self.assertEqual(R.main(["--plan-digest", str(d / "plan.json")]), 2)
        shutil.rmtree(d)


GOOD_MOUNTS = (
    "100 99 0:50 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "101 100 0:51 / /proc rw,nosuid,nodev,noexec - proc proc rw\n"
    "102 100 0:52 / /sys ro,nosuid,nodev,noexec - tmpfs tmpfs rw,size=4096k,mode=555,uid=524288,gid=524288\n"
    "103 100 8:1 /srv/rehearsal/repo /srv/rehearsal/repo ro,nosuid,nodev,idmapped - ext4 /dev/sda1 rw\n"
    "104 102 0:60 / /sys/fs/pstore ro,nosuid - pstore pstore ro\n"
    "105 100 0:61 / /dev/console rw,nosuid - devpts devpts rw\n"
    "106 100 0:62 / /run rw,nosuid,nodev - tmpfs tmpfs rw,mode=755,uid=524288,gid=524288\n")


#: Recovery D15 W3 (T-5703): rehearsal 1's REAL P0 capture (run 1990856d106c), byte-for-byte
REAL_P0 = (Path(__file__).resolve().parent / "fixtures" / "host_broker_rehearsal" / "1990856d106c"
           / "P0-mountinfo.txt").read_text(encoding="utf-8")


def _real_line(mount_id: int) -> str:
    (line,) = [ln for ln in REAL_P0.splitlines() if ln.startswith(f"{mount_id} ")]
    return line


class RuledMountsW3HostTest(unittest.TestCase):
    """Recovery D15 W3 on the host checker (C-MOUNTS): the same exact table as U4b."""

    def _swap(self, mount_id: int, old: str, new: str) -> tuple[str, str]:
        line = _real_line(mount_id)
        self.assertIn(old, line)
        bad = line.replace(old, new, 1)
        return REAL_P0.replace(line + "\n", bad + "\n"), bad

    def test_the_real_p0_passes(self) -> None:
        self.assertEqual(R.mount_refusals(REAL_P0), [])

    def test_each_ruled_mount_refuses_one_changed_attribute(self) -> None:
        for mid, old, new in (
                (178, "uid=1708523520", "uid=1708523521"),           # 1: uid != the machine /run's
                (178, "size=4096k", "size=8192k"),
                (188, "188 187 0:74", "188 187 0:99"),               # 2: not a bind of /run
                (188, "/run/host ro,", "/run/host rw,"),
                (202, "202 196 0:78", "202 196 0:99"),               # 3: not the machine /proc
                (174, "ida6c5390f447ec4de//deleted", "ida6c5390f447ec4de"),  # 4: non-matching deleted root
                (172, "nodev,noexec shared", "nodev shared"),
                (191, "/proc/kmsg rw,", "/proc/kmsg ro,"),           # 5
                (192, "192 187 0:76", "192 187 0:74"),               # 6: /run/lock sharing /run's source
                (192, "size=5120k", "size=6144k")):
            with self.subTest(line=mid, change=new):
                text, bad = self._swap(mid, old, new)
                self.assertEqual(R.mount_refusals(text), [R.UNLISTED_MOUNT + bad])

    def test_f2_any_fstype_at_a_ruled_mountpoint_must_match_that_mountpoints_entries(self) -> None:
        """Coordinator F2 ruling (fail-closed tightening): keyed on the mountpoint, not (mountpoint, fstype)."""

        for line in ("990 195 253:1 /x /proc/kmsg rw,nosuid,nodev - ext4 /dev/vda1 rw",
                     "990 176 0:79 / /sys ro,nosuid,nodev,noexec - sysfs sysfs ro",
                     "990 196 0:74 / /proc/sys/net ro,nosuid,nodev,noexec - tmpfs tmpfs rw,uid=1708523520,gid=1708523520"):
            with self.subTest(line=line):
                self.assertEqual(R.mount_refusals(REAL_P0 + line + "\n"), [R.UNLISTED_MOUNT + line])
        self.assertNotIn("/sys", dict(R.MOUNT_VIRTUAL_EXACT))

    def test_a_third_proc_sys_net_line_refuses(self) -> None:
        third = "999 195 0:78 /sys/net /proc/sys/net rw,nosuid,nodev,noexec,relatime shared:493 - proc proc rw"
        self.assertEqual(R.mount_refusals(REAL_P0 + third + "\n"), [R.UNLISTED_MOUNT + third])

    def test_proc_sys_net_refuses_once_the_pinned_argv_drops_private_network(self) -> None:
        real_argv = R.nspawn_argv

        def dropped(run_id: str) -> list[str]:
            return [a for a in real_argv(run_id) if a != "--private-network"]

        with mock.patch.object(R, "nspawn_argv", dropped):
            self.assertEqual(R.mount_refusals(REAL_P0),
                             [R.UNLISTED_MOUNT + _real_line(202), R.UNLISTED_MOUNT + _real_line(203)])

    def test_the_host_userns_check_follows_the_machine_run_superblock(self) -> None:
        run = _real_line(187)
        text = REAL_P0.replace(run + "\n", run.replace("uid=1708523520,", "uid=1708523599,") + "\n")
        refused = {r[len(R.UNLISTED_MOUNT):].split()[0] for r in R.mount_refusals(text)}
        self.assertEqual(refused, {"178", "188", "172", "174", "191", "192"})


class MountAllowlistTest(unittest.TestCase):
    """Recovery D15 standing rule: a fixed allowlist, no widening knob, and an explicit refusal line."""

    def test_good_snapshot_passes(self) -> None:
        self.assertEqual(R.mount_refusals(GOOD_MOUNTS), [])
        self.assertEqual(R.mount_refusals((_CHECKED.good / "P0-mountinfo.txt").read_text()), [])

    def test_unlisted_mount_refusal_quotes_the_captured_line(self) -> None:
        for line in ("200 104 0:70 / /run/lock rw,nosuid,nodev - tmpfs tmpfs rw",
                     "201 102 0:71 / /sys/fs/bpf rw,nosuid - bpf bpf rw",
                     "202 100 8:1 /home/orchestrator /var/lib/pantheon-rehearsal-inputs rw - ext4 /dev/sda1 rw",
                     "203 100 8:1 /x /srv/extra ro - ext4 /dev/sda1 rw"):
            with self.subTest(line=line):
                got = R.mount_refusals(GOOD_MOUNTS + line + "\n")
                self.assertEqual(len(got), 1, got)
                self.assertTrue(got[0].startswith("attestation refused: unlisted mount " + line), got)
        pstore_rw = "204 102 0:72 / /sys/fs/pstore rw,nosuid - pstore pstore rw"
        self.assertEqual(R.mount_refusals(GOOD_MOUNTS.replace(
            "104 102 0:60 / /sys/fs/pstore ro,nosuid - pstore pstore ro\n", pstore_rw + "\n")),
            [f"attestation refused: unlisted mount {pstore_rw}  (a mask must be read-only)"])

    def test_repo_bind_must_be_exactly_one_and_read_only(self) -> None:
        rw = GOOD_MOUNTS.replace("/srv/rehearsal/repo ro,", "/srv/rehearsal/repo rw,")
        self.assertTrue(R.mount_refusals(rw)[0].startswith("attestation refused: unlisted mount 103 "))
        two = GOOD_MOUNTS + "300 100 8:1 /y /srv/rehearsal/repo ro - ext4 /dev/sda1 rw\n"
        self.assertIn("attestation refused: 2 mounts at /srv/rehearsal/repo (exactly one read-only bind)",
                      R.mount_refusals(two))
        self.assertIn("attestation refused: mountinfo is truncated (no final newline)",
                      R.mount_refusals(GOOD_MOUNTS[:-1]))

    def test_the_checker_reports_the_line_in_its_receipt(self) -> None:
        d = _CHECKED.copy()
        line = "200 104 0:70 / /run/lock rw,nosuid,nodev - tmpfs tmpfs rw"
        (d / "P0-mountinfo.txt").write_text((d / "P0-mountinfo.txt").read_text() + line + "\n")
        r = R.check_evidence(d)
        self.assertIn(f"C-MOUNTS: attestation refused: unlisted mount {line}", r["failures"])
        shutil.rmtree(d)

    def test_there_is_no_knob_that_widens_the_allowlist(self) -> None:
        import inspect

        self.assertEqual(list(inspect.signature(R.mount_refusals).parameters), ["text"])
        self.assertEqual(list(inspect.signature(R.check_evidence).parameters), ["root", "repo_root"])
        for const in (R.MOUNT_VIRTUAL_EXACT, R.MOUNT_VIRTUAL_PREFIX, R.MOUNT_RO_MASK_PREFIXES, R.MOUNT_CONSOLE,
                      R.MOUNT_RULED, R.MOUNT_RULED_ANCHORS, R.MOUNT_RULED_FLAG_SET):
            self.assertIsInstance(const, tuple)
        tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
        flags = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "add_argument" and n.args and isinstance(n.args[0], ast.Constant)]
        self.assertEqual(sorted(flags), sorted(["--emit-commands", "--check-evidence", "--fixture-inputs",
                                                "--tree-manifest", "--plan-digest", "--host-manifest", "--plan",
                                                "--run-id", "--repo-commit",
                                                "--repo", "--root"]))
        self.assertFalse([f for f in flags if "mount" in f or "allow" in f])
        self.assertNotIn("environ", {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})

    def test_parity_with_u4b_mount_factor(self) -> None:
        live = H.REPO_ROOT / R.LIVE_ENTRYPOINT_REL
        if not live.exists():
            self.skipTest("U4b (tools/host_broker_rehearsal_live.py) is not on this branch yet")
        consts = {}
        for node in ast.parse(live.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                consts[node.target.id] = node.value
        def lit(name):
            return ast.literal_eval(ast.unparse(consts[name]).replace("frozenset(", "("))
        exact = {k: set(v) for k, v in R.MOUNT_VIRTUAL_EXACT}
        self.assertEqual(lit("VIRTUAL_EXACT"), exact)
        self.assertEqual([(p, set(f), r) for p, f, r in lit("VIRTUAL_PREFIX")],
                         [(p, set(f), r) for p, f, r in R.MOUNT_VIRTUAL_PREFIX])
        self.assertEqual(tuple(lit("RO_MASK_PREFIXES")), R.MOUNT_RO_MASK_PREFIXES)
        self.assertEqual(tuple(lit("CONSOLE")), R.MOUNT_CONSOLE)
        self.assertEqual(set(lit("ATTESTATION_KEYS")), set(R.ATTESTATION_KEYS))
        self.assertEqual(lit("ATTESTATION_SCHEMA"), R.ATTESTATION_SCHEMA)
        self.assertEqual(lit("REPO_BIND"), R.REPO_STAGE)
        self.assertEqual(lit("REPO_BIND"), R.LIVE_CWD)
        self.assertEqual(lit("RECEIPT_DIR"), R.M_RECEIPTS)
        self.assertEqual(set(lit("VERBS")), set(R.LIVE_VERBS))
        self.assertEqual(lit("REQUIRED_ENV"), dict(x.split("=", 1) for x in R.C_ENV_LINES))
        self.assertEqual(lit("NONCE_FILE"), R.ATTESTATION_DIR + "/nonce")
        self.assertEqual(lit("ATTESTATION_FILE"), R.ATTESTATION_DIR + "/attestation.json")
        self.assertTrue(R.CONTAINER_REFUSAL_PREFIX.startswith(lit("CONTAINER_FILE") + ": "))
        # U4b final (48915a86d): /dev is tmpfs ONLY (devtmpfs dropped) on both sides
        self.assertEqual(lit("VIRTUAL_EXACT")["/dev"], {"tmpfs"})
        self.assertEqual(dict(R.MOUNT_VIRTUAL_EXACT)["/dev"], frozenset({"tmpfs"}))
        self.assertNotIn("devtmpfs", set().union(*(set(v) for _k, v in R.MOUNT_VIRTUAL_EXACT)))
        # Recovery's exact refusal text
        self.assertEqual(lit("UNLISTED_MOUNT"), R.UNLISTED_MOUNT)
        # Recovery D15 W3 (2026-11-01, T-5703): the ruled nspawn 255 table, its anchors and flag set
        self.assertEqual(tuple(lit("RULED_MOUNTS")), R.MOUNT_RULED)
        self.assertEqual(lit("RULED_ANCHORS"), dict(R.MOUNT_RULED_ANCHORS))
        self.assertEqual(tuple(lit("RULED_FLAG_SET")), R.MOUNT_RULED_FLAG_SET)
        self.assertNotIn("/run/host", lit("VIRTUAL_EXACT"))
        # the --private-network tie: U4b's pinned flags ARE U4a's pinned argv minus the per-run tokens
        per_run = ("/usr/bin/systemd-nspawn", "--machine=", "--directory=", "--bind-ro=")
        self.assertEqual(tuple(lit("NSPAWN_PINNED_FLAGS")),
                         tuple(a for a in R.nspawn_argv(RUN_ID) if not a.startswith(per_run)))
        self.assertIn("--private-network", lit("NSPAWN_PINNED_FLAGS"))
        self.assertEqual(set(lit("OBSERVE_ONLY_VERBS")) - set(R.LIVE_VERBS), set())
        # the nspawn argv properties U4b's mount factor relies on as its PRIMARY control
        argv = R.nspawn_argv(RUN_ID)
        self.assertIn("-U", argv)
        self.assertFalse([a for a in argv if a.startswith("--bind=") or a.startswith("--volatile")])
        self.assertEqual([a for a in argv if a.startswith("--bind-ro=")], [f"--bind-ro={lit('REPO_BIND')}"])
        # every emitted entrypoint call parses with U4b's OWN parser, verb and flags, and names a valid receipt
        from tools import host_broker_rehearsal_live as U  # noqa: E402  (argparse only; nothing runs)

        calls = 0
        for ln in emitted().splitlines():
            if ln.lstrip().startswith("#") or f"-I {R.M_REPO}/{R.LIVE_ENTRYPOINT_REL} " not in ln:
                continue
            tail = ln.split(f"{R.LIVE_ENTRYPOINT_REL} ", 1)[1].split(" > ", 1)[0].split(" || ", 1)[0]
            toks = [t.strip('"') for t in tail.split()]
            with self.subTest(call=toks[0]):
                ns = U._parser().parse_args(toks)
                self.assertIn(ns.verb, U.VERBS)
                self.assertRegex(ns.receipt, U._RECEIPT_RE)
                self.assertEqual(ns.execute, ns.verb == "apply" and "--execute" in toks)
                if ns.verb in U.OBSERVE_ONLY_VERBS:
                    self.assertFalse(ns.execute, "an observe-only verb never carries --execute")
                if "/usr/bin/systemd-run --machine=" in ln:
                    self.assertIn(f"--working-directory={U.REPO_BIND}", ln)
                else:  # the HOST canary: U4b must refuse there; it is observe-only and never executes
                    self.assertIn(ns.verb, U.OBSERVE_ONLY_VERBS)
                    self.assertNotIn("--execute", toks)
                calls += 1
        self.assertGreater(calls, 0)


class TreeManifestTest(unittest.TestCase):
    def test_never_hashes_the_token_or_shadow_and_never_follows_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a").mkdir()
            (base / "a/bot-token").write_bytes(b"secret")
            (base / "a/shadow").write_bytes(b"x")
            (base / "a/f").write_bytes(b"data")
            os.symlink("/etc", base / "a/link")
            doc = R.tree_manifest(["/a", "/missing"], base=base)
            by = {e["path"]: e for e in doc["entries"]}
            self.assertIsNone(by["/a/bot-token"]["sha256"])
            self.assertIsNone(by["/a/shadow"]["sha256"])
            self.assertEqual(by["/a/f"]["sha256"], hashlib.sha256(b"data").hexdigest())
            self.assertEqual(by["/a/link"], {"path": "/a/link", "type": "symlink", "target": "/etc",
                                             "uid": os.getuid(), "gid": os.getgid()})
            self.assertEqual(by["/missing"], {"path": "/missing", "type": "absent"})
            self.assertNotIn("/a/link/passwd", by)
            with self.assertRaises(ValueError):
                R.tree_manifest(["/"], base=base)


if __name__ == "__main__":
    unittest.main()
