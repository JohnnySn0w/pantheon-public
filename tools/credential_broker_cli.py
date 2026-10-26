#!/usr/bin/env python3
"""Installer-interface CLI over tools/credential_broker.py (ORG-051, T-2868).

Phase 0 groundwork only (T-2868, parent scoping T-2828 / notes/2026-08-11-
org051-credential-migration-scoping.md). This CLI never reads, prints, or
accepts a secret VALUE -- every verb operates on non-secret reference
metadata (ids, owners, on-disk paths, modes, expiry), the same fields
tools/credential_broker.py's own dataclasses already treat as safe.

Verbs, matching ORG-051 (architecture/adr/ORG-051-secret-and-authentication-lifecycle.md)
Installer interface:

    check / verify <ref-spec.json> --audit-path PATH
        Build a CredentialRef from a ref-spec file (metadata only -- see
        `_ref_from_spec`) and call broker.verify(). Prints owner/mode/expiry/
        path STATUS only; the value is never opened for reading here, only
        stat()'d. `check` and `verify` are the same operation -- ORG-051
        names both; this CLI treats them as aliases.
    plan [--manifest instance.yaml] [--persona orchestrator]
        Read instance.yaml's `roles.<role>.credential_refs` (via
        tools/persona_installer.py's manifest parser) and print reference
        ids + intended ownership ONLY, per ORG-051's literal text ("plan
        reports reference ids and intended ownership only") -- not kind,
        provider, scope, or location, even though none of those are secret
        either.
    reap --audit-path PATH
        Call broker.reap_expired() and print revoked lease ids. A fresh CLI
        process always starts with an empty in-memory lease table (leases
        are process-local in tools/credential_broker.py by design -- T-2380
        never persisted them), so a standalone invocation always reports
        zero revocations. This verb exists to prove the reaper access-path
        exists per the installer contract; wiring it to fire periodically
        (a systemd timer against a long-running broker consumer) is
        deliberately out of scope here -- that consumer doesn't exist until
        T-2870.
    install / rotate / revoke <credential-id | lease-id | ref-spec.json>
        The secret-touching installer verbs. GATED STUBS this phase: each
        validates its non-secret arguments (ref-spec shape, or a bounded
        identifier) and then refuses with a fixed message, pending the
        T-2869 custody-envelope decision (operator-reserved). No verb accepts
        a secret as a positional argument -- `install` takes a ref-spec
        *path*, `rotate`/`revoke` take a bounded identifier, never a value.

Two different "provider" vocabularies meet at this boundary and are easy to
conflate: instance.yaml's persona_installer schema uses `provider: broker`
to mean "this reference is arbitrated by the broker" (vs. a direct,
justified reference) -- an installer-contract concept. A ref-spec fed to
`check`/`verify` instead uses tools/credential_broker.py's own `provider`
field, whose only implemented value today is `"host-file"` (the concrete
storage backend). They are not interchangeable; a ref-spec's `provider` must
be `"host-file"` even when the owning instance.yaml entry says `broker`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

_THIS = Path(__file__).resolve()
_REPO_ROOT_DEFAULT = _THIS.parent.parent
if str(_REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_DEFAULT))

from tools.credential_broker import (  # noqa: E402
    IDENTIFIER_RE,
    MAX_IDENTIFIER_LENGTH,
    CredentialRef,
    HostFileConstraints,
    HostFileCredentialBroker,
)
from tools.persona_installer import Manifest, ManifestError  # noqa: E402

REPO_ROOT = _REPO_ROOT_DEFAULT
DEFAULT_MANIFEST = REPO_ROOT / "instance.yaml"

# T-2869 (operator-reserved custody envelope) gates every secret-touching
# installer verb. See notes/2026-08-11-org051-credential-migration-scoping.md
# section 6.
GATE_MESSAGE = (
    "secret-touching op gated on T-2869 custody decision; not performed in Phase 0"
)


class CliError(RuntimeError):
    """A refusal reported on stderr with a non-zero exit; never wraps a secret."""


def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _validate_identifier_arg(label: str, value: str) -> None:
    """Reuse the broker's own identifier bound so CLI + library never drift."""
    if len(value) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER_RE.fullmatch(value):
        raise CliError(f"{label} must be a bounded non-secret identifier")


def _parse_mode(label: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise CliError(f"{label} must be an octal mode string")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw, 8)
        except ValueError as exc:
            raise CliError(f"{label} must be an octal mode string") from exc
    else:
        raise CliError(f"{label} must be an octal mode string")
    if value < 0 or value > 0o777:
        raise CliError(f"{label} must be a valid Unix permission mode")
    return value


