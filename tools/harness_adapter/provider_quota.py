"""Provider-specific quota observation parsers for runtime switching.

The runtime policy in :mod:`tools.harness_adapter.quota` is intentionally pure.
This module is the evidence boundary: it translates Claude status-line captures
and Codex app-server rate-limit responses into quota snapshots without treating
terminal text as quota authority.
"""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any, BinaryIO

from tools.harness_adapter.quota import QuotaSnapshot, QuotaWindow


DEFAULT_MAX_AGE = timedelta(minutes=15)
_FUTURE_SKEW = timedelta(minutes=1)
ACCEPTANCE_OVERLAY_SCHEMA = "pantheon-quota-acceptance-overlay/v1"


def valid_acceptance_run_id(value: Any) -> bool:
    """Return whether *value* is a bounded acceptance-run path token."""

    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 64
        and all(char in "0123456789TZ-" for char in value)
    )


def read_codex_app_server_rate_limits(
    executable: Path,
    *,
    codex_home: Path,
    timeout: float = 15.0,
) -> Mapping[str, Any] | None:
    """Read the authenticated ChatGPT quota through Codex's JSON-RPC method.

    This starts a short-lived local app-server, performs only initialize plus
    ``account/rateLimits/read``, and returns that method's result. It does not
    create a thread or submit a model turn.
    """

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=environment,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            return None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout.fileno(), selectors.EVENT_READ, "stdout")
        selector.register(process.stderr.fileno(), selectors.EVENT_READ, "stderr")
        stdout_buffer = bytearray()
        active_streams = {"stdout": True, "stderr": True}
        if not _write_json_line(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "pantheon-runtime-controller",
                        "version": "1",
                    }
                },
            },
        ):
            return None
        if (
            _wait_for_rpc_response(process, selector, stdout_buffer, active_streams, deadline, 1)
            is None
        ):
            return None
        if not _write_json_line(process.stdin, {"method": "initialized"}):
            return None
        if not _write_json_line(process.stdin, {"id": 2, "method": "account/rateLimits/read"}):
            return None
        response = _wait_for_rpc_response(
            process, selector, stdout_buffer, active_streams, deadline, 2
        )
    except (OSError, subprocess.SubprocessError, _CodexRpcReadFailure):
        return None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            _reap_process(process)
    if response is None:
        return None
    payload = response.get("result")
    return payload if isinstance(payload, Mapping) else None


class _CodexRpcReadFailure(Exception):
    """Internal sentinel for malformed app-server JSON-RPC output."""


def _write_json_line(stdin: BinaryIO, payload: Mapping[str, Any]) -> bool:
    try:
        stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        stdin.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def _wait_for_rpc_response(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    stdout_buffer: bytearray,
    active_streams: dict[str, bool],
    deadline: float,
    response_id: int,
) -> Mapping[str, Any] | None:
    while True:
        message = _pop_stdout_message(stdout_buffer)
        while message is not None:
            response = _matching_rpc_response(message, response_id)
            if response is not None:
                return response
            message = _pop_stdout_message(stdout_buffer)
        if not active_streams.get("stdout", False):
            if stdout_buffer:
                raise _CodexRpcReadFailure
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        events = selector.select(remaining)
        if not events:
            return None
        for key, _ in events:
            chunk = os.read(key.fd, 65536)
            if not chunk:
                active_streams[str(key.data)] = False
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
                continue
            if key.data == "stdout":
                stdout_buffer.extend(chunk)
        if process.poll() is not None and not active_streams.get("stdout", False):
            return None


def _pop_stdout_message(stdout_buffer: bytearray) -> Mapping[str, Any] | None:
    try:
        newline = stdout_buffer.index(10)
    except ValueError:
        return None
    line = bytes(stdout_buffer[:newline])
    del stdout_buffer[: newline + 1]
    if not line.strip():
        raise _CodexRpcReadFailure
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CodexRpcReadFailure from exc
    if not isinstance(payload, Mapping):
        raise _CodexRpcReadFailure
    return payload


