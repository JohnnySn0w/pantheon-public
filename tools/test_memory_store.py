#!/usr/bin/env python3
"""Tests for the disposable ORG-058 memory store (T-3198) and the additive
``memory_envelope.envelope_to_json``.

Covers: ``envelope_to_json`` as the exact inverse of ``envelope_from_json`` on
every envelope fixture; ``init_store`` manifest declarations (explicit-empty vs
missing = unknown); admission refusals as typed values with zero mutation;
version-file immutability and the event-log fold; per-version payloads verified
on read; the real FTS5 + subject projections (built only from the retrievable
set, generation manifests with per-file sha256, failed build leaves CURRENT,
atomic ``os.replace`` swap, one-prior-generation rollback, filtered vs raw
lookups); write-once receipts.

The builders here are also imported by ``test_memory_lifecycle_ops``. All test
content is synthetic and non-personal.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import tempfile
import multiprocessing
import queue
import threading
import time
import unicodedata
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_envelope as me  # noqa: E402
import memory_store as ms  # noqa: E402
from memory_store import LifecycleRefusal as LR  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "memory_envelope")

LC_REV = "lc-rev-1"
SRC_SYSTEM = "file-src"
SRC_OBJECT = "standup-notes.md"
NOW = "2026-09-28T12:00:00Z"
TUESDAY = "synthetic note: the standup moved to tuesday"
THURSDAY = "synthetic note: the standup moved to thursday"
SUBJECT = "team-standup"

SI1 = me.SourceIntegrity("e1", "srev-1", "sha256:sd-e1", "reviewed", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)
SI2 = me.SourceIntegrity("e2", "srev-2", "sha256:sd-e2", "untrusted", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)


# --- shared synthetic builders ---------------------------------------------
def trust_policy() -> me.TrustPolicy:
    """Fixture lattice untrusted <= reviewed (a FIXTURE, not Pantheon's taxonomy)."""
    return me.TrustPolicy(
        policy_ref="P-lattice", revision="rev-1", digest="sha256:d",
        labels=frozenset({"untrusted", "reviewed"}),
        order_pairs=frozenset({("untrusted", "reviewed")}),
        conservative_state="untrusted",
        purpose_floors={"companionship": "untrusted", "assertion": "reviewed"},
        privacy_labels=frozenset({"internal", "personal"}),
        privacy_order_pairs=frozenset({("internal", "personal")}),
    )


def source_ref(
    *, digest: str = "sha256:" + "0" * 64, object_id: str = SRC_OBJECT,
    revision: str = "r1", system: str = SRC_SYSTEM,
) -> me.SourceRef:
    return me.SourceRef(system, object_id, revision, digest, "2026-09-01T00:00:00Z", "sha256")


def store_policy(revision: str = LC_REV) -> ms.LifecyclePolicy:
    """Minimal lifecycle policy to admit under (a FIXTURE). The lifecycle-op
    suite admits under its own richer policy; each store pins its own."""
    return ms.make_lifecycle_policy(
        "LP-store-test", revision, correction_policies={}, forgetting_policies={}
    )


def make_env(
    memory_id: str = "m-1", version_id: str = "v-1", payload: str = TUESDAY, **kw
) -> me.MemoryEnvelope:
    c1 = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "untrusted", None)
    d = dict(
        memory_id=memory_id, version_id=version_id, memory_class=me.MemoryClass.EPISODIC,
        lifecycle_state=me.LifecycleState.RETAINED_DERIVED, canonical_source_ref=source_ref(),
        created_at="2026-09-01T00:00:00Z", observed_at="2026-09-01T00:00:00Z",
        valid_from="2026-09-01T00:00:00Z", valid_to=None, owner="team-bot", subjects=(SUBJECT,),
        audience="team", privacy_class="internal", confidence_kind=me.ConfidenceKind.OBSERVED,
        confidence=0.9, supporting_refs=("e1", "e2"), contradicting_refs=(), retention_until=None,
        correction_policy="corr-std", forgetting_policy="forget-std", retrieval_authority="team",
        action_authority_ref=None, supersedes=(), superseded_by=(), last_consolidated_at=None,
        payload_ref=f"payload://{memory_id}/{version_id}", payload_digest=ms.payload_digest(payload),
        policy_revision=LC_REV, trust_policy_ref="P-lattice", trust_policy_revision="rev-1",
        trust_policy_digest="sha256:d", source_integrity=(SI1, SI2), claim_trust=(c1,),
        inherited_trust_floor="untrusted", effective_trust_floor="untrusted",
        trust_adjudication_receipt_refs=(),
    )
    d.update(kw)
    return me.MemoryEnvelope(**d)


