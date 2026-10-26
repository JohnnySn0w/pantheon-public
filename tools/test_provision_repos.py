"""test_provision_repos.py — unit tests for tools/provision_repos.py (T-0761 c4).

All tests inject a fake git_runner — NO network calls, NO real git operations.
The fake git_runner signature: (argv, cwd, timeout) -> CompletedProcess.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

import tools.provision_repos as pr
import tools.ticket as ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_runner(argv, cwd, timeout):
    """Fake git_runner that always succeeds."""
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")


def _fail_runner(argv, cwd, timeout):
    """Fake git_runner that always returns non-zero."""
    return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="fatal: error")


def _raise_runner(argv, cwd, timeout):
    """Fake git_runner that raises an unexpected exception."""
    raise RuntimeError("boom")


def _timeout_runner(argv, cwd, timeout):
    """Fake git_runner that raises TimeoutExpired."""
    raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)


def _make_registry_yaml(repos: str = "", always: str = "", ignored: str = "") -> str:
    """Build a minimal repos.yaml string for testing."""
    parts = ["schema_version: 1"]
    if always:
        parts.append(f"always_available:\n{always}")
    if repos:
        parts.append(f"repos:\n{repos}")
    if ignored:
        parts.append(f"ignored:\n{ignored}")
    return "\n".join(parts) + "\n"


_STANDARD_REGISTRY = _make_registry_yaml(
    repos=(
        "  - name: echo-prismatic\n"
        "    grants: {orchestrator: rw}\n"
        "  - name: revenue-project\n"
        "    grants: {orchestrator: ro, specialist: ro}\n"
        "  - name: pantheon-config\n"
        "    grants: {orchestrator: rw, specialist: rw, recovery: rw}\n"
    ),
    always="  - repo: pantheon-config\n    access: rw\n    agents: [orchestrator]\n",
)


def _write_registry(root: Path, content: str = _STANDARD_REGISTRY) -> None:
    (root / "repos.yaml").write_text(content, encoding="utf-8")


def _provision(fields, root, runner=_ok_runner, clone_base=None, timeout=30):
    """Convenience wrapper for provision_repos with a temp clone_base."""
    return pr.provision_repos(
        fields,
        repo_root=root,
        git_runner=runner,
        clone_base=clone_base,
        timeout=timeout,
    )


def _write_ticket(path: Path, **fields) -> None:
    """Write a minimal ticket file with the given front-matter fields."""
    defaults = {
        "id": path.stem,
        "created": "2026-06-14T00:00:00Z",
        "requester": "orchestrator",
        "target": "orchestrator",
        "state": "inbox",
    }
    defaults.update(fields)
    lines = ["---"]
    for k, v in defaults.items():
        if isinstance(v, list):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines += ["---", "", f"# {path.stem}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core provision_repos tests
# ---------------------------------------------------------------------------


class TestNoReposField(unittest.TestCase):
    """No `repos:` field → fast no-op, git_runner never called."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_repos_field_returns_empty_warnings(self) -> None:
        calls = []
        def counting_runner(argv, cwd, timeout):
            calls.append(argv)
            return _ok_runner(argv, cwd, timeout)

        warnings = _provision({"target": "orchestrator"}, self.root, runner=counting_runner,
                               clone_base=self.root / "clones")
        self.assertEqual(warnings, [])
        self.assertEqual(calls, [], "git_runner must not be called when repos: absent")

    def test_empty_repos_list_no_op(self) -> None:
        warnings = _provision({"target": "orchestrator", "repos": []}, self.root,
                               clone_base=self.root / "clones")
        self.assertEqual(warnings, [])

    def test_none_repos_no_op(self) -> None:
        # YAML null repos: field
        warnings = _provision({"target": "orchestrator", "repos": None}, self.root,
                               clone_base=self.root / "clones")
        self.assertEqual(warnings, [])


