"""In-sandbox command agent (T-2918). Runs ONLY inside the disposable-host sandbox.

Line protocol on stdin/stdout, one JSON object per line. Every request is
refused unless (a) the per-run marker in the PRIVATE /etc matches this run's
nonce, (b) this process is uid 0 of a user namespace whose uid 0 is NOT the
real root, and (c) /etc, /home, /srv and /var/lib/systemd sit on the sandbox
tmpfs. Commands are fixed argv lists from an allowlist; there is no shell.
``/usr/bin/sudo`` is never allowlisted (T-2918 ruling A1).

Path arguments (T-2918 review N2): for the allowlisted path-taking mutators
(and for anything run through ``setpriv``), every absolute path argument must
resolve under the sandbox roots. ``python3`` is allowed only as ``-I -c``
(the E19 broker-load probe); its file access cannot be checked here, so it is
bounded instead by the sandbox: /etc, /home, /srv, /var/lib/systemd, /tmp,
/var/tmp and /dev/shm are private tmpfs, the repo view is read-only, and the
namespace's uid 0 is the account's own uid on the real host (real root is not
mapped), so OS permissions bound whatever remains.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from typing import Callable, Iterable

MARKER = "/etc/.pantheon-sandbox-marker"
RESET_ROOTS = {"/etc": "etc", "/home": "home", "/srv": "srv", "/var/lib/systemd": "vlsd"}
WRITE_ROOTS = ("/etc/sudoers.d/", "/etc/systemd/system/", "/etc/dbus-1/system.d/", "/home/", "/srv/")
# set by configure() from argv when run as the agent; never at import
NONCE = ""
MOUNT = ""
READ_ROOTS: tuple[str, ...] = ()
STAGING = ""
PATH_ROOTS: tuple[str, ...] = ()
#: Allowlisted binaries whose absolute path arguments must stay in the sandbox.
PATH_CHECKED = frozenset({
    "/usr/bin/install", "/usr/bin/mv", "/usr/bin/chmod", "/usr/bin/chown", "/usr/bin/unlink",
    "/usr/bin/setfacl", "/usr/sbin/visudo",
})
#: Absolute paths that are argument VALUES, not write targets (shells, devices).
PATH_VALUES = frozenset({"/dev/null", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/sbin/nologin", "/nonexistent"})


def configure(nonce: str, mount: str) -> None:
    global NONCE, MOUNT, READ_ROOTS, STAGING, PATH_ROOTS
    NONCE, MOUNT = nonce, mount
    STAGING = mount + "/staging"
    READ_ROOTS = ("/etc/", "/home/", "/srv/", "/var/lib/systemd/", STAGING + "/")
    PATH_ROOTS = WRITE_ROOTS + ("/etc/", "/var/lib/systemd/", STAGING + "/")

ALLOWED = frozenset(
    {
        # the host-substrate port's fixed binaries (tools/germinate/host_substrate/ports.py)
        "/usr/sbin/groupadd", "/usr/sbin/useradd", "/usr/sbin/usermod", "/usr/sbin/userdel",
        "/usr/sbin/groupdel", "/usr/bin/gpasswd", "/usr/bin/setfacl", "/usr/bin/chown",
        "/usr/bin/chmod", "/usr/bin/install", "/usr/bin/unlink", "/usr/bin/mv", "/usr/sbin/visudo",
        "/usr/bin/loginctl", "/usr/bin/systemctl",
        # observation and evidence
        "/usr/bin/getent", "/usr/bin/stat", "/usr/bin/getfacl", "/usr/bin/sha256sum",
        "/usr/bin/cvtsudoers", "/usr/bin/setpriv", "/usr/bin/python3",
    }
)


def check_sandbox(
    nonce: str,
    mount: str,
    *,
    marker: str = MARKER,
    uid_map: str = "/proc/self/uid_map",
    roots: Iterable[str] = tuple(RESET_ROOTS),
    geteuid: Callable[[], int] = os.geteuid,
) -> None:
    """Refuse (PermissionError / OSError) unless this IS the sandbox: the private
    marker carries this run's nonce, we are uid 0 of a namespace whose uid 0 is
    NOT the real root, and every reset root sits on the sandbox tmpfs."""

    with open(marker, encoding="ascii") as handle:
        if not nonce or handle.read().strip() != nonce:
            raise PermissionError("sandbox marker mismatch")
    if geteuid() != 0:
        raise PermissionError("agent is not namespace root")
    with open(uid_map, encoding="ascii") as handle:
        first = handle.read().split()
    if len(first) < 3 or first[0] != "0" or first[1] == "0":
        raise PermissionError("uid 0 here is the real root; refusing")
    tmpfs = os.stat(mount).st_dev
    for root in roots:
        if os.stat(root).st_dev != tmpfs:
            raise PermissionError(f"{root} is not on the sandbox tmpfs")


def _guard() -> None:
    check_sandbox(NONCE, MOUNT)


def check_paths(argv: list[str]) -> None:
    """Review N2: absolute path arguments of path-taking mutators stay in the sandbox."""

    if argv and argv[0] == "/usr/bin/setpriv":  # check the wrapped command
        wrapped = [a for a in argv[1:] if a.startswith("/")]
        if not wrapped:
            raise PermissionError("setpriv without an absolute wrapped binary")
        inner = argv[argv.index(wrapped[0]):]
        if inner[0] not in ALLOWED or inner[0] == "/usr/bin/setpriv":
            raise PermissionError(f"setpriv wraps a non-allowlisted binary: {inner[0]}")
        return check_paths(inner)
    if argv and argv[0] == "/usr/bin/python3":
        if argv[1:3] != ["-I", "-c"] or len(argv) != 4:
            raise PermissionError("python3 is allowed only as `python3 -I -c <code>`")
        return None
    if not argv or argv[0] not in PATH_CHECKED:
        return None
    for arg in path_arguments(argv):
        if arg in PATH_VALUES:
            continue
        if not arg.startswith("/"):  # relative: would resolve against a cwd, never admitted
            raise PermissionError(f"{argv[0]}: relative path argument {arg!r} refused")
        norm = os.path.normpath(arg)
        if not any(norm.startswith(root) for root in PATH_ROOTS):
            raise PermissionError(f"{argv[0]}: path argument {arg!r} is outside the sandbox roots")


#: Per path-taking mutator: options that take a value, the subset whose value
#: is itself a path, and how many leading positionals are NOT paths (mode/owner).
_VALUED = {
    "install": {"-o", "-g", "-m", "-t", "-S", "--owner", "--group", "--mode", "--target-directory", "--suffix"},
    "mv": {"-t", "-S", "--target-directory", "--suffix"},
    "setfacl": {"--set", "-m", "-M", "-x", "-X", "--modify", "--modify-file", "--remove", "--remove-file", "--set-file"},
    "visudo": {"-f", "--file"},
    "chmod": {"--reference"},
    "chown": {"--reference", "--from"},
    "unlink": set(),
}
_PATH_VALUED = {"install": {"-t", "--target-directory"}, "mv": {"-t", "--target-directory"},
                "visudo": {"-f", "--file"}, "chmod": {"--reference"}, "chown": {"--reference"},
                "setfacl": {"-M", "-X", "--modify-file", "--remove-file", "--set-file"}}
_LEADING_VALUES = {"chmod": 1, "chown": 1}


def path_arguments(argv: list[str]) -> list[str]:
    """Every argument of a path-taking mutator that names a FILE (positionals
    after any mode/owner operand, and path-valued option values); option values
    such as modes, owners and ACL entry lists are not paths. A missing option
    value refuses."""

    name = os.path.basename(argv[0])
    valued, path_valued = _VALUED[name], _PATH_VALUED.get(name, set())
    positional, paths, i, end = [], [], 1, False
    while i < len(argv):
        arg = argv[i]
        if not end and arg == "--":
            end = True
        elif not end and arg.startswith("--") and "=" in arg:
            opt, value = arg.split("=", 1)
            if opt in path_valued:
                paths.append(value)
        elif not end and arg.startswith("--"):
            if arg in valued:
                i += 1
                if i >= len(argv):
                    raise PermissionError(f"{argv[0]}: option {arg} is missing its value")
                if arg in path_valued:
                    paths.append(argv[i])
        elif not end and arg.startswith("-") and arg != "-":
            for j, ch in enumerate(arg[1:]):  # a short-option cluster such as -cf
                opt = "-" + ch
                if opt in valued:
                    rest = arg[2 + j:]
                    if not rest:
                        i += 1
                        if i >= len(argv):
                            raise PermissionError(f"{argv[0]}: option {opt} is missing its value")
                        rest = argv[i]
                    if opt in path_valued:
                        paths.append(rest)
                    break
        else:
            positional.append(arg)
        i += 1
    return positional[_LEADING_VALUES.get(name, 0):] + paths


def _under(path: str, roots: tuple[str, ...]) -> str:
    norm = os.path.normpath(path)
    if not any(norm.startswith(root) or norm == root.rstrip("/") for root in roots):
        raise PermissionError(f"{path} is outside the sandbox roots")
    return norm


def _run(argv: list[str]) -> dict:
    if not argv or argv[0] not in ALLOWED:
        raise PermissionError(f"binary not allowlisted: {argv[:1]}")
    check_paths(argv)
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    return {"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _tree(path: str) -> list:
    out = []
    base = _under(path, READ_ROOTS + ("/etc",))
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            digest = ""
            if stat.S_ISREG(st.st_mode):
                with open(full, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            out.append([os.path.relpath(full, base), stat.filemode(st.st_mode), st.st_uid, st.st_gid, digest])
    return out


def _reset() -> None:
    tmpfs = os.stat(MOUNT).st_dev
    for root, name in RESET_ROOTS.items():
        if os.stat(root).st_dev != tmpfs:  # never touch anything off the sandbox tmpfs
            raise PermissionError(f"{root} left the sandbox tmpfs")
        for child in os.listdir(root):
            full = os.path.join(root, child)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.unlink(full)
        subprocess.run(["/usr/bin/cp", "-a", f"{MOUNT}/.base/{name}/.", f"{root}/"], check=True)
    for child in os.listdir(STAGING):
        os.unlink(os.path.join(STAGING, child))


def handle(req: dict) -> dict:
    _guard()
    op = req.get("op")
    if op == "run":
        return _run(list(req["argv"]))
    if op == "stage":
        data = base64.b64decode(req["b64"])
        name = hashlib.sha256(data).hexdigest()
        if name != req["sha256"]:
            raise ValueError("staged bytes do not match their hash")
        path = os.path.join(STAGING, name)
        with open(path, "wb") as handle_:
            handle_.write(data)
        os.chmod(path, 0o440)
        return {"path": path}
    if op == "write":
        path = _under(req["path"], WRITE_ROOTS)
        with open(path, "wb") as handle_:
            handle_.write(base64.b64decode(req["b64"]))
        os.chmod(path, int(req["mode"]))
        os.chown(path, int(req.get("uid", 0)), int(req.get("gid", 0)))
        return {"path": path}
    if op == "mkdir":
        path = _under(req["path"], WRITE_ROOTS)
        os.makedirs(path, exist_ok=True)
        return {"path": path}
    if op == "read":
        path = _under(req["path"], READ_ROOTS)
        if not os.path.exists(path):
            return {"absent": True}
        with open(path, "rb") as handle_:
            return {"b64": base64.b64encode(handle_.read()).decode("ascii")}
    if op == "tree":
        return {"tree": _tree(req["path"])}
    if op == "reset":
        _reset()
        return {"reset": True}
    if op == "whoami":
        with open("/proc/self/uid_map", encoding="ascii") as handle_:
            return {"uid": os.geteuid(), "uid_map": handle_.read(), "nonce": NONCE}
    raise ValueError(f"unknown op {op!r}")


def main() -> None:
    configure(sys.argv[1], sys.argv[2])
    _guard()  # refuses before "ready" anywhere but the sandbox
    print(json.dumps({"ready": NONCE}), flush=True)
    for line in sys.stdin:
        try:
            reply = {"ok": True, "result": handle(json.loads(line))}
        except Exception as exc:  # reported to the caller, never swallowed
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
