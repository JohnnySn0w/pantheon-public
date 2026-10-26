#!/usr/bin/env python3
"""operator_decision_sweep.py (T-5015 phase 2) — catch the operator rulings with no register row.

Serves "the operator involved but not necessary" (TG 11821). Phase 1 gates rulings that
close a ticket; this nightly sweep catches rulings given in chat that close none.

Flow: read this account's session jsonls (structural parsing shared with
tools/telegram_inbound.py), pull the operator's inbound Telegram messages in the window,
keep the decision-shaped ones whose message id is not cited in
notes/operator-decision-register.md as `TG <id>`, and file ONE ticket per run listing
only ids and UTC timestamps.

Privacy (load-bearing): the operator's message text never goes into git. The full digest
(text, prior ask, reason) is written to `<state-dir>/<date>.md` (0600, dir 0700); the
ticket carries metadata only.

Heuristic tradeoff: `is_decision_shaped` is deliberately loose. A false positive costs
the HO a one-line dismissal; a missed ruling is a lost throughline datum and a future
needless interruption of the operator. When in doubt, flag.

Scope: the account running the sweep (orchestrator). Other seats' sessions are out of scope.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_inbound as tin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OPERATOR_CHAT_ID = "424242424"
REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
PRIOR_ASK_MAX = 300
SHORT_REPLY_MAX = 120

_VOCAB = re.compile(
    r"\b(ratif\w*|approv\w*|go ahead|go for it|do it|sounds good|ship it|hold|cut|keep|swap|"
    r"should|shouldn'?t|let'?s|prefer|instead|never|always|from now on|do not|don'?t)\b", re.I)
_LEADING = re.compile(r"^\W*(yes|yep|yeah|no|nope)\b", re.I)
_ACKS = {"gotcha", "ok", "okay", "kk", "k", "thanks", "thank you", "thx", "ty", "nice", "lol",
         "cool", "got it", "great", "np", "haha", "lmao"}
_COVER_RE = re.compile(r"TGs?\s*(\d+(?:\s*[/,&]\s*\d+)*)")


def _is_pure_ack(text: str) -> bool:
    norm = re.sub(r"[^\w\s]", "", text, flags=re.U).strip().lower()
    return not norm or norm in _ACKS


def is_decision_shaped(text: str, prior_ask: str) -> tuple[bool, str]:
    """(flag, reason). Loose on purpose: see module docstring for the tradeoff."""
    if _is_pure_ack(text):
        return False, "pure acknowledgement"
    m = _LEADING.match(text)
    if m:
        return True, f"leading '{m.group(1).lower()}'"
    m = _VOCAB.search(text)
    if m:
        return True, f"ruling vocabulary '{m.group(1).lower()}'"
    if "?" in (prior_ask or "") and len(text) <= SHORT_REPLY_MAX:
        return True, "short answer to a question"
    return False, "no signal"


def covered_ids(register_text: str) -> set[str]:
    """Message ids cited in the register as `TG 1`, `TG1`, or `TG 1/2` lists."""
    out: set[str] = set()
    for m in _COVER_RE.finditer(register_text):
        out.update(re.findall(r"\d+", m.group(1)))
    return out


def _scan_file(path: Path) -> list[dict]:
    msgs: list[dict] = []
    prior_ask = ""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if tin._SOURCE_ID not in line and REPLY_TOOL not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "assistant":
                content = (obj.get("message") or {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == REPLY_TOOL):
                        text = (block.get("input") or {}).get("text")
                        if isinstance(text, str):
                            prior_ask = text[:PRIOR_ASK_MAX]
                continue
            for parsed in tin.inbound_messages_from_obj(obj):
                a = parsed["attrs"]
                if a.get("chat_id") != OPERATOR_CHAT_ID or not a.get("message_id"):
                    continue
                msgs.append({"id": a["message_id"], "ts": a.get("ts", ""),
                             "text": parsed["body"], "prior_ask": prior_ask})
    return msgs


def extract_messages(projects_root: Path, since: float, now: float) -> list[dict]:
    """the operator's inbound messages with ts in [since, now+skew], deduped by id.

    Raises OSError if projects_root is unreadable; single unreadable files are skipped.
    """
    ceiling = now + tin.MAX_FUTURE_SKEW_SECONDS
    seen: dict[str, dict] = {}
    for project in sorted(projects_root.iterdir()):
        try:
            if not project.is_dir():
                continue
            files = [p for p in project.glob("*.jsonl") if p.stat().st_mtime >= since]
        except OSError:
            continue
        for path in sorted(files):
            try:
                found = _scan_file(path)
            except OSError:
                continue
            for m in found:
                ts = tin._iso_to_unix(m["ts"])
                if ts is None or ts < since or ts > ceiling or m["id"] in seen:
                    continue
                m["unix"] = ts
                seen[m["id"]] = m
    return sorted(seen.values(), key=lambda m: m["unix"])


def _load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return {"flagged": set(map(str, d.get("flagged", []))), "covered": set(map(str, d.get("covered", [])))}
    except (OSError, ValueError, AttributeError):
        return {"flagged": set(), "covered": set()}


def _save_state(path: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({k: sorted(v, key=int) for k, v in state.items()}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utc(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _write_digest(state_dir: Path, date: str, cands: list[dict]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    path = state_dir / f"{date}.md"
    lines = [f"# Decision-register sweep digest {date} (private; never commit)\n"]
    for c in cands:
        lines.append(f"## TG {c['id']} — {_utc(c['unix'])}\n- reason: {c['reason']}\n"
                     f"- prior ask: {c['prior_ask'] or '(none)'}\n- text: {c['text']}\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def _file_ticket(cands: list[dict], digest: Path, date: str) -> None:
    ids = "\n".join(f"- TG {c['id']} ({_utc(c['unix'])})" for c in cands)
    ask = (f"{len(cands)} the operator message(s) look like rulings with no row in "
           f"notes/operator-decision-register.md:\n{ids}\n\n"
           f"Full text is in the local digest (not in git): {digest}\n\n"
           "Triage: for each id, either add a register row (cite `TG <id>`) or dismiss it "
           "as not a ruling; then resolve this ticket.")
    cmd = [sys.executable, str(REPO / "tools" / "ticket.py"), "file", "--requester", "orchestrator",
           "--target", "role:engineering_orchestrator", "--title",
           f"decision-register sweep {date}: {len(cands)} uncovered the operator rulings",
           "--labels", "decision-register,sweep", "--stale-after", "14d",
           "--project-id", "null",
           "--nearest-intent-ref", "maintenance:decision-register",
           "--serves", "every the operator ruling lands in the decision register so his calls can be delegated (T-5015, TG 11821)",
           "--ask", ask]
    res = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ticket.py file failed rc={res.returncode}: {res.stderr.strip()[:500]}")


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since-hours", type=float, default=26)
    ap.add_argument("--projects-root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--state-dir", type=Path,
                    default=Path.home() / ".local" / "state" / "pantheon" / "decision-sweep")
    ap.add_argument("--register", type=Path, default=REPO / "notes" / "operator-decision-register.md")
    ap.add_argument("--dry-run", action="store_true", help="count only; write nothing, file nothing")
    ap.add_argument("--no-file", action="store_true", help="write digest+state but file no ticket")
    args = ap.parse_args(argv)

    now = time.time() if now is None else now
    try:
        msgs = extract_messages(args.projects_root, now - args.since_hours * 3600, now)
        covered_text = args.register.read_text(encoding="utf-8")
    except OSError as e:
        print(f"decision-sweep: error: {e}", file=sys.stderr)
        return 2

    state = _load_state(args.state_dir / "state.json")
    reg_ids = covered_ids(covered_text)
    cands, n_covered = [], 0
    for m in msgs:
        if m["id"] in state["flagged"] or m["id"] in state["covered"]:
            continue
        ok, reason = is_decision_shaped(m["text"], m["prior_ask"])
        if not ok:
            continue
        if m["id"] in reg_ids:
            n_covered += 1
            state["covered"].add(m["id"])
            continue
        m["reason"] = reason
        cands.append(m)
    print(f"decision-sweep: messages={len(msgs)} decision-shaped-new={len(cands) + n_covered} "
          f"covered={n_covered} uncovered={len(cands)}")
    if args.dry_run:
        return 0

    date = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    if cands:
        digest = _write_digest(args.state_dir, date, cands)
        if not args.no_file:
            try:
                _file_ticket(cands, digest, date)
            except (RuntimeError, OSError) as e:
                print(f"decision-sweep: error: {e}", file=sys.stderr)
                return 1
        state["flagged"].update(c["id"] for c in cands)
    _save_state(args.state_dir / "state.json", state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
