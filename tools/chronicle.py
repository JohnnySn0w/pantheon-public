"""tools/chronicle.py — durable HANDOFF archive for Veneration session-selection (T-0766).

Formerly tools/graveyard.py; renamed to Chronicle 2026-08-23 (T-3039) — "graveyard" was a
holdover from the Veneration session-re-open pattern being retired (T-2156), and "Chronicle"
better fits what the mechanism actually is: a grep-able historical record of past session
content. The T-3039 compat layer (old-name shim + aliases + env-var/directory fallback) was
fully stripped 2026-09-06 (T-3090, Phase 5) after its observation window closed clean.

Problem this solves: session jsonls have opaque UUID names and HANDOFF.md is overwritten
on every restart, so "which dead session do I resurrect, and what did it last say?" is
unanswered and the handoff history is lost. The Chronicle preserves the *content* of each
dying HANDOFF, keyed so a successor can find its predecessor.

ON-DISK, NOT IN THE REPO (the operator, msg 2758): stored in per-account local state
(<state-dir>/chronicle/, mirroring session_reset_log) rather than committed to
pantheon-config. Rationale: portability + the snapshots are a LOT of data — repo-homing
would steadily bloat .git (the retention concern flagged as deferred under msg 2447). The
tradeoff vs the earlier repo-homing (msg 2445): we give up git-replication-to-GitHub and
"present in every clone", so cross-AGENT history serving is the the-ancestor daemon's job
(it reads other accounts' chronicles), not free-in-every-clone. The common case — an agent
reading ITS OWN predecessor — is local to its own account, so this fits.

Layout (day-chunked so dirs stay small/traversable):

    <state-dir>/chronicle/<agent>/<YYYY-MM-DD>/<ts>-<uuid>.md   — the archived HANDOFF content
    <state-dir>/chronicle/<agent>/index.md                       — one line per snapshot (newest last)

Index line:  `- <ts> | <reason> | <uuid> | <one-line-summary>`

Why this tiers the recovery (the operator, msg 2442-2447): MOST recovery becomes *reading* the
latest archived handoff (cheap, no resurrection); the live `claude --resume` Veneration is
the FALLBACK tier — only needed when the handoff was cut-off/never-written.

Discipline: archiving is BEST-EFFORT and NON-FATAL — it must NEVER block a restart. Every
write path swallows its errors and returns None rather than raising (same contract as the
session_reset emit in handoff_and_restart.sh). A failed archive degrades gracefully to "no
chronicle entry for this death", never a wedged restart.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Repo root on sys.path so `from tools.agent_role_map import ...` resolves whether
# this file is run as a script (handoff_and_restart.sh) or imported top-level as
# `chronicle` (test_chronicle.py). Standard guard, same as tools/links.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PANTHEON_STATE_DIR_DEFAULT = ".local/state/pantheon"


def _chronicle_root() -> Path:
    """On-disk, per-account Chronicle root — NOT the repo. Mirrors session_reset_log's
    state-dir convention.

    Override precedence: PANTHEON_CHRONICLE_ROOT (points directly at the chronicle root)
    > PANTHEON_STATE_DIR (points at the state dir; chronicle/ is appended) > default
    ~/.local/state/pantheon.
    """
    direct = os.environ.get("PANTHEON_CHRONICLE_ROOT")
    if direct:
        return Path(direct)
    override = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(override) if override else Path.home() / _PANTHEON_STATE_DIR_DEFAULT
    return base / "chronicle"


CHRONICLE_ROOT = _chronicle_root()
_INDEX_NAME = "index.md"


class ChronicleDurabilityError(RuntimeError):
    """Raised when a strict Chronicle archive cannot be proven durable."""


# Unix-account → agent-name map: lets the CLI derive the agent from a
# /home/<user>/HANDOFF.md path so the restart hook needn't know it.
#
# DERIVED from instance.yaml (T-2256) — it used to be a hand-carried four that
# stopped at the pre-ORG-042 accounts, so a handoff dying on exocortex/suborch1/
# suborch2 resolved to None and `chronicle archive` refused the archive with
# "cannot resolve agent" rather than mis-filing it. Loud, but still three seats'
# handoffs never reaching the Chronicle.
#
# THE ONE OVERRIDE, and it is deliberate: the-ancestor's agent-name here is
# "ancestor", not the "the-ancestor" persona the registry carries. That string is
# a DIRECTORY NAME under ~/.local/state/pantheon/chronicle/ with archived
# snapshots and an index.md already filed under it. Deriving it would re-home
# future archives to a sibling dir and orphan everything written before this
# commit, in a tool whose entire job is not losing dying context — a behaviour
# change smuggled into a refactor. Renaming it is a data-migration ticket, not
# this one. Kept verbatim, with the reason attached so it reads as a decision.
_AGENT_NAME_OVERRIDES = {"the-ancestor": "ancestor"}


def _build_user_to_agent() -> dict[str, str]:
    from tools.agent_role_map import account_persona_map

    return {
        account: _AGENT_NAME_OVERRIDES.get(account, persona)
        for account, persona in account_persona_map().items()
    }


_USER_TO_AGENT = _build_user_to_agent()


def _log(msg: str) -> None:
    print(f"[chronicle] {msg}", file=sys.stderr)


def _fs_safe_ts(ts: str) -> str:
    """Filesystem-safe, still-sortable form of an ISO ts: drop ':' (and any '/')."""
    return ts.replace(":", "").replace("/", "")


def _one_line_summary(handoff_text: str, limit: int = 100) -> str:
    """First meaningful line of the handoff, stripped of markdown header marks and
    collapsed to one line — for the index. Empty string if nothing usable."""
    for raw in handoff_text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            line = " ".join(line.split())  # collapse internal whitespace
            return line[:limit]
    return ""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("short write")
        view = view[written:]


def _cleanup_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _truncate_file(path: Path, size: int) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        os.ftruncate(fd, size)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_snapshot_required(snap: Path, handoff_text: str) -> Path:
    fd = -1
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=snap.parent,
            prefix=f".{snap.name}.",
            suffix=".tmp",
            text=True,
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(handoff_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, snap)
        tmp_path = None
        _fsync_dir(snap.parent)
        return snap
    except Exception as exc:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        _cleanup_file(tmp_path)
        raise ChronicleDurabilityError("snapshot write/fsync/rename failed") from exc


def _append_index_required(index: Path, index_line: str) -> None:
    data = index_line.encode("utf-8")
    start_size = 0
    try:
        start_size = index.stat().st_size
    except FileNotFoundError:
        start_size = 0
    fd = -1
    try:
        fd = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        _write_all(fd, data)
        os.fsync(fd)
    except Exception as exc:
        if fd != -1:
            try:
                os.ftruncate(fd, start_size)
            except OSError:
                pass
        raise ChronicleDurabilityError("index append/fsync failed") from exc
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        _fsync_dir(index.parent)
    except Exception as exc:
        try:
            _truncate_file(index, start_size)
        except OSError:
            pass
        raise ChronicleDurabilityError("index directory fsync failed") from exc


def _verify_snapshot_readable(snap: Path, expected_text: str) -> None:
    try:
        if snap.read_text(encoding="utf-8") != expected_text:
            raise ChronicleDurabilityError("snapshot verification mismatch")
    except ChronicleDurabilityError:
        raise
    except Exception as exc:
        raise ChronicleDurabilityError("snapshot readability verification failed") from exc


def detect_session_uuid(projects_dir: "str | os.PathLike | None") -> str:
    """Best-effort: the dying session's jsonl uuid == the stem of the most-recently
    modified ``*.jsonl`` under the agent's Claude Code projects dir at archive time
    (the session that just wrote this handoff is the freshest log). Returns "unknown"
    on an absent dir, no jsonls, or ANY error — the restart path must never break on
    this, and "unknown" preserves the pre-T-1030 default. (T-1030)"""
    try:
        if not projects_dir:
            return "unknown"
        newest, newest_mtime = None, -1.0
        for jf in Path(projects_dir).rglob("*.jsonl"):
            try:
                m = jf.stat().st_mtime
            except OSError:
                continue
            if m > newest_mtime:
                newest_mtime, newest = m, jf
        return newest.stem if newest is not None else "unknown"
    except Exception:  # never fatal — fall back to the old default
        return "unknown"


def archive(agent: str, handoff_text: str, *, jsonl_uuid: str, reason: str, ts: str,
            root: Path | None = None) -> Path | None:
    """Snapshot one dying HANDOFF into the Chronicle + append its index line.

    `ts` is an ISO-8601 string supplied by the caller (the reset hook knows it) — this
    module never reads the clock, so it stays deterministic + testable. Returns the
    snapshot path, or None on ANY failure (non-fatal: a restart must never block on this).
    """
    try:
        base = Path(root) if root is not None else CHRONICLE_ROOT
        day = ts[:10]  # YYYY-MM-DD
        agent_dir = base / agent
        day_dir = agent_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)

        snap = day_dir / f"{_fs_safe_ts(ts)}-{jsonl_uuid}.md"
        snap.write_text(handoff_text, encoding="utf-8")

        summary = _one_line_summary(handoff_text)
        index_line = f"- {ts} | {reason} | {jsonl_uuid} | {summary}\n"
        with (agent_dir / _INDEX_NAME).open("a", encoding="utf-8") as fh:
            fh.write(index_line)
        return snap
    except Exception as exc:  # non-fatal by contract
        _log(f"archive failed for {agent} (non-fatal): {exc!r}")
        return None


def archive_required(agent: str, handoff_text: str, *, jsonl_uuid: str, reason: str,
                     ts: str, root: Path | None = None) -> Path:
    """Strict Chronicle archive for controller-mediated runtime transfer.

    Unlike archive(), every durability failure raises ChronicleDurabilityError so
    the caller can fail closed before starting or submitting to a replacement
    harness.
    """
    base = Path(root) if root is not None else CHRONICLE_ROOT
    day = ts[:10]
    agent_dir = base / agent
    day_dir = agent_dir / day
    snap = day_dir / f"{_fs_safe_ts(ts)}-{jsonl_uuid}.md"
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        snap = _write_snapshot_required(snap, handoff_text)
        _verify_snapshot_readable(snap, handoff_text)

        summary = _one_line_summary(handoff_text)
        index_line = f"- {ts} | {reason} | {jsonl_uuid} | {summary}\n"
        try:
            _append_index_required(agent_dir / _INDEX_NAME, index_line)
        except Exception:
            _cleanup_file(snap)
            raise
        return snap
    except ChronicleDurabilityError:
        _cleanup_file(snap)
        raise
    except Exception as exc:
        _cleanup_file(snap)
        raise ChronicleDurabilityError("strict archive failed") from exc


# Credential-scrub for pane-log archival (T-1683): an archival sink must never
# persist plaintext credentials, no matter how careless the upstream scrollback
# is. Best-effort defense-in-depth — patterns cover the OBVIOUS inline forms
# (the 2026-07-11 finding was `sshpass -p '<pw>'` in contract-debugging
# scrollback); it is NOT a guarantee that nothing leaks, so the real fix stays
# "don't put creds in argv/scrollback". Ordered, applied per line.
import re as _re  # noqa: E402

# Strip terminal escape/control sequences FIRST (T-1683 follow-up): a raw tmux
# pane capture interleaves cursor-positioning codes (e.g. `sshpass\x1b[16G-p`),
# which split credential tokens so a line-based cred regex never matches. It
# also makes the timestamped side-log far more readable (its whole purpose).
# Covers CSI (ESC [ … final-byte), OSC (ESC ] … BEL/ST), and stray single-char
# escapes; plus lone control chars except tab.
_ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL_RE = _re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_SECRET = r"['\"]?([^'\"\s|]+)['\"]?"
_CRED_PATTERNS = [
    # sshpass -p SECRET  /  sshpass -p'SECRET'  /  sshpass --password=SECRET
    (_re.compile(r"(sshpass\s+(?:-p\s*|--password[= ]))" + _SECRET), r"\1<redacted-cred>"),
    # echo SECRET | sudo -S   (piping a password to sudo's stdin — the form the
    # regex-only pass missed on the 2026-07-11 archive)
    (_re.compile(r"(echo\s+)(\S+)(\s*\|\s*sudo\s+-S)"), r"\1<redacted-cred>\3"),
    # key=value / key: value for secret-ish keys (bare or quoted). \w* around the
    # stem catches SENTHOME_PW, DB_PASSWORD, api_key, etc.
    (_re.compile(r"(?i)(\b\w*(?:password|passwd|pwd|secret|token|api[_-]?key|"
                 r"access[_-]?key|auth[_-]?token|client[_-]?secret|_pw|_pass)\w*|pw|pass)"
                 r"(\s*[=:]\s*)" + _SECRET), r"\1\2<redacted-cred>"),
    # bare `pw <secret>` / `pass <secret>` (space-separated; short stems only,
    # accepted FP risk on a human side-log)
    (_re.compile(r"(?i)\b(pw|pass)(\s+)" + _SECRET), r"\1\2<redacted-cred>"),
    # Authorization: Bearer/Basic <jwt/opaque>
    (_re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)(\S+)"), r"\1<redacted-cred>"),
    # Anthropic-style key shapes anywhere (sk-ant-…, sk-…)
    (_re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}"), "<redacted-cred>"),
]


def scrub_credentials(text: str) -> str:
    """Strip terminal escapes then redact obvious inline credential forms
    (T-1683). Never raises — a scrub error must not lose the archive (the
    caller falls through to raw copy). ANSI-stripping is deliberate: pane logs
    are human-forensics side-logs, and the escapes both defeat token matching
    and hurt readability."""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    for pat, repl in _CRED_PATTERNS:
        text = pat.sub(repl, text)
    return text


def archive_companion(agent: str, source: "str | os.PathLike", *, jsonl_uuid: str,
                      ts: str, suffix: str = "pane.log",
                      root: Path | None = None) -> Path | None:
    """Archive a COMPANION artifact beside a session's HANDOFF snapshot (T-1636).

    the operator TG 3724: complementary files to the Chronicle files. The companion
    gets the SAME ``<ts>-<uuid>`` stem as the handoff snapshot (self-pairing by
    name) plus ``.<suffix>.gz`` — e.g. the timestamped pane side-log becomes
    ``<ts>-<uuid>.pane.log.gz``. gzip'd because pane scrollback dwarfs handoff
    text (the msg-2447 retention concern).

    The caller passes a STAGED file from a DEAD session (handoff_and_restart
    renames the live log to ``*.dying`` BEFORE the restart — the tap's awk
    follows the inode so the session's last lines land in the staged file —
    and calls this AFTER the restart kills the session, so the source is
    complete and no live writer holds it). The source is consumed on success;
    if only the unlink fails the archive still counts (dest returned, stray
    source logged — it can't re-blend: staging globs ``*.log``, not
    ``*.dying``). Never-overwrite: an existing dest gets a ``.N`` counter.
    Same non-fatal contract as archive(): returns the path or None; NEVER
    raises.
    """
    try:
        import gzip
        import shutil as _shutil
        src = Path(source)
        if not src.is_file() or src.stat().st_size == 0:
            return None  # nothing to archive (no tap armed / empty) — benign
        base = Path(root) if root is not None else CHRONICLE_ROOT
        day_dir = base / agent / ts[:10]
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_fs_safe_ts(ts)}-{jsonl_uuid}.{suffix}"
        dest = day_dir / f"{stem}.gz"
        n = 2
        while dest.exists():  # never overwrite a prior companion (stray retries)
            dest = day_dir / f"{stem}.{n}.gz"
            n += 1
        # T-1683: scrub obvious plaintext credentials line-by-line on the way
        # into the archive (bounded memory for large logs). If the scrub path
        # errors for any reason, fall through to a raw byte copy — losing the
        # log is worse than a possible surviving cred, and a scrub failure
        # should be rare. Text decode is errors='replace' so a binary/garbled
        # line can't abort the archive.
        try:
            with src.open("r", encoding="utf-8", errors="replace") as fin, \
                    gzip.open(dest, "wt", encoding="utf-8") as fout:
                for line in fin:
                    fout.write(scrub_credentials(line))
        except Exception as scrub_exc:  # never lose the archive over a scrub fault
            _log(f"companion scrub path failed for {agent} ({scrub_exc!r}); raw-copy fallback")
            with src.open("rb") as fin, gzip.open(dest, "wb") as fout:
                _shutil.copyfileobj(fin, fout)
        try:
            src.unlink()
        except OSError as exc:
            # Archived successfully; only consumption failed. Report success —
            # returning None here would make the caller re-archive the same
            # ever-growing file as a duplicate on every subsequent restart.
            _log(f"companion archived but source not consumed for {agent} "
                 f"(stray {src} left; non-fatal): {exc!r}")
        return dest
    except Exception as exc:  # non-fatal by contract
        _log(f"companion archive failed for {agent} (non-fatal): {exc!r}")
        return None


def entries(agent: str, root: Path | None = None) -> list[dict]:
    """Parse this agent's index into ordered records (oldest first, as written).
    Returns [] if the index is absent/empty. Malformed lines are skipped."""
    base = Path(root) if root is not None else CHRONICLE_ROOT
    index = base / agent / _INDEX_NAME
    try:
        raw = index.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [p.strip() for p in line[2:].split("|", 3)]
        if len(parts) < 3:
            continue
        ts, reason, uuid = parts[0], parts[1], parts[2]
        summary = parts[3] if len(parts) == 4 else ""
        out.append({"ts": ts, "reason": reason, "uuid": uuid, "summary": summary})
    return out


def snapshot_path(agent: str, entry: dict, root: Path | None = None) -> Path | None:
    """Resolve the archived .md path for an index entry (the file archive() wrote)."""
    base = Path(root) if root is not None else CHRONICLE_ROOT
    ts, uuid = entry.get("ts", ""), entry.get("uuid", "")
    if not ts or not uuid:
        return None
    return base / agent / ts[:10] / f"{_fs_safe_ts(ts)}-{uuid}.md"


def latest(agent: str, root: Path | None = None) -> dict | None:
    """The most recently archived predecessor record for `agent` (the default
    resurrection target / handoff-to-read), or None if the Chronicle is empty.
    Ordering is by the ts field (lexical = chronological for ISO-8601)."""
    rows = entries(agent, root)
    if not rows:
        return None
    return max(rows, key=lambda r: r["ts"])


def latest_handoff_text(agent: str, root: Path | None = None) -> str | None:
    """Read the content of the latest archived handoff — the cheap recovery tier
    (read the predecessor's last handoff instead of resurrecting it). None if absent."""
    rec = latest(agent, root)
    if rec is None:
        return None
    p = snapshot_path(agent, rec, root)
    try:
        return p.read_text(encoding="utf-8") if p else None
    except (FileNotFoundError, OSError):
        return None


# --------------------------------------------------------------------------- CLI
def agent_from_handoff_path(handoff_file: str) -> str | None:
    """Derive the agent name from a /home/<user>/HANDOFF.md path, or None."""
    parts = Path(handoff_file).resolve().parts
    try:
        user = parts[parts.index("home") + 1]
    except (ValueError, IndexError):
        return None
    return _USER_TO_AGENT.get(user)


def _cli_archive(args: argparse.Namespace) -> int:
    """archive a dying HANDOFF (called best-effort from the session-reset path)."""
    try:
        text = Path(args.handoff_file).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        _log(f"cannot read handoff {args.handoff_file} (non-fatal): {exc}")
        return 1
    agent = args.agent or agent_from_handoff_path(args.handoff_file)
    if not agent:
        _log(f"cannot resolve agent from {args.handoff_file} and no --agent given")
        return 1
    ts = args.ts
    if not ts:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = Path(args.root) if args.root else CHRONICLE_ROOT
    # Explicit --uuid wins; else best-effort detect from --projects-dir; else "unknown".
    uuid = args.uuid or detect_session_uuid(args.projects_dir)
    snap = archive(agent, text, jsonl_uuid=uuid,
                   reason=args.reason, ts=ts, root=root)
    if snap is None:
        return 1
    print(f"[chronicle] archived {snap}")
    return 0


def _cli_companion(args: argparse.Namespace) -> int:
    """archive a companion artifact (e.g. the pane time-log) beside a snapshot (T-1636)."""
    agent = args.agent
    if not agent and args.handoff_file:
        agent = agent_from_handoff_path(args.handoff_file)
    if not agent:
        # Fallback: the tap names its files pane-timed-<agent>.log[.dying] —
        # the source basename carries the agent when the handoff path doesn't
        # (non-standard homes, sandboxed tests).
        import re as _re
        m = _re.match(r"pane-timed-(.+?)\.log(\.dying)?$", Path(args.source).name)
        if m:
            agent = m.group(1)
    if not agent:
        _log("cannot resolve agent (need --agent, --handoff-file, or a pane-timed-<agent> source)")
        return 1
    ts = args.ts
    if not ts:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = Path(args.root) if args.root else CHRONICLE_ROOT
    uuid = args.uuid or detect_session_uuid(args.projects_dir)
    dest = archive_companion(agent, args.source, jsonl_uuid=uuid,
                             ts=ts, suffix=args.suffix, root=root)
    if dest is None:
        # Missing/empty source is the common benign case (no tap armed) — still rc 0?
        # No: rc 1 lets the caller's `if ! …` log the non-fatal line; the restart
        # path treats it as best-effort either way.
        return 1
    print(f"[chronicle] archived companion {dest}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Veneration HANDOFF Chronicle (T-0766).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive", help="snapshot a dying HANDOFF into the Chronicle")
    a.add_argument("--handoff-file", required=True)
    a.add_argument("--reason", required=True, help="reset trigger/class")
    a.add_argument("--ts", default=None, help="ISO-8601 ts (default: now UTC)")
    a.add_argument("--uuid", default=None, help="dead session's jsonl uuid")
    a.add_argument("--projects-dir", default=None,
                   help="Claude Code projects dir; if --uuid is absent, the newest "
                        "*.jsonl here is taken as the dying session's uuid (best-effort)")
    a.add_argument("--agent", default=None, help="override (else derived from handoff path)")
    a.add_argument("--root", default=None, help="chronicle root (default: <state-dir>/chronicle)")
    a.set_defaults(func=_cli_archive)

    c = sub.add_parser("companion",
                       help="archive a companion artifact (pane time-log) beside a snapshot (T-1636)")
    c.add_argument("--source", required=True, help="file to gzip-archive + consume (move semantics)")
    c.add_argument("--suffix", default="pane.log", help="companion suffix (default pane.log → .pane.log.gz)")
    c.add_argument("--handoff-file", default=None, help="for agent derivation (same as archive)")
    c.add_argument("--ts", default=None, help="ISO-8601 ts — pass the SAME ts as the paired archive call")
    c.add_argument("--uuid", default=None)
    c.add_argument("--projects-dir", default=None)
    c.add_argument("--agent", default=None)
    c.add_argument("--root", default=None)
    c.set_defaults(func=_cli_companion)

    d = sub.add_parser("detect-uuid",
                       help="print the dying session's uuid (newest jsonl stem) — lets the "
                            "restart script detect ONCE and pass --uuid to both archive + "
                            "companion so their stems can't diverge (T-1636)")
    d.add_argument("--projects-dir", required=True)
    d.set_defaults(func=lambda args: (print(detect_session_uuid(args.projects_dir)), 0)[1])
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
