#!/usr/bin/env python3
"""ORG-076 BT3a -- recovery-side single-store runner (T-4720).

Serves ORG-076 fleet health-and-repair (RFC-004) and D1: ONE episode store, under recovery's seat,
with ONE dedicated clone as its only writer. Seats never touch the store: they drop one
append-only sample file per sample (``tools/quota_probe_detector.py``); this runner ingests them.

Recovery steward constraints (ORG-076 section 4, T-4709 transition log, binding):
  * Runs from a DEDICATED CLONE (``--clone``, default ``~/.local/state/pantheon/org076/
    pantheon-config`` resolved from the running user's home -- never hard-coded). The unit's
    ExecStart runs the clone's OWN copy of this script, so ``_git_utils.REPO_ROOT`` == clone.
    Startup asserts that (script location, REPO_ROOT, ``--clone`` and ``git rev-parse
    --show-toplevel`` must all agree) and refuses with exit 2 otherwise.
  * The state dir is the DEFAULT (``~/.local/state/pantheon``). This module never sets
    ``PANTHEON_STATE_DIR``; ``--state-dir`` exists for tests only.
  * Heartbeat is written to ``--heartbeat-path`` (separate from the state dir).
  * Push is the store's own (seat creds, BT2 scoped-push guard, ORG-096 rebase edge); no new path.

Each cycle (``run_cycle``):
  1. ``git fetch origin main``.
  2. GUARD: refuse + page (exit 3, NO heartbeat so it goes stale) if the clone is dirty with
     anything the store cannot explain, or is ahead of origin/main with a commit the store does
     not own. "Owned" = the ``commit_sha`` of an in-flight episode, or the commit
     ``delivery_rebase_controller.find_episode_commit`` resolves for one (rebase rewrites shas; a
     crash in COMMIT_INFLIGHT leaves a commit with no persisted sha yet) -- AND, for either, the
     commit must introduce the id in a ticket's FRONTMATTER, touch only that one ticket (+ the
     README index) and have exactly one parent (``_is_store_commit``; ``log -S`` alone is unanchored). Dirty is explained only by: new ``tickets/*.md`` carrying the
     FRONTMATTER ``episode_id`` of a COMMIT_INFLIGHT episode (reconcile's cleanup removes it),
     plus ``tickets/README.md`` alongside one (``ticket.py file`` regenerates and stages the
     README index, ``ticket.py`` ``regenerate_readme``/commit pipeline).
  3. ``git merge --ff-only origin/main`` -- skipped while the store owns ahead commits (the
     rebase edge handles non-ff at push time).
  4. INGEST the drop dir in name (= chronological) order; delete each file ONLY after its
     ``observe`` result is durable. Every drop record of ANY kind refreshes the persona's ``seen``
     time (state.json) -- a ``not-measured`` record (BT3b) does only that: it is never observed.
  5. FRESHNESS (BT3b, ``tools/org076_sample_freshness.py``): per enrolled persona, "no drop record for
     > 60 min while its quota unit ran (journal)" => Unknown on ``org076-sample-freshness:<persona>``.
  6. ``reconcile`` the personas seen so far + the meta/freshness domains.
  7. SWEEPS (BT3b, ``tools/org076_reconciler.py``: stale-EMITTED / healed-under-ticket / orphans), only
     on a passed guard AND a successful fetch; bounded; fail-closed on UNKNOWN. A refused cycle returns
     at step 2 and never reaches them.
  8. META (BT3b, ``tools/org076_meta.py``): record this COMPLETED cycle (guard passed + reconcile
     finished; the list is trimmed to the trailing window) and feed the three store-liveness detectors.

Ingest crash safety. ``observe(..., sample_key=)`` persists the sample key inside the SAME atomic
store save as the counters/transition (episode_store.observe docstring). So a crash:
  * before that save  -> the file is still there, nothing was counted: replay counts it once;
  * after it, before the unlink -> replay sees the key, returns ``deduped``, and only deletes.
No double count, no lost sample, and no second write whose ordering has to be argued.
The key is the sample's ``run_id`` (the probe bucket; also dedupes two files for one bucket) or,
when absent, its unique ``sample_id``; keys are scoped to the persona's domain doc.
Other outcomes: ``busy_suppressed`` / an observe exception keep the file (and hold back that
persona's LATER files this cycle so order is preserved); a malformed/unknown-schema file is moved
to ``<drop>/rejected/`` and paged (never silently deleted, never wedges the queue); a missing drop
dir is a clean no-op; hidden writer temp files older than an hour are reaped.

Drop records: TWO accepted shapes, side by side (T-5999, ORG-076 BT5.0).
  * schema 1, kind ``quota-probe-sample`` (``tools/quota_probe_detector.py``): the BT2/BT3a/BT3b path,
    byte-for-byte unchanged -- persona enrollment ``personas``/``seen``, ``not-measured`` freshness-only
    records, ``qpd.spec_for`` routing. It is not migrated.
  * schema 2, kind ``org076-detector-sample``: the generic envelope. Body JSON: schema, kind, detector,
    domain_key, persona, account, verdict (healthy|broken|unknown), fault_id (null iff healthy), detail
    (<= 1000 chars), run_id|null, sample_id, sampled_at. Same file-name, writer (tmp -> fsync -> link,
    no-clobber) and dedupe-key (run_id, else sample_id) contract as schema 1. The runner TRUSTS NOTHING
    the writer names for routing: ``detector`` must be in the runner-side ``REGISTRY`` (detector ->
    ``DropDetector``, whose ``spec_for`` builds the ``episode_store.DetectorSpec``); ``domain_key`` must
    equal the entry's ``domain_key(persona)``; ``fault_id`` must be ``<kind>:<persona>`` with ``kind`` in
    the entry's ``fault_kinds``; the domain's store slug must not truncate. Anything else -> ``rejected/``
    + page. Schema-2 records never touch the quota enrollment (``personas``/``seen``); they enroll under
    ``detectors[<detector>][<persona>] = account`` (reconciled + swept each cycle) and, ONLY if the entry
    sets ``freshness_unit`` (opt-in), refresh ``detector_seen[<detector>][<persona>]`` for
    ``org076_sample_freshness.feed_detector``. Registry specs route through
    ``repair_evidence.REPAIR_CANDIDATES`` (watchman@codex, then recovery@claude: a codex-faulted episode routes
    other_harness to Recovery, RFC-004 s5 codex row); the harness is fixed per entry. The ingest hold key is
    still the persona: a held file (busy domain, observe error, unreadable) holds that seat's later files of
    BOTH shapes this cycle.
  ROLLOUT ORDER (Recovery steward rule): this runner must be live in the dedicated clone BEFORE any writer emits
  schema 2 -- an older runner quarantines + pages every schema-2 file. ``REGISTRY`` ships with the mail-delivery
  (T-6001) and telegram-inbound (T-6000) entries; each further BT5 detector ticket adds its entry, runner-first.
  KNOWN LIMIT (stated, not fixed): the drop dir is group ``pantheon-shared`` and the accounts ``codex`` and
  ``codex-fallback`` are NOT in that group, so a detector hosted on a codex account CANNOT write drop files.
  Such a detector needs a group grant (an operator-gate privilege change) or must report through the ORG-080
  health collector instead.
  Rollback of the schema-2 layer: revert it; with an empty registry nothing else changes. If schema-2
  detectors were enrolled, ``--forget-persona`` each seat FIRST (closes its schema-2 + schema-2 freshness
  domains), then optionally delete those ``<state_dir>/episode-store/<detector>-*.json`` files and the
  ``detectors`` / ``detector_seen`` keys of ``org076-runner/state.json`` (an older runner ignores them).

Exit codes: 0 ok; 2 clone/REPO_ROOT mismatch (or a bad ``--forget-persona`` name); 3 guard refusal
(unit fails -> OnFailure alert, ``unit_failure_alert.py`` cooldown 6h, so a wedged store is a
persistently failed unit); 4 partial failure (fetch failed, unreadable file, observe/sweep/meta error).
Exit 4 on a sustained fetch outage fires OnFailure every 5 min, which ``unit_failure_alert`` coalesces to
ONE alert per 6h cooldown per unit (proved in tools/test_org076_bt3b_carryovers.py). Honest edges: an
outage longer than 6h re-alerts every 6h; a second DISTINCT outage that starts within 6h of the first
alert is coalesced (no alert). Deliberately NO SuccessExitStatus and exit 4 is not masked.

``--forget-persona X`` (instead of ``--run``): de-enroll a seat -- removes it from ``personas`` / ``seen``
and RESOLVES its active episodes in both its readability and freshness domains (reason
``persona-forgotten``; no ticket mutation). Takes the runner lock, so it cannot race a cycle (a ``--run``
that finds the lock held skips that tick, exit 0, no heartbeat).

Pages are deduped per condition in ``<state_dir>/org076-runner/state.json`` (which also holds the
persona -> account map reconcile needs). The page callable is the store's placeholder (stderr /
journal) until BT4 wires the real path.

Uninstall leg (ORG-068 Remove), recovery side, in order:
  ``systemctl --user disable --now pantheon-org076-store-runner.timer``; remove
  ``~/.config/systemd/user/pantheon-org076-store-runner.{service,timer}`` and ``daemon-reload``;
  unset ``PANTHEON_ORG076_QUOTA_DETECTOR`` on every seat (stops new drop files); run
  ``org076_store_runner.py --forget-persona <X>`` for every enrolled seat (otherwise a seat that stops
  dropping while its quota unit still runs would read as silent forever); optionally drain
  or delete ``/var/lib/pantheon/org076/samples`` (queued files are otherwise simply left, and are
  ingested -- deduped -- if the runner is ever re-enabled); remove the state
  ``<state_dir>/episode-store/`` (includes the ``org076-store-liveness`` and ``org076-sample-freshness-*``
  domains), ``<state_dir>/delivery-rebase/``, ``<state_dir>/org076-runner/`` and the heartbeat file; tell
  the external watcher (Consultant) to stop polling ``tools/org076_heartbeat_check.py``; delete the dedicated clone; then delete this module, its tests
  (``tools/test_org076_store_runner.py``), the BT3b modules (``org076_reconciler.py``, ``org076_meta.py``,
  ``org076_sample_freshness.py``, ``org076_heartbeat_check.py`` + their tests), and the two unit files
  from ``tools/systemd/``.

Seams (BT3b): ``Config.journal`` (a ``org076_sample_freshness.JournalPort``; default ``JournalctlPort``,
needs the running account in group ``systemd-journal``) and ``Config.uid_of`` (account -> uid) are the
only injected ports besides ``es.Ports``; sweeps/meta take everything from ``Ports``/``Config``.

Tests: python3 -m pytest tools/test_org076_store_runner.py tools/test_org076_sample_drop.py \
    tools/test_org076_drop_envelope.py
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import getpass
import json
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _episode_corpus, _git_utils  # noqa: E402
from tools import episode_store as es  # noqa: E402
from tools import org076_meta, org076_ports, org076_reconciler, org076_sample_freshness  # noqa: E402
from tools import org076_mail_delivery as omd  # noqa: E402
from tools import org076_telegram_inbound as tgi  # noqa: E402
from tools import quota_probe_detector as qpd  # noqa: E402
from tools import repair_evidence  # noqa: E402

EXIT_OK, EXIT_MISMATCH, EXIT_REFUSED, EXIT_PARTIAL = 0, 2, 3, 4
MAX_SAMPLE_BYTES = 64 * 1024
STALE_TMP_SECS = 3600
REJECTED_DIR = "rejected"
ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_KINDS = frozenset((qpd.READ, qpd.CAPPED, qpd.PROBE_ERROR, qpd.CACHED_FAILURE,
                    qpd.PROBE_INVALID, qpd.EVIDENCE_MISMATCH, qpd.NOT_MEASURED))
_INFLIGHT_SHA_PHASES = (es.LOCAL_COMMIT, es.PUSH_INFLIGHT, es.PUSHED)
ENVELOPE_SCHEMA = 2
ENVELOPE_KIND = "org076-detector-sample"
VERDICTS = ("healthy", "broken", "unknown")
DETAIL_LIMIT = qpd.DETAIL_LIMIT
STORE_SLUG_LIMIT = 64          # episode_store.store_path truncates the domain slug here
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")   # detector / fault-kind names: no ':' (keys stay unambiguous)
_RESERVED_DETECTORS = frozenset((qpd.DETECTOR, org076_sample_freshness.DETECTOR, *org076_meta.DETECTORS))
_RESERVED_PREFIXES = tuple(sorted(_RESERVED_DETECTORS | {org076_sample_freshness.DOMAIN_PREFIX, org076_meta.DOMAIN}))


def _log(msg: str) -> None:
    print(f"[org076_store_runner] {msg}", file=sys.stderr)


class CloneMismatch(Exception):
    pass


class SampleError(Exception):
    pass


def default_clone() -> Path:
    return Path.home() / ".local" / "state" / "pantheon" / "org076" / "pantheon-config"


@dataclasses.dataclass
class Config:
    clone: Path
    drop: Path
    state_dir: Path | None = None       # None => episode_store default (never PANTHEON_STATE_DIR here)
    heartbeat: Path | None = None
    stale_tmp_secs: int = STALE_TMP_SECS
    journal: object = None              # org076_sample_freshness.JournalPort (None => JournalctlPort)
    uid_of: object = None               # account -> uid (None => pwd lookup)
    account: str | None = None          # OS account of THIS runner (meta-detector redactor); None => getpass
    registry: "dict | None" = None      # detector -> DropDetector (None => module REGISTRY); tests inject


@dataclasses.dataclass
class Report:
    ingested: int = 0
    deduped: int = 0
    rejected: int = 0
    kept: int = 0
    reaped: int = 0
    not_measured: int = 0
    freshness: list = dataclasses.field(default_factory=list)
    refused: str | None = None
    fetch_ok: bool = True
    sweeps: object = None   # org076_reconciler.SweepReport (None when the sweeps did not run)
    errors: list = dataclasses.field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.refused:
            return EXIT_REFUSED
        return EXIT_PARTIAL if (self.errors or not self.fetch_ok) else EXIT_OK


# --------------------------------------------------------------------------- #
# Generic drop-detector registry (T-5999, BT5.0)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class DropDetector:
    """Runner-side description of one schema-2 drop detector. The runner builds the episode-store
    ``DetectorSpec`` from this, never from the record."""
    detector: str
    subsystem: str
    harness: str                         # the harness whose fault this detector measures (routing)
    fault_kinds: frozenset               # fault_id must be "<kind>:<persona>", kind in this set
    n: int = es.DEFAULT_N
    m: int = es.DEFAULT_M
    attempts_budget: int = es.DEFAULT_ATTEMPTS_BUDGET
    freshness_unit: "str | None" = None  # opt-in: systemd user-unit template containing "{persona}"
    # T-6001 (BT5a): optional RUNNER-SIDE per-persona seat lookup, persona -> (harness, account) or None.
    # When set, the harness of the spec is ``seat_for(persona)[0]`` (``harness`` above is only the fallback),
    # and a record whose persona has no seat, or whose account differs from the seat's, is rejected. The
    # record never supplies either value. LIMIT: the lookup is static (a seat running its fallback harness
    # is routed as its primary one); a misclassification only reorders the repair candidates.
    seat_for: "Callable[[str], tuple | None] | None" = dataclasses.field(default=None, compare=False)

    def __post_init__(self):
        if not NAME_RE.match(self.detector) or self.detector in _RESERVED_DETECTORS:
            raise ValueError(f"bad or reserved drop detector name {self.detector!r}")
        if self.harness not in ("claude", "codex"):
            raise ValueError(f"{self.detector}: harness must be claude|codex, not {self.harness!r}")
        if not self.fault_kinds or not all(isinstance(k, str) and NAME_RE.match(k) for k in self.fault_kinds):
            raise ValueError(f"{self.detector}: fault_kinds must be non-empty names without ':'")
        if self.freshness_unit is not None and "{persona}" not in self.freshness_unit:
            raise ValueError(f"{self.detector}: freshness_unit must contain {{persona}}")
        for r in _RESERVED_PREFIXES:  # T-5999 review: no hyphen-prefix alias of a store-owned domain prefix
            if r.startswith(self.detector + "-") or self.detector.startswith(r + "-"):
                raise ValueError(f"{self.detector}: aliases the reserved domain prefix {r!r}")

    def domain_key(self, persona: str) -> str:
        return f"{self.detector}:{persona}"

    def harness_of(self, persona: str) -> str:
        seat = self.seat_for(persona) if self.seat_for else None
        return seat[0] if seat else self.harness

    def spec_for(self, persona: str, account: str) -> es.DetectorSpec:
        return es.DetectorSpec(
            detector=self.detector, domain_key=self.domain_key(persona), subsystem=self.subsystem,
            requester=persona, n=self.n, m=self.m, harness=self.harness_of(persona), account=account,
            candidates=repair_evidence.REPAIR_CANDIDATES,
            redactor=repair_evidence.make_default_redactor(account), attempts_budget=self.attempts_budget)


# BT5 detector tickets add entries here (runner-first rollout, see the module docstring). T-6001: the
# mail-delivery staleness detector (writer: tools/org076_mail_delivery.py, dark by default).
REGISTRY: "dict[str, DropDetector]" = {
    omd.DETECTOR: DropDetector(
        detector=omd.DETECTOR, subsystem=omd.SUBSYSTEM, harness="claude", fault_kinds=omd.FAULT_KINDS,
        n=omd.N, m=omd.M, seat_for=omd.seat_for),
    # T-6000: Telegram inbound liveness (writer: tools/org076_telegram_inbound.py, dark by default).
    tgi.DETECTOR: DropDetector(
        detector=tgi.DETECTOR, subsystem=tgi.SUBSYSTEM, harness="claude", fault_kinds=tgi.FAULT_KINDS,
        n=tgi.N, m=tgi.M, seat_for=tgi.seat_for),
}


def _registry(cfg: "Config") -> "dict[str, DropDetector]":
    return REGISTRY if cfg.registry is None else cfg.registry


# --------------------------------------------------------------------------- #
# Startup assertion
# --------------------------------------------------------------------------- #
def assert_clone(cfg: Config, *, script: Path | str | None = None, repo_root: Path | None = None,
                 git=_git_utils) -> None:
    """REPO_ROOT, this script's own checkout, ``--clone`` and the clone's git toplevel agree."""
    script_root = Path(script if script is not None else __file__).resolve().parent.parent
    root = Path(repo_root if repo_root is not None else _git_utils.REPO_ROOT).resolve()
    clone = Path(cfg.clone).resolve()
    if not (script_root == root == clone):
        raise CloneMismatch(f"script checkout {script_root}, _git_utils.REPO_ROOT {root} and --clone {clone} "
                            f"must all be the same dedicated clone")
    top = git._git("rev-parse", "--show-toplevel", cwd=clone)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != clone:
        raise CloneMismatch(f"{clone} is not a git toplevel ({(top.stderr or top.stdout).strip()[:120]})")


# --------------------------------------------------------------------------- #
# Runner state (page dedupe + persona -> account)
# --------------------------------------------------------------------------- #
def _state_path(cfg: Config) -> Path:
    return es.resolve_state_dir(cfg.state_dir) / "org076-runner" / "state.json"


def _load_state(cfg: Config) -> dict:
    try:
        data = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("personas", {})
    data.setdefault("paged", {})
    data.setdefault("seen", {})  # persona -> runner-clock epoch of the last drop record of ANY kind
    return data


def _save_state(cfg: Config, state: dict) -> None:
    es._atomic_write(_state_path(cfg), state)


def _page_once(cfg: Config, state: dict, ports: es.Ports, key: str, subject: str, body: str) -> None:
    if key in state["paged"]:
        return
    ports.page(subject, body)
    state["paged"][key] = ports.clock().astimezone(timezone.utc).isoformat()
    _save_state(cfg, state)


def _clear_paged(cfg: Config, state: dict, prefix: str) -> None:
    gone = [k for k in state["paged"] if k.startswith(prefix)]
    for k in gone:
        del state["paged"][k]
    if gone:
        _save_state(cfg, state)


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
def _episodes(cfg: Config) -> list:
    out = []
    d = es.resolve_state_dir(cfg.state_dir) / "episode-store"
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.extend(e for e in (doc.get("episodes") or {}).values() if isinstance(e, dict))
    return out


def _is_store_commit(sha: str, episode_id: str, repo: Path, git) -> bool:
    """A commit the store may own: it (i) introduces the episode_id in a ticket's FRONTMATTER,
    (ii) touches nothing but ONE ``tickets/*.md`` ticket (+ ``tickets/README.md``), and (iii) has
    exactly one parent. ``find_episode_commit`` is an unanchored ``log -S``; this is the check that
    stops a foreign commit pasting the id into a body from being treated as the store's (B1).

    WHAT IS OWNED, exactly (BT3b review note N2): ticket-CREATE commits for an in-flight episode, as
    above -- nothing else. The BT3b healed-under-ticket sweep (``org076_reconciler``) does NOT need this
    widened: it builds its commit with git plumbing on top of origin/main and pushes ``<sha>:main``
    without touching the clone's branch, index or working tree, so it can never leave a commit ahead of
    origin/main for the guard to judge. Widening is required only if a future store path COMMITS ON THE
    CLONE'S BRANCH anything other than a ticket-create (e.g. an annotate/flag commit via ticket.py in
    this clone, or a ticket create that stages more than ticket + README): then the owned-sha rule here
    must learn that exact shape (one ticket, one parent, the persisted episode link) -- narrowly."""
    if _episode_corpus._sha_introduces_in_frontmatter(sha, episode_id, repo, git) is not True:
        return False
    names = git._git("show", "--name-only", "--format=", sha, cwd=repo)
    if names.returncode != 0:
        return False
    files = [ln.strip() for ln in names.stdout.splitlines() if ln.strip()]
    tickets = [f for f in files if f.startswith("tickets/") and f.endswith(".md") and f != "tickets/README.md"]
    others = [f for f in files if f not in tickets and f != "tickets/README.md"]
    if others or len(tickets) != 1:
        return False
    par = git._git("rev-list", "--parents", "-n", "1", sha, cwd=repo)
    return par.returncode == 0 and len(par.stdout.split()) == 2


def _owned_ahead_shas(cfg: Config, repo: Path, git) -> set:
    from tools import delivery_rebase_controller as drc
    owned = set()
    for ep in _episodes(cfg):
        d = ep.get("delivery") or {}
        phase = d.get("phase")
        eid = ep["episode_id"]
        cands = set()
        if phase in _INFLIGHT_SHA_PHASES and d.get("commit_sha"):
            cands.add(d["commit_sha"])
        if phase in (es.COMMIT_INFLIGHT,) + _INFLIGHT_SHA_PHASES:
            try:
                sha = drc.find_episode_commit(eid, repo=repo, git=git)
            except Exception:  # noqa: BLE001 -- ambiguous/err => not owned => the guard refuses
                sha = None
            if sha:
                cands.add(sha)
        owned.update(c for c in cands if _is_store_commit(c, eid, repo, git))
    return owned


def _porcelain(repo: Path, git) -> "list[tuple[str, str]]":
    r = git._git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {(r.stderr or '').strip()[:200]}")
    return [(ln[:2], ln[3:].strip().strip('"')) for ln in r.stdout.splitlines() if len(ln) > 3]


def guard(cfg: Config, repo: Path, git=_git_utils) -> "tuple[str | None, bool]":
    """(refusal reason | None, store_owns_ahead_commits)."""
    inflight = [e["episode_id"] for e in _episodes(cfg)
                if (e.get("delivery") or {}).get("phase") == es.COMMIT_INFLIGHT]
    entries = _porcelain(repo, git)
    unexplained, explained_ticket = [], False
    for code, rel in entries:
        if rel == "tickets/README.md":
            continue
        if (rel.startswith("tickets/") and rel.endswith(".md") and code.strip() in ("??", "A", "AM")
                and any(es._frontmatter_has_episode(repo / rel, eid) for eid in inflight)):
            explained_ticket = True
            continue
        unexplained.append(f"{code} {rel}")
    if any(rel == "tickets/README.md" for _, rel in entries) and not explained_ticket:
        unexplained.append("tickets/README.md (no in-flight episode ticket explains it)")
    if unexplained:
        return f"clone is dirty: {', '.join(unexplained[:5])}", False
    r = git._git("rev-list", "origin/main..HEAD", cwd=repo)
    if r.returncode != 0:
        return f"cannot compute ahead commits: {(r.stderr or '').strip()[:200]}", False
    ahead = [s.strip() for s in r.stdout.splitlines() if s.strip()]
    if not ahead:
        return None, False
    foreign = [s for s in ahead if s not in _owned_ahead_shas(cfg, repo, git)]
    if foreign:
        return f"clone is ahead of origin/main with commits the store does not own: {[s[:9] for s in foreign][:5]}", False
    return None, True


# --------------------------------------------------------------------------- #
# Drop-dir ingest
# --------------------------------------------------------------------------- #
def _slug_truncates(domain_key: str) -> bool:
    return len(re.sub(r"[^a-z0-9]+", "-", domain_key.lower()).strip("-")) > STORE_SLUG_LIMIT


def _parse_envelope(d: dict, registry: dict) -> dict:
    """Schema 2 (T-5999). Every routing-relevant field is checked against the registry entry."""
    entry = registry.get(d.get("detector")) if isinstance(d.get("detector"), str) else None
    if entry is None:
        raise SampleError(f"unknown detector {d.get('detector')!r}")
    persona = d.get("persona")
    if not isinstance(persona, str) or not qpd.PERSONA_RE.match(persona):
        raise SampleError("bad persona")
    if not isinstance(d.get("account"), str) or not ACCOUNT_RE.match(d["account"]):
        raise SampleError("bad account")
    if entry.seat_for is not None:  # T-6001: harness + account are the runner's own knowledge, not the record's
        seat = entry.seat_for(persona)
        if seat is None:
            raise SampleError(f"no seat for persona {persona!r} on this runner")
        if d["account"] != seat[1]:
            raise SampleError(f"account {d['account']!r} is not this runner's account {seat[1]!r} for {persona!r}")
    if d.get("domain_key") != entry.domain_key(persona):
        raise SampleError(f"domain_key {d.get('domain_key')!r} is not {entry.domain_key(persona)!r}")
    if _slug_truncates(entry.domain_key(persona)) or (
            entry.freshness_unit and _slug_truncates(org076_sample_freshness.domain_key(persona, entry.detector))):
        raise SampleError("domain key too long for the store (slug would truncate)")
    verdict, fid = d.get("verdict"), d.get("fault_id")
    if verdict not in VERDICTS:
        raise SampleError(f"bad verdict {verdict!r}")
    if verdict == "healthy":
        if fid is not None:
            raise SampleError("healthy verdict carries a fault_id")
    else:
        kind, sep, who = fid.rpartition(":") if isinstance(fid, str) else ("", "", "")
        if not sep or who != persona or kind not in entry.fault_kinds:
            raise SampleError(f"bad fault_id {fid!r} for {entry.detector}/{persona}")
    detail = d.get("detail", "")
    if not isinstance(detail, str) or len(detail) > DETAIL_LIMIT:
        raise SampleError("bad detail")
    rid = d.get("run_id")
    if rid is not None and (not isinstance(rid, str) or not rid or len(rid) > 128):
        raise SampleError("bad run_id")
    sid = d.get("sample_id")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        raise SampleError("bad sample_id")
    if not isinstance(d.get("sampled_at"), str):
        raise SampleError("bad sampled_at")
    return d


def _envelope_verdict(d: dict):
    if d["verdict"] == "healthy":
        return es.Healthy()
    evidence = (f"detector: {d['detector']}\nverdict: {d['verdict']}\nrun_id: {d.get('run_id')}\n"
                f"persona: {d['persona']}\nsampled_at: {d['sampled_at']}\ndetail: {d.get('detail', '')}\n")
    return (es.Broken if d["verdict"] == "broken" else es.Unknown)(d["fault_id"], evidence)


def parse_sample(raw: bytes, registry: "dict | None" = None) -> dict:
    """Schema 1 (quota probe, unchanged) or schema 2 (generic envelope; ``registry`` None => REGISTRY)."""
    if len(raw) > MAX_SAMPLE_BYTES:
        raise SampleError("sample larger than 64KB")
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SampleError(f"not JSON: {exc}") from exc
    if not isinstance(d, dict):
        raise SampleError("not a JSON object")
    if d.get("schema") == ENVELOPE_SCHEMA and d.get("kind") == ENVELOPE_KIND:
        return _parse_envelope(d, REGISTRY if registry is None else registry)
    if d.get("schema") != qpd.SAMPLE_SCHEMA or d.get("kind") != qpd.SAMPLE_KIND:
        raise SampleError(f"unknown schema/kind: {d.get('schema')!r}/{d.get('kind')!r}")
    if d.get("detector") != qpd.DETECTOR:
        raise SampleError(f"unknown detector {d.get('detector')!r}")
    if not isinstance(d.get("persona"), str) or not qpd.PERSONA_RE.match(d["persona"]):
        raise SampleError("bad persona")
    if not isinstance(d.get("account"), str) or not ACCOUNT_RE.match(d["account"]):
        raise SampleError("bad account")
    if d.get("outcome_kind") not in _KINDS:
        raise SampleError(f"bad outcome_kind {d.get('outcome_kind')!r}")
    rid = d.get("run_id")
    if rid is not None and (not isinstance(rid, str) or not rid or len(rid) > 128):
        raise SampleError("bad run_id")
    sid = d.get("sample_id")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        raise SampleError("bad sample_id")
    if not isinstance(d.get("detail", ""), str):
        raise SampleError("bad detail")
    return d


def _reap_stale_tmp(cfg: Config, report: Report) -> None:
    now = time.time()
    for p in cfg.drop.iterdir():
        if not p.name.startswith(qpd.TMP_PREFIX):
            continue
        try:
            st = p.lstat()
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > cfg.stale_tmp_secs:
                p.unlink()
                report.reaped += 1
        except OSError as exc:
            _log(f"cannot reap {p.name}: {exc}")


def _reject(cfg: Config, state: dict, ports: es.Ports, path: Path, why: str, report: Report) -> None:
    rej = cfg.drop / REJECTED_DIR
    try:
        rej.mkdir(exist_ok=True)
        try:
            os.chmod(rej, 0o2775)
        except OSError:
            pass
        os.replace(path, rej / path.name)
        report.rejected += 1
        ports.page("ORG-076 drop file rejected", f"{path.name}: {why} (moved to {REJECTED_DIR}/)")
    except OSError as exc:
        report.kept += 1
        report.errors.append(f"cannot quarantine {path.name}: {exc}")
        _page_once(cfg, state, ports, f"reject:{path.name}", "ORG-076 drop file rejected, not quarantined",
                   f"{path.name}: {why}; quarantine failed: {exc}")


def _persona_from_name(name: str) -> "str | None":
    parts = name.split("__")
    return parts[1] if len(parts) >= 4 and qpd.PERSONA_RE.match(parts[1]) else None


def ingest(cfg: Config, ports: es.Ports, state: dict, report: Report,
           kill_hook: Callable[[str], None] = lambda point: None) -> None:
    if not cfg.drop.is_dir():
        return  # clean no-op
    _reap_stale_tmp(cfg, report)
    seen_now = ports.clock().timestamp()
    dirty = False
    files = sorted(p for p in cfg.drop.iterdir()
                   if p.name.endswith(".json") and not p.name.startswith(".") and p.is_file())
    held: set = set()  # personas whose later files must wait (order preserved)
    for path in files:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            who = _persona_from_name(path.name)
            if who:
                held.add(who)  # keep this persona's later files behind the unreadable one (order)
                if who in state["personas"]:  # a PRESENT file proves the seat's drop writes work
                    state["seen"][who] = seen_now
                    dirty = True
            report.kept += 1
            report.errors.append(f"unreadable {path.name}: {exc}")
            _page_once(cfg, state, ports, f"unreadable:{path.name}", "ORG-076 drop file unreadable",
                       f"{path.name}: {exc}")
            continue
        try:
            sample = parse_sample(raw, _registry(cfg))
        except SampleError as exc:
            who = _persona_from_name(path.name)
            if who in state["personas"] and not _identifiably_not_schema1(raw):
                state["seen"][who] = seen_now
                dirty = True
            _reject(cfg, state, ports, path, str(exc), report)
            continue
        persona = sample["persona"]
        if sample["schema"] == ENVELOPE_SCHEMA:  # T-5999: never touches the quota personas/seen maps
            entry = _registry(cfg)[sample["detector"]]
            alias = _alias_conflict(cfg, entry, persona)
            if alias:
                _reject(cfg, state, ports, path, alias, report)
                continue
            enrolled = state.setdefault("detectors", {}).setdefault(entry.detector, {})
            if entry.freshness_unit:
                state.setdefault("detector_seen", {}).setdefault(entry.detector, {})[persona] = seen_now
                dirty = True
            if persona in held:
                report.kept += 1
                continue
            if enrolled.get(persona) != sample["account"]:
                enrolled[persona] = sample["account"]
                _save_state(cfg, state)
            _observe_and_unlink(cfg, ports, state, report, path, persona, held, kill_hook,
                                lambda e=entry, s=sample: e.spec_for(s["persona"], s["account"]),
                                lambda s=sample: _envelope_verdict(s), sample.get("run_id") or sample["sample_id"])
            continue
        state["seen"][persona] = seen_now  # freshness: a drop record of ANY kind arrived (BT3b)
        dirty = True
        if sample["outcome_kind"] == qpd.NOT_MEASURED:
            # "the quota unit ran, nothing to measure": counts for freshness ONLY, never observed.
            state["personas"][persona] = sample["account"]
            _save_state(cfg, state)  # durable BEFORE the unlink (a crash replays it harmlessly)
            dirty = False
            path.unlink()
            report.not_measured += 1
            continue
        if persona in held:
            report.kept += 1
            continue
        binding = SimpleNamespace(persona=persona, account=sample["account"])
        if state["personas"].get(persona) != sample["account"]:
            state["personas"][persona] = sample["account"]
            _save_state(cfg, state)
        outcome = qpd.ProbeOutcome(sample["outcome_kind"], sample.get("run_id"), sample.get("detail", ""))
        _observe_and_unlink(cfg, ports, state, report, path, persona, held, kill_hook,
                            lambda b=binding: qpd.spec_for(b),
                            lambda o=outcome, p=persona: qpd.outcome_to_verdict(o, p),
                            sample.get("run_id") or sample["sample_id"])
    if dirty:
        _save_state(cfg, state)
    if not held:
        _clear_paged(cfg, state, "observe:")


def _identifiably_not_schema1(raw: bytes) -> bool:
    """T-5999 review (LOW): a REJECTED file refreshes the quota ``seen`` time only if it may be a quota
    (schema-1) record. A JSON object carrying any other ``schema`` (a schema-2 writer's bad file) must
    not mask a silent quota writer. Unparseable bytes keep the BT3b behaviour (a present file proves the
    seat's drop writes work; ``test_a_rejected_file_from_an_enrolled_seat_still_proves_its_writes_work``)."""
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(d, dict) and "schema" in d and d["schema"] != qpd.SAMPLE_SCHEMA


def _alias_conflict(cfg: Config, entry: "DropDetector", persona: str) -> "str | None":
    """T-5999 review (MED), runner pre-check: the store file a schema-2 record would write (its domain and,
    if opted in, its freshness domain) already holds a DIFFERENT domain => reject. ``episode_store._load``
    fails closed on the same condition store-wide; this keeps the bad record out of the queue (quarantine +
    page) instead of failing observe every cycle. An unreadable existing file is left to observe."""
    keys = [entry.domain_key(persona)]
    if entry.freshness_unit:
        keys.append(org076_sample_freshness.domain_key(persona, entry.detector))
    for key in keys:
        p = es.store_path(key, cfg.state_dir)
        try:
            held = json.loads(p.read_text(encoding="utf-8")).get("domain_key")
        except (OSError, ValueError, AttributeError):
            continue
        if held != key:
            return f"domain {key!r} aliases the existing store file {p.name} (holds {held!r})"
    return None


def _observe_and_unlink(cfg: Config, ports: es.Ports, state: dict, report: Report, path: Path, persona: str,
                        held: set, kill_hook: Callable[[str], None], make_spec: Callable, make_verdict: Callable,
                        sample_key: str) -> None:
    """observe -> (durable) -> unlink, shared by both record shapes. Spec and verdict are built INSIDE the
    try, so a failure there keeps the file and holds the persona exactly like an observe exception."""
    try:
        result = es.observe(make_spec(), make_verdict(), ports=ports, state_dir=cfg.state_dir,
                            sample_key=sample_key)
    except Exception as exc:  # noqa: BLE001 -- keep the file; one bad domain must not stop the rest
        held.add(persona)
        report.kept += 1
        report.errors.append(f"observe {path.name}: {type(exc).__name__}: {exc}")
        _page_once(cfg, state, ports, f"observe:{persona}", "ORG-076 store ingest failed",
                   f"{persona}: {type(exc).__name__}: {exc}")
        return
    if result.status == "busy_suppressed":
        held.add(persona)
        report.kept += 1
        return
    kill_hook("after_observe_before_unlink")  # tests: os._exit here == the crash window
    path.unlink()
    if result.status == "deduped":
        report.deduped += 1
    else:
        report.ingested += 1


def _registry_specs(cfg: Config, state: dict, me: str) -> list:
    """Episode specs of every ENROLLED schema-2 (detector, persona) whose detector is still registered,
    plus the opted-in freshness specs (reconcile + sweeps)."""
    reg, out = _registry(cfg), []
    for det, enrolled in sorted((state.get("detectors") or {}).items()):
        entry = reg.get(det)
        if entry is None:
            continue  # de-registered: its domains stop reconciling; --forget-persona closes them
        for persona, account in sorted(enrolled.items()):
            out.append(entry.spec_for(persona, account))
            if entry.freshness_unit:
                out.append(org076_sample_freshness.spec_for(persona, me, detector=det))
    return out


def _feed_registry_freshness(cfg: Config, ports: es.Ports, state: dict, report: Report, me: str) -> None:
    """Per-detector, OPT-IN freshness (T-5999): only entries with ``freshness_unit``."""
    reg = _registry(cfg)
    for det, enrolled in sorted((state.get("detectors") or {}).items()):
        entry = reg.get(det)
        if entry is None or not entry.freshness_unit or not enrolled:
            continue
        try:
            got = org076_sample_freshness.feed_detector(
                det, entry.freshness_unit, enrolled, state.setdefault("detector_seen", {}).setdefault(det, {}),
                ports=ports, state_dir=cfg.state_dir, now_epoch=ports.clock().timestamp(), runner_account=me,
                journal=cfg.journal or org076_sample_freshness.JournalctlPort(),
                uid_of=cfg.uid_of or org076_sample_freshness.resolve_uid, errors=report.errors)
            report.freshness += [(f"{det}/{p}", st) for p, st in got]
            _save_state(cfg, state)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"freshness {det}: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def run_cycle(cfg: Config, ports: es.Ports, *, kill_hook: Callable[[str], None] = lambda point: None) -> Report:
    report = Report()
    repo = Path(cfg.clone)
    # ONE token per runner cycle: every observe()/reconcile() below counts the verify-loop at most once.
    ports = dataclasses.replace(ports, cycle_token=uuid.uuid4().hex)
    git = ports.git
    state = _load_state(cfg)
    cycle_start = ports.clock().timestamp()
    fetch = git._git("fetch", "origin", "main", cwd=repo)
    if fetch.returncode != 0:
        report.fetch_ok = False
        _log(f"fetch failed (continuing with the last known origin/main): {(fetch.stderr or '').strip()[:200]}")
    try:
        reason, owns_ahead = guard(cfg, repo, git)
    except RuntimeError as exc:
        reason, owns_ahead = str(exc), False
    if reason:
        report.refused = reason
        _page_once(cfg, state, ports, "guard", "ORG-076 store runner refused: dedicated clone not clean", reason)
        return report  # NO heartbeat: a wedged store must read stale (RFC-004 section 6.9)
    _clear_paged(cfg, state, "guard")
    if not owns_ahead:
        merge = git._git("merge", "--ff-only", "origin/main", cwd=repo)
        if merge.returncode != 0:
            report.refused = f"ff-only merge failed: {(merge.stderr or merge.stdout).strip()[:200]}"
            _page_once(cfg, state, ports, "guard", "ORG-076 store runner refused: ff-only pull failed",
                       report.refused)
            return report
    ingest(cfg, ports, state, report, kill_hook)
    me = cfg.account or getpass.getuser()
    meta_specs = org076_meta.specs(me)
    specs = [qpd.spec_for(SimpleNamespace(persona=p, account=a)) for p, a in sorted(state["personas"].items())]
    specs += [org076_sample_freshness.spec_for(p, me) for p in sorted(state["personas"])]
    specs += _registry_specs(cfg, state, me)
    specs += meta_specs
    try:  # per-seat freshness (BT3b); a failure here is a partial failure, never a crash
        report.freshness = org076_sample_freshness.feed_all(
            state, ports=ports, state_dir=cfg.state_dir, now_epoch=ports.clock().timestamp(), runner_account=me,
            journal=cfg.journal or org076_sample_freshness.JournalctlPort(),
            uid_of=cfg.uid_of or org076_sample_freshness.resolve_uid, errors=report.errors)
        _save_state(cfg, state)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"freshness: {type(exc).__name__}: {exc}")
    _feed_registry_freshness(cfg, ports, state, report, me)
    rec = es.reconcile(specs, ports=ports, state_dir=cfg.state_dir)
    for domain, why in getattr(rec, "corrupt", None) or []:  # T-5999: skipped, not a crash; page once per domain
        report.errors.append(f"reconcile {domain}: {why}")
        _page_once(cfg, state, ports, f"corrupt:{domain}", "ORG-076 store domain corrupt or aliased", f"{domain}: {why}")
    if report.fetch_ok:  # sweeps judge origin/main: never on a stale view (fail closed)
        report.sweeps = org076_reconciler.run_sweeps(specs, ports=ports, state_dir=cfg.state_dir)
        report.errors.extend(report.sweeps.errors)
    # META (BT3b): only a cycle that passed the guard AND finished reconcile counts as completed.
    now = ports.clock().timestamp()
    org076_meta.record_cycle(state, now)
    _save_state(cfg, state)
    try:
        org076_meta.feed(meta_specs, state=state, drop=cfg.drop, cycle_start_epoch=cycle_start, now_epoch=now,
                         heartbeat=es.heartbeat_path(cfg.state_dir, ports.heartbeat_path), ports=ports,
                         state_dir=cfg.state_dir)
    except Exception as exc:  # noqa: BLE001 -- the meta layer must not stop the cycle's own result
        report.errors.append(f"meta-detector: {type(exc).__name__}: {exc}")
    return report


def build_ports(cfg: Config, page=None, liveness=None) -> es.Ports:
    """Production ports (T-5998): the real page route (``org076_ports.make_page``) and the real seat
    liveness (``org076_ports.make_liveness``, health export; fails toward not-live). Tests inject fakes."""
    page = org076_ports.make_page() if page is None else page
    liveness = org076_ports.make_liveness() if liveness is None else liveness
    edge = es.make_rebase_edge(repo=cfg.clone, state_dir=cfg.state_dir, page=page)
    return es.Ports(repo=cfg.clone, on_nonff=edge.on_nonff, on_recover=edge.on_recover, page=page,
                    liveness=liveness, heartbeat_path=cfg.heartbeat)


class RunnerBusy(Exception):
    pass


@contextlib.contextmanager
def runner_lock(cfg: Config, *, blocking: bool, timeout: float = 30.0):
    """One runner operation at a time (a cycle OR a ``--forget-persona``): they share state.json."""
    lock = _state_path(cfg).with_name("runner.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + (timeout if blocking else 0.0)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunnerBusy() from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def forget_persona(cfg: Config, ports: es.Ports, persona: str) -> int:
    """De-enroll a seat (Uninstall leg): drop its enrollment and resolve its active episodes."""
    if not qpd.PERSONA_RE.match(persona or ""):
        _log(f"bad persona name {persona!r}")
        return EXIT_MISMATCH
    state = _load_state(cfg)
    known = persona in state["personas"] or persona in state["seen"]
    state["personas"].pop(persona, None)
    state["seen"].pop(persona, None)
    v2_domains = []  # T-5999: the persona's schema-2 enrollments (registered or not) + their freshness domains
    for det, enrolled in sorted((state.get("detectors") or {}).items()):
        if enrolled.pop(persona, None) is not None:
            known = True
            v2_domains += [f"{det}:{persona}", org076_sample_freshness.domain_key(persona, det)]
        (state.get("detector_seen") or {}).get(det, {}).pop(persona, None)
    for k in [k for k in state["paged"] if persona in k]:
        del state["paged"][k]
    _save_state(cfg, state)
    closed = []
    failed = []
    for dom in (f"{qpd.DETECTOR}:{persona}", org076_sample_freshness.domain_key(persona), *v2_domains):
        try:
            closed += es.close_domain_episodes(dom, "persona-forgotten", ports=ports, state_dir=cfg.state_dir)
        except es.StoreCorruptionError as exc:  # T-5999: an aliased/corrupt file must not stop the others
            failed.append(dom)
            _log(f"cannot close {dom}: {exc}")
    _log(f"forgot {persona}: {'was enrolled' if known else 'was not enrolled'}; closed episodes {closed}")
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="store_true", help="run one cycle")
    ap.add_argument("--forget-persona", default=None, metavar="PERSONA",
                    help="de-enroll a seat (Uninstall leg) instead of running a cycle")
    ap.add_argument("--clone", default=None, help="dedicated clone (default ~/.local/state/pantheon/org076/pantheon-config)")
    ap.add_argument("--drop-dir", default=None, help=f"sample drop dir (default ${qpd.ENV_DROP_DIR} or {qpd.DEFAULT_DROP_DIR})")
    ap.add_argument("--state-dir", default=None, help="TEST ONLY; the unit uses the default state dir")
    ap.add_argument("--heartbeat-path", default=None, help="heartbeat file (separate from the state dir)")
    args = ap.parse_args(argv)
    if bool(args.run) == bool(args.forget_persona):
        ap.error("exactly one of --run / --forget-persona is required")
    cfg = Config(clone=Path(args.clone) if args.clone else default_clone(), drop=qpd.drop_dir(args.drop_dir),
                 state_dir=Path(args.state_dir) if args.state_dir else None,
                 heartbeat=Path(args.heartbeat_path) if args.heartbeat_path else None)
    try:
        assert_clone(cfg)
    except CloneMismatch as exc:
        _log(f"REFUSING to run: {exc}")
        return EXIT_MISMATCH
    try:
        with runner_lock(cfg, blocking=bool(args.forget_persona)):
            if args.forget_persona:
                return forget_persona(cfg, build_ports(cfg), args.forget_persona)
            report = run_cycle(cfg, build_ports(cfg))
    except RunnerBusy:
        _log("another runner operation holds the runner lock; skipping this tick" if args.run
             else "runner lock busy for 30s; try again")
        return EXIT_OK if args.run else EXIT_PARTIAL
    _log(f"cycle: {dataclasses.asdict(report)}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
