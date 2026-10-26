"""tools/staged_content_gate.py — fail at the WRITER, not in the next nightly (T-4402).

THE FAILURE THIS EXISTS FOR, measured: the scheduled CI run was red every day from
2026-09-08 to 09-27 and nobody noticed. Two of its failures were CONTENT-COUPLED tests,
tests that read live repo data rather than code:

  * tools/test_ticket_purpose.py::test_initial_backfill_never_infers_project_from_repo_or_persona
    -- a non-done ticket whose `project_id` is not justified (no exact-label match, no
    `ticket:` chain hop to a same-project ancestor). T-3986 was the instance.
  * tools/test_watch_runner.py::test_every_active_topic_has_policy_entry and
    ::test_every_active_topic_in_the_real_topics_file_is_registered -- an ACTIVE topic in
    knowledge/watch/topics.md with no TOPIC_POLICY entry in tools/watch_runner.py.

Both are properties of CONTENT that a plain commit can break. This module evaluates the
same predicates the tests use, against the STAGED blobs, so the commit that would turn the
nightly red is refused instead. `.githooks/pre-commit` is the caller (a thin adapter, the
`main_guard_lint` / `ledger_placement_lint` shape); the readers are injected so the logic
runs without a git repo.

WHY A HOOK AND NOT ticket.py: `ticket.py file` / `purpose` / `label remove` already enforce
the ticket invariant at write time since T-4090 (they share `tp.project_id_justified`, the
predicate used here). What that cannot see is a write that never goes through ticket.py --
a hand edit, a migration -- which is how T-4080's 8 unjustified tickets reached main. There
is no tool writer at all for topics.md (operator-curated, never auto-edited, D2), so the
hook is the only writer-side gate that topics.md can have.

BLOCKING RULE: only a violation INTRODUCED by the commit blocks. The staged result is
compared with HEAD's copy of the same path; if the path is absent at HEAD, with the RENAME
SOURCE git reports for it (`git diff --cached -M --name-status`); if there is neither, the
file is new and every violation is introduced. A violation already present in HEAD is
main's existing red, surfaced by the nightly watcher (tools/ci_nightly_watch.py) -- it must
not lock unrelated operations on that ticket (`annotate`, `claim`, `resolve`). The rename
lookup is load-bearing: the hook's staged list includes renames (T-4451), and `ticket.py
claim` moves inbox/ -> active/, so HEAD:<new path> does not exist; without the source lookup
a pre-existing red ticket would read as newly introduced and its claim would be refused. The
hook computes the `{destination: source}` map once from `git diff --cached -M --name-status`
(`_staged_changes` in `.githooks/pre-commit`) and passes it in as `rename_source`.

POSTURE (the hook's): fail-OPEN on any infrastructural problem -- an import failure, an
unreadable blob, an unparseable staged ticket, an unloadable ticket corpus -- each warns and
skips; fail-CLOSED only on a real, introduced finding. This gates every seat's commits, so a
bug here must never brick filing (T-1102 blast radius). CI remains the backstop for
everything the hook skips.

NOT COVERED, stated rather than left to be discovered:
  * `tools/watch_runner.py` edits that REMOVE a TOPIC_POLICY entry while topics.md keeps the
    topic active -- rare, and ci.yml's `tools/**` pull_request trigger (code is PR-only) runs
    the test for it.
  * a staged ticket's chain hop is resolved against the WORKING-TREE corpus, not the staged
    one -- a commit that changes an ancestor and a descendant together is judged on the
    ancestor as it is on disk.
  * a merge that combines two individually-clean commits into a red -- pre-commit cannot see
    it; the nightly watcher can.
"""

from __future__ import annotations

import importlib
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Callable

ReadBlob = Callable[[str], "bytes | None"]
RenameSource = Callable[[str], "str | None"]

TOPICS_PATH = "knowledge/watch/topics.md"
# Non-done tickets only: `done` is the terminal state the live-corpus test exempts.
# `\Z`, not `$` (T-4541): `$` also matches before a trailing newline, and `T-N.md\n` is
# not a ticket (the corpus globs `T-*.md`).
TICKET_PATH_RE = re.compile(r"^tickets/(inbox|active)/T-\d+\.md\Z")


def _stderr_warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _head_blob(path: str, read_head: ReadBlob, rename_source: RenameSource) -> bytes | None:
    """HEAD's copy of `path`, following a rename to its source; None = new file."""
    blob = read_head(path)
    if blob is not None:
        return blob
    source = rename_source(path)
    return read_head(source) if source else None


class _LazyIndex(Mapping):
    """The working-tree ticket corpus, loaded on first access only.

    Most staged tickets are justified by an exact label (or carry no project_id), so the
    ~1.4s, 4400-file load is skipped for them. A load failure is remembered and re-raised
    on each access so every chain-walk candidate fails open with the real reason."""

    def __init__(self, tickets_root: Path):
        self._root = tickets_root
        self._data: dict | None = None
        self._error: Exception | None = None

    def _load(self) -> dict:
        if self._error is not None:
            raise self._error
        if self._data is None:
            from tools import ticket_purpose as tp
            try:
                self._data = tp.load_ticket_purpose_index(self._root)
            except Exception as exc:
                self._error = exc
                raise
        return self._data

    def __getitem__(self, key):
        return self._load()[key]

    def __iter__(self) -> Iterator:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def __contains__(self, key) -> bool:
        return key in self._load()


