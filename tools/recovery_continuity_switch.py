#!/usr/bin/env python3
"""Operator entrypoint for Recovery's ORG-057:R1 continuity switch (T-3178 gate G2).

``tools/continuity_switch_coordinator.py`` (T-3177) is a library: no operator
CLI, no exact CoordinatorConfig paths, no crash-recovery command. consultant's
T-3178 §8 review gated the fire on that gap ("recovery is not fire-ready until
Recovery adds and dry-runs that entrypoint without receiver mutation"). This module
closes it: it reconstructs the CoordinatorConfig from the pinned paths gate G3
recorded (``tools/recovery_continuity_bundle.py``'s readback inventory) and exposes
the read-only + recovery surface the fire needs.

SAFETY -- dry-run is the default and NEVER mutates a receiver:
  * Every MUTATING coordinator port (start/stop runtime, transfer receiver,
    isolate/release candidate, enter/exit maintenance) is replaced with a
    logging no-op, so plan/preflight/resume/rollback exercise their real
    decision logic against real READ facts (bindings, liveness, receiver
    presence, git revision) without touching tmux, systemd, or the receiver.
  * Dry-run writes transition/receipt evidence into a SANDBOX switch-state root
    (``<continuity>/switch-dryrun``), never the real ``switch`` root, so a
    dry-run cannot corrupt a real in-flight transition.
Only ``--live`` uses the real ports + real switch root, and is intended solely
for the operator-supervised fire (consultant as external driver, model A).

Commands (all default to dry-run):
  show-config             print the reconstructed CoordinatorConfig + pins
  plan   [--candidate-harness codex]   read-only SwitchPlan (writes plan JSON)
  preflight                            plan -> preflight (compatibility/authority/receiver)
  resume --transition-id ID            resume_after_crash: re-derive keep-vs-rollback from live facts
  rollback --plan FILE --reason R --stage PHASE   explicit rollback from a captured plan

This module has NO import-time side effects and performs none until a command
runs.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import continuity_switch_coordinator as csc  # noqa: E402
from tools import persona_continuity as pc  # noqa: E402
from tools import harness_state as hs  # noqa: E402

DEFAULT_REPO_ROOT = REPO_ROOT
RECOVERY_HOME = Path("/home/claude-fallback")


def _default_home() -> Path:
    """Recovery's identity-source home, even when another account drives the fire."""
    return RECOVERY_HOME


def _default_state_root() -> Path:
    """The personas dir, resolved exactly as the runtime planes resolve it (T-6285):
    PANTHEON_PERSONA_STATE_DIR, else $PANTHEON_STATE_DIR/personas, else Recovery's home
    default; a disagreeing pair is refused (HarnessStateError), not guessed."""
    return hs.state_root_from_env(default=_default_home() / ".local/state/pantheon") / "personas"


def _default_continuity_root() -> Path:
    return _default_state_root() / "recovery/continuity"


# The coordinator actor authority Recovery's continuity switch runs under.
ACTOR_PURPOSES = (
    pc.ContinuityPurpose.HYDRATE_RUNTIME,
    pc.ContinuityPurpose.VERIFY_CANDIDATE,
    pc.ContinuityPurpose.ROLLBACK_VERIFY,
    pc.ContinuityPurpose.OPERATOR_AUDIT,
)


def load_pins(continuity_root: Path) -> dict:
    """Read the gate-G3 readback inventory -- the single source of truth for pins."""
    inventory = continuity_root / "readback-inventory.json"
    if not inventory.is_file():
        raise FileNotFoundError(
            f"no continuity readback inventory at {inventory}; run "
            f"`python3 -m tools.recovery_continuity_bundle build` (gate G3) first"
        )
    return json.loads(inventory.read_bytes())


