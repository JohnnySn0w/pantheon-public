#!/usr/bin/env python3
"""Persist Claude Code status-line quota data as a minimal atomic observation.

Claude Code invokes a configured ``statusLine`` command with one JSON object on
stdin after an assistant response.  This helper deliberately retains only the
fields needed to correlate that response and normalize provider quota.  It
never reads credentials and never treats missing ``rate_limits`` as healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence


SCHEMA = "pantheon-claude-statusline-quota/v1"
MAX_INPUT_BYTES = 1024 * 1024


class StatuslineCaptureError(RuntimeError):
    """The provider observation could not be persisted safely."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_payload(source: BinaryIO) -> Mapping[str, Any]:
    raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise StatuslineCaptureError("status-line payload exceeds 1 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatuslineCaptureError("status-line payload is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise StatuslineCaptureError("status-line payload must be a JSON object")
    return payload


def _minimal_observation(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "observed_at": _iso(observed_at),
    }
    for key in ("session_id", "transcript_path", "model", "rate_limits"):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool, Mapping, list)) or value is None:
            if key in payload:
                result[key] = value
    return result


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise StatuslineCaptureError("status-line output path must be absolute")
    if path.exists() and path.is_symlink():
        raise StatuslineCaptureError("status-line output path must not be a symlink")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise StatuslineCaptureError("status-line output directory must not be a symlink")
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise StatuslineCaptureError("status-line observation write failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def capture(
    source: BinaryIO,
    output: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Read one Claude status-line payload and atomically persist its quota slice."""

    observation = _minimal_observation(_read_payload(source), observed_at=now())
    _atomic_write(output, observation)
    return observation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        capture(os.fdopen(os.dup(0), "rb"), args.output)
    except StatuslineCaptureError as exc:
        print(f"REFUSE: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