def ticket_purpose_findings(
    staged: list[str],
    *,
    read_staged: ReadBlob,
    read_head: ReadBlob,
    rename_source: RenameSource,
    tickets_root: Path,
    warn: Callable[[str], None] = _stderr_warn,
) -> list[str]:
    """Introduced-by-this-commit `project_id` justification violations (empty = clean)."""
    targets = [(p, m.group(1)) for p in staged if (m := TICKET_PATH_RE.match(p))]
    if not targets:
        return []
    try:
        from tools import ticket_purpose as tp
    except Exception as exc:  # fail-OPEN: nothing to evaluate with
        warn(f"content-drift gate WARNING (non-blocking, fail-open): {exc.__class__.__name__}: "
             f"{exc} -- ticket_purpose unavailable, SKIPPING the ticket project_id check")
        return []

    index = _LazyIndex(tickets_root)
    findings: list[str] = []
    for path, directory in targets:
        try:
            blob = read_staged(path)
            if blob is None:
                continue
            candidate = tp.purpose_from_text(
                blob.decode("utf-8"), source=path, default_state=directory)
            if candidate.state == "done" or tp.project_id_justified(candidate, index):
                continue
            head = _head_blob(path, read_head, rename_source)
            if head is not None:
                try:
                    before = tp.purpose_from_text(
                        head.decode("utf-8"), source=f"HEAD:{path}", default_state=directory)
                except Exception:
                    before = None  # an unreadable HEAD copy proves nothing pre-existed
                if (before is not None and before.project_id == candidate.project_id
                        and not tp.project_id_justified(before, index)):
                    continue  # already red at HEAD: main's problem, not this commit's
            pid = candidate.project_id
            findings.append(
                f"{path}: project_id {pid!r} is not justified -- the ticket has no "
                f"{pid!r} label and no `ticket:` nearest_intent_ref chain reaching a ticket "
                f"that carries it (tools/test_ticket_purpose.py, "
                f"test_initial_backfill_never_infers_project_from_repo_or_persona)"
            )
        except Exception as exc:  # fail-OPEN per file: keep real findings on siblings
            warn(f"content-drift gate WARNING (non-blocking, fail-open): {path}: "
                 f"{exc.__class__.__name__}: {exc} -- SKIPPING the project_id check for "
                 f"this file, commit allowed for it")
    return findings


def _active_unregistered(blob: bytes, wr) -> set[str]:
    """Active (not paused) topic slugs in `blob` with no TOPIC_POLICY entry.

    The predicate is the test's own (`cadence != "paused"` and slug not in TOPIC_POLICY),
    not parse_topics' narrower warning rule, so the gate and the nightly can't disagree."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "topics.md"
        path.write_bytes(blob)
        topics = wr.parse_topics(path)
    return {t.slug for t in topics if t.cadence != "paused" and t.slug not in wr.TOPIC_POLICY}


def topic_policy_findings(
    staged: list[str],
    *,
    read_staged: ReadBlob,
    read_head: ReadBlob,
    rename_source: RenameSource,
    warn: Callable[[str], None] = _stderr_warn,
) -> list[str]:
    """Active topics the staged topics.md leaves unregistered that HEAD's did not.

    TOPIC_POLICY is read from the WORKING-TREE tools/watch_runner.py (imported), for both
    the staged and the HEAD side, so the comparison isolates what the topics.md change did."""
    if TOPICS_PATH not in staged:
        return []
    try:
        wr = importlib.import_module("tools.watch_runner")
        blob = read_staged(TOPICS_PATH)
        if blob is None:
            warn("content-drift gate WARNING (non-blocking, fail-open): the staged "
                 f"{TOPICS_PATH} could not be read -- SKIPPING the topic-policy check")
            return []
        introduced = _active_unregistered(blob, wr)
        head = _head_blob(TOPICS_PATH, read_head, rename_source)
        if head is not None:
            try:
                introduced -= _active_unregistered(head, wr)
            except Exception:
                pass  # an unreadable HEAD copy proves nothing pre-existed
    except Exception as exc:  # fail-OPEN
        warn(f"content-drift gate WARNING (non-blocking, fail-open): {exc.__class__.__name__}: "
             f"{exc} -- SKIPPING the topic-policy check, commit allowed")
        return []
    if not introduced:
        return []
    slugs = ", ".join(sorted(introduced))
    return [
        f"{TOPICS_PATH}: ACTIVE topic(s) with no TOPIC_POLICY entry: {slugs}. Each falls "
        f"through to _DEFAULT_POLICY and its findings are silently undelivered "
        f"(tools/test_watch_runner.py). Add a TOPIC_POLICY entry (target / labels / "
        f"interactive_required) in tools/watch_runner.py in the same change, or set the "
        f"topic's cadence to paused"
    ]


def content_gate_findings(
    staged: list[str],
    *,
    read_staged: ReadBlob,
    read_head: ReadBlob,
    rename_source: RenameSource,
    tickets_root: Path,
    warn: Callable[[str], None] = _stderr_warn,
) -> list[str]:
    """Every introduced content-drift finding for the staged set (empty = clean)."""
    return (
        ticket_purpose_findings(
            staged, read_staged=read_staged, read_head=read_head,
            rename_source=rename_source, tickets_root=tickets_root, warn=warn)
        + topic_policy_findings(
            staged, read_staged=read_staged, read_head=read_head,
            rename_source=rename_source, warn=warn)
    )
