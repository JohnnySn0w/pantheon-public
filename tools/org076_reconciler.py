"""ORG-076 BT3b -- reconciler sweeps (RFC-004 section 2b / acceptance 6.5), T-4709.

Serves ORG-076 fleet health-and-repair and D1: no episode is stranded or wrongly left open.
Wired by ``tools/org076_store_runner.py`` into each cycle AFTER ingest + ``es.reconcile``.

Three sweeps, all idempotent, fail-closed, and bounded (``MAX_MUTATIONS_PER_CYCLE`` shared; the
leftover resumes next cycle because the trigger conditions persist):

  stale-EMITTED     the repair ticket is RESOLVED (``tickets/done/`` on origin/main) but the episode
                    still measures non-healthy. Cool-down = one full open-threshold window in
                    DETECTOR-NATIVE units: ``stale_watch.nh_run`` counts consecutive non-healthy
                    samples observed AFTER the sweep first saw the ticket resolved (a healthy
                    sample resets it); at ``spec.n`` the stale episode is closed and a fresh one
                    opened (``episode_store.close_stale_and_reopen``, predecessor linked). An episode
                    whose ticket is merely still open is never touched (no second ticket per fault).
  healed-under-ticket  the episode hysteresis-closed while its ticket is still OPEN on origin: set
                    ``recovered_before_repair: true`` on that ticket (one frontmatter line + one
                    appended transition entry, built by ticket.py's OWN helpers) and leave the ticket
                    open. See "Ticket mutation" below.
  orphan            an EMITTED+VERIFIED episode whose ticket is GONE from the origin/main TREE
                    (frontmatter-anchored ``episode_id`` under ``tickets/**``; a ticket merely moved
                    to done/ or elsewhere still exists) -> ``episode_store.reemit_orphan`` under the
                    SAME episode_id. ``lookup_episode`` greps the tree tip for origin, so a deleted
                    ticket is ABSENT there (not found "in history") and the re-emit is not suppressed.

Ticket mutation (healed-under-ticket) and why the runner guard needs NO widening: the commit is built
with git plumbing against ``origin/main`` (temp index file -> ``write-tree`` -> ``commit-tree``) and
pushed as ``<sha>:main`` through the store's own scoped push (``episode_store._default_push``, normal
``git push`` so the pre-push hooks run -- no hook bypass). It never touches the clone's working tree,
index or branch, so a failed push leaves only unreachable objects and NOTHING ahead of origin/main:
``org076_store_runner._is_store_commit`` (which owns only commits that introduce an episode_id in a
ticket's frontmatter) is never asked about it. Non-ff rebuilds against the new origin/main (bounded,
``PUSH_ATTEMPTS``). Idempotency anchor = origin's own value: the flag already true -> nothing to do;
push first, THEN persist ``rbr_applied`` (a crash between the two converges next cycle).

Per-cycle git queries are bounded: one ``ls-tree`` for the ticket map, one batched ``git grep`` per
50 live episode ids for the orphan check (+ one ``show`` per hit), and one confirming
``lookup_episode`` per orphan CANDIDATE only (capped by the mutation budget). Every query's failure
returns UNKNOWN and the sweep does nothing (never "absent").

Uninstall leg (ORG-068 Remove): delete this module and ``tools/test_org076_reconciler.py``; drop the
sweep call from ``org076_store_runner.run_cycle``; the episode-record keys ``stale_watch`` /
``rbr_applied`` / ``chain_paged`` are inert without it. Tickets already flagged keep the flag.

Tests: python3 -m pytest tools/test_org076_reconciler.py
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _episode_corpus, _git_utils  # noqa: E402
from tools import episode_store as es  # noqa: E402

MAX_MUTATIONS_PER_CYCLE = 5
PUSH_ATTEMPTS = 3
GREP_CHUNK = 50
_TID_RE = re.compile(r"(?:^|/)(T-\d+)\.md$")

PushFn = Callable[[Path, str], "es.PushResult"]


@dataclasses.dataclass
class SweepReport:
    stale_reopened: int = 0
    healed_flagged: int = 0
    healed_settled: int = 0      # nothing to flag (already / ticket closed / gone)
    orphans_reemitted: int = 0
    deferred: int = 0            # work left for next cycle by the mutation cap
    skipped_unknown: int = 0
    errors: list = dataclasses.field(default_factory=list)

    @property
    def mutations(self) -> int:
        return self.stale_reopened + self.healed_flagged + self.orphans_reemitted


def _log(msg: str) -> None:
    print(f"[org076_reconciler] {msg}", file=sys.stderr)


def ticket_map(repo: Path, git) -> "dict | None":
    """{ticket_id: (state_dir, path)} for origin/main's tickets/ tree, or None on any git trouble."""
    r = git._git("ls-tree", "-r", "--name-only", "origin/main", "--", "tickets/", cwd=repo)
    if r.returncode != 0:
        return None
    out: dict = {}
    for rel in r.stdout.splitlines():
        m = _TID_RE.search(rel.strip())
        parts = rel.strip().split("/")
        if m and len(parts) == 3:
            out[m.group(1)] = (parts[1], rel.strip())
    return out


