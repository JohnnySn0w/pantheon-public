#!/usr/bin/env python3
"""Author Recovery's canonical continuity bundle + capture a verified checkpoint (T-3178 / ORG-057:R1, gate G3).

The ORG-057:R1 acceptance (T-3178) requires Recovery to hydrate from *the same
canonical bundle* on the Claude and Codex harnesses and to prove a
``require_checkpoint`` against a **verified** ``CheckpointEnvelope``. Neither
artifact existed on disk before this tool -- only the schema
(``schemas/persona-continuity-bundle.schema.json``), the T-3176 test fixture,
and a stale ``personas/recovery/staged-cutover-*.json``. The 2026-09-07 "188 tests
green" was the acceptance MACHINERY, not the fire ARTIFACTS.

WHY canonical sources are STAGED into a private root (not read from the repo):
``persona_continuity``'s source resolver (``_read_under_root`` /
``_open_directory_no_follow``) refuses any path component that is
group/other-writable (``mode & 0o022``) or not owned by the expected uid. The
live ``pantheon-config`` checkout is mode 0775 (group ``claude-fallback``) with
0664 files, so the resolver rejects it. Canonical sources are therefore copied
(0644) into a Recovery-owned 0700 recovery root, exactly the "Recovery-owned private
recovery root" consultant specified in the T-3178 §8 review (Q3). The staged copies
are reproducible from a pinned pantheon-config revision, recorded in the
readback inventory this tool emits.

The tool is deterministic-by-inputs and append-only: ``--force`` rebuilds the
regenerable subtrees (sources/descriptor/checkpoints/inventory) but PRESERVES
the coordinator's ``switch/`` transition+receipt history. Repo-origin bytes are
read from the pinned git object (not the worktree), which must be clean unless
``--allow-dirty``. It never mutates any receiver, never touches tmux or systemd,
and writes only under the (private) continuity root -- so it is safe to run
outside a fire window.

CoordinatorConfig path pins this bundle establishes (documented for gate G2's
operator entrypoint):

    persona_id           = recovery
    organism_id          = pantheon
    runtime_manifest_path= <repo>/instance.yaml
    repo_root            = <repo>                      (real git tree; git-rev evidence)
    state_root           = ~/.local/state/pantheon/personas  (runtime_controller state)
    bundle_path          = <continuity>/descriptor/bundle.yaml
    descriptor_root      = <continuity>/descriptor
    checkpoint_root      = <continuity>/checkpoints
    switch_state_root    = <continuity>/switch

Run:  python3 -m tools.recovery_continuity_bundle build [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import persona_continuity as pc  # noqa: E402

PERSONA = "recovery"
ORGANISM = "pantheon"
DEFAULT_HOME = Path("/home/claude-fallback")
DEFAULT_CONTINUITY_ROOT = DEFAULT_HOME / ".local/state/pantheon/personas/recovery/continuity"
DEFAULT_STATE_ROOT = DEFAULT_HOME / ".local/state/pantheon/personas"

PURPOSES = ("hydrate-runtime", "checkpoint-boundary", "verify-candidate")


@dataclass(frozen=True)
class CanonicalSource:
    """One canonical identity source that composes Recovery's continuity bundle.

    ``origin`` is ``repo`` (relative to the pantheon-config checkout) or
    ``home`` (relative to Recovery's account home). ``scheme`` is the staged root
    the resolver reads it back under (repo/state); it preserves provenance
    meaning but every source is a staged 0644 copy regardless.
    """

    label: str
    kind: str
    origin: str          # "repo" | "home"
    origin_rel: str      # path under repo_root or home
    scheme: str          # "repo" | "state" -- staged root
    staged_rel: str      # path under the staged root
    privacy: str
    redacted: bool


# The canonical Recovery identity map (gate G3). Each ref points at the real source
# that governs that ORG-057 logical dimension; kinds are the resolver's enum.
# Selection is a reviewable design decision -- consultant re-reviews it per T-3178.
CANONICAL_SOURCES: tuple[CanonicalSource, ...] = (
    CanonicalSource("role_contract_ref", "role", "repo",
                    "rulebook/persona-orientation/role-deltas.yaml",
                    "repo", "role/role-deltas.yaml", "internal", False),
    CanonicalSource("identity_canon_ref", "identity", "home",
                    ".claude/projects/-home-claude-fallback/memory/identity_recovery.md",
                    "state", "identity/identity_recovery.md", "internal", False),
    CanonicalSource("goal_goals", "goal", "repo", "rulebook/goals.md",
                    "repo", "goals/goals.md", "internal", False),
    CanonicalSource("goal_principles", "goal", "repo", "rulebook/principles.md",
                    "repo", "goals/principles.md", "internal", False),
    CanonicalSource("capability_profile_ref", "capability", "repo",
                    "rulebook/persona-orientation/manifest.yaml",
                    "repo", "capability/persona-orientation-manifest.yaml", "internal", False),
    CanonicalSource("authority_profile_ref", "authority", "repo", "rulebook/directives.md",
                    "repo", "authority/directives.md", "internal", False),
    CanonicalSource("memory_root_ref", "memory", "home",
                    ".claude/projects/-home-claude-fallback/memory/MEMORY.md",
                    "state", "memory/MEMORY.md", "private", True),
    CanonicalSource("runtime_binding_ref", "runtime", "repo", "instance.yaml",
                    "repo", "runtime/instance.yaml", "internal", False),
    # provenance.persona_primitive_ref -- the pinned persona brief (CLAUDE.md).
    CanonicalSource("persona_primitive_ref", "identity", "home", "CLAUDE.md",
                    "state", "persona/CLAUDE.md", "internal", False),
)

_SOURCE_REVISION_TAG = "r1"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo_root), "rev-parse", "HEAD"),
        check=False, capture_output=True, text=True, shell=False,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def _ref_dict(kind: str, scheme: str, staged_rel: str, digest: str) -> dict:
    return {
        "kind": kind,
        "uri": f"{scheme}:{staged_rel}",
        "expected_revision": _SOURCE_REVISION_TAG,
        "expected_sha256": digest,
        "authority_class": "canonical",
        "required": True,
        "purpose_limits": list(PURPOSES),
    }


@dataclass(frozen=True)
class BundleArtifacts:
    continuity_root: Path
    bundle_path: Path
    descriptor_root: Path
    checkpoint_root: Path
    switch_state_root: Path
    descriptor_sha256: str
    checkpoint_path: Path
    payload_sha256: str
    source_policy_sha256: str
    inventory_path: Path
    pinned_revision: str


def build_bundle(
    *,
    continuity_root: Path,
    sources: tuple[CanonicalSource, ...],
    read_source,             # Callable[[CanonicalSource], bytes] -> canonical source bytes
    pinned_revision: str,
    boundary_id: str = "recovery-continuity-canonical",
    uid: int | None = None,
    now: datetime | None = None,
) -> BundleArtifacts:
    """Stage sources, author descriptor+policy, resolve, and capture a verified checkpoint.

    Pure of any tmux/systemd/receiver side effect; writes only under
    ``continuity_root``. Hermetic-testable: callers inject ``sources`` +
    ``read_source`` (returning bytes) so the build logic can run against
    synthetic sources. The production ``read_source`` sources repo bytes from
    the pinned git object (not the worktree) and validates home-source
    provenance.

    APPEND-ONLY: the regenerable subtrees (sources/descriptor/checkpoints/
    inventory) are rebuilt, but the coordinator's ``switch/`` transition+receipt
    history is PRESERVED across rebuilds (consultant T-3178 finding 5).
    """
    uid = os.getuid() if uid is None else uid
    now = now or datetime.now(timezone.utc)

    repo_root = continuity_root / "sources" / "repo"
    state_root = continuity_root / "sources" / "state"
    descriptor_root = continuity_root / "descriptor"
    checkpoint_root = continuity_root / "checkpoints"
    switch_state_root = continuity_root / "switch"
    # Rebuild only the regenerable subtrees; never destroy switch history.
    for regen in (continuity_root / "sources", descriptor_root, checkpoint_root):
        if regen.exists():
            shutil.rmtree(regen)
    (continuity_root / "readback-inventory.json").unlink(missing_ok=True)
    for d in (repo_root, state_root, descriptor_root, checkpoint_root, switch_state_root):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)

    staged: dict[str, dict] = {}
    for src in sources:
        root = repo_root if src.scheme == "repo" else state_root
        dst = root / src.staged_rel
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = read_source(src)
        if not raw:
            raise ValueError(f"canonical source is empty: {src.label}")
        dst.write_bytes(raw)
        os.chmod(dst, 0o644)
        staged[src.label] = {"kind": src.kind, "scheme": src.scheme,
                             "staged_rel": src.staged_rel, "sha256": _sha256(raw),
                             "privacy": src.privacy, "redacted": src.redacted}

    def ref(label: str) -> dict:
        s = staged[label]
        return _ref_dict(s["kind"], s["scheme"], s["staged_rel"], s["sha256"])

    manifest_data = {
        "schema": pc.BUNDLE_SCHEMA_ID,
        "persona_id": PERSONA,
        "organism_id": ORGANISM,
        "role_contract_ref": ref("role_contract_ref"),
        "identity_canon_ref": ref("identity_canon_ref"),
        "values_and_goals_refs": [ref("goal_goals"), ref("goal_principles")],
        "capability_profile_ref": ref("capability_profile_ref"),
        "authority_profile_ref": ref("authority_profile_ref"),
        "memory_root_refs": [ref("memory_root_ref")],
        "commitment_query": {"kind": "commitment", "persona_id": PERSONA,
                             "states": ["active", "waiting"]},
        "runtime_binding_ref": ref("runtime_binding_ref"),
        "provenance": {
            "authored_by": PERSONA,
            "adopted_by": "operator",
            "source_revision": pinned_revision,
            "migrations": [],
            "persona_primitive_ref": ref("persona_primitive_ref"),
        },
    }
    bundle_path = descriptor_root / "bundle.yaml"
    # JSON round-trip breaks shared list refs (PURPOSES) so safe_dump emits no
    # YAML anchors/aliases -- persona_continuity's strict loader forbids them.
    bundle_path.write_text(yaml.safe_dump(json.loads(json.dumps(manifest_data)), sort_keys=False))
    os.chmod(bundle_path, 0o644)

    policy_path = descriptor_root / "policy.json"
    policy_data = {
        "schema": pc.SOURCE_POLICY_SCHEMA_ID,
        "source_locator": "recovery-continuity-canonical",
        "catalog_revision": pinned_revision,
        "actors": [{"actor_id": PERSONA, "authority_refs": ["authority:canonical"],
                    "allowed_purposes": list(PURPOSES)}],
        "entries": [
            {"uri": f"{s['scheme']}:{s['staged_rel']}", "source_revision": _SOURCE_REVISION_TAG,
             "authority_class": "canonical", "privacy": s["privacy"], "redacted": s["redacted"],
             "allowed_purposes": list(PURPOSES), "expected_uid": uid, "max_mode": "0644"}
            for s in staged.values()
        ],
    }
    policy_path.write_bytes(_json_bytes(policy_data))
    os.chmod(policy_path, 0o644)

    # Resolve the canonical view + capture a verified checkpoint.
    manifest = pc.load_continuity_bundle(bundle_path, descriptor_root=descriptor_root)
    policy = pc.load_source_policy_catalog(policy_path)
    store = pc.ContinuitySourceStore(repo_root=repo_root, state_root=state_root, source_policy=policy)
    actor = policy.actor_context(PERSONA, pc.ContinuityPurpose.HYDRATE_RUNTIME)
    resolver = pc.ContinuityResolver(store, checkpoint_root=checkpoint_root)
    view = resolver.resolve(manifest, purpose="hydrate-runtime", actor=actor, as_of=now)

    content = pc.CheckpointContent(
        active_ticket_refs=("ticket:T-3178",),
        promise_refs=(),
        wait_refs=(),
        escalation_refs=(),
        expected_next_action="action:resume-head-system-orchestrator-duties",
        decision_refs=("decision:org-057-r1-model-A-consultant-external-driver",),
        unresolved_questions=(),
        memory_candidate_refs=(),
    )
    boundary = pc.CheckpointBoundary(
        boundary_id=boundary_id, author=PERSONA,
        correlation_id=f"{boundary_id}-{view.view_sha256[:12]}",
        created_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        supersedes_receipt_path=None, supersedes_payload_sha256=None,
    )
    obs = pc.RuntimeObservation(
        runtime_id=f"{PERSONA}-continuity-canonical", harness_id="claude",
        account_ref="account:claude-fallback", responsive=True,
        self_identification="Recovery", role_identification="head-system-orchestrator",
        authority_limits=("authority:canonical",), current_work_refs=("ticket:T-3178",),
        receiver_evidence_sha256=None,
        # A canonical checkpoint captures an ISOLATED boundary state (external
        # actions disabled) -- the discipline persona_continuity enforces
        # (runtime-not-isolated otherwise). During a live fire the checkpoint
        # is re-captured inside the maintenance window; this canonical one
        # records the quiescent identity boundary.
        external_actions_disabled=True,
        observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    receipt = resolver.checkpoint(
        view, boundary_id=boundary_id, runtime_observation=obs, content=content,
        boundary=boundary, producer_revision=pinned_revision, actor=actor, purpose="hydrate-runtime",
    )
    envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=checkpoint_root)
    if envelope.descriptor_sha256 != manifest.descriptor_sha256:
        raise RuntimeError("descriptor-drift: checkpoint envelope does not match bundle descriptor")
    if envelope.persona_id != PERSONA:
        raise RuntimeError(f"checkpoint persona mismatch: {envelope.persona_id}")

    inventory = {
        "schema": "recovery-continuity-readback-inventory/v1",
        "persona_id": PERSONA,
        "organism_id": ORGANISM,
        "generated_at": _now_iso(),
        "pinned_revision": pinned_revision,
        "continuity_root": str(continuity_root),
        "coordinator_config_paths": {
            "bundle_path": str(bundle_path),
            "descriptor_root": str(descriptor_root),
            "checkpoint_root": str(checkpoint_root),
            "switch_state_root": str(switch_state_root),
        },
        "descriptor_sha256": manifest.descriptor_sha256,
        "source_policy_sha256": policy.catalog_sha256,
        "provenance": {
            "repo_sources": "pinned-git-object",
            "home_sources": "symlink-into-repo read from pinned object; non-repo memory read live",
            "worktree": "clean-required (cmd_build refuses dirty unless --allow-dirty)",
        },
        "checkpoint": {
            "path": receipt.path,
            "payload_sha256": envelope.payload_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "boundary_id": boundary_id,
            # HONEST LABEL (consultant T-3178 finding 5): the runtime observation is a
            # pre-filled CANONICAL boundary, not a captured live one. The fire
            # re-captures a live isolated boundary inside the maintenance window.
            "kind": "canonical-fixture-pending-live-capture",
        },
        "sources": [
            {"label": lbl, "kind": s["kind"], "uri": f"{s['scheme']}:{s['staged_rel']}",
             "sha256": s["sha256"], "privacy": s["privacy"], "redacted": s["redacted"]}
            for lbl, s in staged.items()
        ],
    }
    inventory_path = continuity_root / "readback-inventory.json"
    inventory_path.write_bytes(_json_bytes(inventory))
    os.chmod(inventory_path, 0o644)

    return BundleArtifacts(
        continuity_root=continuity_root, bundle_path=bundle_path, descriptor_root=descriptor_root,
        checkpoint_root=checkpoint_root, switch_state_root=switch_state_root,
        descriptor_sha256=manifest.descriptor_sha256, checkpoint_path=Path(receipt.path),
        payload_sha256=envelope.payload_sha256, source_policy_sha256=policy.catalog_sha256,
        inventory_path=inventory_path, pinned_revision=pinned_revision,
    )


def _git_show(repo_root: Path, rev: str, rel: str) -> bytes:
    """Bytes of ``rel`` at the exact pinned object ``rev`` (not the worktree)."""
    result = subprocess.run(
        ("git", "-C", str(repo_root), "show", f"{rev}:{rel}"),
        check=False, capture_output=True, shell=False,
    )
    if result.returncode != 0:
        raise FileNotFoundError(
            f"pinned object missing: {rev}:{rel} :: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def worktree_is_clean(repo_root: Path) -> bool:
    result = subprocess.run(
        ("git", "-C", str(repo_root), "status", "--porcelain"),
        check=False, capture_output=True, text=True, shell=False,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def _production_read_source(repo_root: Path, home: Path, pinned_revision: str):
    """Return canonical source BYTES with real provenance (consultant T-3178 finding 5/6):
    repo-origin bytes come from the PINNED git object; home-origin sources are
    symlink-resolved and validated to live within the repo/home tree before
    reading — a home source that resolves into the repo is read from the pinned
    object too (full provenance), never blindly followed."""
    repo_r = repo_root.resolve()
    home_r = home.resolve()

    def read_source(src: CanonicalSource) -> bytes:
        if src.origin == "repo":
            return _git_show(repo_root, pinned_revision, src.origin_rel)
        # home origin: resolve (following symlinks) and validate the real target.
        real = (home / src.origin_rel).resolve()
        inside_repo = str(real).startswith(str(repo_r) + os.sep)
        inside_home = str(real).startswith(str(home_r) + os.sep)
        if not (inside_repo or inside_home):
            raise ValueError(f"home source {src.label} resolves outside repo/home: {real}")
        if inside_repo:
            # e.g. CLAUDE.md -> pantheon-config/pinned-rules/recovery.md: pin it.
            return _git_show(repo_root, pinned_revision, str(real.relative_to(repo_r)))
        if not real.is_file():
            raise FileNotFoundError(f"canonical source missing: {src.label} -> {real}")
        return real.read_bytes()  # live (non-version-controlled) memory source

    return read_source


def cmd_build(args: argparse.Namespace) -> int:
    continuity_root = Path(args.continuity_root)
    repo_root = Path(args.repo_root)
    home = Path(args.home)
    if continuity_root.exists() and not args.force:
        print(f"refusing to overwrite existing continuity root without --force: {continuity_root}",
              file=sys.stderr)
        return 2
    # Provenance requires a clean worktree so the pinned rev fully describes the
    # repo-origin bytes (consultant T-3178 finding 5). --allow-dirty overrides knowingly.
    if not worktree_is_clean(repo_root) and not args.allow_dirty:
        print(f"refusing to build against a DIRTY worktree at {repo_root} "
              f"(pinned-object provenance would not describe the bytes); pass --allow-dirty to override",
              file=sys.stderr)
        return 3
    pinned = git_head(repo_root)
    artifacts = build_bundle(
        continuity_root=continuity_root,
        sources=CANONICAL_SOURCES,
        read_source=_production_read_source(repo_root, home, pinned),
        pinned_revision=pinned,
    )
    # Lock the continuity root to 0700 (private recovery root).
    os.chmod(continuity_root, 0o700)
    summary = {
        "continuity_root": str(artifacts.continuity_root),
        "pinned_revision": artifacts.pinned_revision,
        "descriptor_sha256": artifacts.descriptor_sha256,
        "checkpoint_path": str(artifacts.checkpoint_path),
        "payload_sha256": artifacts.payload_sha256,
        "source_policy_sha256": artifacts.source_policy_sha256,
        "readback_inventory": str(artifacts.inventory_path),
        "coordinator_config_paths": {
            "persona_id": PERSONA, "organism_id": ORGANISM,
            "runtime_manifest_path": str(repo_root / "instance.yaml"),
            "repo_root": str(repo_root), "state_root": str(args.state_root),
            "bundle_path": str(artifacts.bundle_path),
            "descriptor_root": str(artifacts.descriptor_root),
            "checkpoint_root": str(artifacts.checkpoint_root),
            "switch_state_root": str(artifacts.switch_state_root),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Author Recovery's canonical continuity bundle + checkpoint (T-3178 G3)")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="stage sources, author descriptor+policy, capture verified checkpoint")
    build.add_argument("--continuity-root", default=str(DEFAULT_CONTINUITY_ROOT))
    build.add_argument("--repo-root", default=str(REPO_ROOT))
    build.add_argument("--home", default=str(DEFAULT_HOME))
    build.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    build.add_argument("--force", action="store_true", help="rebuild an existing continuity root")
    build.add_argument("--allow-dirty", action="store_true",
                       help="build even if the worktree is dirty (pinned-object provenance caveat)")
    build.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
