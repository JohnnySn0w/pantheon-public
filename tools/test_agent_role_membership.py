"""test_agent_role_membership.py — falsifiers for the L3 PersonaRoleMembership
reader (ORG-088 D-2 / ORG-085 L3; T-3757, the ORG-088 riskiest-assumption GATE).

HERMETIC. Each test writes a tmp persona_roles.yaml + a tmp instance.yaml projection
and repoints agent_role_map's module-level source/manifest paths at them
(monkeypatch), then calls the public reader. This exercises the fail-closed load
contract without touching the live tree.

The three falsifiers are behavior-bound + mutation-gated (Amendment J §J6 — the
independent reviewer picks final mutation targets; these survive that). Each names,
in its docstring, the reader mutation that turns it RED. F1 (digest) and F3
(revision) gate DISTINCT branches — each fixture keeps the OTHER invariant
satisfied — so removing one check cannot mask the other.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

# Import the SAME module object other consumers load as `tools.agent_role_map`
# (repo root on sys.path), matching the sibling test_instance_role_grants.py
# pattern and avoiding the run-as-script vs run-as-package dual-module-identity
# problem — so monkeypatching arm._INSTANCE_YAML/_PERSONA_ROLES_YAML repoints the
# module the reader actually resolves against.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from tools import agent_role_map as arm  # noqa: E402


# --- fixture builders --------------------------------------------------------

def _digest(doc: dict) -> str:
    """Mirror of arm._compute_source_digest: sha256 over the sorted-key compact JSON
    of the content EXCLUDING source_digest. Kept independent (not importing the
    module fn) so a mutation to the reader's canonicalization cannot silently make
    the fixtures agree with a broken reader."""
    content = {k: v for k, v in doc.items() if k != "source_digest"}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _valid_doc() -> dict:
    doc = {
        "schema": "pantheon-persona-role-membership/v1",
        "source_revision": 1,
        "personas": {
            "orchestrator": {
                "role_ids": ["orchestrator", "orchestrator"],
                "default_role_id": "orchestrator",
                "lifecycle": "declared",
            },
            "mason": {
                "role_ids": ["sub_orchestrator", "orchestrator"],
                "default_role_id": "sub_orchestrator",
                "lifecycle": "declared",
            },
        },
    }
    doc["source_digest"] = _digest(doc)
    return doc


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Return a wire(doc, expected_revision, expected_digest) helper that writes the
    source + projection to tmp and repoints the reader at them."""
    src = tmp_path / "persona_roles.yaml"
    manifest = tmp_path / "instance.yaml"

    def _wire(doc: dict, *, expected_revision, expected_digest) -> None:
        src.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        manifest.write_text(
            yaml.safe_dump(
                {
                    "role_membership_projection": {
                        "schema": "pantheon-persona-role-membership-projection/v1",
                        "source": str(src),
                        "expected_revision": expected_revision,
                        "expected_digest": expected_digest,
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(arm, "_PERSONA_ROLES_YAML", src)
        monkeypatch.setattr(arm, "_INSTANCE_YAML", manifest)

    return _wire


# --- FALSIFIER 1 -------------------------------------------------------------

def test_f1_digest_mismatch_on_projection_refuses(wired):
    """FALSIFIER 1: a projection whose expected_digest disagrees with the source's
    actual content -> load REFUSES (MembershipDigestMismatch). The revision matches
    (expected_revision == source_revision == 1), so ONLY the digest check can fire.

    RED-mutation (proves non-vacuity): delete the
    `if ... actual_digest != expected_digest: raise MembershipDigestMismatch`
    branch in _load_membership_source -> the first call below returns orchestrator's roles
    instead of raising, so this test goes RED. Independent of F3 (revision is valid
    here)."""
    doc = _valid_doc()
    good_digest = doc["source_digest"]

    # WRONG digest pinned; valid revision -> only the digest check can fire.
    wired(doc, expected_revision=1, expected_digest="0" * 64)
    with pytest.raises(arm.MembershipDigestMismatch):
        arm.functional_roles("orchestrator")

    # Control: with the CORRECT digest, the identical source loads cleanly. This
    # proves the refusal above was caused by the digest, not an unconditional error.
    wired(doc, expected_revision=1, expected_digest=good_digest)
    assert arm.functional_roles("orchestrator") == frozenset({"orchestrator", "orchestrator"})


# --- FALSIFIER 2 -------------------------------------------------------------

def test_f2_reader_round_trips_declared_role_ids(wired):
    """FALSIFIER 2: the reader returns exactly the role_ids the L3 source declares,
    resolving OVER the source (not a hardcoded/literal map).

    RED-mutation: make functional_roles/role_members return a fixed set, or read
    from AGENT_ROLE_MAP, instead of _resolve_membership() -> the round-trip and the
    follows-the-source re-wire below fail."""
    doc = _valid_doc()
    wired(doc, expected_revision=1, expected_digest=doc["source_digest"])

    assert arm.functional_roles("orchestrator") == frozenset({"orchestrator", "orchestrator"})
    assert arm.functional_roles("mason") == frozenset({"sub_orchestrator", "orchestrator"})
    assert arm.role_members("orchestrator") == frozenset({"orchestrator", "mason"})
    assert arm.role_members("sub_orchestrator") == frozenset({"mason"})
    assert arm.default_functional_role("orchestrator") == "orchestrator"

    # Unknown persona / role -> empty frozenset (truthful "no membership", not error).
    assert arm.functional_roles("stranger") == frozenset()
    assert arm.role_members("nonrole") == frozenset()

    # The reader FOLLOWS the source: re-declare mason and the output changes. This
    # defeats any literal-map / hardcoded-return mutation.
    doc2 = _valid_doc()
    doc2["personas"]["mason"]["role_ids"] = ["reviewer", "orchestrator"]
    doc2["personas"]["mason"]["default_role_id"] = "reviewer"
    doc2["source_digest"] = _digest(doc2)
    wired(doc2, expected_revision=1, expected_digest=doc2["source_digest"])
    assert arm.functional_roles("mason") == frozenset({"reviewer", "orchestrator"})
    assert arm.role_members("sub_orchestrator") == frozenset()  # mason no longer holds it
    assert arm.role_members("reviewer") == frozenset({"mason"})


# --- FALSIFIER 3 -------------------------------------------------------------

def test_f3_stale_or_missing_revision_refuses(wired):
    """FALSIFIER 3: a missing OR stale source_revision -> fail-closed
    (MembershipStaleRevision). Both fixtures keep the DIGEST valid, so only the
    revision check can fire -> independent of F1.

    RED-mutation (proves non-vacuity): delete the
    `if source_revision is None or source_revision != expected_revision:`
    branch in _load_membership_source -> both cases below return roles instead of
    raising."""
    # (A) MISSING source_revision. Digest is computed over the content that lacks the
    # field, so the digest check passes and the revision check is what fails.
    doc_missing = {
        "schema": "pantheon-persona-role-membership/v1",
        "personas": {
            "orchestrator": {
                "role_ids": ["orchestrator"],
                "default_role_id": "orchestrator",
                "lifecycle": "declared",
            }
        },
    }
    doc_missing["source_digest"] = _digest(doc_missing)
    wired(doc_missing, expected_revision=1, expected_digest=doc_missing["source_digest"])
    with pytest.raises(arm.MembershipStaleRevision):
        arm.functional_roles("orchestrator")

    # (B) STALE: source_revision=1 but the projection pins expected_revision=2. The
    # expected_digest still matches the rev-1 content, so the digest check passes and
    # only the revision check fires.
    doc_stale = _valid_doc()  # source_revision == 1
    wired(doc_stale, expected_revision=2, expected_digest=doc_stale["source_digest"])
    with pytest.raises(arm.MembershipStaleRevision):
        arm.functional_roles("orchestrator")


# --- guard: missing projection / source is fail-closed, not fail-safe --------

def test_absent_projection_block_refuses(wired, tmp_path, monkeypatch):
    """Belt-and-braces: an instance.yaml with NO role_membership_projection block
    fails closed (MembershipSourceMissing), unlike the fail-safe binding loaders that
    fall back to a hardcoded mirror. A silent fallback here would be the second
    authority ORG-085 §Field ownership and authority bans."""
    manifest = tmp_path / "instance.yaml"
    manifest.write_text(yaml.safe_dump({"roles": {}}, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(arm, "_INSTANCE_YAML", manifest)
    monkeypatch.setattr(arm, "_PERSONA_ROLES_YAML", tmp_path / "persona_roles.yaml")
    with pytest.raises(arm.MembershipSourceMissing):
        arm.functional_roles("orchestrator")


def test_known_role_ids_is_the_union_over_the_verified_source(wired):
    """T-3761 COMMIT 1: known_role_ids() is the UNION of every declared role_id,
    derived over the same verified reader (_resolve_membership) — not a literal list.

    RED-mutation (proves non-vacuity): make known_role_ids return a fixed/literal set
    instead of unioning _resolve_membership() -> the follows-the-source re-wire below
    fails. Deriving over the reader also means it INHERITS the fail-closed contract
    (see the two guard tests below)."""
    doc = _valid_doc()  # orchestrator: {orchestrator, orchestrator}; mason: {sub_orchestrator, orchestrator}
    wired(doc, expected_revision=1, expected_digest=doc["source_digest"])
    assert arm.known_role_ids() == frozenset(
        {"orchestrator", "orchestrator", "sub_orchestrator"}
    )

    # FOLLOWS the source: re-declare mason onto reviewer and the vocab changes — a
    # hardcoded/literal-return mutation cannot track this.
    doc2 = _valid_doc()
    doc2["personas"]["mason"]["role_ids"] = ["reviewer", "orchestrator"]
    doc2["personas"]["mason"]["default_role_id"] = "reviewer"
    doc2["source_digest"] = _digest(doc2)
    wired(doc2, expected_revision=1, expected_digest=doc2["source_digest"])
    assert arm.known_role_ids() == frozenset(
        {"orchestrator", "orchestrator", "reviewer"}
    )


def test_known_role_ids_fail_closed_on_digest_mismatch(wired):
    """T-3761 COMMIT 1 (fail-closed contract): known_role_ids inherits the reader's
    typed refusal — an unverifiable source RAISES, it does NOT return an empty set.

    A silent empty-set here would make every role read as "unknown" on the WRITE
    surface (refuse everything) AND make the READ-surface vocab check vacuous — both
    wrong. RED-mutation: wrap the _resolve_membership() call in a
    try/except returning frozenset() -> this raises-check fails."""
    doc = _valid_doc()
    wired(doc, expected_revision=1, expected_digest="0" * 64)  # wrong digest, valid revision
    with pytest.raises(arm.PersonaRoleMembershipError):
        arm.known_role_ids()


def test_known_role_ids_fail_closed_on_absent_projection(tmp_path, monkeypatch):
    """T-3761 COMMIT 1: a missing role_membership_projection block is a typed refusal
    from known_role_ids, same as functional_roles — never a silent empty vocab."""
    manifest = tmp_path / "instance.yaml"
    manifest.write_text(yaml.safe_dump({"roles": {}}, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(arm, "_INSTANCE_YAML", manifest)
    monkeypatch.setattr(arm, "_PERSONA_ROLES_YAML", tmp_path / "persona_roles.yaml")
    with pytest.raises(arm.MembershipSourceMissing):
        arm.known_role_ids()


def test_known_role_ids_live_tree():
    """Live-tree smoke: the committed persona_roles.yaml seed vocab is exactly
    the operator's T-3807 ratified per-persona set — the universal `orchestrator` tier
    retired, each persona holding one narrow head/engineering role (guards a
    role-vocabulary drift the hermetic tests cannot see, mirroring
    test_live_tree_membership_loads_and_round_trips)."""
    # T-4701 (the operator TG 11444): each head role also implies its regular role, so the
    # vocab is the declared set PLUS the implied regulars.
    assert arm.known_role_ids() == frozenset({
        "head_engineering_orchestrator", "head_memory_orchestrator", "engineering_orchestrator",
        "head_research_orchestrator", "head_system_orchestrator", "head_auditor",
        "memory_orchestrator", "research_orchestrator", "system_orchestrator", "auditor",
    })


def test_live_tree_membership_loads_and_round_trips():
    """Live-tree smoke: the REAL persona_roles.yaml + instance.yaml projection load
    and verify (digest + revision) with no monkeypatching. Guards the committed seed
    against a digest/revision drift the hermetic tests cannot see. Mirrors the
    live-tree posture of test_instance_role_grants.py."""
    assert arm.functional_roles("mason") == frozenset({"engineering_orchestrator"})
    # T-4701: the head seat (orchestrator) is implicitly in its regular pool too.
    assert arm.role_members("engineering_orchestrator") == frozenset({"mason", "artisan", "orchestrator"})
    assert "orchestrator" in arm.role_members("head_engineering_orchestrator")
    # Universal `orchestrator` tier retired by T-3807 — no persona holds it.
    assert arm.role_members("orchestrator") == frozenset()
    assert arm.default_functional_role("specialist") == "head_research_orchestrator"


# --- T-3758 LOW-fix falsifiers (folded from the T1 review) -------------------

def test_lowfix1_self_inconsistent_source_digest_refuses(wired):
    """LOW-fix #1 FALSIFIER: a source whose OWN source_digest field disagrees with its
    recomputed content -> refuse (MembershipMalformed), even though the PROJECTION's
    expected_digest matches (so the projection check passes) and the revision is valid
    (so the revision check passes). ONLY the self-consistency gate can fire.

    RED-mutation (proves non-vacuity): delete the `own_digest != actual_digest ->
    raise MembershipMalformed` block in _load_membership_source -> the call below
    returns orchestrator's roles instead of raising."""
    doc = _valid_doc()
    computed = doc["source_digest"]  # correct content digest
    doc["source_digest"] = "0" * 64  # corrupt the source's OWN recorded digest
    # Projection pins the CORRECT digest + revision, so projection-digest and revision
    # checks both pass; only the self-consistency check disagrees with the source.
    wired(doc, expected_revision=1, expected_digest=computed)
    with pytest.raises(arm.MembershipMalformed):
        arm.functional_roles("orchestrator")

    # Control: restore the source's own digest and the identical source loads cleanly,
    # proving the refusal was the self-consistency gate, not an unconditional error.
    doc["source_digest"] = computed
    wired(doc, expected_revision=1, expected_digest=computed)
    assert arm.functional_roles("orchestrator") == frozenset({"orchestrator", "orchestrator"})


def test_lowfix2_source_path_resolved_from_projection(tmp_path, monkeypatch):
    """LOW-fix #2 FALSIFIER: with source_path=None, the reader resolves the
    projection's DECLARED `source:` (relative to the manifest's dir), NOT the hardcoded
    _PERSONA_ROLES_YAML. The source lives at a NON-default filename and the hardcode is
    pointed at a path that does not exist, so a load that SUCCEEDS proves the projection
    was followed.

    RED-mutation (proves non-vacuity): revert _resolve_membership to
    `source_path = source_path or _PERSONA_ROLES_YAML` -> the reader reads the
    non-existent hardcode path and raises MembershipSourceMissing instead of loading."""
    relocated = tmp_path / "roles_relocated.yaml"
    manifest = tmp_path / "instance.yaml"
    doc = _valid_doc()
    relocated.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    manifest.write_text(
        yaml.safe_dump(
            {
                "role_membership_projection": {
                    "schema": "pantheon-persona-role-membership-projection/v1",
                    "source": "roles_relocated.yaml",  # RELATIVE to the manifest dir
                    "expected_revision": 1,
                    "expected_digest": doc["source_digest"],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # Point the hardcode fallback at a path that does NOT exist: if the reader used it
    # instead of the projection's declared source, the load would fail-closed.
    monkeypatch.setattr(arm, "_PERSONA_ROLES_YAML", tmp_path / "does_not_exist.yaml")
    monkeypatch.setattr(arm, "_INSTANCE_YAML", manifest)
    assert arm.functional_roles("orchestrator") == frozenset({"orchestrator", "orchestrator"})
    assert arm.functional_roles("mason") == frozenset({"sub_orchestrator", "orchestrator"})


def test_lowfix3_canonical_source_bytes_typed_error_and_membership_unchanged():
    """LOW-fix #3 FALSIFIER (two bindings on one primitive):

    (a) canonical_source_bytes raises the TYPED MembershipMalformed (not a bare
        TypeError) on content that is not JSON-serializable.
        RED-mutation: drop the `except TypeError -> raise MembershipMalformed` wrapper
        -> a bare TypeError escapes and pytest.raises(MembershipMalformed) fails.

    (b) the generalization is byte-FOR-byte identical to the old membership pre-image
        at the default exclude — an INDEPENDENT mirror (not importing the module fn)
        must equal canonical_source_bytes, so the persona_roles.yaml seed digest still
        verifies. RED-mutation: change the default exclude / separators / sort_keys ->
        the bytes diverge from the mirror."""
    # (a) a set is not JSON-serializable -> typed refusal, not a raw TypeError.
    with pytest.raises(arm.MembershipMalformed):
        arm.canonical_source_bytes({"schema": "x", "bad": {1, 2, 3}})

    # (b) default-exclude bytes match an independent mirror of the T1 canonical form.
    doc = _valid_doc()
    mirror = json.dumps(
        {k: v for k, v in doc.items() if k != "source_digest"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    assert arm.canonical_source_bytes(doc) == mirror
    # And the custom-exclude form drops the extra field, proving `exclude` is honored.
    assert b"authority_ref" not in arm.canonical_source_bytes(
        {**doc, "authority_ref": ["ORG-027"]}, exclude=("source_digest", "authority_ref")
    )


# ---- T-4701: head roles implicitly hold their non-head role (the operator TG 11444) ----

def test_with_implied_adds_the_regular_role_for_each_head_role():
    got = arm._with_implied(frozenset({"head_engineering_orchestrator", "head_auditor"}))
    assert got == frozenset({
        "head_engineering_orchestrator", "engineering_orchestrator",
        "head_auditor", "auditor",
    })


def test_with_implied_leaves_regular_and_bare_head_alone():
    assert arm._with_implied(frozenset({"engineering_orchestrator"})) == frozenset({"engineering_orchestrator"})
    # a degenerate "head_" with nothing after it implies nothing (no empty role id)
    assert arm._with_implied(frozenset({"head_"})) == frozenset({"head_"})


def test_live_head_seat_is_a_member_of_its_regular_pool():
    # The live source: orchestrator holds head_engineering_orchestrator, so it is also in the
    # engineering_orchestrator pool alongside the regulars (the operator TG 11444/11445).
    assert "engineering_orchestrator" in arm.functional_roles("orchestrator")
    assert "orchestrator" in arm.role_members("engineering_orchestrator")
    assert "engineering_orchestrator" in arm.known_role_ids()
    # the default role is unchanged: still the declared head role
    assert arm.default_functional_role("orchestrator") == "head_engineering_orchestrator"


def test_implied_regular_roles_are_known_vocabulary_for_role_targets():
    # Each head role's regular form is a valid role:<R> pool target, even when no
    # persona declares it explicitly (e.g. research_orchestrator via specialist).
    known = arm.known_role_ids()
    for head in [r for r in known if r.startswith("head_")]:
        assert head[len("head_"):] in known