def _load_ref_spec(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read ref spec: {exc.__class__.__name__}") from exc
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"ref spec is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise CliError("ref spec must be a JSON object")
    return spec


def _ref_from_spec(spec: dict) -> CredentialRef:
    """Build a broker CredentialRef from non-secret reference metadata.

    Every field here is an identifier, a path, or ownership/mode metadata --
    mirroring tools/credential_broker.py's own dataclasses, which likewise
    never carry a secret value (the value lives only in the bytes behind
    `location`, and this function never opens that path).
    """
    try:
        host_file_spec = spec.get("host_file")
        host_file = None
        if host_file_spec is not None:
            if not isinstance(host_file_spec, dict):
                raise CliError("ref spec: host_file must be an object")
            host_file = HostFileConstraints(
                parent_dir=Path(host_file_spec["parent_dir"]),
                parent_owner_uid=int(host_file_spec["parent_owner_uid"]),
                parent_owner_gid=int(host_file_spec["parent_owner_gid"]),
                parent_mode=_parse_mode(
                    "ref spec: host_file.parent_mode",
                    host_file_spec.get("parent_mode", "0700"),
                ),
            )
        return CredentialRef(
            credential_id=spec["credential_id"],
            kind=spec["kind"],
            provider=spec["provider"],
            location=Path(spec["location"]),
            owner_uid=int(spec["owner_uid"]),
            owner_gid=int(spec["owner_gid"]),
            mode=_parse_mode("ref spec: mode", spec.get("mode", "0600")),
            roles=tuple(spec.get("roles", ())),
            instances=tuple(spec.get("instances", ())),
            services=tuple(spec.get("services", ())),
            actions=tuple(spec.get("actions", ())),
            host_file=host_file,
            expires_at=spec.get("expires_at"),
        )
    except KeyError as exc:
        raise CliError(f"ref spec missing required field: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise CliError(f"ref spec is invalid: {exc}") from exc


def _cmd_verify(args: argparse.Namespace) -> int:
    spec = _load_ref_spec(args.ref_spec)
    ref = _ref_from_spec(spec)
    try:
        broker = HostFileCredentialBroker((ref,), audit_path=args.audit_path)
    except ValueError as exc:
        raise CliError(f"ref spec is incompatible with the broker: {exc}") from exc
    status = broker.verify(ref)
    _print_json(
        {
            "credential_id": status.credential_id,
            "available": status.available,
            "location": str(ref.location),
            "owner_uid": status.owner_uid,
            "owner_gid": status.owner_gid,
            "mode": None if status.mode is None else format(status.mode, "04o"),
            "expires_at": status.expires_at,
            "errors": list(status.errors),
        }
    )
    return 0 if status.available else 1


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        manifest = Manifest.load(args.manifest, strict_flip_keys=False)
        binding = manifest.binding(args.persona)
    except ManifestError as exc:
        raise CliError(str(exc)) from exc
    # ORG-051 §Installer interface, verbatim: "plan reports reference ids and
    # intended ownership only." No kind/provider/scope/location.
    refs = [
        {"id": ref.reference_id, "owner": ref.owner} for ref in binding.credential_refs
    ]
    _print_json({"persona": args.persona, "credential_refs": refs})
    return 0


def _cmd_reap(args: argparse.Namespace) -> int:
    broker = HostFileCredentialBroker((), audit_path=args.audit_path)
    revoked = broker.reap_expired()
    _print_json({"revoked_lease_ids": list(revoked)})
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    # Validate the ref-spec shape only -- never opened as material.
    _ref_from_spec(_load_ref_spec(args.ref_spec))
    print(GATE_MESSAGE, file=sys.stderr)
    return 1


def _cmd_rotate(args: argparse.Namespace) -> int:
    _validate_identifier_arg("credential_id", args.credential_id)
    print(GATE_MESSAGE, file=sys.stderr)
    return 1


def _cmd_revoke(args: argparse.Namespace) -> int:
    _validate_identifier_arg("lease_id", args.lease_id)
    print(GATE_MESSAGE, file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credential_broker_cli.py", description=__doc__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("check", "verify"):
        child = subparsers.add_parser(
            name, help="resolve a ref-spec file and call broker.verify()"
        )
        child.add_argument(
            "ref_spec", type=Path, help="path to a JSON ref-spec file (metadata only)"
        )
        child.add_argument("--audit-path", required=True, type=Path)

    plan_parser = subparsers.add_parser(
        "plan", help="print credential_refs ids + intended ownership"
    )
    plan_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    plan_parser.add_argument("--persona", default="orchestrator")

    reap_parser = subparsers.add_parser(
        "reap", help="call broker.reap_expired() and print revoked lease ids"
    )
    reap_parser.add_argument("--audit-path", required=True, type=Path)

    install_parser = subparsers.add_parser(
        "install", help="GATED in Phase 0 -- refuses, pending T-2869"
    )
    install_parser.add_argument("ref_spec", type=Path)

    rotate_parser = subparsers.add_parser(
        "rotate", help="GATED in Phase 0 -- refuses, pending T-2869"
    )
    rotate_parser.add_argument("credential_id")

    revoke_parser = subparsers.add_parser(
        "revoke", help="GATED in Phase 0 -- refuses, pending T-2869"
    )
    revoke_parser.add_argument("lease_id")

    return parser


_HANDLERS = {
    "check": _cmd_verify,
    "verify": _cmd_verify,
    "plan": _cmd_plan,
    "reap": _cmd_reap,
    "install": _cmd_install,
    "rotate": _cmd_rotate,
    "revoke": _cmd_revoke,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _HANDLERS[args.command](args)
    except CliError as exc:
        print(f"credential_broker_cli: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
