"""Tests for tools/systemd_drift_lint.py (T-2179).

Hermetic: every case builds its own repo-side and installed-side directories in a
tempdir. Nothing reads the real ~/.config/systemd/user, so these mean the same
thing on every seat and on a CI runner — which is the property the lint itself is
about, and which several suites in this repo did NOT have until 2026-07-29.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import systemd_drift_lint as sdl


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.inst = root / "inst"
        self.repo.mkdir()
        self.inst.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, where: Path, name: str, text: str) -> None:
        (where / name).write_text(text, encoding="utf-8")

    def _kinds(self, findings, kind):
        return [f for f in findings if f.kind == kind]


class TestSemanticComparison(_Base):
    def test_comment_and_order_differences_are_not_drift(self):
        # THE T-2179 NOISE: 9 of 10 files differed only in where OnFailure sat and
        # whether it carried an explanatory comment. Reporting those made a real
        # difference invisible. Byte comparison would flag this; semantic must not.
        self._write(self.repo, "a.service",
                    "[Unit]\n# explains why, per T-1868\nOnFailure=x.service\n\n[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, "a.service",
                    "[Unit]\nOnFailure=x.service\n[Service]\nExecStart=/bin/true\n")
        self.assertEqual(self._kinds(sdl.check(self.repo, self.inst), "drift"), [])

    def test_real_directive_difference_is_drift(self):
        # The tenth file: a missing directive with real scheduling consequences.
        self._write(self.repo, "b.timer", "[Timer]\nOnBootSec=2min\nOnActiveSec=5min\n")
        self._write(self.inst, "b.timer", "[Timer]\nOnBootSec=2min\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual(len(drift), 1)
        self.assertIn("OnActiveSec=5min", drift[0].detail)

    def test_drift_hidden_among_cosmetic_siblings_is_still_found(self):
        # The actual T-2179 shape, and the one that matters: many cosmetic-only
        # files PLUS one real difference. The real one must survive the crowd.
        for i in range(9):
            self._write(self.repo, f"c{i}.service", "[Unit]\n# note\nOnFailure=x\n[Service]\nExecStart=/bin/true\n")
            self._write(self.inst, f"c{i}.service", "[Unit]\nOnFailure=x\n[Service]\nExecStart=/bin/true\n")
        self._write(self.repo, "real.service", "[Service]\nExecStart=/bin/true serve\n")
        self._write(self.inst, "real.service", "[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.unit for f in drift], ["real.service"])


class TestSkipsAreNotClean(_Base):
    def test_uninstalled_unit_is_skipped_not_passed(self):
        # A unit absent from the installed side is UNVERIFIED. If this ever became
        # a silent pass, the lint would report all-clear on a host where it checked
        # nothing at all — the "degrades correctly is not covered" distinction.
        self._write(self.repo, "d.service", "[Service]\nExecStart=/bin/true\n")
        findings = sdl.check(self.repo, self.inst)
        self.assertEqual(self._kinds(findings, "drift"), [])
        skipped = self._kinds(findings, "skipped")
        self.assertEqual([f.unit for f in skipped], ["d.service"])
        self.assertIn("NOT verified clean", skipped[0].detail)

    def test_exempt_unit_is_reported_as_skipped_with_its_reason(self):
        # An exemption must stay visible and carry its justification, so a reader
        # can judge whether it is still true rather than inheriting it silently.
        name = next(iter(sdl.KNOWN_EXEMPT))
        self._write(self.repo, name, "[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, name, "[Service]\nExecStart=/bin/false\n")
        findings = sdl.check(self.repo, self.inst)
        self.assertEqual(self._kinds(findings, "drift"), [])
        skipped = self._kinds(findings, "skipped")
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].detail.startswith("exempt:"))

    def test_non_unit_files_ignored(self):
        self._write(self.repo, "README.md", "not a unit\n")
        self.assertEqual(sdl.check(self.repo, self.inst), [])


class TestRebound1Holes(_Base):
    """The three holes a reviewer found in r1 — each asserted, not just fixed."""

    def test_directive_relocated_across_sections_is_drift(self):
        # r1 sorted every line into ONE flat pool, so a directive moved between
        # sections vanished. Relocation is NOT cosmetic: Restart=always under
        # [Service] is a restart policy; under [Unit] systemd ignores it. r1
        # returned zero findings for exactly this, while its docstring claimed
        # the behaviour was intended and correct.
        self._write(self.repo, "r.service",
                    "[Unit]\nDescription=x\n[Service]\nExecStart=/bin/true\nRestart=always\n")
        self._write(self.inst, "r.service",
                    "[Unit]\nDescription=x\nRestart=always\n[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual(len(drift), 1, "cross-section relocation must be drift")
        self.assertIn("Restart=always", drift[0].detail)

    def test_intra_section_reordering_is_still_not_drift(self):
        # The fix must not overcorrect: order WITHIN a section carries no
        # behaviour and is the noise the lint exists to suppress.
        self._write(self.repo, "s.service", "[Service]\nExecStart=/bin/true\nRestart=always\n")
        self._write(self.inst, "s.service", "[Service]\nRestart=always\nExecStart=/bin/true\n")
        self.assertEqual(self._kinds(sdl.check(self.repo, self.inst), "drift"), [])

    def test_path_unit_is_compared_not_invisible(self):
        # r1 skipped anything not .service/.timer BEFORE creating a Finding, so a
        # tracked .path unit (pantheon-mailbox-wake@.path — same family as the
        # timer that actually broke) was absent from drift, skips AND --json.
        self._write(self.repo, "w@.path", "[Path]\nPathExists=/tmp/a\n")
        self._write(self.inst, "w@.path", "[Path]\nPathExists=/tmp/b\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.unit for f in drift], ["w@.path"])

    def test_uncompared_unit_suffix_is_reported_never_dropped(self):
        # A unit-ish suffix outside the compared set must surface as a SKIP.
        # Outside the data model is worse than skipped: it contradicts the
        # module's own invariant that an all-clear cannot hide an all-unchecked.
        self._write(self.repo, "x.automount", "[Automount]\nWhere=/mnt/x\n")
        findings = sdl.check(self.repo, self.inst)
        skipped = self._kinds(findings, "skipped")
        self.assertEqual([f.unit for f in skipped], ["x.automount"])
        self.assertIn("NOT verified clean", skipped[0].detail)

    def test_comparable_count_distinguishes_checked_many_from_checked_one(self):
        # "checked 35, clean" and "checked 1, clean" must not read the same.
        self._write(self.repo, "a.service", "[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, "a.service", "[Service]\nExecStart=/bin/true\n")
        self.assertEqual(sdl._comparable_count(self.repo, self.inst), 1)
        self._write(self.repo, "b.timer", "[Timer]\nOnBootSec=1min\n")
        self._write(self.inst, "b.timer", "[Timer]\nOnBootSec=1min\n")
        self.assertEqual(sdl._comparable_count(self.repo, self.inst), 2)


    def test_duplicate_section_headers_do_not_truncate_the_detail(self):
        # systemd merges repeated section headers. A dict-comprehension build of
        # the report kept only the LAST occurrence, so a difference in an earlier
        # one was detected but missing from the human-readable detail — a drift
        # report that under-states the difference. Detection was never wrong;
        # the text someone acts on was.
        self._write(self.repo, "dup.service",
                    "[Service]\nExecStart=/bin/true\n[Unit]\nDescription=d\n[Service]\nRestart=always\n")
        self._write(self.inst, "dup.service",
                    "[Service]\nExecStart=/bin/false\n[Unit]\nDescription=d\n[Service]\nRestart=always\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual(len(drift), 1)
        self.assertIn("ExecStart=/bin/true", drift[0].detail,
                      "earlier occurrence of a repeated section must survive into the detail")


class TestDriftSeverity(_Base):
    """T-2444: a difference confined to inert metadata directives is 'cosmetic',
    everything systemd acts on is 'substantive'. The sweep alerts on the second
    and merely reports the first, so a doc-path reshuffle does not page."""

    def test_documentation_only_difference_is_cosmetic(self):
        # The exact specialist case: Documentation= path moved in the ORG-017 migration.
        self._write(self.repo, "d.service",
                    "[Unit]\nDescription=x\nDocumentation=file:///new/path\n[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, "d.service",
                    "[Unit]\nDescription=x\nDocumentation=file:///old/path\n[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].severity, "cosmetic")

    def test_description_only_difference_is_cosmetic(self):
        self._write(self.repo, "e.service", "[Unit]\nDescription=new words\n[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, "e.service", "[Unit]\nDescription=old words\n[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["cosmetic"])

    def test_execstart_difference_is_substantive(self):
        self._write(self.repo, "f.service", "[Service]\nExecStart=/bin/true serve\n")
        self._write(self.inst, "f.service", "[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["substantive"])

    def test_inert_plus_real_difference_is_substantive(self):
        # A doc reshuffle must NOT downgrade a real change that rides alongside it —
        # otherwise the cosmetic tier becomes a place for real drift to hide, the
        # very failure T-2179 is about, one level up.
        self._write(self.repo, "g.service",
                    "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true serve\n")
        self._write(self.inst, "g.service",
                    "[Unit]\nDocumentation=file:///old\n[Service]\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["substantive"])

    # T-2846: the memory-snapshot unit's per-account Environment overrides are
    # EXPECTED drift (each seat's real memory dir + REQUIRE_HANDOFF on the
    # sub-orchs); Environment drift there is cosmetic, but ExecStart drift there
    # AND Environment drift on any OTHER unit stay substantive.
    _SNAP = "pantheon-agent-state-snapshot.service"

    def test_memory_snapshot_environment_drift_is_cosmetic(self):
        self._write(self.repo, self._SNAP,
                    "[Service]\nType=oneshot\n"
                    "Environment=PANTHEON_MEMORY_DIR=/home/claude/x/memory\n"
                    "ExecStart=%h/pantheon-config/x.sh\n")
        self._write(self.inst, self._SNAP,
                    "[Service]\nType=oneshot\n"
                    "Environment=PANTHEON_MEMORY_DIR=/home/suborch1/x/memory\n"
                    "Environment=PANTHEON_SNAPSHOT_REQUIRE_HANDOFF=0\n"
                    "ExecStart=%h/pantheon-config/x.sh\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["cosmetic"])

    def test_memory_snapshot_execstart_drift_still_substantive(self):
        # Signal preserved: a real logic drift on the exempt unit still pages.
        self._write(self.repo, self._SNAP,
                    "[Service]\nEnvironment=PANTHEON_MEMORY_DIR=/home/claude/x/memory\n"
                    "ExecStart=%h/pantheon-config/x.sh\n")
        self._write(self.inst, self._SNAP,
                    "[Service]\nEnvironment=PANTHEON_MEMORY_DIR=/home/suborch1/x/memory\n"
                    "ExecStart=/tmp/EVIL.sh\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["substantive"])

    def test_environment_drift_on_other_unit_stays_substantive(self):
        # The exemption is scoped per-unit: Environment is NOT globally inert.
        self._write(self.repo, "other.service",
                    "[Service]\nEnvironment=FOO=1\nExecStart=/bin/true\n")
        self._write(self.inst, "other.service",
                    "[Service]\nEnvironment=FOO=2\nExecStart=/bin/true\n")
        drift = self._kinds(sdl.check(self.repo, self.inst), "drift")
        self.assertEqual([f.severity for f in drift], ["substantive"])


class _FakeSource(sdl.InstalledSource):
    """A hermetic InstalledSource — no filesystem, no sudo. Models the three
    outcomes check() must keep distinct."""

    def __init__(self, files, reachable=True, reason="", unreadable=frozenset()):
        self._files = files
        self.reachable = reachable
        self.reason = reason
        self.label = "fake:acct"
        self._unreadable = frozenset(unreadable)

    def has(self, name):
        return name in self._files

    def read(self, name):
        if name in self._unreadable:
            return None
        return self._files.get(name)


class TestInstalledSourceSeam(_Base):
    """T-2444: check() reads the installed side through an injectable source, so
    the sweep can point it at another account without a second comparison path."""

    def test_unreachable_source_is_one_skip_never_empty_clean(self):
        # The load-bearing trap: an account that could not be read must NEVER come
        # back as an empty (clean-looking) finding list.
        self._write(self.repo, "a.service", "[Service]\nExecStart=/bin/true\n")
        findings = sdl.check(self.repo, installed=_FakeSource({}, reachable=False, reason="sudo denied"))
        self.assertEqual([f.kind for f in findings], ["skipped"])
        self.assertEqual(findings[0].unit, "*")
        self.assertIn("NOT verified clean", findings[0].detail)
        self.assertIn("sudo denied", findings[0].detail)
        self.assertEqual(sdl._comparable_count(self.repo, installed=_FakeSource({}, reachable=False)), 0)

    def test_content_diff_through_source_is_drift_not_link_check(self):
        # trap (c): a seat's units are cp's, so drift is a CONTENT difference. The
        # source has the unit present but with different bytes.
        self._write(self.repo, "b.timer", "[Timer]\nOnBootSec=2min\nOnActiveSec=5min\n")
        src = _FakeSource({"b.timer": "[Timer]\nOnBootSec=2min\n"})
        drift = self._kinds(sdl.check(self.repo, installed=src), "drift")
        self.assertEqual([f.unit for f in drift], ["b.timer"])
        self.assertEqual(drift[0].severity, "substantive")

    def test_installed_but_unreadable_unit_is_skip_not_clean(self):
        self._write(self.repo, "c.service", "[Service]\nExecStart=/bin/true\n")
        src = _FakeSource({"c.service": "..."}, unreadable={"c.service"})
        findings = sdl.check(self.repo, installed=src)
        self.assertEqual(self._kinds(findings, "drift"), [])
        skipped = self._kinds(findings, "skipped")
        self.assertEqual([f.unit for f in skipped], ["c.service"])
        self.assertIn("UNVERIFIED", skipped[0].detail)

    def test_not_installed_through_source_is_skip(self):
        self._write(self.repo, "d.service", "[Service]\nExecStart=/bin/true\n")
        findings = sdl.check(self.repo, installed=_FakeSource({}))
        self.assertEqual([f.unit for f in self._kinds(findings, "skipped")], ["d.service"])
        self.assertEqual(sdl._comparable_count(self.repo, installed=_FakeSource({})), 0)


class TestExitCode(_Base):
    def test_clean_tree_exits_zero_and_drift_exits_one(self):
        self._write(self.repo, "e.service", "[Service]\nExecStart=/bin/true\n")
        self._write(self.inst, "e.service", "[Service]\nExecStart=/bin/true\n")
        self.assertEqual(self._kinds(sdl.check(self.repo, self.inst), "drift"), [])
        self._write(self.inst, "e.service", "[Service]\nExecStart=/bin/false\n")
        self.assertEqual(len(self._kinds(sdl.check(self.repo, self.inst), "drift")), 1)


class TestLiveTreeDriftGuard(unittest.TestCase):
    """THE PART THAT MAKES THIS A GUARD RATHER THAN A DECORATION (r1 finding 3).

    Every test above is hermetic — they prove the lint's LOGIC and would pass on a
    machine with no systemd at all. None of them would ever notice that this seat's
    installed units had drifted from the repo, which is the thing the lint exists
    for. A lint nothing invokes is a script someone has to remember.

    This runs the real lint against the real installed directory, so it fires on
    every seat's ordinary `python3 -m unittest tools.test_systemd_drift_lint` and
    on the full-suite sweep. Same shape as test_main_guard_lint's live-tree guard:
    the runner IS the test.

    ⚠ IT IS HOST-COUPLED BY CONSTRUCTION and skips where the units are absent — a
    CI runner has no ~/.config/systemd/user. Be exact about what that buys:
    "it degrades correctly" and "it is covered in CI" are DIFFERENT claims and only
    the first is true. This covers six real seats and nothing else. A periodic
    timer would cover the case where nobody runs the suite at all; that is filed
    separately rather than pretended here.
    """

    def test_installed_units_match_the_repo(self):
        findings = sdl.check()
        compared = sdl._comparable_count()
        if compared == 0:
            self.skipTest(
                "no tracked units installed for this account (CI runner or a seat "
                "that never installed them) — UNVERIFIED here, not verified clean"
            )
        # T-2846: fail on SUBSTANTIVE drift only. The severity system exists to
        # separate "a repo edit did not reach the unit" (substantive — sync it)
        # from intentional cosmetic/per-account drift (inert directive, or a
        # required per-account Environment override like the memory-snapshot
        # unit's PANTHEON_MEMORY_DIR — must NOT be synced away). A guard that
        # tripped on the latter would be permanently red on every seat that
        # carries a legitimate per-account override, which is exactly the noise
        # the severity tier removes. Cosmetic drift is still surfaced by check().
        drift = [f for f in findings if f.kind == "drift" and f.severity == "substantive"]
        self.assertEqual(
            [f.unit for f in drift], [],
            "installed unit(s) SUBSTANTIVELY differ from tools/systemd/ — a repo "
            "edit did NOT reach the running unit. Sync deliberately, then "
            f"daemon-reload. Details: {[f.detail for f in drift]}",
        )


if __name__ == "__main__":
    unittest.main()
