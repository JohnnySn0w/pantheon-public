"""The live seat port: systemd --user and the seat's own files (T-5940, S5). DARK.

``LIVE_ACTUATION_ENABLED`` maps a persona to its own literal pin. Every value is the
literal ``False``: no env var, no flag, no override. A persona not listed can never go
live. Enabling one seat is a reviewed one-line flip (``"artisan": True``) at a supervised
first fire (T-5867); it enables that seat only, and ``backend.resolve_identity`` then
also requires the process to run as that seat's own account, so the flip cannot
actuate another seat.

Every command is a FIXED argv list run through an INJECTED ``CommandRunner``
(tools/_organ_lifecycle.py): no shell, no string-built command, no default runner, and
this module imports no subprocess (the host-substrate port precedent,
host_substrate/ports.py). The systemd manager is always the seat's own user manager,
addressed by ``XDG_RUNTIME_DIR=/run/user/<uid>`` (the codex_recovery.py argv shape).

``_LiveSeatOps`` holds the operations and has no ``__init__``; ``LiveSeatPort`` is the
only production subclass and its constructor is the dark gate (a structural test
asserts no other production class subclasses the mixin).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from tools._organ_lifecycle import CommandRunner
from tools.germinate.engine.backend import BackendRefused

#: persona -> its literal pin. T-5940 lands dark: every value is False.
LIVE_ACTUATION_ENABLED: Final = MappingProxyType({"artisan": False})

ENV_BIN: Final = "/usr/bin/env"
SYSTEMCTL: Final = "/usr/bin/systemctl"

#: Exact unit identifiers: no globs, spaces, separators or a leading dash (equal to
#: harness_manager_run_state.UNIT_RE, asserted by a test).
UNIT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9@_.:-]{0,127}\.(service|timer)")
#: The systemd properties a show reads, as exact fields. A service has no
#: NextElapseUSecRealtime (a timer property), so only REQUIRED_PROPERTIES must appear.
SHOW_PROPERTIES: Final = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "NextElapseUSecRealtime")
REQUIRED_PROPERTIES: Final = frozenset(SHOW_PROPERTIES) - {"NextElapseUSecRealtime"}


class LiveActuationDisabled(BackendRefused):
    """The seat's pin is off (or the seat has none); nothing was touched."""


class SeatPortError(RuntimeError):
    """A port operation was refused or the host answered badly. Never a pass."""


def is_live(persona: str, table: Mapping[str, object] = LIVE_ACTUATION_ENABLED) -> bool:
    """True only when ``persona`` has its own pin and that pin is exactly ``True``."""

    return table.get(persona) is True


# --- argv (pure) ------------------------------------------------------------------


def _uid(uid: object) -> int:
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise SeatPortError(f"uid {uid!r} is not a non-root uid")
    return uid


def _unit(unit: object) -> str:
    if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
        raise SeatPortError(f"unit {unit!r} is not an exact .service/.timer name")
    return unit


def _units(units: Sequence[object]) -> list[str]:
    if isinstance(units, str) or not units:
        raise SeatPortError("expected a non-empty sequence of unit names")
    out = [_unit(u) for u in units]
    if len(set(out)) != len(out):
        raise SeatPortError(f"duplicate unit in {out}")
    return out


def user_manager_prefix(uid: int) -> list[str]:
    return [ENV_BIN, f"XDG_RUNTIME_DIR=/run/user/{_uid(uid)}", SYSTEMCTL, "--user"]


def show_argv(uid: int, units: Sequence[str]) -> list[str]:
    return user_manager_prefix(uid) + ["show", "--property=" + ",".join(SHOW_PROPERTIES), "--", *_units(units)]


def list_units_argv(uid: int) -> list[str]:
    return user_manager_prefix(uid) + ["list-units", "--all", "--plain", "--no-legend", "--no-pager", "--type=service,timer"]


def list_unit_files_argv(uid: int) -> list[str]:
    return user_manager_prefix(uid) + ["list-unit-files", "--plain", "--no-legend", "--no-pager", "--type=service,timer"]


def disable_now_argv(uid: int, unit: str) -> list[str]:
    return user_manager_prefix(uid) + ["disable", "--now", "--", _unit(unit)]


def enable_now_argv(uid: int, unit: str) -> list[str]:
    return user_manager_prefix(uid) + ["enable", "--now", "--", _unit(unit)]


def reset_failed_argv(uid: int, units: Sequence[str]) -> list[str]:
    return user_manager_prefix(uid) + ["reset-failed", "--", *_units(units)]


# --- parsers (pure) ---------------------------------------------------------------


