"""Tri-state committed-corpus lookup of an ORG-076 ``episode_id`` (T-4707 / BT1).

RFC-004 §2: queue membership is "committed", never the working tree and never
``ls tickets/inbox/``. This is the single query shared by ``tools/ticket.py``
(CLI uniqueness refusal) and ``tools/episode_store.py`` (idempotent-create check
and startup reconciliation).

The result is TRI-STATE so a failed query is never read as "absent":

  FOUND    a committed ticket carries ``episode_id: <id>`` -- on origin/main
           (``where="origin"``) or only in local-ahead commits
           (``where="local-ahead"``, with the introducing ``sha``).
  ABSENT   the query ran cleanly and found nothing.
  UNKNOWN  fetch failed / bad ref / git error. Callers fail CLOSED (do not
           emit, retry next cycle). ``tools/delivery_rebase_controller.py``'s
           ``_find_introducing_commits`` returns [] on error, which is why this
           module re-implements the query instead of reusing it.

Origin membership is a tree-tip exact-line grep (anchored ``git grep -E``), so a
prefix-colliding id never matches and a ticket that was removed from the tree
no longer counts (the BT3 orphan case). Local-ahead uses the same pickaxe
(``log -S`` over ``origin/main..HEAD``) as the rebase controller, because the
introducing commit sha is needed.
"""
from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _git_utils  # noqa: E402

FOUND = "FOUND"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class Lookup:
    status: str                 # FOUND | ABSENT | UNKNOWN
    where: str = ""             # "origin" | "local-ahead" (FOUND only)
    sha: str | None = None      # introducing commit (local-ahead FOUND)
    detail: str = ""


def episode_needle(episode_id: str) -> str:
    return f"episode_id: {episode_id}"


_OPEN_FENCE = re.compile(r"---[ \t]*\r?\n")
_CLOSE_FENCE = re.compile(r"\n---[ \t]*(?:\r?\n|$)")


def frontmatter_has_episode(text: str, episode_id: str) -> bool:
    """True iff ``text`` OPENS with a ``---`` fence (optional BOM) and its FRONTMATTER block, up to
    the closing ``---`` fence (LF or CRLF; may sit at EOF), carries a whole-line
    ``episode_id: <id>``. No frontmatter, an unterminated block, or a body line with the same text
    never counts (recovery steward note (a) / BT3a review B3, T-4720): the ticket's Context may paste it."""
    if text.startswith("\ufeff"):
        text = text[1:]
    opened = _OPEN_FENCE.match(text)
    if not opened:
        return False
    rest = "\n" + text[opened.end():]  # leading \n lets an immediately-closing fence match
    closed = _CLOSE_FENCE.search(rest)
    if not closed:
        return False
    needle = episode_needle(episode_id)
    return any(line.rstrip() == needle for line in rest[:closed.start()].splitlines())


def _show(git, repo: Path, spec: str):
    return git._git("show", spec, cwd=repo)


def _sha_introduces_in_frontmatter(sha: str, episode_id: str, repo: Path, git) -> "bool | None":
    """Does commit ``sha`` leave a ticket whose FRONTMATTER carries the id? None = git error."""
    r = git._git("show", "--name-only", "--format=", sha, "--", "tickets/", cwd=repo)
    if r.returncode != 0:
        return None
    for rel in r.stdout.splitlines():
        rel = rel.strip()
        if not rel.endswith(".md") or "README" in rel:
            continue
        got = _show(git, repo, f"{sha}:{rel}")
        if got.returncode == 0 and frontmatter_has_episode(got.stdout, episode_id):
            return True
    return False


def lookup_episode(episode_id: str, *, repo: Path, fetch: bool = True,
                   git=_git_utils) -> Lookup:
    """Where (if anywhere) is ``episode_id`` COMMITTED? Never raises."""
    try:
        if fetch:
            res = git._git("fetch", "origin", "main", cwd=repo)
            if res.returncode != 0:
                return Lookup(UNKNOWN, detail=f"fetch failed: {(res.stderr or res.stdout).strip()[:200]}")
        if git._git("rev-parse", "--verify", "-q", "origin/main", cwd=repo).returncode != 0:
            return Lookup(UNKNOWN, detail="origin/main not resolvable")
        needle = episode_needle(episode_id)
        pattern = "^" + re.escape(needle) + "$"
        # Candidate paths by exact whole-line grep, then ANCHOR to the frontmatter block.
        res = git._git("grep", "-l", "-E", "-e", pattern, "origin/main", "--", "tickets/", cwd=repo)
        if res.returncode not in (0, 1):
            return Lookup(UNKNOWN, detail=f"git grep rc={res.returncode}: {res.stderr.strip()[:200]}")
        for hit in (res.stdout.splitlines() if res.returncode == 0 else []):
            hit = hit.strip()  # "origin/main:tickets/inbox/T-1.md"
            if not hit:
                continue
            got = _show(git, repo, hit)
            if got.returncode != 0:
                return Lookup(UNKNOWN, detail=f"git show {hit} rc={got.returncode}")
            if frontmatter_has_episode(got.stdout, episode_id):
                return Lookup(FOUND, where="origin", detail=hit)
        res = git._git("log", "--format=%H", "-S", needle, "origin/main..HEAD", "--", "tickets/", cwd=repo)
        if res.returncode != 0:
            return Lookup(UNKNOWN, detail=f"git log rc={res.returncode}: {res.stderr.strip()[:200]}")
        shas = []
        for sha in (s.strip() for s in res.stdout.splitlines() if s.strip()):
            ok = _sha_introduces_in_frontmatter(sha, episode_id, repo, git)
            if ok is None:
                return Lookup(UNKNOWN, detail=f"git show {sha} failed")
            if ok:
                shas.append(sha)
        if shas:
            # Oldest introducing commit; a second one would be a duplicate-emission bug, surfaced by lint.
            return Lookup(FOUND, where="local-ahead", sha=shas[-1])
        return Lookup(ABSENT)
    except Exception as exc:  # noqa: BLE001 -- fail closed
        return Lookup(UNKNOWN, detail=f"{type(exc).__name__}: {exc}")