def _matching_rpc_response(
    message: Mapping[str, Any], response_id: int
) -> Mapping[str, Any] | None:
    if "error" in message:
        raise _CodexRpcReadFailure
    if message.get("id") != response_id:
        return None
    if "result" not in message:
        raise _CodexRpcReadFailure
    return message


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    try:
        if process.poll() is not None:
            process.wait(timeout=0)
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.SubprocessError:
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except OSError:
                pass


@dataclass(frozen=True)
class ClaudeStatuslineQuotaObserver:
    """Read the atomic quota observation written by Claude's status-line hook."""

    path: Path
    profile: str
    max_age: timedelta = DEFAULT_MAX_AGE
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def read(self) -> QuotaSnapshot:
        payload = _read_json_mapping(self.path)
        if payload is None:
            return _unknown_snapshot(
                self.profile,
                now=self.now(),
                freshness="unavailable",
                evidence={
                    "provider": "claude",
                    "source": "statusline",
                    "path": str(self.path),
                    "reason": "missing-or-invalid-json",
                },
            )
        return parse_claude_statusline_observation(
            payload,
            profile=self.profile,
            now=self.now(),
            max_age=self.max_age,
            extra_evidence={
                "path": str(self.path),
                "sha256": _sha256_file(self.path) or "unavailable",
                "session_id": str(payload.get("session_id", "")),
            },
        )


@dataclass(frozen=True)
class UnavailableQuotaObserver:
    """Return an unavailable quota snapshot without touching provider files."""

    profile: str
    reason: str
    evidence: Mapping[str, str] = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def read(self) -> QuotaSnapshot:
        observed_at = _as_aware_utc(self.now())
        return _unknown_snapshot(
            self.profile,
            now=observed_at,
            freshness="unavailable",
            evidence={**dict(self.evidence), "reason": self.reason},
        )


