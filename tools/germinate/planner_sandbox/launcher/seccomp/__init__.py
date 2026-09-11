"""Vendored libseccomp Python bindings, with a runtime-path pin.

**Load-bearing finding (T-3295 Phase 1, empirically verified 2026-08-26):**
``ctypes.util.find_library()`` cannot be trusted at runtime inside *any*
process confined by ``SystemCallFilter=~@process`` (or an equivalent
fork/exec-denying seccomp policy). Its Linux implementation
(``ctypes/util.py::_findSoname_ldconfig``) shells out to
``/sbin/ldconfig -p`` via ``subprocess.Popen`` to resolve a bare library
name to a SONAME path; when the process-family syscalls that
``subprocess.Popen`` needs (``fork``/``vfork``/``clone``/``clone3``) are
denied, CPython's own code catches the resulting ``OSError`` and falls
through, returning ``None`` (empirically verified: pre-installing a
fork/exec-denying filter and then calling ``find_library`` raises nothing,
just returns ``None``) -- so this does NOT crash immediately, but the
upstream ``pyseccomp`` module's own top-level code does::

    _libseccomp_path = ctypes.util.find_library("seccomp")
    if _libseccomp_path is None:
        raise RuntimeError("Unable to find libseccomp")

That ``RuntimeError`` would fire on every real sandboxed invocation, before
the launcher ever gets to install its own filter -- because systemd's
``SystemCallFilter=~@process`` is enforced on the unit from process start,
not from whenever launcher code decides to install something.

**Fix:** pin the resolved absolute path here (discovered once, outside any
sandbox, at implementation time -- this is the launcher's own bootstrap
concern, not a thing any sandboxed code needs to compute) and monkeypatch
``ctypes.util.find_library`` to return it for the name ``"seccomp"``
*before* importing the vendored module. The vendored file itself stays
pristine; this workaround lives here, in launcher-owned glue code.

The pin is verified to exist at import time (fail-closed per C4 -- a stale
pin on a different host/arch raises a typed ``PrimitiveUnavailable`` rather
than a confusing ``OSError`` from a bad ``ctypes.CDLL()`` call).
"""

from __future__ import annotations

import ctypes.util
import os

# Discovered empirically on this host (Ubuntu 24.04 noble, x86_64,
# libseccomp2 2.5.5-1ubuntu3.1) via `ctypes.util.find_library("seccomp")`
# run OUTSIDE any sandbox, T-3295 Phase 1 planning. Single-architecture
# assumption -- matches ORG-080 Component contract's own x86_64-linux-gnu
# hardcoding for BindReadOnlyPaths, not a new constraint this file adds.
_PINNED_LIBSECCOMP_PATH = "/usr/lib/x86_64-linux-gnu/libseccomp.so.2"


class PrimitiveUnavailable(Exception):
    """Typed refusal when the pinned libseccomp path doesn't hold on this
    host (C4 fail-closed -- never silently degrade to "try find_library
    anyway and hope").
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")


if not os.path.exists(_PINNED_LIBSECCOMP_PATH):
    raise PrimitiveUnavailable(
        "libseccomp",
        f"pinned path {_PINNED_LIBSECCOMP_PATH!r} does not exist on this "
        "host -- the pin in launcher/seccomp/__init__.py needs updating "
        "for this architecture/distro (see module docstring).",
    )

_original_find_library = ctypes.util.find_library


def _pinned_find_library(name: str):
    if name == "seccomp":
        return _PINNED_LIBSECCOMP_PATH
    return _original_find_library(name)


ctypes.util.find_library = _pinned_find_library

from ._vendor.pyseccomp import (  # noqa: E402  (must follow the monkeypatch)
    ALLOW,
    ERRNO,
    Attr,
    SyscallFilter,
)

__all__ = ["ALLOW", "ERRNO", "Attr", "SyscallFilter", "PrimitiveUnavailable"]
