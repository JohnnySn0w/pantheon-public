"""Start and drive the T-2918 disposable-host sandbox (see ``__init__``)."""

from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pwd
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from tools._organ_lifecycle import CommandResult

PKG = Path(__file__).resolve().parent
REPO = PKG.parents[4]
REQUIRED_BINARIES = (
    "/usr/sbin/useradd", "/usr/sbin/groupadd", "/usr/sbin/usermod", "/usr/sbin/visudo",
    "/usr/bin/cvtsudoers", "/usr/bin/setfacl", "/usr/bin/getfacl", "/usr/bin/setpriv",
)


class SandboxError(RuntimeError):
    pass


def availability() -> tuple[bool, str]:
    """Whether the userns sandbox can run here, and why not when it cannot.

    Deliberately OFF on CI runners (``CI`` set): the tier is a seat-host
    acceptance run, and a self-hosted runner must not silently change what CI
    measures (T-2918 CI skip budget).
    """

    if os.environ.get("CI"):
        return False, "disposable-host userns sandbox tier does not run on CI runners (T-2918)"
    if os.geteuid() == 0:
        return False, "the sandbox is built for an unprivileged account"
    for tool in ("podman", "sg", "unshare"):
        if shutil.which(tool) is None:
            return False, f"{tool} not installed"
    missing = [b for b in REQUIRED_BINARIES if not os.access(b, os.X_OK)]
    if missing:
        return False, f"missing host binaries {missing}"
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        with open("/etc/subuid", encoding="ascii") as handle:
            if not any(line.split(":")[0] == user and int(line.split(":")[2]) >= 65536 for line in handle if line.count(":") == 2):
                return False, f"no 65536-wide subuid range for {user}"
    except OSError:
        return False, "/etc/subuid unreadable"
    return True, ""


def _launch_argv(mount_dir: Path, nonce: str) -> list[str]:
    inner = [
        "podman", "unshare", "unshare", "--mount", "--propagation", "private",
        "/bin/bash", str(PKG / "enter.sh"), str(mount_dir), str(REPO), nonce, str(PKG),
    ]
    entry = pwd.getpwuid(os.getuid())
    if os.getgid() != entry.pw_gid:
        # the seat's session gid differs from its passwd gid; rootless podman
        # needs the passwd one (T-2918 brief): a fixed, quoted command string
        return ["sg", grp.getgrgid(entry.pw_gid).gr_name, "-c", shlex.join(inner)]
    return inner


class Sandbox:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.mount_dir = workdir / "mnt"
        self.mount_dir.mkdir(mode=0o700)
        self.nonce = secrets.token_hex(16)
        self.stderr_path = workdir / "sandbox.stderr"
        self._stderr = open(self.stderr_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            _launch_argv(self.mount_dir, self.nonce),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, bufsize=1,
        )
        ready = self.proc.stdout.readline()
        if json.loads(ready or "{}").get("ready") != self.nonce:
            self.close()
            raise SandboxError(f"sandbox did not start: {self.stderr_path.read_text()[-2000:]}")

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise SandboxError(f"sandbox agent exited: {self.stderr_path.read_text()[-2000:]}")
        reply = json.loads(line)
        if not reply["ok"]:
            raise SandboxError(reply["error"])
        return reply["result"]

    # convenience wrappers -------------------------------------------------

    def run(self, argv: Sequence[str]) -> CommandResult:
        result = self.call({"op": "run", "argv": list(argv)})
        return CommandResult(result["rc"], result["stdout"], result["stderr"])

    def stage(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        return self.call({"op": "stage", "sha256": sha, "b64": base64.b64encode(data).decode("ascii")})["path"]

    def write(self, path: str, data: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
        self.call({"op": "write", "path": path, "b64": base64.b64encode(data).decode("ascii"),
                   "mode": mode, "uid": uid, "gid": gid})

    def mkdir(self, path: str) -> None:
        self.call({"op": "mkdir", "path": path})

    def read(self, path: str) -> bytes | None:
        result = self.call({"op": "read", "path": path})
        return None if result.get("absent") else base64.b64decode(result["b64"])

    def tree(self, path: str) -> list[list[Any]]:
        return self.call({"op": "tree", "path": path})["tree"]

    def reset(self) -> None:
        self.call({"op": "reset"})

    def close(self) -> None:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.wait(timeout=60)
        self._stderr.close()


class SandboxRunner:
    """A ``tools._organ_lifecycle.CommandRunner`` that executes inside the sandbox
    and records every argv it was handed."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> CommandResult:
        if not argv or not str(argv[0]).startswith("/"):
            raise SandboxError("argv[0] must be an absolute binary")
        self.calls.append(tuple(argv))
        return self.sandbox.run(argv)