@dataclass(frozen=True)
class ClaudeAcceptanceQuotaOverlayObserver:
    """Read a tamper-checked Claude quota fixture for one live acceptance run."""

    path: Path
    canonical_path: Path
    profile: str
    run_id: str
    state_dir: Path
    max_age: timedelta = DEFAULT_MAX_AGE
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def read(self) -> QuotaSnapshot:
        now = _as_aware_utc(self.now())
        paths = self._validated_paths(now)
        if not isinstance(paths, _AcceptanceOverlayPaths):
            return paths

        overlay_bytes = self._read_file_bytes(paths.overlay, now, "overlay-read-failed")
        if not isinstance(overlay_bytes, bytes):
            return overlay_bytes
        overlay_hash = sha256(overlay_bytes).hexdigest()
        try:
            envelope = json.loads(overlay_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._unavailable(now, "invalid-overlay-json", overlay_sha256=overlay_hash)
        if not isinstance(envelope, Mapping):
            return self._unavailable(now, "invalid-overlay-envelope", overlay_sha256=overlay_hash)

        payload = self._validated_envelope(envelope, paths, now, overlay_hash)
        if not isinstance(payload, Mapping):
            return payload

        canonical_hash = _sha256_file(paths.canonical)
        if canonical_hash is None:
            return self._unavailable(now, "canonical-read-failed", overlay_sha256=overlay_hash)
        if canonical_hash != envelope["canonical_sha256_at_install"]:
            return self._unavailable(
                now,
                "canonical-hash-mismatch",
                overlay_sha256=overlay_hash,
                canonical_sha256=canonical_hash,
            )

        return parse_claude_statusline_observation(
            payload,
            profile=self.profile,
            now=now,
            max_age=self.max_age,
            extra_evidence={
                "source": "acceptance-overlay",
                "underlying_source": "statusline",
                "fixture": "true",
                "run_id": self.run_id,
                "overlay_path": str(paths.overlay),
                "canonical_path": str(paths.canonical),
                "overlay_sha256": overlay_hash,
                "canonical_sha256": canonical_hash,
            },
        )

    def _validated_paths(self, now: datetime) -> "_AcceptanceOverlayPaths | QuotaSnapshot":
        if not valid_acceptance_run_id(self.run_id):
            return self._unavailable(now, "invalid-run-id")
        try:
            overlay = self.path.resolve(strict=False)
            canonical = self.canonical_path.resolve(strict=False)
            acceptance_dir = (self.state_dir / "acceptance" / self.run_id).resolve(
                strict=False
            )
        except OSError:
            return self._unavailable(now, "path-resolution-failed")

        if overlay == canonical:
            return self._unavailable(now, "overlay-is-canonical")
        if overlay != acceptance_dir / "quota-overlay.json":
            return self._unavailable(now, "unexpected-overlay-path")
        if self.path.is_symlink():
            return self._unavailable(now, "overlay-symlink")
        if self.canonical_path.is_symlink():
            return self._unavailable(now, "canonical-symlink")

        overlay_stat = self._regular_file_stat(self.path)
        if overlay_stat is None:
            return self._unavailable(now, "overlay-not-regular-file")
        canonical_stat = self._regular_file_stat(self.canonical_path)
        if canonical_stat is None:
            return self._unavailable(now, "canonical-not-regular-file")
        if os.path.samestat(overlay_stat, canonical_stat):
            return self._unavailable(now, "overlay-is-canonical")
        if overlay_stat.st_uid != os.geteuid():
            return self._unavailable(now, "overlay-owner-mismatch")
        if stat.S_IMODE(overlay_stat.st_mode) != 0o600:
            return self._unavailable(now, "overlay-mode-not-0600")

        return _AcceptanceOverlayPaths(overlay=overlay, canonical=canonical)

    def _validated_envelope(
        self,
        envelope: Mapping[str, Any],
        paths: "_AcceptanceOverlayPaths",
        now: datetime,
        overlay_hash: str,
    ) -> "Mapping[str, Any] | QuotaSnapshot":
        if envelope.get("schema") != ACCEPTANCE_OVERLAY_SCHEMA:
            return self._unavailable(now, "schema-mismatch", overlay_sha256=overlay_hash)
        if envelope.get("run_id") != self.run_id:
            return self._unavailable(now, "run-id-mismatch", overlay_sha256=overlay_hash)
        if _parse_datetime(envelope.get("created_at")) is None:
            return self._unavailable(now, "invalid-created-at", overlay_sha256=overlay_hash)
        if envelope.get("canonical_path") != str(paths.canonical):
            return self._unavailable(now, "canonical-path-mismatch", overlay_sha256=overlay_hash)
        install_hash = envelope.get("canonical_sha256_at_install")
        if not isinstance(install_hash, str) or not install_hash:
            return self._unavailable(
                now,
                "invalid-canonical-install-hash",
                overlay_sha256=overlay_hash,
            )
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return self._unavailable(now, "invalid-payload", overlay_sha256=overlay_hash)
        return payload

    def _regular_file_stat(self, path: Path) -> os.stat_result | None:
        try:
            path_stat = path.stat(follow_symlinks=False)
        except OSError:
            return None
        return path_stat if stat.S_ISREG(path_stat.st_mode) else None

    def _read_file_bytes(
        self,
        path: Path,
        now: datetime,
        reason: str,
    ) -> "bytes | QuotaSnapshot":
        try:
            return path.read_bytes()
        except OSError:
            return self._unavailable(now, reason)

    def _unavailable(
        self,
        now: datetime,
        reason: str,
        *,
        overlay_sha256: str | None = None,
        canonical_sha256: str | None = None,
    ) -> QuotaSnapshot:
        evidence = {
            "provider": "claude",
            "source": "acceptance-overlay",
            "underlying_source": "statusline",
            "fixture": "true",
            "run_id": self.run_id,
            "path": str(self.path),
            "canonical_path": str(self.canonical_path),
            "reason": reason,
        }
        if overlay_sha256 is not None:
            evidence["overlay_sha256"] = overlay_sha256
        if canonical_sha256 is not None:
            evidence["canonical_sha256"] = canonical_sha256
        return _unknown_snapshot(
            self.profile,
            now=now,
            freshness="unavailable",
            evidence=evidence,
        )


@dataclass(frozen=True)
class _AcceptanceOverlayPaths:
    overlay: Path
    canonical: Path


@dataclass(frozen=True)
class CodexAppServerQuotaObserver:
    """Call a local Codex app-server quota reader and normalize its response."""

    read_observation: Callable[[], Mapping[str, Any] | None]
    profile: str
    max_age: timedelta = DEFAULT_MAX_AGE
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def read(self) -> QuotaSnapshot:
        observed_at = _as_aware_utc(self.now())
        try:
            payload = self.read_observation()
        except Exception as exc:  # provider read failures are unknown/nonblocking
            return _unknown_snapshot(
                self.profile,
                now=observed_at,
                freshness="unavailable",
                evidence={
                    "provider": "codex",
                    "source": "app-server",
                    "reason": "read-failed",
                    "error_type": type(exc).__name__,
                },
            )
        if not isinstance(payload, Mapping):
            return _unknown_snapshot(
                self.profile,
                now=observed_at,
                freshness="unavailable",
                evidence={
                    "provider": "codex",
                    "source": "app-server",
                    "reason": "missing-observation",
                },
            )
        return parse_codex_app_server_quota_observation(
            payload,
            profile=self.profile,
            observed_at=observed_at,
            now=observed_at,
            max_age=self.max_age,
        )


def parse_claude_statusline_observation(
    payload: Mapping[str, Any],
    *,
    profile: str,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
    extra_evidence: Mapping[str, str] | None = None,
) -> QuotaSnapshot:
    """Normalize a Claude interactive status-line quota capture.

    The capture must include its own observation timestamp because the status
    line writes asynchronously from controller reads.
    """

    observed_now = _as_aware_utc(now or datetime.now(timezone.utc))
    observed_at = _observed_at(payload)
    evidence = {
        "provider": "claude",
        "source": "statusline",
        "authoritative_limit": str(_provider_limit_signal(payload)).lower(),
        **dict(extra_evidence or {}),
    }
    if observed_at is None:
        return _unknown_snapshot(
            profile,
            now=observed_now,
            freshness="unavailable",
            evidence={**evidence, "reason": "missing-observed-at"},
        )

    windows = _quota_windows(payload, provider="claude", now=observed_now)
    return _snapshot_from_windows(
        profile,
        windows=windows,
        observed_at=observed_at,
        now=observed_now,
        max_age=max_age,
        evidence=evidence,
    )


def parse_codex_app_server_quota_observation(
    payload: Mapping[str, Any],
    *,
    profile: str,
    observed_at: datetime | None = None,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
    extra_evidence: Mapping[str, str] | None = None,
) -> QuotaSnapshot:
    """Normalize a Codex app-server account rate-limit observation."""

    observed_now = _as_aware_utc(now or datetime.now(timezone.utc))
    sample_at = _as_aware_utc(observed_at) if observed_at is not None else _observed_at(payload)
    evidence = {
        "provider": "codex",
        "source": "app-server",
        "authoritative_limit": str(_provider_limit_signal(payload)).lower(),
        **dict(extra_evidence or {}),
    }
    if sample_at is None:
        return _unknown_snapshot(
            profile,
            now=observed_now,
            freshness="unavailable",
            evidence={**evidence, "reason": "missing-observed-at"},
        )

    windows = _quota_windows(payload, provider="codex", now=observed_now)
    return _snapshot_from_windows(
        profile,
        windows=windows,
        observed_at=sample_at,
        now=observed_now,
        max_age=max_age,
        evidence=evidence,
    )


def _read_json_mapping(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _sha256_file(path: Path) -> str | None:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _provider_limit_signal(payload: Mapping[str, Any]) -> bool:
    """Return only explicit provider limit states, never inferred percentages."""

    keys = (
        "rateLimitReachedType",
        "rate_limit_reached_type",
        "authoritative_limit",
        "quota_exhausted",
    )
    for container in _containers(payload):
        for key in keys:
            value = container.get(key)
            if value is True:
                return True
            if isinstance(value, str) and value.strip().lower() not in {
                "",
                "none",
                "false",
                "null",
                "unknown",
            }:
                return True
        for key in ("rateLimits", "rate_limits"):
            nested = container.get(key)
            if isinstance(nested, Mapping) and _provider_limit_signal(nested):
                return True
    return False


def _snapshot_from_windows(
    profile: str,
    *,
    windows: tuple[QuotaWindow, ...],
    observed_at: datetime,
    now: datetime,
    max_age: timedelta,
    evidence: Mapping[str, str],
) -> QuotaSnapshot:
    freshness, reason = _freshness(observed_at, now=now, max_age=max_age)
    if not windows and freshness == "fresh":
        freshness = "unavailable"
        reason = "missing-valid-windows"
    return QuotaSnapshot(
        profile=profile,
        windows=windows,
        observed_at=observed_at,
        freshness=freshness,
        evidence={
            **dict(evidence),
            "observed_at": _iso(observed_at),
            "window_count": str(len(windows)),
            "reason": reason,
        },
    )


def _unknown_snapshot(
    profile: str,
    *,
    now: datetime,
    freshness: str,
    evidence: Mapping[str, str],
) -> QuotaSnapshot:
    return QuotaSnapshot(
        profile=profile,
        windows=(),
        observed_at=_as_aware_utc(now),
        freshness=freshness,  # type: ignore[arg-type]
        evidence=dict(evidence),
    )


def _quota_windows(
    payload: Mapping[str, Any],
    *,
    provider: str,
    now: datetime,
) -> tuple[QuotaWindow, ...]:
    windows: list[QuotaWindow] = []
    for name, raw in _window_candidates(payload):
        window = _quota_window(name, raw, provider=provider, now=now)
        if window is not None:
            windows.append(window)
    return tuple(windows)


def _window_candidates(payload: Mapping[str, Any]) -> list[tuple[str | None, Mapping[str, Any]]]:
    candidates: list[tuple[str | None, Mapping[str, Any]]] = []
    for container in _containers(payload):
        for key in (
            "windows",
            "limits",
            "quota",
            "quotas",
            "rate_limits",
            "rateLimits",
            "account_rate_limits",
            "accountRateLimits",
        ):
            raw = container.get(key)
            candidates.extend(_items_from_container(raw))
    return candidates or _items_from_container(payload)


def _containers(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    nested = []
    for key in ("statusline", "status_line", "quota", "account", "data", "rate_limits"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            nested.append(value)
    return (payload, *nested)


def _items_from_container(raw: Any) -> list[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(raw, Mapping):
        if _looks_like_window(raw):
            name = _string_field(raw, "name", "window", "period", "bucket")
            return [(name, raw)]
        return [
            (str(name), value)
            for name, value in raw.items()
            if isinstance(value, Mapping)
        ]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [
            (None, value)
            for value in raw
            if isinstance(value, Mapping)
        ]
    return []


def _looks_like_window(raw: Mapping[str, Any]) -> bool:
    return any(
        key in raw
        for key in (
            "used_percent",
            "usedPercent",
            "usage_percent",
            "remaining_percent",
            "resets_at",
            "resetsAt",
            "reset_at",
            "limit",
            "limit_tokens",
        )
    )


def _quota_window(
    name_hint: str | None,
    raw: Mapping[str, Any],
    *,
    provider: str,
    now: datetime,
) -> QuotaWindow | None:
    name = _window_name(name_hint, raw)
    if name is None:
        return None
    used, remaining = _percent_pair(raw)
    resets_at = _reset_time(raw, now=now)
    if used is None or remaining is None or resets_at is None:
        return None
    if not (_valid_percent(used) and _valid_percent(remaining)):
        return None
    canonical_name = _canonical_window_name(name, provider=provider)
    duration_minutes = _number_field(raw, "windowDurationMins", "window_duration_mins")
    if provider == "codex" and duration_minutes is not None:
        if duration_minutes == 300:
            canonical_name = "5h"
        elif duration_minutes == 10080:
            canonical_name = "weekly"
    return QuotaWindow(
        name=canonical_name,
        used_percent=float(used),
        remaining_percent=float(remaining),
        resets_at=resets_at,
    )


def _window_name(name_hint: str | None, raw: Mapping[str, Any]) -> str | None:
    return name_hint or _string_field(
        raw,
        "name",
        "window",
        "window_name",
        "period",
        "bucket",
        "quota",
        "limit_name",
    )


def _canonical_window_name(name: str, *, provider: str) -> str:
    normalized = name.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "5-hour": "5h",
        "five-hour": "5h",
        "five-hours": "5h",
        "five-hour-limit": "5h",
        "5h-limit": "5h",
        "7-day": "weekly",
        "7d": "weekly",
        "seven-day": "weekly",
        "week": "weekly",
        "weekly-quota": "weekly",
        "weekly-remaining": "weekly",
    }
    if provider == "codex" and normalized == "chatgpt-weekly":
        return "weekly"
    return aliases.get(normalized, normalized)


def _percent_pair(raw: Mapping[str, Any]) -> tuple[float | None, float | None]:
    used = _number_field(
        raw,
        "used_percent",
        "usage_percent",
        "percent_used",
        "usedPercentage",
        "usedPercent",
        "used_pct",
    )
    remaining = _number_field(
        raw,
        "remaining_percent",
        "percent_remaining",
        "remainingPercentage",
        "remainingPercent",
        "remaining_pct",
        "available_percent",
    )
    if remaining is None:
        remaining = _remaining_from_counts(raw)
    if used is None:
        used = _used_from_counts(raw)
    if used is None and remaining is not None:
        used = 100.0 - remaining
    if remaining is None and used is not None:
        remaining = 100.0 - used
    return used, remaining


def _remaining_from_counts(raw: Mapping[str, Any]) -> float | None:
    limit = _limit_count(raw)
    remaining = _number_field(raw, "remaining", "remaining_tokens", "remainingRequests")
    if limit is None or remaining is None or limit <= 0:
        return None
    return (remaining / limit) * 100.0


def _used_from_counts(raw: Mapping[str, Any]) -> float | None:
    limit = _limit_count(raw)
    used = _number_field(raw, "used", "used_tokens", "usedRequests")
    if limit is None or used is None or limit <= 0:
        return None
    return (used / limit) * 100.0


def _limit_count(raw: Mapping[str, Any]) -> float | None:
    return _number_field(raw, "limit", "limit_tokens", "total", "total_tokens", "max")


def _reset_time(raw: Mapping[str, Any], *, now: datetime) -> datetime | None:
    absolute = _first_present(
        raw,
        "resets_at",
        "reset_at",
        "reset_time",
        "resetAt",
        "resetsAt",
        "resetTime",
    )
    parsed = _parse_datetime(absolute)
    if parsed is not None:
        return parsed
    seconds = _number_field(raw, "reset_after_seconds", "resets_in_seconds", "retry_after")
    if seconds is None or seconds < 0:
        return None
    return _as_aware_utc(now) + timedelta(seconds=seconds)


def _observed_at(payload: Mapping[str, Any]) -> datetime | None:
    direct = _parse_datetime(
        _first_present(payload, "observed_at", "observedAt", "timestamp", "ts")
    )
    if direct is not None:
        return direct
    for key in ("statusline", "status_line", "meta", "metadata"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            parsed = _parse_datetime(
                _first_present(nested, "observed_at", "observedAt", "timestamp", "ts")
            )
            if parsed is not None:
                return parsed
    return None


def _freshness(
    observed_at: datetime,
    *,
    now: datetime,
    max_age: timedelta,
) -> tuple[str, str]:
    observed_at = _as_aware_utc(observed_at)
    now = _as_aware_utc(now)
    if observed_at - now > _FUTURE_SKEW:
        return "unavailable", "future-observed-at"
    if now - observed_at > max_age:
        return "stale", "stale"
    return "fresh", "fresh"


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_aware_utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _as_aware_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _string_field(raw: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _number_field(raw: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and isfinite(float(value)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip().rstrip("%")
            try:
                parsed = float(stripped)
            except ValueError:
                continue
            if isfinite(parsed):
                return parsed
    return None


def _first_present(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _valid_percent(value: float) -> bool:
    return isfinite(float(value)) and 0.0 <= float(value) <= 100.0


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_aware_utc(value).isoformat().replace("+00:00", "Z")