def validate_pins(continuity_root: Path) -> dict:
    """Load the readback inventory AND fail-closed if the on-disk descriptor /
    checkpoint digests have drifted from it (consultant T-3178 finding 4: G2 must
    consume + validate a strict inventory, not merely print it)."""
    pins = load_pins(continuity_root)
    descriptor_root = continuity_root / "descriptor"
    manifest = pc.load_continuity_bundle(descriptor_root / "bundle.yaml", descriptor_root=descriptor_root)
    if manifest.descriptor_sha256 != pins.get("descriptor_sha256"):
        raise ValueError(
            f"descriptor digest drift vs inventory: {manifest.descriptor_sha256} != {pins.get('descriptor_sha256')}")
    ckpt = pins.get("checkpoint") or {}
    envelope = pc.load_checkpoint_envelope(Path(ckpt["path"]), checkpoint_root=continuity_root / "checkpoints")
    if envelope.payload_sha256 != ckpt.get("payload_sha256"):
        raise ValueError("checkpoint payload digest drift vs inventory")
    if envelope.descriptor_sha256 != manifest.descriptor_sha256:
        raise ValueError("checkpoint descriptor does not match bundle descriptor")
    return pins


_PLAN_FIELDS = {"schema", "plan_id", "persona_id", "organism_id", "old_binding_ref",
                "candidate_binding_ref", "manifest_sha256", "requested_at", "plan_sha256"}