def _origin_text(repo: Path, git, rel: str) -> "str | None":
    r = git._git("show", f"origin/main:{rel}", cwd=repo)
    return r.stdout if r.returncode == 0 else None


def _plumb(repo: Path, env: dict, *args: str, stdin: "str | None" = None) -> str:
    r = subprocess.run(["git", *args], cwd=repo, env=env, input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def _build_commit(repo: Path, git, tid: str, rel: str, new_text: str, message: str) -> str:
    """A commit = origin/main + exactly ``rel`` replaced. No working tree / index / ref is touched."""
    parent = git._git("rev-parse", "origin/main", cwd=repo).stdout.strip()
    if not parent:
        raise RuntimeError("origin/main not resolvable")
    with tempfile.TemporaryDirectory(prefix="org076-idx-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        blob = _plumb(repo, env, "hash-object", "-w", "--stdin", stdin=new_text)
        _plumb(repo, env, "read-tree", parent)
        mode = _plumb(repo, env, "ls-tree", parent, "--", rel).split()[0]
        _plumb(repo, env, "update-index", "--cacheinfo", f"{mode},{blob},{rel}")
        tree = _plumb(repo, env, "write-tree")
        return _plumb(repo, env, "commit-tree", tree, "-p", parent, "-m", message)


def flag_ticket_recovered(repo: Path, tid: str, episode_id: str, *, git=_git_utils, push: PushFn,
                          actor: "str | None" = None, fetch: bool = True) -> str:
    """Set ``recovered_before_repair: true`` on origin's copy of ``tid``. Returns one of
    ``flagged`` / ``already`` / ``ticket-closed`` / ``ticket-gone`` / ``unknown:<why>`` /
    ``failed:<why>``. Never raises."""
    from tools import ticket as ticket_mod
    actor = actor or os.environ.get("PANTHEON_ACTOR", "").strip() or getpass.getuser()
    note = (f"ORG-076 reconciler: fault healed before repair (episode {episode_id}); "
            f"recovered_before_repair set -- verify-and-close, do not repair blind")
    try:
        with _git_utils.git_mutation_lock(label=f"org076 recovered_before_repair {tid}", timeout_secs=30.0):
            for attempt in range(PUSH_ATTEMPTS):
                if attempt and git._git("fetch", "origin", "main", cwd=repo).returncode != 0:
                    return "unknown:fetch failed"
                tmap = ticket_map(repo, git)
                if tmap is None:
                    return "unknown:ls-tree failed"
                hit = tmap.get(tid)
                if hit is None:
                    return "ticket-gone"
                state, rel = hit
                if state == "done":
                    return "ticket-closed"
                text = _origin_text(repo, git, rel)
                if text is None:
                    return "unknown:show failed"
                mutation = ticket_mod.recovered_before_repair_mutation(text, tid, actor, note)
                if mutation is None:
                    return "already"
                new_text, message = mutation
                sha = _build_commit(repo, git, tid, rel, new_text, message)
                res = push(repo, f"{sha}:main")
                if res.ok:
                    return "flagged"
                if not res.nonff:
                    return f"failed:{res.detail[-200:]}"
            return "failed:non-ff after retries"
    except _git_utils.GitMutationLockTimeout:
        return "failed:git mutation lock busy"
    except Exception as exc:  # noqa: BLE001 -- fail closed: no mutation claimed
        return f"failed:{type(exc).__name__}: {exc}"


def _frontmatter_ids_present(repo: Path, git, ids: Sequence[str]) -> "set | None":
    """Which of ``ids`` have a frontmatter-anchored ``episode_id:`` line in origin/main's tickets/ tree.
    Batched: one ``git grep`` per GREP_CHUNK ids. None on any git trouble."""
    found: set = set()
    for i in range(0, len(ids), GREP_CHUNK):
        chunk = list(ids[i:i + GREP_CHUNK])
        pat = "^episode_id: (" + "|".join(re.escape(c) for c in chunk) + ")$"
        r = git._git("grep", "-l", "-E", "-e", pat, "origin/main", "--", "tickets/", cwd=repo)
        if r.returncode not in (0, 1):
            return None
        for hit in (r.stdout.splitlines() if r.returncode == 0 else []):
            got = git._git("show", hit.strip(), cwd=repo)
            if got.returncode != 0:
                return None
            found.update(c for c in chunk if _episode_corpus.frontmatter_has_episode(got.stdout, c))
    return found


def run_sweeps(specs: Sequence["es.DetectorSpec"], *, ports: "es.Ports", state_dir=None,
               push: "PushFn | None" = None, max_mutations: int = MAX_MUTATIONS_PER_CYCLE) -> SweepReport:
    rep = SweepReport()
    repo = ports.resolved_repo()
    git = ports.git
    if push is None:
        push = lambda r, refspec: es._default_push(r, refspec, ports.mirror_url)  # noqa: E731
    tmap = ticket_map(repo, git)
    if tmap is None:
        rep.skipped_unknown += 1
        _log("origin ticket map unavailable; no sweep this cycle")
        return rep

    docs: list = []  # (spec, doc)
    seen = set()
    for spec in specs:
        if (spec.domain_key, spec.detector) in seen or not es.store_path(spec.domain_key, state_dir).exists():
            continue
        seen.add((spec.domain_key, spec.detector))
        try:
            docs.append((spec, es.load_domain(spec.domain_key, state_dir)))
        except es.StoreCorruptionError as exc:
            rep.errors.append(f"{spec.domain_key}: {exc}")

    def mine(spec, doc):
        return [e for e in doc["episodes"].values() if e["detector"] == spec.detector]

    # ---- healed-under-ticket
    for spec, doc in docs:
        for ep in mine(spec, doc):
            tid = ep["delivery"].get("ticket_id")
            if ep["state"] != es.RESOLVED or not ep.get("healed_under_ticket") or ep.get("rbr_applied") or not tid:
                continue
            hit = tmap.get(tid)
            if hit is None or hit[0] == "done":
                es.mark_rbr_applied(spec.domain_key, ep["episode_id"],
                                    "ticket-gone" if hit is None else "ticket-closed",
                                    ports=ports, state_dir=state_dir)
                rep.healed_settled += 1
                continue
            if rep.mutations >= max_mutations:
                rep.deferred += 1
                continue
            out = flag_ticket_recovered(repo, tid, ep["episode_id"], git=git, push=push)
            if out in ("flagged", "already"):
                es.mark_rbr_applied(spec.domain_key, ep["episode_id"], out, ports=ports, state_dir=state_dir)
                if out == "flagged":
                    rep.healed_flagged += 1
                else:
                    rep.healed_settled += 1
            elif out.startswith("unknown"):
                rep.skipped_unknown += 1
            elif out in ("ticket-closed", "ticket-gone"):
                es.mark_rbr_applied(spec.domain_key, ep["episode_id"], out, ports=ports, state_dir=state_dir)
                rep.healed_settled += 1
            else:
                rep.errors.append(f"recovered_before_repair {tid}: {out}")

    # ---- stale-EMITTED
    for spec, doc in docs:
        for ep in mine(spec, doc):
            if ep["state"] != es.EMITTED or ep["delivery"].get("phase") != es.VERIFIED:
                continue
            hit = tmap.get(ep["delivery"].get("ticket_id") or "")
            resolved = hit is not None and hit[0] == "done"
            watch = ep.get("stale_watch")
            if not resolved:
                if watch is not None:
                    es.disarm_stale_watch(spec.domain_key, ep["episode_id"], ports=ports, state_dir=state_dir)
                continue
            if watch is None:
                es.arm_stale_watch(spec.domain_key, ep["episode_id"], ports=ports, state_dir=state_dir)
                continue
            if int(watch.get("nh_run", 0)) < spec.n:
                continue
            if rep.mutations >= max_mutations:
                rep.deferred += 1
                continue
            try:
                es.close_stale_and_reopen(spec, ep["episode_id"], ports=ports, state_dir=state_dir)
                rep.stale_reopened += 1
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(f"stale reopen {ep['episode_id']}: {type(exc).__name__}: {exc}")

    # ---- orphans (batched; documents are re-read: the sweeps above may have changed them)
    live: dict = {}  # episode_id -> spec
    for spec, _old in docs:
        try:
            doc = es.load_domain(spec.domain_key, state_dir)
        except es.StoreCorruptionError:
            continue
        for ep in mine(spec, doc):
            if ep["state"] == es.EMITTED and ep["delivery"].get("phase") == es.VERIFIED:
                live[ep["episode_id"]] = spec
    if live:
        present = _frontmatter_ids_present(repo, git, sorted(live))
        if present is None:
            rep.skipped_unknown += 1
        else:
            for eid in sorted(set(live) - present):
                if rep.mutations >= max_mutations:
                    rep.deferred += 1
                    continue
                lk = _episode_corpus.lookup_episode(eid, repo=repo, fetch=False, git=git)
                if lk.status != _episode_corpus.ABSENT:
                    if lk.status == _episode_corpus.UNKNOWN:
                        rep.skipped_unknown += 1
                    continue
                try:
                    es.reemit_orphan(live[eid], eid, ports=ports, state_dir=state_dir)
                    rep.orphans_reemitted += 1
                except Exception as exc:  # noqa: BLE001
                    rep.errors.append(f"orphan re-emit {eid}: {type(exc).__name__}: {exc}")
    return rep