class TestUrlConstruction(unittest.TestCase):
    """ro → HTTPS URL; rw → SSH alias URL."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(self.root)
        self.clone_base = self.root / "clones"
        self.clone_base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _capture_argv(self):
        """Return a runner that captures call args."""
        captured = []
        def runner(argv, cwd, timeout):
            captured.append((argv[:], cwd))
            return _ok_runner(argv, cwd, timeout)
        return runner, captured

    def test_ro_uses_https_url(self) -> None:
        runner, captured = self._capture_argv()
        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(captured), 1)
        argv, _ = captured[0]
        self.assertIn("git", argv[0])
        self.assertIn("clone", argv)
        # URL must use HTTPS
        url = next(a for a in argv if "github.com" in a)
        self.assertTrue(url.startswith("https://"), f"Expected HTTPS URL, got: {url}")
        self.assertIn("example-org/revenue-project", url)

    def test_rw_uses_ssh_alias_url(self) -> None:
        runner, captured = self._capture_argv()
        warnings = _provision(
            {"repos": ["echo-prismatic:rw"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(captured), 1)
        argv, _ = captured[0]
        self.assertIn("clone", argv)
        # URL must use SSH alias format
        url = next(a for a in argv if "git@github-" in a)
        self.assertEqual(url, "git@github-echo-prismatic:example-org/echo-prismatic.git")

    def test_default_access_is_ro_uses_https(self) -> None:
        """Entry without :access defaults to ro → HTTPS."""
        runner, captured = self._capture_argv()
        warnings = _provision(
            {"repos": ["revenue-project"]},  # no :access
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings, [])
        argv, _ = captured[0]
        url = next(a for a in argv if "github.com" in a)
        self.assertTrue(url.startswith("https://"))


class TestCloneVsFetch(unittest.TestCase):
    """Absent dir → clone; present dir → fetch (idempotent reclaim)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(self.root)
        self.clone_base = self.root / "clones"
        self.clone_base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _capture_argv(self):
        captured = []
        def runner(argv, cwd, timeout):
            captured.append((argv[:], cwd))
            return _ok_runner(argv, cwd, timeout)
        return runner, captured

    def test_clone_when_dir_absent(self) -> None:
        runner, captured = self._capture_argv()
        target_dir = self.clone_base / "revenue-project"
        self.assertFalse(target_dir.exists())

        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings, [])
        argv, cwd = captured[0]
        self.assertIn("clone", argv)
        self.assertIsNone(cwd, "clone runs with cwd=None (git creates the dir)")
        self.assertIn(str(target_dir), argv)

    def test_fetch_when_dir_present(self) -> None:
        """Idempotent reclaim: if dir exists, git fetch (not clone)."""
        runner, captured = self._capture_argv()
        target_dir = self.clone_base / "revenue-project"
        target_dir.mkdir()  # simulate existing repo

        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings, [])
        argv, cwd = captured[0]
        self.assertIn("fetch", argv)
        self.assertNotIn("clone", argv)
        self.assertEqual(cwd, target_dir, "fetch runs in the existing repo dir")

    def test_reclaim_same_ticket_idempotent(self) -> None:
        """Second claim (dir already present) → fetch, no error."""
        runner, captured = self._capture_argv()
        target_dir = self.clone_base / "revenue-project"
        target_dir.mkdir()

        # First provision
        warnings1 = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        # Second provision (same ticket, same dir)
        warnings2 = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(warnings1, [])
        self.assertEqual(warnings2, [])
        # Both calls used fetch
        for argv, _ in captured:
            self.assertIn("fetch", argv)
            self.assertNotIn("clone", argv)


