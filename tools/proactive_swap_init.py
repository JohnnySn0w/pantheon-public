#!/usr/bin/env python3
"""Initiate an operator-directed harness swap for one persona."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import persona_bringup


COMPLETION_GATE_SCHEMA = "pantheon-operator-completion-gate/v1"
OPERATOR_RECEIPT_LIMIT_BYTES = 65536


@dataclass(frozen=True)
class OperatorCompletionGateReceipt:
    schema: str
    ok: bool
    status: str | None
    correlation_id: str | None
    destination: str | None
    error: str | None = None


def _default_manifest() -> Path:
    return Path(os.environ.get("PANTHEON_INSTANCE_MANIFEST", str(Path(__file__).resolve().parent.parent / "instance.yaml")))


def _default_state_root() -> Path:
    """T-5028 (b): the same personas-dir resolution as runtime_controller._state_root."""
    value = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    if value:
        return Path(value)
    state_dir = os.environ.get("PANTHEON_STATE_DIR")
    return Path(state_dir) / "personas" if state_dir else Path.home() / ".local" / "state" / "pantheon" / "personas"


def _bounded_error(value: str, *, limit: int = 240) -> str:
    cleaned = " ".join(value.replace("\x00", "").split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def parse_operator_receipt(raw: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("operator receipt is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("operator receipt must be a JSON object")
    return payload


def operator_completion_gate(
    payload: Mapping[str, Any],
    *,
    expected_correlation_id: str,
    expected_destination: str,
) -> OperatorCompletionGateReceipt:
    receipt = payload.get("receipt")
    if not expected_correlation_id or not expected_destination:
        return OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=False,
            status=None,
            correlation_id=None,
            destination=None,
            error="completion gate expectations must be nonempty",
        )
    if not isinstance(payload.get("ok"), bool) or payload.get("ok") is not True or not isinstance(receipt, Mapping):
        return OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=False,
            status=None,
            correlation_id=None,
            destination=None,
            error="operator receipt is not ok:true with receipt object",
        )
    status = receipt.get("status")
    correlation_id = receipt.get("correlation_id")
    destination = receipt.get("destination")
    if not isinstance(status, str) or not isinstance(correlation_id, str) or not isinstance(destination, str):
        return OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=False,
            status=status if isinstance(status, str) else None,
            correlation_id=correlation_id if isinstance(correlation_id, str) else None,
            destination=destination if isinstance(destination, str) else None,
            error="operator completion receipt missing required identity fields",
        )
    if correlation_id != expected_correlation_id or destination != expected_destination:
        return OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=False,
            status=status,
            correlation_id=correlation_id,
            destination=destination,
            error="operator completion receipt identity mismatch",
        )
    if status == "completed":
        return OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=True,
            status=status,
            correlation_id=correlation_id,
            destination=destination,
        )
    return OperatorCompletionGateReceipt(
        schema=COMPLETION_GATE_SCHEMA,
        ok=False,
        status=status,
        correlation_id=correlation_id,
        destination=destination,
        error=f"operator receipt status is not completed: {status!r}",
    )


def completion_gate_main(
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    expected_correlation_id: str,
    expected_destination: str,
) -> int:
    del stderr
    try:
        raw = stdin.read(OPERATOR_RECEIPT_LIMIT_BYTES + 1)
        if len(raw) > OPERATOR_RECEIPT_LIMIT_BYTES:
            raise ValueError("operator receipt exceeds bounded completion-gate limit")
        payload = parse_operator_receipt(raw)
        receipt = operator_completion_gate(
            payload,
            expected_correlation_id=expected_correlation_id,
            expected_destination=expected_destination,
        )
    except Exception as exc:
        receipt = OperatorCompletionGateReceipt(
            schema=COMPLETION_GATE_SCHEMA,
            ok=False,
            status=None,
            correlation_id=None,
            destination=None,
            error=_bounded_error(f"{type(exc).__name__}: {exc}"),
        )
    print(json.dumps(dataclasses.asdict(receipt), sort_keys=True), file=stdout)
    return 0 if receipt.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completion-gate", action="store_true")
    parser.add_argument("--recover-compensation-failed", action="store_true")
    parser.add_argument("--recover-compensation-failed-target-live", action="store_true")
    parser.add_argument("--recover-started-unverified", action="store_true")
    parser.add_argument("--expect-correlation-id")
    parser.add_argument("--expect-destination")
    parser.add_argument("--expect-transition-id")
    parser.add_argument("--expect-transition-sha256")
    parser.add_argument("--expect-predecessor-sha256")
    parser.add_argument("legacy_persona", nargs="?")
    parser.add_argument("--manifest", type=Path, default=_default_manifest())
    parser.add_argument("--persona")
    parser.add_argument("--source")
    parser.add_argument("--destination")
    parser.add_argument("--trigger", choices=("operator",), default="operator")
    parser.add_argument("--correlation-id")
    parser.add_argument("--recovery-id")
    parser.add_argument("--reason")
    parser.add_argument(
        "--override-hold",
        action="store_true",
        help="T-5028: if the destination is operator-held, clear the hold (ledgered via the "
        "harness CLI), alert Recovery, then swap. Requires --reason. Fresh initiations only.",
    )
    parser.add_argument("--state-root", type=Path, default=_default_state_root())
    args = parser.parse_args(argv)

    modes = [
        args.completion_gate,
        args.recover_compensation_failed,
        args.recover_compensation_failed_target_live,
        args.recover_started_unverified,
    ]
    if sum(1 for item in modes if item) > 1:
        parser.error(
            "--completion-gate, --recover-compensation-failed, "
            "--recover-compensation-failed-target-live, and --recover-started-unverified "
            "are mutually exclusive"
        )
    if args.override_hold and any(modes):
        parser.error("--override-hold applies to a fresh operator swap only, not to a gate or recovery mode")
    if args.completion_gate:
        if not args.expect_correlation_id or not args.expect_destination:
            parser.error("--completion-gate requires --expect-correlation-id and --expect-destination")
        return completion_gate_main(
            expected_correlation_id=args.expect_correlation_id,
            expected_destination=args.expect_destination,
        )

    persona = args.persona or args.legacy_persona
    if not persona:
        parser.error("--persona or legacy <persona> is required")
    if args.persona and args.legacy_persona and args.persona != args.legacy_persona:
        parser.error("legacy <persona> must match --persona")
    if args.recover_compensation_failed:
        missing = [
            option
            for option, value in (
                ("--source", args.source),
                ("--destination", args.destination),
                ("--recovery-id", args.recovery_id),
                ("--expect-transition-id", args.expect_transition_id),
                ("--expect-transition-sha256", args.expect_transition_sha256),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--recover-compensation-failed requires " + ", ".join(missing))
        try:
            receipt = persona_bringup.operator_compensation_recovery(
                manifest_path=args.manifest,
                persona=persona,
                source=args.source,
                destination=args.destination,
                recovery_id=args.recovery_id,
                expected_transition_id=args.expect_transition_id,
                expected_transition_sha256=args.expect_transition_sha256,
                reason=args.reason,
                state_root=args.state_root,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "receipt": dataclasses.asdict(receipt)}, sort_keys=True))
        return 0
    if args.recover_compensation_failed_target_live:
        missing = [
            option
            for option, value in (
                ("--source", args.source),
                ("--destination", args.destination),
                ("--recovery-id", args.recovery_id),
                ("--expect-transition-id", args.expect_transition_id),
                ("--expect-transition-sha256", args.expect_transition_sha256),
                ("--expect-predecessor-sha256", args.expect_predecessor_sha256),
                ("--correlation-id", args.correlation_id),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--recover-compensation-failed-target-live requires " + ", ".join(missing))
        try:
            receipt = persona_bringup.operator_compensation_failed_target_live_recovery(
                manifest_path=args.manifest,
                persona=persona,
                source=args.source,
                destination=args.destination,
                recovery_id=args.recovery_id,
                expected_transition_id=args.expect_transition_id,
                expected_transition_sha256=args.expect_transition_sha256,
                expected_predecessor_sha256=args.expect_predecessor_sha256,
                correlation_id=args.correlation_id,
                reason=args.reason,
                state_root=args.state_root,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "receipt": dataclasses.asdict(receipt)}, sort_keys=True))
        return 0
    if args.recover_started_unverified:
        missing = [
            option
            for option, value in (
                ("--source", args.source),
                ("--destination", args.destination),
                ("--recovery-id", args.recovery_id),
                ("--expect-transition-id", args.expect_transition_id),
                ("--expect-transition-sha256", args.expect_transition_sha256),
                ("--correlation-id", args.correlation_id),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--recover-started-unverified requires " + ", ".join(missing))
        try:
            receipt = persona_bringup.operator_started_unverified_recovery(
                manifest_path=args.manifest,
                persona=persona,
                source=args.source,
                destination=args.destination,
                recovery_id=args.recovery_id,
                expected_transition_id=args.expect_transition_id,
                expected_transition_sha256=args.expect_transition_sha256,
                correlation_id=args.correlation_id,
                reason=args.reason,
                state_root=args.state_root,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "receipt": dataclasses.asdict(receipt)}, sort_keys=True))
        return 0
    destination = args.destination
    if destination is None:
        current = persona_bringup.load_binding(args.manifest, persona)
        fallback = persona_bringup.first_fallback_binding(args.manifest, current)
        if fallback is None:
            parser.error(f"{persona} has no configured fallback in {args.manifest}")
        destination = persona_bringup.runtime_reference(fallback)
    correlation_id = args.correlation_id or f"operator-{uuid.uuid4().hex}"
    try:
        receipt = persona_bringup.operator_target_swap(
            manifest_path=args.manifest,
            persona=persona,
            destination=destination,
            trigger=args.trigger,
            correlation_id=correlation_id,
            state_root=args.state_root,
            override_hold=args.override_hold,
            override_reason=args.reason,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "receipt": dataclasses.asdict(receipt)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
