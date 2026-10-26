"""T-4433: the autouse conftest guard gives every test a private tmux server, so running
the suite inside a live seat's pane can never type into, or read, that pane.

These fail when the guard is missing and the suite runs inside tmux (every seat's sweep).
In CI there is no tmux around the runner, so there they pin that the guard stays wired.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest


def test_no_test_inherits_the_callers_tmux_server():
    assert "TMUX" not in os.environ, "TMUX names the caller's live server; the guard must unset it"
    assert "TMUX_PANE" not in os.environ


def test_the_default_socket_is_private():
    d = os.environ.get("TMUX_TMPDIR", "")
    assert d.startswith("/tmp/pt-tmux-") and os.path.isdir(d), d
    # Long socket paths fail to bind: keep the whole default socket path well under ~108.
    assert len(os.path.join(d, f"tmux-{os.getuid()}", "default")) < 80


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_tmux_itself_resolves_to_the_private_socket():
    # With no server in the private dir, tmux names the socket it tried. A reachable
    # real server would answer instead, which is the failure this guards against.
    r = subprocess.run(["tmux", "-f", "/dev/null", "list-sessions"],
                       capture_output=True, text=True, timeout=10)
    assert os.environ["TMUX_TMPDIR"] in (r.stderr + r.stdout) or r.returncode != 0, r
    assert r.returncode != 0, f"a tmux server answered inside the test: {r.stdout!r}"


def test_a_spawned_shell_inherits_the_isolation():
    out = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('TMUX', '-'), "
                          "os.environ.get('TMUX_TMPDIR', '-'))"],
                         capture_output=True, text=True, timeout=10).stdout.split()
    assert out[0] == "-" and out[1].startswith("/tmp/pt-tmux-"), out