def admit(store: ms.MemoryStore, memory_id: str = "m-1", version_id: str = "v-1",
          payload: str = TUESDAY, **kw) -> me.MemoryEnvelope:
    env = make_env(memory_id, version_id, payload, **kw)
    res = store.append_version(env, payload, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
    if res.refusal is not None:
        raise AssertionError(f"seed refused: {res.refusal.value} {res.detail}")
    return env


def tree_digest(root: Path, exclude: tuple[str, ...] = ("receipts",)) -> dict[str, str]:
    """Path -> content digest for every entry under ``root`` (dirs as 'dir'),
    skipping the top-level names in ``exclude``."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts[0] in exclude:
            continue
        out[str(rel)] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def retrieval_request(**kw) -> me.RetrievalRequest:
    d = dict(
        request_id="rq-1", caller_identity="bridge", persona="bridge", purpose="companionship",
        subject_scope=(SUBJECT,), memory_classes=(me.MemoryClass.EPISODIC,), recency="recent",
        depth=3, token_budget=10_000, item_budget=50, privacy_ceiling="personal", request_time=NOW,
    )
    d.update(kw)
    return me.RetrievalRequest(**d)


_CAN_FORK = hasattr(os, "register_at_fork") and "fork" in multiprocessing.get_all_start_methods()


def _take_lock_and_report(store: ms.MemoryStore, q) -> None:
    with store.locked():
        q.put("entered")


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "store"
        self.store = ms.init_store(self.root, backup_surfaces=[], media_scrubbed=False)


# --- envelope_to_json -------------------------------------------------------
class TestEnvelopeToJson(unittest.TestCase):
    def test_round_trips_every_envelope_fixture(self) -> None:
        checked = 0
        for path in sorted(glob.glob(os.path.join(FIXTURES, "*.json"))):
            with open(path) as f:
                raw = json.load(f)
            try:
                env = me.envelope_from_json(raw)
            except me.SchemaError:
                continue  # not an envelope (policy / receipt fixture)
            with self.subTest(fixture=os.path.basename(path)):
                self.assertEqual(me.envelope_from_json(me.envelope_to_json(env)), env)
                self.assertEqual(me.envelope_to_json(env), raw)  # emits every key, nothing extra
                checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_round_trips_optional_and_nested_fields(self) -> None:
        env = make_env(
            valid_to="2026-10-01T00:00:00Z", retention_until="2026-11-01T00:00:00Z",
            action_authority_ref="auth:x", last_consolidated_at="2026-09-02T00:00:00Z",
            supersedes=("v-0",), superseded_by=("v-9",), contradicting_refs=("e3",),
        )
        self.assertEqual(me.envelope_from_json(me.envelope_to_json(env)), env)
        self.assertEqual(me.envelope_to_json(env)["memory_class"], "episodic")


# --- init / manifest --------------------------------------------------------
class TestManifest(_StoreCase):
    def test_explicit_empty_backups_is_declared_none(self) -> None:
        m = self.store.read_manifest()
        assert m is not None
        self.assertEqual(m.backup_surfaces, ())
        self.assertIs(m.media_scrubbed, False)

    def test_missing_manifest_is_unknown(self) -> None:
        (self.root / ms.STORE_MANIFEST).unlink()
        self.assertIsNone(self.store.read_manifest())

    def test_missing_key_is_unknown_not_none(self) -> None:
        (self.root / ms.STORE_MANIFEST).write_text(json.dumps({"media_scrubbed": True}))
        m = self.store.read_manifest()
        assert m is not None
        self.assertIsNone(m.backup_surfaces)

    def test_declared_backup_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = ms.init_store(
                Path(d) / "s",
                backup_surfaces=[{"name": "nightly", "retention": "30d", "verifiable": False}],
                media_scrubbed=True,
            )
            m = s.read_manifest()
            assert m is not None
            self.assertEqual(m.backup_surfaces, (ms.BackupSurface("nightly", "30d", False),))

    def test_malformed_declaration_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(me.SchemaError):
                ms.init_store(Path(d) / "s", backup_surfaces=[{"name": "x"}], media_scrubbed=False)


# --- admission --------------------------------------------------------------
class TestAdmission(_StoreCase):
    def test_version_file_is_canonical_bytes_and_digest_is_stable(self) -> None:
        env = admit(self.store)
        path = self.root / "versions" / "m-1" / "v-1.json"
        self.assertEqual(path.read_bytes(), me.canonical_json_bytes(me.envelope_to_json(env)))
        self.assertEqual(self.store.version_digest("m-1", "v-1"), ms.envelope_version_digest(env))

    def test_same_version_again_is_version_exists_and_file_unchanged(self) -> None:
        env = admit(self.store)
        before = tree_digest(self.root)
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.VERSION_EXISTS)
        self.assertEqual(tree_digest(self.root), before)

    def test_second_version_without_supersession_refused(self) -> None:
        admit(self.store)
        env = make_env("m-1", "v-2", THURSDAY)
        res = self.store.append_version(env, THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.VERSION_EXISTS)

    def test_invalid_envelope_refused_without_mutation(self) -> None:
        before = tree_digest(self.root)
        env = make_env(trust_policy_digest="sha256:other")
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.ENVELOPE_INVALID)
        res = self.store.append_version(make_env(), TUESDAY, lifecycle_policy=store_policy(), trust_policy=None, now=NOW)
        self.assertEqual(res.refusal, LR.ENVELOPE_INVALID)
        self.assertEqual(tree_digest(self.root), before)

    def test_admission_refuses_a_policy_that_is_not_the_envelopes_or_does_not_recompute(self) -> None:
        before = tree_digest(self.root)
        good = store_policy()
        cases = {
            "other-revision": store_policy("lc-rev-other"),
            "stale-digest": ms.LifecyclePolicy(good.policy_ref, LC_REV, "sha256:" + "0" * 64, {}, {}),
            "not-a-policy": None,
        }
        for label, policy in cases.items():
            with self.subTest(case=label):
                res = self.store.append_version(
                    make_env(), TUESDAY, lifecycle_policy=policy, trust_policy=trust_policy(), now=NOW  # type: ignore[arg-type]
                )
                self.assertEqual(res.refusal, LR.UNKNOWN_LIFECYCLE_POLICY)
        self.assertEqual(tree_digest(self.root), before)

    def test_admission_pins_the_policy_and_a_successor_must_carry_the_same_pin(self) -> None:
        admit(self.store)
        pin = ms.policy_pin(store_policy())
        self.assertEqual(self.store.policy_pin("m-1", "v-1"), pin)
        self.assertEqual(self.store.events()[0]["transitions"][0]["lifecycle_policy"],
                         {"ref": "LP-store-test@" + LC_REV, "digest": pin.digest})
        other = ms.make_lifecycle_policy("LP-store-test", LC_REV, correction_policies={"c": "auth:x"},
                                         forgetting_policies={})
        succ = make_env("m-1", "v-2", THURSDAY, supersedes=("v-1",))
        before = tree_digest(self.root)
        res = self.store.append_version(succ, THURSDAY, lifecycle_policy=other, trust_policy=trust_policy(),
                                        now=NOW, supersedes_version="v-1")
        self.assertEqual(res.refusal, LR.UNKNOWN_LIFECYCLE_POLICY)
        self.assertEqual(tree_digest(self.root), before)

    def test_event_log_refuses_an_admission_without_a_pin(self) -> None:
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.append_event(
                "retain", operation_id="op", now=NOW,
                transitions=[{"memory_id": "m-1", "version_id": "v-1", "to": "RETAINED_DERIVED"}],
            )
        self.assertEqual(self.store.event_log_offset(), 0)

    def test_payload_not_matching_digest_refused(self) -> None:
        res = self.store.append_version(make_env(), THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.ENVELOPE_INVALID)

    def test_non_retained_state_is_illegal_transition(self) -> None:
        env = make_env(lifecycle_state=me.LifecycleState.OBSERVED)
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.ILLEGAL_TRANSITION)

    def test_unsafe_ids_refused(self) -> None:
        env = make_env(memory_id="../escape")
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.MALFORMED_INPUT)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_non_canonical_source_object_id_is_refused_without_mutation(self) -> None:
        # One source object has exactly one spelling, so an alias can never
        # slip past a tombstone written under the canonical one.
        before = tree_digest(self.root)
        for alias in ("./" + SRC_OBJECT, SRC_OBJECT + "/", "././" + SRC_OBJECT, "notes//" + SRC_OBJECT,
                      "notes/../" + SRC_OBJECT, "/" + SRC_OBJECT, "."):
            with self.subTest(object_id=alias):
                env = make_env(canonical_source_ref=source_ref(object_id=alias))
                res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
                self.assertEqual(res.refusal, LR.MALFORMED_INPUT, res.detail)
        self.assertEqual(tree_digest(self.root), before)
        admit(self.store, canonical_source_ref=source_ref(object_id="notes/" + SRC_OBJECT))  # canonical: fine

    def test_a_store_level_successor_cannot_rehome_the_source_object(self) -> None:
        # Review n6: the #16 tombstone scoping (successors checked against
        # their OWN memory only) is sound only if a successor cites its prior's
        # object; the store now enforces that itself, not only correct().
        admit(self.store, "m-gone", "v-1", TUESDAY, canonical_source_ref=source_ref(object_id="forgotten.md"))
        self.store.add_tombstone(memory_id="m-gone", source_system=SRC_SYSTEM, object_id="forgotten.md",
                                 operation_id="op", now=NOW, policy_revision=LC_REV, forgetting_policy="f")
        admit(self.store, "m-live", "v-1", TUESDAY)
        before = tree_digest(self.root)
        for label, ref in (("object", source_ref(object_id="forgotten.md")), ("system", source_ref(system="other-src"))):
            with self.subTest(changed=label):
                succ = make_env("m-live", f"v-2{label}", THURSDAY, canonical_source_ref=ref, supersedes=("v-1",))
                res = self.store.append_version(succ, THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(),
                                                now=NOW, supersedes_version="v-1")
                self.assertEqual(res.refusal, LR.SOURCE_IDENTITY_CHANGE, res.detail)
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(self.store.retrievable_version_ids("m-live"), ("v-1",))

    def test_successor_of_a_memory_tombstoned_for_itself_is_refused(self) -> None:
        admit(self.store)
        self.store.add_tombstone(memory_id="m-1", source_system=SRC_SYSTEM, object_id=SRC_OBJECT,
                                 operation_id="op", now=NOW, policy_revision=LC_REV, forgetting_policy="f")
        succ = make_env("m-1", "v-2", THURSDAY, supersedes=("v-1",))
        res = self.store.append_version(succ, THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW, supersedes_version="v-1")
        self.assertEqual(res.refusal, LR.TOMBSTONED)
        self.assertEqual(self.store.version_ids("m-1"), ("v-1",))

    def test_source_system_outside_the_closed_alphabet_is_refused(self) -> None:
        # The "system/object" name on receipts is injective only if a system
        # can never contain "/" (review n4: "a/b"+"c" vs "a"+"b/c").
        before = tree_digest(self.root)
        for bad in ("a/b", "File-Src", "", "-src", "src ", "s" * 65, "src/", "sr\\c"):
            with self.subTest(source_system=bad):
                env = make_env(canonical_source_ref=source_ref(system=bad))
                res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
                self.assertEqual(res.refusal, LR.MALFORMED_INPUT, res.detail)
        self.assertEqual(tree_digest(self.root), before)
        admit(self.store, canonical_source_ref=source_ref(system="a.b_c-9"))

    def test_object_id_must_be_nfc_without_controls_backslash_or_edge_whitespace(self) -> None:
        # Review n8: spellings PurePosixPath does not fold. Case and %-encoding
        # are source-specific and stay admissible (documented risk).
        nfc = unicodedata.normalize("NFC", "notes/Café.md")
        before = tree_digest(self.root)
        for label, oid in (("NFD", unicodedata.normalize("NFD", nfc)), ("newline", nfc + "\n"), ("tab", "notes/\tx.md"),
                           ("NUL", "notes/x\x00.md"), ("DEL", "notes/x\x7f.md"), ("backslash", "notes\\Café.md"),
                           ("trailing space", nfc + " "), ("leading space", " " + nfc)):
            with self.subTest(case=label):
                self.assertFalse(ms.is_canonical_object_id(oid))
                env = make_env(canonical_source_ref=source_ref(object_id=oid))
                res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
                self.assertEqual(res.refusal, LR.MALFORMED_INPUT, res.detail)
        self.assertEqual(tree_digest(self.root), before)
        for oid in (nfc, "NOTES/CAFÉ.MD", "notes/%2e/Café.md", "notes/a b.md"):
            with self.subTest(admissible=oid):
                self.assertTrue(ms.is_canonical_object_id(oid))

    def test_a_torn_tombstone_line_is_void_even_when_it_parses_and_after_healing(self) -> None:
        # Review p3 (iii): a complete tombstone record whose newline was lost.
        tomb = {"memory_id": "m-x", "source_system": SRC_SYSTEM, "object_id": SRC_OBJECT, "operation_id": "o",
                "at": NOW, "policy_revision": LC_REV, "forgetting_policy": "f"}
        with open(self.root / "tombstones.jsonl", "ab") as fh:
            fh.write(json.dumps(tomb).encode())  # no newline: its forget never completed
        self.assertFalse(self.store.is_tombstoned(SRC_SYSTEM, SRC_OBJECT))
        self.store.add_tombstone(memory_id="m-y", source_system=SRC_SYSTEM, object_id="other.md",
                                 operation_id="op", now=NOW, policy_revision=LC_REV, forgetting_policy="f")
        self.assertEqual([t["memory_id"] for t in self.store.tombstones()], ["m-y"])  # healed, still void
        voided = self.store.tombstones()[0]["torn_predecessor"]
        self.assertEqual(voided["digest"], "sha256:" + hashlib.sha256(json.dumps(tomb).encode()).hexdigest())

    def test_tombstones_are_written_and_checked_in_canonical_form(self) -> None:
        self.store.add_tombstone(memory_id="m-1", source_system=SRC_SYSTEM, object_id="./" + SRC_OBJECT,
                                 operation_id="op", now=NOW, policy_revision=LC_REV, forgetting_policy="f")
        self.assertEqual(self.store.tombstones()[0]["object_id"], SRC_OBJECT)
        self.assertTrue(self.store.is_tombstoned(SRC_SYSTEM, SRC_OBJECT))
        self.assertTrue(self.store.is_tombstoned(SRC_SYSTEM, SRC_OBJECT + "/"))


# --- fold / immutability ----------------------------------------------------
class TestFold(_StoreCase):
    def test_supersession_is_a_fold_and_version_file_never_rewritten(self) -> None:
        admit(self.store)
        v1_path = self.root / "versions" / "m-1" / "v-1.json"
        v1_bytes = v1_path.read_bytes()
        succ = make_env("m-1", "v-2", THURSDAY, supersedes=("v-1",))
        res = self.store.append_version(
            succ, THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW,
            supersedes_version="v-1",
        )
        self.assertIsNone(res.refusal, res.detail)
        self.assertEqual(v1_path.read_bytes(), v1_bytes)
        stored = self.store.stored_envelope("m-1", "v-1")
        assert stored is not None
        self.assertEqual(stored.lifecycle_state, me.LifecycleState.RETAINED_DERIVED)
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.SUPERSEDED)
        self.assertEqual(cur.superseded_by, ("v-2",))
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        # ONE event records both the closure and the successor's retention.
        last = self.store.events()[-1]
        self.assertEqual(
            {(t["version_id"], t["to"]) for t in last["transitions"]},
            {("v-1", "SUPERSEDED"), ("v-2", "RETAINED_DERIVED")},
        )

    def test_event_log_cannot_record_an_illegal_transition(self) -> None:
        admit(self.store)
        size = self.store.event_log_offset()
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.append_event(
                "bogus", operation_id="op", now=NOW,
                transitions=[{"memory_id": "m-1", "version_id": "v-1", "to": "OBSERVED"}],
            )
        self.assertEqual(self.store.event_log_offset(), size)

    def _crash_before_event(self, env: me.MemoryEnvelope, payload: str, **kw) -> None:
        class Crash(BaseException):
            pass

        with mock.patch.object(ms.MemoryStore, "append_event", side_effect=Crash("before the event")):
            with self.assertRaises(Crash):
                self.store.append_version(env, payload, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW, **kw)

    def test_orphan_with_identical_bytes_is_completed_by_the_retry(self) -> None:
        # A crash between the version-file write and the event leaves an
        # orphan; retrying the SAME admission completes it (idempotent).
        env = make_env()
        self._crash_before_event(env, TUESDAY)
        self.assertEqual(self.store.version_ids("m-1"), ("v-1",))
        self.assertIsNone(self.store.current_envelope("m-1", "v-1"))
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertIsNone(res.refusal, res.detail)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        self.assertEqual(len(self.store.events()), 1)

    def test_orphan_with_different_bytes_is_version_exists(self) -> None:
        self._crash_before_event(make_env(), TUESDAY)
        before = tree_digest(self.root)
        other = make_env(payload=THURSDAY)
        res = self.store.append_version(other, THURSDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.VERSION_EXISTS)
        self.assertEqual(tree_digest(self.root), before)

    def test_orphan_with_the_same_payload_but_a_different_envelope_is_version_exists(self) -> None:
        # The payload check alone cannot see this: only the envelope differs,
        # so the version-file byte comparison is what refuses it (review S15).
        self._crash_before_event(make_env(), TUESDAY)
        before = tree_digest(self.root)
        other = make_env(observed_at="2026-09-02T00:00:00Z")  # same payload bytes + digest
        res = self.store.append_version(other, TUESDAY, lifecycle_policy=store_policy(), trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.VERSION_EXISTS, res.detail)
        self.assertIn("never-admitted", res.detail)
        self.assertEqual(tree_digest(self.root), before)

    def test_version_without_event_is_not_retrievable(self) -> None:
        env = make_env()
        (self.root / "versions" / "m-1").mkdir(parents=True)
        (self.root / "versions" / "m-1" / "v-1.json").write_bytes(
            me.canonical_json_bytes(me.envelope_to_json(env))
        )
        (self.root / "payloads" / "m-1").mkdir(parents=True)
        (self.root / "payloads" / "m-1" / "v-1").write_text(TUESDAY)
        self.assertIsNone(self.store.current_envelope("m-1", "v-1"))
        self.assertEqual(self.store.retrievable_envelopes(), ())


# --- payloads ---------------------------------------------------------------
class TestPayloads(_StoreCase):
    def test_tampered_payload_is_never_served(self) -> None:
        admit(self.store)
        (self.root / "payloads" / "m-1" / "v-1").write_text("tampered")
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.read_payload("m-1", "v-1")
        self.assertEqual(self.store.retrievable_envelopes(), ())

    def test_a_memory_tombstoned_for_itself_is_never_retrievable(self) -> None:
        # Defence in depth: even while its folded state is still RETAINED, a
        # memory whose source object is tombstoned FOR THAT MEMORY is not served.
        admit(self.store)
        admit(self.store, "m-2", "v-1", TUESDAY, canonical_source_ref=source_ref(object_id="other.md"))
        self.store.add_tombstone(memory_id="m-1", source_system=SRC_SYSTEM, object_id=SRC_OBJECT,
                                 operation_id="op", now=NOW, policy_revision=LC_REV, forgetting_policy="f")
        self.assertEqual(self.store.folded()[("m-1", "v-1")].state, me.LifecycleState.RETAINED_DERIVED)
        self.assertEqual([e.memory_id for e in self.store.retrievable_envelopes()], ["m-2"])
        self.store.rebuild_projections()
        self.assertEqual(self.store.raw_lookup_subject(SUBJECT), (("m-2", "v-1"),))

    def test_payloads_are_per_version_not_shared(self) -> None:
        admit(self.store, "m-1", "v-1", TUESDAY)
        admit(self.store, "m-2", "v-1", TUESDAY, subjects=("other",), canonical_source_ref=source_ref(object_id="other.md"))
        self.assertTrue(self.store.purge_payload_file("m-1", "v-1"))
        self.assertEqual(self.store.read_payload("m-2", "v-1"), TUESDAY)
        self.assertEqual([e.memory_id for e in self.store.retrievable_envelopes()], ["m-2"])


# --- projections ------------------------------------------------------------
class TestProjections(_StoreCase):
    def test_built_only_from_retrievable_set(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        self.assertEqual(self.store.lookup_content("tuesday"), (("m-1", "v-1"),))
        self.assertEqual(self.store.lookup_subject(SUBJECT), (("m-1", "v-1"),))
        self.store.append_event(
            "decay", operation_id="op", now=NOW,
            transitions=[{"memory_id": "m-1", "version_id": "v-1", "to": "DECAYED"}],
        )
        self.store.rebuild_projections()
        self.assertEqual(self.store.raw_lookup_content("tuesday"), ())
        self.assertEqual(self.store.raw_lookup_subject(SUBJECT), ())

    def test_query_path_filters_against_retrievable_set(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        self.store.append_event(
            "decay", operation_id="op", now=NOW,
            transitions=[{"memory_id": "m-1", "version_id": "v-1", "to": "DECAYED"}],
        )
        # Not yet rebuilt: the generation physically holds it, the query path does not serve it.
        self.assertEqual(self.store.raw_lookup_content("tuesday"), (("m-1", "v-1"),))
        self.assertEqual(self.store.lookup_content("tuesday"), ())
        self.assertEqual(self.store.lookup_subject(SUBJECT), ())

    def test_manifest_carries_per_file_sha256_and_event_offset(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        for name, fname in (("content", "content.sqlite"), ("subject", "subject.json")):
            with self.subTest(projection=name):
                gen = self.store.current_generation(name)
                assert gen is not None
                m = self.store.generation_manifest(name, gen)
                assert m is not None
                data = (self.root / "projections" / name / gen / fname).read_bytes()
                self.assertEqual(m["files"], {fname: "sha256:" + hashlib.sha256(data).hexdigest()})
                self.assertEqual(m["event_log_offset"], self.store.event_log_offset())
                self.assertEqual(m["entries"], [["m-1", "v-1"]])
                self.assertTrue(self.store.verify_generation(name, gen))

    def test_tampered_generation_fails_manifest_and_lookup_refuses(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        gen = self.store.current_generation("subject")
        (self.root / "projections" / "subject" / str(gen) / "subject.json").write_text("{}")
        self.assertFalse(self.store.verify_generation("subject", str(gen)))
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.lookup_subject(SUBJECT)

    def test_failed_build_leaves_current_unchanged(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        before = {n: self.store.current_generation(n) for n in ms.PROJECTIONS}

        def fault(step: str) -> None:
            if step == "build:subject:before-manifest":
                raise RuntimeError("injected build failure")

        with self.assertRaises(RuntimeError):
            self.store.rebuild_projections(_fault=fault)
        for name in ms.PROJECTIONS:
            with self.subTest(projection=name):
                self.assertEqual(self.store.current_generation(name), before[name])
                self.assertEqual(self.store.generations_on_disk(name), (before[name],))

    def test_swap_is_atomic_os_replace_of_a_complete_temp(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()
        real_replace = os.replace
        seen: list[tuple[str, bytes, bytes, str]] = []

        def spy(src, dst):  # observe the instant of the swap
            if Path(dst).name == "CURRENT":
                seen.append((Path(dst).parent.name, Path(dst).read_bytes(), Path(src).read_bytes(), Path(src).name))
            return real_replace(src, dst)

        with mock.patch.object(ms.os, "replace", side_effect=spy):
            self.store.rebuild_projections()
        self.assertEqual(sorted(s[0] for s in seen), ["content", "subject"])
        for _name, live, staged, tmp_name in seen:
            self.assertEqual(live, b"gen-1\n")  # live CURRENT is complete up to the swap
            self.assertEqual(staged, b"gen-2\n")  # the swapped-in file is already complete
            self.assertTrue(tmp_name.startswith(".tmp-"))

    def test_swap_failure_leaves_no_partial_current(self) -> None:
        admit(self.store)
        self.store.rebuild_projections()

        def boom(src, dst):
            raise OSError("injected swap failure")

        with mock.patch.object(ms.os, "replace", side_effect=boom):
            with self.assertRaises(OSError):
                self.store.rebuild_projections()
        for name in ms.PROJECTIONS:
            d = self.root / "projections" / name
            with self.subTest(projection=name):
                self.assertEqual((d / "CURRENT").read_bytes(), b"gen-1\n")
                self.assertEqual([p.name for p in d.iterdir() if p.name.startswith(".tmp-")], [])
                self.assertEqual(self.store.generations_on_disk(name), ("gen-1",))

    def test_keeps_one_prior_generation_and_rolls_back(self) -> None:
        admit(self.store)
        for _ in range(3):
            self.store.rebuild_projections()
        self.assertEqual(self.store.generations_on_disk("content"), ("gen-2", "gen-3"))
        self.assertEqual(self.store.rollback_projections(), {"content": "gen-2", "subject": "gen-2"})
        self.assertEqual(self.store.current_generation("content"), "gen-2")


# --- lock -------------------------------------------------------------------
class TestLock(_StoreCase):
    def test_second_thread_on_one_store_object_waits_for_the_lock(self) -> None:
        # Re-entrancy is per THREAD: a second thread sharing this MemoryStore
        # object must block until the holder leaves, never walk in on its depth.
        held, release, entered = threading.Event(), threading.Event(), threading.Event()

        def holder() -> None:
            with self.store.locked():
                held.set()
                release.wait(10)

        def intruder() -> None:
            held.wait(10)
            with self.store.locked():
                entered.set()

        threads = [threading.Thread(target=holder), threading.Thread(target=intruder)]
        for t in threads:
            t.start()
        try:
            self.assertTrue(held.wait(10))
            self.assertFalse(entered.wait(0.5), "second thread entered locked() while the first held it")
        finally:
            release.set()
            for t in threads:
                t.join(10)
        self.assertTrue(entered.is_set())

    @unittest.skipUnless(_CAN_FORK, "needs os.register_at_fork and the fork start method")
    def test_a_child_forked_while_another_thread_holds_the_lock_can_still_take_it(self) -> None:
        # Review n7 (b): the child inherited an RLock owned by a thread that
        # does not exist in it, and deadlocked forever.
        held, release = threading.Event(), threading.Event()

        def holder() -> None:
            with self.store.locked():
                held.set()
                release.wait(10)

        th = threading.Thread(target=holder)
        th.start()
        self.assertTrue(held.wait(10))
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a multi-threaded process
            child = ctx.Process(target=_take_lock_and_report, args=(self.store, q))
            child.start()
        time.sleep(0.3)
        release.set()
        th.join(10)
        child.join(15)
        deadlocked = child.is_alive()
        if deadlocked:
            child.terminate()
            child.join()
        self.assertFalse(deadlocked, "the forked child deadlocked on an inherited lock")
        self.assertEqual(q.get(timeout=5), "entered")

    @unittest.skipUnless(_CAN_FORK, "needs os.register_at_fork and the fork start method")
    def test_a_child_forked_inside_locked_takes_its_own_flock(self) -> None:
        # Review n7 (c): the child inherited the thread-local depth and the
        # flock's open file description, so it entered while the parent held.
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        with self.store.locked():
            child = ctx.Process(target=_take_lock_and_report, args=(self.store, q))
            child.start()
            try:
                entered_while_held = q.get(timeout=1.0) == "entered"
            except queue.Empty:
                entered_while_held = False
        self.assertFalse(entered_while_held, "the forked child entered locked() while the parent held it")
        self.assertEqual(q.get(timeout=10), "entered")  # after the parent releases, the child gets it
        child.join(10)
        self.assertEqual(child.exitcode, 0)

    def test_lock_is_reentrant_within_one_thread(self) -> None:
        with self.store.locked():
            with self.store.locked():
                admit(self.store)  # append_version + append_event take the lock again
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))


# --- receipts ---------------------------------------------------------------
class TestReceiptFiles(_StoreCase):
    def test_receipts_are_write_once_per_attempt(self) -> None:
        first = self.store.write_receipt("op-1", {"attempt": 1})
        body = first.read_bytes()
        second = self.store.write_receipt("op-1", {"attempt": 2})
        self.assertEqual(first.name, "op-1.json")
        self.assertEqual(second.name, "op-1+attempt-2.json")
        self.assertEqual(first.read_bytes(), body)
        self.assertEqual(self.store.receipt_paths("op-1"), (first, second))


if __name__ == "__main__":
    unittest.main()