def load_validated_plan(path: Path) -> csc.SwitchPlan:
    """Parse a captured plan JSON with strict schema/persona validation (consultant
    finding 4: rollback must not accept an arbitrary JSON plan)."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")
    missing = _PLAN_FIELDS - set(raw)
    extra = set(raw) - _PLAN_FIELDS
    if missing:
        raise ValueError(f"plan missing fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"plan has unknown fields: {sorted(extra)}")
    if raw["schema"] != csc.SWITCH_PLAN_SCHEMA_ID:
        raise ValueError(f"plan schema mismatch: {raw['schema']}")
    if raw["persona_id"] != "recovery":
        raise ValueError(f"plan persona mismatch: {raw['persona_id']}")
    return csc.SwitchPlan(**raw)


def enforce_live_gate(args) -> dict:
    """Hard-gate --live: require an explicit operator token and a
    digest-consistent inventory before ANY real-port command runs (consultant
    finding 4: live mode had no expected-revision/digest/confirmation gate)."""
    if getattr(args, "confirm_fire", None) != "FIRE":
        raise SystemExit("live mode requires --confirm-fire FIRE (operator-supervised fire only)")
    pins = validate_pins(Path(args.continuity_root))
    from tools.recovery_continuity_bundle import git_head
    head = git_head(Path(args.repo_root))
    pinned = pins.get("pinned_revision")
    if pinned and pinned != head:
        # Not fatal (HEAD legitimately advances after the G3 build), but the
        # operator must acknowledge the delta explicitly.
        if getattr(args, "expect_rev", None) != pinned:
            raise SystemExit(
                f"bundle pinned at {pinned} but HEAD is {head}; re-run the G3 build or pass "
                f"--expect-rev {pinned} to fire against the pinned bundle knowingly")
    return pins


def build_config(
    *,
    continuity_root: Path | None = None,
    repo_root: Path = DEFAULT_REPO_ROOT,
    state_root: Path | None = None,
    switch_state_root: Path | None = None,
) -> csc.CoordinatorConfig:
    """Reconstruct the CoordinatorConfig from the pinned continuity paths."""
    continuity_root = continuity_root or _default_continuity_root()
    state_root = state_root or _default_state_root()
    descriptor_root = continuity_root / "descriptor"
    return csc.CoordinatorConfig(
        persona_id="recovery",
        organism_id="pantheon",
        runtime_manifest_path=repo_root / "instance.yaml",
        bundle_path=descriptor_root / "bundle.yaml",
        descriptor_root=descriptor_root,
        repo_root=repo_root,
        state_root=state_root,
        checkpoint_root=continuity_root / "checkpoints",
        switch_state_root=switch_state_root or (continuity_root / "switch"),
        actor=pc.ActorContext("recovery", ("authority:canonical",), ACTOR_PURPOSES),
    )


class _DryRunLog:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, name: str, detail: str = "") -> None:
        self.calls.append(f"{name}({detail})")


@dataclasses.dataclass(frozen=True)
class _DryRunHandle:
    """A stand-in RuntimeHandle for dry-run start_runtime; consumed by nothing
    in the resume/rollback paths (they re-derive from live is_live facts)."""

    binding_ref: str


def dry_run_ports(config: csc.CoordinatorConfig, log: _DryRunLog,
                  base: csc.CoordinatorPorts | None = None) -> csc.CoordinatorPorts:
    """Wrap the real default ports, replacing every MUTATING port with a
    logging no-op. READ ports stay real so decisions reflect live facts.

    ``base`` is injectable so tests can wrap fake ports; production uses the
    real ``default_ports``."""
    base = base if base is not None else csc.default_ports(config)

    def start_runtime(binding):
        log.record("start_runtime", csc._binding_ref(binding))
        return _DryRunHandle(csc._binding_ref(binding))

    def stop_runtime(binding):
        log.record("stop_runtime", csc._binding_ref(binding))

    def transfer_receiver(**kwargs):
        target = kwargs.get("target")
        log.record("transfer_receiver", csc._binding_ref(target) if target is not None else "")
        return _DryRunHandle("transfer")

    def isolate_candidate(binding):
        # TRUE no-op: synthesize the proof; never invoke the real isolation
        # port (which composes writable-roots/network-access changes). consultant
        # T-3178 finding 4 caught the prior base delegation breaking the
        # "every mutating port is a no-op" contract.
        log.record("isolate_candidate", csc._binding_ref(binding))
        mechanism = ("codex-writable-roots-empty" if binding.launch.harness == "codex"
                     else "claude-candidate-guard-active")
        proof = csc.CandidateIsolationProof(
            mechanism=mechanism, evidence_sha256="0" * 64,
            verified_at=csc._iso(csc._now()),
        )
        return binding, proof

    def verify_isolation(binding, proof):
        # Read-classified, but in dry-run the candidate was never really
        # isolated, so a real probe would spuriously fail; report the dry-run
        # synthetic proof as consistent without touching the live sandbox.
        log.record("verify_isolation", csc._binding_ref(binding))
        return True

    def release_isolation(binding):
        log.record("release_isolation", csc._binding_ref(binding))

    def enter_maintenance(persona, *, minutes, reason):
        log.record("enter_maintenance", f"{persona},{minutes}m,{reason}")
        return "dry-run-maintenance"

    def exit_maintenance(persona):
        log.record("exit_maintenance", str(persona))
        return True

    return dataclasses.replace(
        base,
        start_runtime=start_runtime,
        stop_runtime=stop_runtime,
        transfer_receiver=transfer_receiver,
        isolate_candidate=isolate_candidate,
        verify_isolation=verify_isolation,
        release_isolation=release_isolation,
        enter_maintenance=enter_maintenance,
        exit_maintenance=exit_maintenance,
    )


def make_coordinator(args, *, dry_run: bool):
    continuity_root = Path(args.continuity_root)
    if dry_run:
        switch_state_root = continuity_root / "switch-dryrun"
        config = build_config(continuity_root=continuity_root, repo_root=Path(args.repo_root),
                              state_root=Path(args.state_root), switch_state_root=switch_state_root)
        log = _DryRunLog()
        return csc.ContinuitySwitchCoordinator(config, dry_run_ports(config, log)), config, log
    # Live mode: hard-gate on the explicit fire token + a digest-consistent
    # inventory before constructing a real-port coordinator.
    enforce_live_gate(args)
    config = build_config(continuity_root=continuity_root, repo_root=Path(args.repo_root),
                          state_root=Path(args.state_root))
    return csc.ContinuitySwitchCoordinator(config, None), config, None


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_show_config(args) -> int:
    _, config, _ = make_coordinator(args, dry_run=not args.live)
    pins = load_pins(Path(args.continuity_root))
    _print({
        "mode": "live" if args.live else "dry-run",
        "config": {f.name: str(getattr(config, f.name)) for f in dataclasses.fields(config)},
        "pinned_revision": pins.get("pinned_revision"),
        "descriptor_sha256": pins.get("descriptor_sha256"),
        "checkpoint": pins.get("checkpoint"),
    })
    return 0


def cmd_plan(args) -> int:
    coord, config, log = make_coordinator(args, dry_run=not args.live)
    plan = coord.plan(candidate_harness=args.candidate_harness, candidate_account=args.candidate_account)
    plan_dict = {f.name: getattr(plan, f.name) for f in dataclasses.fields(plan)}
    if args.out:
        Path(args.out).write_text(json.dumps(plan_dict, indent=2))
    _print({"mode": "live" if args.live else "dry-run", "plan": plan_dict,
            "dry_run_mutations": (log.calls if log else None)})
    return 0


def cmd_preflight(args) -> int:
    coord, config, log = make_coordinator(args, dry_run=not args.live)
    plan = coord.plan(candidate_harness=args.candidate_harness, candidate_account=args.candidate_account)
    try:
        result = coord.preflight(plan)
    except pc.ContinuityError as exc:
        _print({"mode": "live" if args.live else "dry-run", "preflight": "refused",
                "code": exc.code, "detail": exc.detail,
                "dry_run_mutations": (log.calls if log else None)})
        return 1
    _print({"mode": "live" if args.live else "dry-run",
            "preflight": {f.name: getattr(result, f.name) for f in dataclasses.fields(result)},
            "dry_run_mutations": (log.calls if log else None)})
    return 0


def cmd_resume(args) -> int:
    coord, config, log = make_coordinator(args, dry_run=not args.live)
    try:
        receipt = coord.resume_after_crash(args.transition_id)
    except pc.ContinuityError as exc:
        _print({"mode": "live" if args.live else "dry-run", "resume": "error",
                "code": exc.code, "detail": exc.detail,
                "dry_run_mutations": (log.calls if log else None)})
        return 1
    _print({"mode": "live" if args.live else "dry-run", "resume": "ok",
            "outcome": receipt.outcome, "receipt_sha256": receipt.receipt_sha256,
            "dry_run_mutations": (log.calls if log else None)})
    return 0


def cmd_rollback(args) -> int:
    try:
        plan = load_validated_plan(Path(args.plan))
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        _print({"rollback": "error", "code": "invalid-plan", "detail": str(exc)})
        return 1
    coord, config, log = make_coordinator(args, dry_run=not args.live)
    try:
        receipt = coord.rollback(plan, reason=args.reason, stage_reached=args.stage)
    except pc.ContinuityError as exc:
        _print({"mode": "live" if args.live else "dry-run", "rollback": "error",
                "code": exc.code, "detail": exc.detail,
                "dry_run_mutations": (log.calls if log else None)})
        return 1
    _print({"mode": "live" if args.live else "dry-run", "rollback": "ok",
            "outcome": receipt.outcome, "receipt_sha256": receipt.receipt_sha256,
            "dry_run_mutations": (log.calls if log else None)})
    return 0


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--continuity-root", default=str(_default_continuity_root()))
    sub.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT))
    sub.add_argument("--state-root", default=str(_default_state_root()))
    sub.add_argument("--live", action="store_true",
                     help="use REAL ports + real switch root (fire only; default is dry-run)")
    sub.add_argument("--confirm-fire", default=None,
                     help="required token 'FIRE' to arm --live (operator-supervised fire only)")
    sub.add_argument("--expect-rev", default=None,
                     help="acknowledge firing against a bundle pinned at a rev != current HEAD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recovery ORG-057:R1 continuity switch operator entrypoint (T-3178 G2)")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show-config", help="print the reconstructed CoordinatorConfig + pins")
    _add_common(show)
    show.set_defaults(func=cmd_show_config)

    plan = sub.add_parser("plan", help="read-only SwitchPlan")
    _add_common(plan)
    plan.add_argument("--candidate-harness", default="codex")
    plan.add_argument("--candidate-account", default=None)
    plan.add_argument("--out", default=None, help="write the plan JSON to this path")
    plan.set_defaults(func=cmd_plan)

    preflight = sub.add_parser("preflight", help="plan -> preflight (compatibility/authority/receiver)")
    _add_common(preflight)
    preflight.add_argument("--candidate-harness", default="codex")
    preflight.add_argument("--candidate-account", default=None)
    preflight.set_defaults(func=cmd_preflight)

    resume = sub.add_parser("resume", help="resume_after_crash: re-derive keep-vs-rollback from live facts")
    _add_common(resume)
    resume.add_argument("--transition-id", required=True)
    resume.set_defaults(func=cmd_resume)

    rollback = sub.add_parser("rollback", help="explicit rollback from a captured plan")
    _add_common(rollback)
    rollback.add_argument("--plan", required=True, help="path to a captured plan JSON")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--stage", required=True, help="stage_reached (SwitchPhase)")
    rollback.set_defaults(func=cmd_rollback)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
