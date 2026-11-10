#!/usr/bin/env python3
"""T-6005: Claude Code ``apiKeyHelper`` that prints the backend key from a 0600 file.

The key never travels on argv or the launch env: Claude Code runs this command
(named in the --settings file) and reads stdout. The file is re-validated at read
time (regular, not a symlink, owned by this uid, mode 0600). Errors are generic
and never include key material.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys


def read_key(path: str) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError("not a regular file")
        if st.st_uid != os.geteuid():
            raise PermissionError("not owned by the running uid")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise PermissionError("mode is not 0600")
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    key = data.decode("utf-8").strip()
    if not key:
        raise ValueError("empty key file")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args(argv)
    try:
        key = read_key(args.key_file)
    except (OSError, ValueError) as exc:
        print(f"claude_apikey_helper: refused ({type(exc).__name__})", file=sys.stderr)
        return 1
    sys.stdout.write(key + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
