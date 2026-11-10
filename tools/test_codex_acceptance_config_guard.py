"""Unit coverage for the codex acceptance config guard's replay fidelity.

The failing symptom this pins is T-3096 (T-3093 Cat C): the guard's scratch
replay creates its ``config.toml`` under the process's own primary gid, but
the real ~/.codex/config.toml can live under a different gid (in the fleet
this happens when a session's primary gid is ``pantheon-shared`` while the
codex config was written earlier under the user's own group). Because
``_snapshot_matches`` compares (content, mode, uid, gid), the drift check
never accepts even a byte-identical prefix. The invariant tested here:
replayed prefix snapshots carry the same ownership fingerprint as the real
``before`` snapshot they were built from.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from tools.bringup_acceptance.codex_acceptance_config_guard import (
    AcceptanceConfigGuardError,
    build_startup_config_expectation,
    snapshot_config_bytes,
)


def _user_groups() -> set[int]:
    return set(os.getgroups()) | {os.getgid()}


def _pick_unreachable_gid() -> int | None:
    """Return a gid the current process (non-root) cannot chown into.

    On most fleet hosts every gid outside the process's group set is
    unreachable. Return None if the process is root (chown succeeds
    universally) or nothing is unreachable.
    """
    if os.geteuid() == 0:
        return None
    reachable = _user_groups()
    for candidate in (65534, 1, 2, 3, 4):
        if candidate not in reachable:
            return candidate
    return None


class ReplayPreservesBeforeOwnershipTests(unittest.TestCase):
    """T-3096: scratch replay must inherit before-file (uid, gid), not the
    process's default primary gid."""

    @pytest.mark.nested_namespace
    def test_scratch_prefix_snapshots_match_before_ownership_when_gid_differs(self) -> None:
        # Pick a supplementary group that differs from the process's primary
        # gid -- this is the shape that trips the drift check in the real
        # fleet (primary gid pantheon-shared, config gid claude).
        primary_gid = os.getgid()
        alt_gids = _user_groups() - {primary_gid}
        if not alt_gids:
            self.skipTest("no supplementary group available to force a gid mismatch")
        target_gid = next(iter(alt_gids))
        target_uid = os.getuid()

        with tempfile.TemporaryDirectory(prefix="codex-guard-probe-") as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'approval_policy = "never"\ncheck_for_update_on_startup = false\n',
                encoding="utf-8",
            )
            os.chown(config_path, target_uid, target_gid)

            project_cwd = Path(tmp) / "project"
            project_cwd.mkdir()

            expectation = build_startup_config_expectation(
                codex_home=codex_home, project_cwd=project_cwd, owner="probe",
            )

            self.assertEqual(expectation.before.uid, target_uid)
            self.assertEqual(expectation.before.gid, target_gid)
            for prefix in expectation.prefixes:
                self.assertEqual(
                    prefix.snapshot.uid, target_uid,
                    f"prefix {prefix.prefix_index} uid drift (writers={prefix.writer_names})",
                )
                self.assertEqual(
                    prefix.snapshot.gid, target_gid,
                    f"prefix {prefix.prefix_index} gid drift (writers={prefix.writer_names})",
                )

    @pytest.mark.nested_namespace
    def test_replay_refuses_when_before_ownership_cannot_be_mirrored(self) -> None:
        # If the process cannot chown scratch to before.(uid, gid) -- e.g.
        # before.gid is not in this process's group set -- the replay must
        # raise clearly rather than silently produce mismatched-fingerprint
        # prefixes that fool later drift checks.
        forbidden_gid = _pick_unreachable_gid()
        if forbidden_gid is None:
            self.skipTest("no reachable-but-forbidden gid to force a chown failure")

        with tempfile.TemporaryDirectory(prefix="codex-guard-probe-") as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text("approval_policy = \"never\"\n", encoding="utf-8")

            snap = snapshot_config_bytes(config_path)
            # Sub in a synthetic before-snapshot via monkey-patch: rebuild the
            # ownership fields to a gid this process cannot set on scratch.
            import tools.bringup_acceptance.codex_acceptance_config_guard as g
            import dataclasses
            fake_before = dataclasses.replace(snap, gid=forbidden_gid)

            orig_snapshot = g.snapshot_config_bytes

            def _fake_snapshot(path: Path):
                real = orig_snapshot(path)
                if path == config_path:
                    return dataclasses.replace(real, gid=forbidden_gid)
                return real

            project_cwd = Path(tmp) / "project"
            project_cwd.mkdir()

            g.snapshot_config_bytes = _fake_snapshot
            try:
                with self.assertRaises(AcceptanceConfigGuardError) as ctx:
                    build_startup_config_expectation(
                        codex_home=codex_home, project_cwd=project_cwd, owner="probe",
                    )
                self.assertIn("mirror before-ownership", str(ctx.exception))
            finally:
                g.snapshot_config_bytes = orig_snapshot


if __name__ == "__main__":
    unittest.main()