def parse_show(stdout: str, units: Sequence[str]) -> dict[str, dict[str, str]]:
    """``systemctl show`` blocks -> {Id: {property: value}}. Every requested unit must
    come back exactly once with every required property; anything else raises."""

    out: dict[str, dict[str, str]] = {}
    for block in stdout.strip("\n").split("\n\n") if stdout.strip() else []:
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in SHOW_PROPERTIES or key in props:
                raise SeatPortError(f"malformed show line {line!r}")
            props[key] = value
        if not REQUIRED_PROPERTIES <= set(props):
            raise SeatPortError(f"show block lacks {sorted(REQUIRED_PROPERTIES - set(props))}")
        unit = props.pop("Id")
        if unit in out:
            raise SeatPortError(f"show returned {unit} twice")
        out[unit] = props
    if sorted(out) != sorted(units):
        raise SeatPortError(f"show returned {sorted(out)}, asked for {sorted(units)}")
    return out


def parse_unit_names(stdout: str) -> list[str]:
    """First column of a --plain --no-legend listing, kept only when it is an exact unit name."""

    names = []
    for line in stdout.splitlines():
        fields = line.split()
        if fields and UNIT_RE.fullmatch(fields[0]):
            names.append(fields[0])
    return names


# --- operations -------------------------------------------------------------------


class _LiveSeatOps:
    """The port's operations. No ``__init__``: ``LiveSeatPort`` sets ``_runner``,
    ``_uid`` and ``_state_root`` behind its dark gate (tests set them on a test-only
    subclass with a ``FakeCommandRunner``)."""

    _runner: CommandRunner
    _uid: int
    _state_root: Path

    def _run(self, argv: Sequence[str]) -> str:
        result = self._runner.run(list(argv))
        if result.returncode != 0:
            raise SeatPortError(f"{' '.join(argv[2:5])} exited {result.returncode}: {result.stderr.strip()[:200]}")
        return result.stdout

    # systemd --user (the seat's own manager)

    def show(self, units: Sequence[str]) -> dict[str, dict[str, str]]:
        return parse_show(self._run(show_argv(self._uid, units)), list(units))

    def unit_names(self) -> list[str]:
        loaded = parse_unit_names(self._run(list_units_argv(self._uid)))
        files = parse_unit_names(self._run(list_unit_files_argv(self._uid)))
        return sorted(set(loaded) | set(files))

    def observe_units(self) -> dict[str, dict[str, str]]:
        names = self.unit_names()
        return self.show(names) if names else {}

    def disable_now(self, unit: str) -> None:
        self._run(disable_now_argv(self._uid, unit))

    def enable_now(self, unit: str) -> None:
        self._run(enable_now_argv(self._uid, unit))

    def reset_failed(self, units: Sequence[str]) -> None:
        self._run(reset_failed_argv(self._uid, units))

    # the seat's own state files (confined to its state root, never followed)

    def _contained(self, path: str | Path) -> Path:
        text = str(path)
        root = str(self._state_root)
        if not os.path.isabs(text) or os.path.normpath(text) != text or not text.startswith(root.rstrip("/") + "/"):
            raise SeatPortError(f"{text} is not a normalized path under the seat state root {root}")
        parent = os.path.dirname(text)
        if os.path.realpath(parent) != parent:
            raise SeatPortError(f"{text}: an ancestor is a symlink")
        return Path(text)

    def ensure_dir(self, path: str | Path) -> None:
        target = self._contained(path)
        if target.is_symlink():
            raise SeatPortError(f"{target} is a symlink")
        if not target.exists():
            target.mkdir(mode=0o700)
            os.chmod(target, 0o700)
        elif not target.is_dir():
            raise SeatPortError(f"{target} is not a directory")

    def read_file(self, path: str | Path) -> bytes | None:
        target = self._contained(path)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def write_file_atomic(self, path: str | Path, data: bytes) -> None:
        target = self._contained(path)
        if target.is_symlink():
            raise SeatPortError(f"{target} is a symlink")
        tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def remove_file(self, path: str | Path) -> None:
        target = self._contained(path)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SeatPortError(f"{target} is not a regular file")
        if target.exists():
            target.unlink()


class LiveSeatPort(_LiveSeatOps):
    """The live port for ONE seat. DARK: refuses to construct while that seat's pin is off."""

    def __init__(self, persona: str, runner: CommandRunner, *, uid: int, state_root: Path) -> None:
        if not is_live(persona):
            raise LiveActuationDisabled(
                f"live seat actuation is disabled for {persona!r} (T-5940 lands dark; a per-seat reviewed "
                "flip of LIVE_ACTUATION_ENABLED at a supervised first fire, T-5867)"
            )
        if runner is None:  # pragma: no cover - unreachable while dark
            raise LiveActuationDisabled("an explicit CommandRunner is required")
        self._runner = runner  # pragma: no cover - unreachable while dark
        self._uid = _uid(uid)  # pragma: no cover - unreachable while dark
        self._state_root = Path(state_root)  # pragma: no cover - unreachable while dark
