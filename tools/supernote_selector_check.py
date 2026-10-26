#!/usr/bin/env python3
"""Fail loudly when the SuperNote ingest selector reports stale pages it cannot refresh.

T-4593. The gpu-ml ingest (VM210, ~/supernote-pipeline) logs `stale_no_png=N` on every run's
diff line and a `FLAG stale-set page(s) with NO P2 png: N` line when N > 0. Such a page is in
memex's stale set but has no P2 png, so it can never be re-OCR'd and nothing else notices it
(T-4462's stale-stuck flag counts only re-OCR'd pages). A failure on gpu-ml reaches no one, so
this VPS unit reads the log and fails instead; its OnFailure= alert is the standard route, and
on ANY failure it also mails archivist, the pipeline's owner (T-4878).

T-5045: it also reads the ingest's pipeline-stage failures. A stamped `FAILED: ...` /
`FAILED cmd: ...` line is a crashed ingest run, and a `FLAG ocr_failed: N` line counts pages
whose OCR failed (retried every drop, un-admitted until OCR succeeds); both used to be silent.

Fails closed: an unreachable gpu-ml, or no ingest run in the window (the 05:17Z backstop runs
daily), is also a failure, so the check can never sit silently green. Content-blind: it reads
only the ingest's count lines and the presence of FAILED lines, never their text.

T-5534: a `FLAG page-versions <kind>=N` line (winner_flip_held, reclassify_held, schema_unknown) means
the ingest HELD a page-versions element that 203 would refuse (a refusal there freezes ALL version
updates), so it needs a deliberate re-pick (apply_page_versions --allow-winner-change / --allow-reclassify).

Exit codes (precedence 4 > 1 > 5 > 6 > 2 > 0): 0 ok, 1 stale FLAG seen, 2 no ingest run in the
window, 3 gpu-ml unreachable, 4 an ingest run FAILED in the window, 5 pages failed OCR in the window,
6 page-versions element(s) held in the window.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HOST = "specialist@gpu-ml"
LOG = "~/supernote-ingest.log"
WINDOW = timedelta(hours=26)
WINDOW_TEXT = "26h"
# A line stamped later than now (beyond small gpu-ml/VPS clock skew) is not evidence of a
# recent run: a bad clock or a mangled line would otherwise read as fresh forever (T-4593
# REBOUND-1). Its FLAG count still counts, since a flag errs toward alerting.
FUTURE_SKEW = timedelta(minutes=5)
_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] (.*)$")
_DIFF = re.compile(r"diff: .*\bstale_no_png=(\d+)")
_FLAG = re.compile(r"FLAG stale-set page\(s\) with NO P2 png: (\d+)")
_OCR_FAILED = re.compile(r"FLAG ocr_failed: (\d+)")  # T-5045
_PV_HELD = re.compile(r"FLAG page-versions (winner_flip_held|reclassify_held|schema_unknown)=(\d+)")  # T-5534
_FAILED = re.compile(r"^FAILED\b")  # T-5045: `FAILED: ...` and `FAILED cmd: ...`


def fetch_log_tail(lines: int = 600) -> str:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", HOST, f"tail -n {lines} {LOG}"],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode != 0:
        raise ConnectionError(f"ssh {HOST} exited {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def assess(text: str, now: datetime) -> tuple[int, str, int]:
    """Return (exit_code, message, worst_count) for the log lines inside WINDOW."""
    runs, worst, future, failed_runs, ocr_worst, pv_worst = 0, 0, 0, 0, 0, 0
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - ts > WINDOW:
            continue
        is_future = ts - now > FUTURE_SKEW
        future += is_future
        body = m.group(2)
        d = _DIFF.search(body)
        if d:
            runs += not is_future
            worst = max(worst, int(d.group(1)))
        f = _FLAG.search(body)
        if f:
            worst = max(worst, int(f.group(1)))
        # T-5045: future-dated lines still count (a flag errs toward alerting). Counts only:
        # a FAILED line's text (exception repr, command) never leaves this function.
        failed_runs += bool(_FAILED.match(body))
        o = _OCR_FAILED.search(body)
        if o:
            ocr_worst = max(ocr_worst, int(o.group(1)))
        for v in _PV_HELD.finditer(body):   # T-5534: the kind name only; never ids
            pv_worst = max(pv_worst, int(v.group(2)))
    findings = []
    if failed_runs:
        findings.append(f"{failed_runs} ingest run(s) FAILED in the last {WINDOW_TEXT}")
    if worst:
        findings.append(f"FLAG: {worst} stale-set page(s) with no P2 png in the last {WINDOW_TEXT}")
    if ocr_worst:
        findings.append(f"ocr_failed: up to {ocr_worst} page(s) failed OCR in a run in the last {WINDOW_TEXT}")
    if pv_worst:
        findings.append(f"page-versions: up to {pv_worst} element(s) held in a run in the last {WINDOW_TEXT}")
    if not runs:
        note = f" ({future} future-dated line(s) ignored; check the gpu-ml clock)" if future else ""
        findings.append(f"no ingest run with a stale_no_png diff line in the last {WINDOW_TEXT}{note}")
    msg = "; ".join(findings)
    if failed_runs:
        return 4, msg, failed_runs
    if worst:
        return 1, msg, worst
    if ocr_worst:
        return 5, msg, ocr_worst
    if pv_worst:
        return 6, msg, pv_worst
    if not runs:
        return 2, msg, 0
    return 0, f"ok: {runs} ingest run(s), stale_no_png=0", 0


def mail_archivist(count: int, code: int = 1, msg: str = "") -> None:
    """Mail archivist, the pipeline's owner (T-4878, the operator TG 687/689: archivist owns its alerts). Every failing
    code mails: 1 FLAG (stale pages with no png), 2 no ingest run in the window, 3 gpu-ml unreachable,
    4 an ingest run FAILED, 5 pages failed OCR (T-5045). Counts only, never page ids or log text."""
    from tools import agent_role_map, mailbox

    hub = str(Path(agent_role_map.mailbox_hub()) / "archivist")
    if code == 4:
        mailbox.send(
            hub,
            f"specialist selector check (T-5045) FAILED, code 4: {msg}. {count} SuperNote ingest run(s) "
            "exited with a FAILED line in the last 26h; new pages from those runs may not have been staged (the next run re-diffs them). "
            "Check ~/supernote-ingest.log on gpu-ml.",
            "specialist", subject=f"SuperNote selector check FAILED (code 4): {count} ingest run(s) crashed",
        )
        return
    if code == 5:
        mailbox.send(
            hub,
            f"specialist selector check (T-5045) FAILED, code 5: {msg}. Up to {count} SuperNote page(s) failed "
            "OCR in a single ingest run in the last 26h. Those pages are retried every drop and stay "
            "un-admitted until OCR succeeds. Count only. Check ~/supernote-ingest.log on gpu-ml.",
            "specialist", subject=f"SuperNote: {count} page(s) failed OCR (code 5, retried every drop)",
        )
        return
    if code == 6:
        mailbox.send(
            hub,
            f"specialist selector check (T-5534) FAILED, code 6: {msg}. The ingest HELD page-versions element(s) that "
            "203 would refuse (a winner flip, an equivalent ink with a distinct record, or an unknown schema). The "
            "rest still flows, but those pages need a deliberate re-pick on 203 (apply_page_versions "
            "--allow-winner-change / --allow-reclassify after checking). Counts only. Kinds: ~/supernote-ingest.log on gpu-ml.",
            "specialist", subject=f"SuperNote: {count} page-versions element(s) held (code 6, needs a re-pick)",
        )
        return
    if code != 1:
        what = "no SuperNote ingest run in the window" if code == 2 else "cannot read the ingest log on gpu-ml"
        mailbox.send(
            str(Path(agent_role_map.mailbox_hub()) / "archivist"),
            f"specialist selector check (T-4593/T-4878) FAILED, code {code}: {msg}. The ingest may not have "
            "run (missed CT205 poke + missed 05:17Z backstop) or gpu-ml/SSH is down. Check run_ingest on gpu-ml.",
            "specialist", subject=f"SuperNote selector check FAILED (code {code}): {what}",
        )
        return
    payload = (
        f"specialist selector check (T-4593): the SuperNote ingest reports {count} stale-set "
        "page(s) with NO P2 png in the last 26h. Those pages cannot be re-OCR'd, so they "
        "sit in the stale set until a surviving copy is re-rendered. Count only; the "
        "page ids are in your stale set. See "
        "knowledge/episodic/2026-10-05-t4593-supernote-shared-page-png/note.md."
    )
    mailbox.send(
        str(Path(agent_role_map.mailbox_hub()) / "archivist"), payload, "specialist",
        subject=f"SuperNote: {count} stale-set page(s) have no P2 png (unrefreshable)",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-mail", action="store_true", help="report only; never mail")
    a = ap.parse_args(argv)
    try:
        text = fetch_log_tail()
    except Exception as e:
        # gpu-ml is reached over the tailnet: if swarm itself is off it, that is the fault, not
        # gpu-ml or the ingest (T-5385; the 2026-10-23 key expiry mailed archivist a false code 3).
        from tools import tailnet_self

        off = tailnet_self.off_tailnet()
        if off:
            if not a.no_mail:
                tailnet_self.report_off_tailnet("supernote-selector-check", off, detail=str(e))
            return 0
        code, msg, worst = 3, f"cannot read the ingest log: {e}", 0
    else:
        from tools import tailnet_self

        tailnet_self.note_reachable()
        code, msg, worst = assess(text, datetime.now(timezone.utc))
    print(f"supernote-selector-check: {msg}", file=sys.stderr if code else sys.stdout)
    if code and not a.no_mail:
        try:
            mail_archivist(worst, code, msg)
        except Exception as e:  # the unit still fails; OnFailure= carries it
            print(f"supernote-selector-check: mail to archivist failed: {e!r}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