class TestBestEffortFailure(unittest.TestCase):
    """Git failures produce warnings, never raise, claim unaffected."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(self.root)
        self.clone_base = self.root / "clones"
        self.clone_base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_git_failure_returns_warning_not_raise(self) -> None:
        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=_fail_runner, clone_base=self.clone_base,
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("revenue-project", warnings[0])
        self.assertIn("failed", warnings[0])

    def test_git_exception_returns_warning_not_raise(self) -> None:
        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=_raise_runner, clone_base=self.clone_base,
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("revenue-project", warnings[0])
        self.assertIn("unexpected error", warnings[0])

    def test_git_timeout_returns_warning_not_raise(self) -> None:
        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=_timeout_runner, clone_base=self.clone_base,
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("revenue-project", warnings[0])
        self.assertIn("timed out", warnings[0])

    def test_multiple_repos_one_fails_others_continue(self) -> None:
        """Failure on one repo does not abort the others."""
        call_count = [0]

        def partial_fail_runner(argv, cwd, timeout):
            call_count[0] += 1
            # Fail only the first call (revenue-project); succeed the rest
            if call_count[0] == 1:
                return _fail_runner(argv, cwd, timeout)
            return _ok_runner(argv, cwd, timeout)

        warnings = _provision(
            {"repos": ["revenue-project:ro", "echo-prismatic:rw"]},
            self.root, runner=partial_fail_runner, clone_base=self.clone_base,
        )
        # revenue-project failed → 1 warning
        self.assertEqual(len(warnings), 1)
        self.assertIn("revenue-project", warnings[0])
        # Both repos were attempted
        self.assertEqual(call_count[0], 2)


class TestSkipLogic(unittest.TestCase):
    """always_available, unknown repos → skipped."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(self.root)
        self.clone_base = self.root / "clones"
        self.clone_base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _capture_argv(self):
        captured = []
        def runner(argv, cwd, timeout):
            captured.append(argv[:])
            return _ok_runner(argv, cwd, timeout)
        return runner, captured

    def test_always_available_repo_skipped_no_git_call(self) -> None:
        """pantheon-config is always_available → skipped, git never called."""
        runner, captured = self._capture_argv()
        # pantheon-config is in _ALWAYS_AVAILABLE_REPOS
        warnings = _provision(
            {"repos": ["pantheon-config:rw"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(captured, [], "git must not be called for always_available repos")
        # skipped-with-warning is acceptable for always_available (non-error skip)
        # or empty warnings — either is fine per design (skip != error)
        # The current impl appends a warning for traceability; check it's informational
        if warnings:
            self.assertIn("pantheon-config", warnings[0])
            self.assertIn("always_available", warnings[0])

    def test_unknown_repo_skipped_with_warning(self) -> None:
        """Repo not in repos.yaml registry → skipped with warning, git not called."""
        runner, captured = self._capture_argv()
        warnings = _provision(
            {"repos": ["totally-unknown-repo:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        self.assertEqual(captured, [], "git must not be called for unknown repos")
        self.assertEqual(len(warnings), 1)
        self.assertIn("totally-unknown-repo", warnings[0])

    def test_registry_absent_fails_open(self) -> None:
        """No repos.yaml → fail-open, still attempt provisioning for known entries."""
        # Remove the registry
        (self.root / "repos.yaml").unlink()
        runner, captured = self._capture_argv()
        # Without registry, _should_skip's rule 2 is skipped → attempt clone
        warnings = _provision(
            {"repos": ["revenue-project:ro"]},
            self.root, runner=runner, clone_base=self.clone_base,
        )
        # git_runner should have been called (fail-open = attempt)
        self.assertGreater(len(captured), 0, "Should attempt clone when registry absent")
        # No failures (runner succeeds)
        self.assertEqual(warnings, [])

    def test_always_available_const_contains_pantheon_config(self) -> None:
        """Constant is correctly defined — basic sanity."""
        self.assertIn("pantheon-config", pr._ALWAYS_AVAILABLE_REPOS)


class TestBuildRemoteUrl(unittest.TestCase):
    """_build_remote_url unit tests."""

    def test_ro_https(self) -> None:
        url = pr._build_remote_url("echo-prismatic", "ro")
        self.assertEqual(url, "https://github.com/example-org/echo-prismatic.git")

    def test_rw_ssh_alias(self) -> None:
        url = pr._build_remote_url("echo-prismatic", "rw")
        self.assertEqual(url, "git@github-echo-prismatic:example-org/echo-prismatic.git")

    def test_unknown_access_falls_back_to_https(self) -> None:
        """Defensive: unknown access value → HTTPS (lint should have caught it)."""
        url = pr._build_remote_url("echo-prismatic", "xx")
        self.assertTrue(url.startswith("https://"))


class TestParseReposEntries(unittest.TestCase):
    """_parse_repos_entries unit tests."""

    def test_name_only_defaults_ro(self) -> None:
        result = pr._parse_repos_entries(["revenue-project"])
        self.assertEqual(result, [("revenue-project", "ro")])

    def test_name_colon_access(self) -> None:
        result = pr._parse_repos_entries(["revenue-project:rw"])
        self.assertEqual(result, [("revenue-project", "rw")])

    def test_multiple_entries(self) -> None:
        result = pr._parse_repos_entries(["a:ro", "b:rw", "c"])
        self.assertEqual(result, [("a", "ro"), ("b", "rw"), ("c", "ro")])

    def test_non_list_returns_empty(self) -> None:
        self.assertEqual(pr._parse_repos_entries(None), [])
        self.assertEqual(pr._parse_repos_entries("revenue-project:ro"), [])
        self.assertEqual(pr._parse_repos_entries(42), [])

    def test_empty_list(self) -> None:
        self.assertEqual(pr._parse_repos_entries([]), [])


# ---------------------------------------------------------------------------
# cmd_claim integration tests (provision hook behavior)
# ---------------------------------------------------------------------------


class TestCmdClaimRepoProvisioning(unittest.TestCase):
    """T-0761 c4: cmd_claim calls provision_repos after a successful claim,
    and never lets provisioning failure affect the claim's return code or raise.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True, exist_ok=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._prev_cap = os.environ.get("PANTHEON_MAX_ACTIVE_TICKETS")
        from datetime import datetime, timedelta, timezone
        # T-4881: override must carry a (run-time computed) expiry.
        _exp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.environ["PANTHEON_MAX_ACTIVE_TICKETS"] = f"100000@{_exp}"

        # Write a ticket with repos: field
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            id="T-0001",
            state="inbox",
            target="orchestrator",
            repos="[echo-prismatic:rw]",
        )

    def tearDown(self) -> None:
        if self._prev_cap is None:
            os.environ.pop("PANTHEON_MAX_ACTIVE_TICKETS", None)
        else:
            os.environ["PANTHEON_MAX_ACTIVE_TICKETS"] = self._prev_cap
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _args(self, tid: str):
        a = argparse.Namespace()
        a.ticket = tid
        a.no_push = True
        return a

    def test_claim_succeeds_when_provision_raises(self) -> None:
        """A provision_repos exception must not propagate; claim returns 0."""
        def exploding_provision(fields, *, repo_root, **kwargs):
            raise RuntimeError("network is down")

        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=0):
                with patch("tools.ticket.provision_repos", exploding_provision):
                    rc = ticket.cmd_claim(self._args("T-0001"))
        self.assertEqual(rc, 0)

    def test_claim_succeeds_when_provision_returns_warnings(self) -> None:
        """Warnings from provision_repos don't alter return code."""
        def warn_provision(fields, *, repo_root, **kwargs):
            return ["echo-prismatic: something went wrong"]

        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=0):
                with patch("tools.ticket.provision_repos", warn_provision):
                    rc = ticket.cmd_claim(self._args("T-0001"))
        self.assertEqual(rc, 0)

    def test_provision_not_called_when_transition_fails(self) -> None:
        """provision_repos must not run if _do_transition returns non-zero."""
        provision_calls = []

        def spy_provision(fields, *, repo_root, **kwargs):
            provision_calls.append(True)
            return []

        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=1):
                with patch("tools.ticket.provision_repos", spy_provision):
                    rc = ticket.cmd_claim(self._args("T-0001"))
        self.assertEqual(rc, 1)
        self.assertEqual(provision_calls, [], "provision must not run on failed transition")

    def test_provision_not_called_when_transition_raises(self) -> None:
        """provision_repos must not run if _do_transition raises TicketError."""
        provision_calls = []

        def spy_provision(fields, *, repo_root, **kwargs):
            provision_calls.append(True)
            return []

        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", side_effect=ticket.TicketError("oops")):
                with patch("tools.ticket.provision_repos", spy_provision):
                    with self.assertRaises(ticket.TicketError):
                        ticket.cmd_claim(self._args("T-0001"))
        self.assertEqual(provision_calls, [])

    def test_provision_called_after_successful_transition(self) -> None:
        """provision_repos IS called when _do_transition returns 0."""
        provision_calls = []

        def spy_provision(fields, *, repo_root, **kwargs):
            provision_calls.append(fields)
            return []

        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=0):
                with patch("tools.ticket.provision_repos", spy_provision):
                    rc = ticket.cmd_claim(self._args("T-0001"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(provision_calls), 1, "provision_repos should be called once")


if __name__ == "__main__":
    unittest.main()
