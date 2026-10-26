#!/usr/bin/env python3
"""Disposable real-user-manager acceptance helper for T-2381.

The root wrapper provisions and removes the synthetic Unix seat.  This helper
owns only fixture-local, unprivileged work: deriving an isolated manifest in
the disposable clone, proving that clone cannot push, running the reviewed
PersonaInstaller through LocalHostBackend, and validating teardown markers.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

# Direct path execution must import from the same disposable clone as this file,
# never accidentally from the operator's current working directory.
SCRIPT_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from tools.persona_installer import (
    ActorContext,
    LocalHostBackend,
    Manifest,
    PersonaInstaller,
)


FIXTURE_ACCOUNT = "t2381-fixture"
FIXTURE_PERSONA = "t2381-fixture"
FIXTURE_PROBE_PEER = "t2381-fixture-peer"
FIXTURE_ROLE = "t2381-fixture-role"
MARKER_SCHEMA = "pantheon-t2381-live-fixture/v1"
REPORT_SCHEMA = "pantheon-t2381-live-proof/v1"
SERVICE_MANIFEST_LINE = (
    "Environment=PANTHEON_INSTANCE_MANIFEST=%h/pantheon-config/instance.yaml"
)
FIXTURE_SERVICE_MANIFEST_LINE = (
    "Environment=PANTHEON_INSTANCE_MANIFEST="
    "%h/pantheon-config/.t2381-fixture/instance.yaml"
)


class FixtureRefused(RuntimeError):
    """Raised before mutation when a disposable-fixture invariant is false."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _atomic_write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_fixture_names(persona: str, account: str) -> None:
    if persona != FIXTURE_PERSONA or account != FIXTURE_ACCOUNT:
        raise FixtureRefused(
            f"fixture identity must be exactly {FIXTURE_PERSONA}/{FIXTURE_ACCOUNT}"
        )
    if persona in {"consultant", "codex", "recovery", "orchestrator", "specialist", "archivist"}:
        raise FixtureRefused("fixture persona collides with a durable Pantheon identity")


def marker_document(account: str, persona: str, run_id: str) -> dict[str, str]:
    _validate_fixture_names(persona, account)
    if not run_id or any(character not in "0123456789TZ-" for character in run_id):
        raise FixtureRefused("run_id must be a bounded UTC timestamp token")
    return {
        "schema": MARKER_SCHEMA,
        "account": account,
        "persona": persona,
        "run_id": run_id,
    }


def write_marker(
    path: Path,
    account: str,
    persona: str,
    run_id: str,
    *,
    require_root: bool = True,
) -> None:
    if require_root and os.geteuid() != 0:
        raise FixtureRefused("marker creation is a root leg")
    document = marker_document(account, persona, run_id)
    raw = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o400)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_marker(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise FixtureRefused(f"cleanup marker is missing or not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureRefused(f"cleanup marker is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise FixtureRefused(f"cleanup marker has invalid shape: {path}")
    return value


def check_cleanup_markers(
    controller_marker: Path,
    mailbox_marker: Path,
    account: str,
    persona: str,
    *,
    require_root_owner: bool = True,
) -> dict[str, str]:
    _validate_fixture_names(persona, account)
    controller = _read_marker(controller_marker)
    mailbox = _read_marker(mailbox_marker)
    if controller != mailbox:
        raise FixtureRefused("controller and mailbox ownership markers differ")
    expected = marker_document(account, persona, controller.get("run_id", ""))
    if controller != expected:
        raise FixtureRefused("ownership marker does not match the exact fixture identity")
    if require_root_owner:
        for path in (controller_marker, mailbox_marker):
            stat = path.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o077:
                raise FixtureRefused(
                    f"cleanup marker must be root-owned and mode 0400: {path}"
                )
    return controller


def marker_field(path: Path, field: str) -> str:
    value = _read_marker(path)
    if field not in value:
        raise FixtureRefused(f"marker field missing: {field}")
    return value[field]


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repo_root), *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def assert_push_incapable(repo_root: Path) -> dict[str, str]:
    remotes = _run_git(repo_root, "remote")
    if remotes.returncode != 0:
        raise FixtureRefused(remotes.stderr.strip() or "cannot inspect fixture remotes")
    remote_names = tuple(item for item in remotes.stdout.splitlines() if item.strip())
    if remote_names:
        raise FixtureRefused(f"fixture clone still has remotes: {remote_names}")
    helpers = _run_git(repo_root, "config", "--show-origin", "--get-all", "credential.helper")
    if helpers.returncode not in {0, 1}:
        raise FixtureRefused(helpers.stderr.strip() or "cannot inspect credential helpers")
    helper_values = tuple(item for item in helpers.stdout.splitlines() if item.strip())
    if helper_values:
        raise FixtureRefused("fixture clone can resolve a credential helper")
    return {
        "remote_count": "0",
        "credential_helper_count": "0",
        "push_capable": "false",
    }


def configure_fixture_probe_peer(document: dict[str, Any]) -> None:
    """Point only the derived fixture at its disposable sibling mailbox."""
    installer = document.get("persona_installer")
    if not isinstance(installer, dict):
        raise FixtureRefused("source manifest lacks persona_installer mapping")
    mailbox = installer.get("mailbox")
    if not isinstance(mailbox, dict):
        raise FixtureRefused("source manifest lacks persona_installer.mailbox mapping")
    installer = dict(installer)
    mailbox = dict(mailbox)
    mailbox["probe_peer"] = FIXTURE_PROBE_PEER
    installer["mailbox"] = mailbox
    document["persona_installer"] = installer


def probe_mailbox_write(directory: Path, direction: str) -> dict[str, str]:
    """Durably create/read/remove one probe or fail with direction and path."""
    if direction not in {"receive", "sibling-send"}:
        raise FixtureRefused(f"unsupported mailbox write direction: {direction}")
    # Lexical normalization preserves the exact denied target in a negative
    # control; Path.resolve() itself may fail before we can report direction.
    directory = Path(os.path.abspath(directory))
    payload = f"t2381-mailbox-write:{direction}:{uuid.uuid4()}".encode("utf-8")
    probe: Path | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".t2381-write-probe-",
            dir=directory,
        )
        probe = Path(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        observed = probe.read_bytes()
        if observed != payload:
            raise FixtureRefused(
                f"mailbox write probe content mismatch: direction={direction} "
                f"path={directory}"
            )
        probe.unlink()
        probe = None
    except OSError as exc:
        raise FixtureRefused(
            f"mailbox write probe failed: direction={direction} "
            f"path={directory}: {exc}"
        ) from exc
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    return {
        "direction": direction,
        "path": str(directory),
        "durable_round_trip": "true",
        "sha256": _sha256(payload),
    }


def prepare_fixture(
    repo_root: Path,
    source_manifest: Path,
    fixture_manifest: Path,
    persona: str,
    account: str,
) -> dict[str, str]:
    """Derive fixture-only config while preserving the clone's source manifest."""
    _validate_fixture_names(persona, account)
    repo_root = repo_root.resolve()
    source_manifest = source_manifest.resolve()
    fixture_manifest = fixture_manifest.resolve()
    expected_home = Path(pwd.getpwnam(account).pw_dir).resolve()
    if repo_root.parent != expected_home:
        raise FixtureRefused("fixture repository must live directly under its fresh home")
    if source_manifest != repo_root / "instance.yaml":
        raise FixtureRefused("source manifest must be the untouched clone instance.yaml")
    expected_fixture = repo_root / ".t2381-fixture" / "instance.yaml"
    if fixture_manifest != expected_fixture:
        raise FixtureRefused("fixture manifest path is not the isolated fixture path")
    source_before = source_manifest.read_bytes()
    try:
        document = yaml.safe_load(source_before.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise FixtureRefused(f"cannot parse source manifest: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("roles"), dict):
        raise FixtureRefused("source manifest lacks a roles mapping")
    if any(
        isinstance(value, dict)
        and (value.get("persona") == persona or value.get("account") == account)
        for value in document["roles"].values()
    ):
        raise FixtureRefused("synthetic fixture identity already exists in source manifest")
    document["instance"] = dict(document.get("instance", {}))
    document["instance"]["id"] = "pantheon-t2381-live-fixture"
    document["roles"] = dict(document["roles"])
    document["roles"][FIXTURE_ROLE] = {
        "persona": persona,
        "account": account,
        "harness": "codex",
        "domain": "synthetic T-2381 disposable live-acceptance seat",
    }
    configure_fixture_probe_peer(document)
    raw_fixture = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
    _atomic_write(fixture_manifest, raw_fixture, 0o600)

    service_template = repo_root / "tools/systemd/pantheon-persona-consume@.service"
    service_before = service_template.read_text(encoding="utf-8")
    if service_before.count(SERVICE_MANIFEST_LINE) != 1:
        raise FixtureRefused("service template manifest seam is missing or ambiguous")
    service_after = service_before.replace(
        SERVICE_MANIFEST_LINE,
        FIXTURE_SERVICE_MANIFEST_LINE,
    )
    _atomic_write(service_template, service_after.encode("utf-8"), 0o644)

    brief = repo_root / "pinned-rules" / f"{persona}.md"
    _atomic_write(
        brief,
        (
            "# Synthetic T-2381 fixture persona\n\n"
            "This identity exists only for the disposable installer acceptance run. "
            "It owns no standing work and must not be used outside that run.\n"
        ).encode("utf-8"),
        0o644,
    )
    home = expected_home
    _atomic_write(
        home / "AGENTS.md",
        (
            "# Disposable acceptance standing prompt\n\n"
            "You are the synthetic T-2381 fixture. Do not initiate work; consume only "
            "the bounded acceptance mailbox turn.\n"
        ).encode("utf-8"),
        0o644,
    )
    _atomic_write(
        home / ".codex" / "config.toml",
        b"# Non-secret fixture config; no auth material is installed.\n",
        0o600,
    )
    if (home / ".codex" / "auth.json").exists():
        raise FixtureRefused("fixture home unexpectedly contains Codex auth material")
    if source_manifest.read_bytes() != source_before:
        raise FixtureRefused("source instance.yaml changed during fixture derivation")
    push = assert_push_incapable(repo_root)
    return {
        "source_manifest_sha256": _sha256(source_before),
        "fixture_manifest_sha256": _sha256(raw_fixture),
        "source_manifest_unchanged": "true",
        "probe_peer": FIXTURE_PROBE_PEER,
        **push,
    }


def _all_capabilities(plan: Any) -> frozenset[str]:
    operations = {
        operation.provenance["required_capability"]
        for operation in plan.operations
        if operation.provenance.get("required_capability")
    }
    actions = {action.required_capability for action in plan.operator_actions}
    return frozenset(operations | actions)


def run_live_acceptance(
    repo_root: Path,
    manifest_path: Path,
    persona: str,
    report_path: Path,
    account: str = FIXTURE_ACCOUNT,
) -> int:
    _validate_fixture_names(persona, account)
    current = pwd.getpwuid(os.geteuid())
    if current.pw_name != account:
        raise FixtureRefused("live acceptance must run as the fixture uid")
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    expected_home = Path(current.pw_dir).resolve()
    if repo_root.parent != expected_home:
        raise FixtureRefused("live repository is outside the fixture home")
    if manifest_path != repo_root / ".t2381-fixture" / "instance.yaml":
        raise FixtureRefused("live run did not receive the isolated fixture manifest")
    push = assert_push_incapable(repo_root)
    if (expected_home / ".codex" / "auth.json").exists():
        raise FixtureRefused("live fixture must not contain copied auth material")

    manifest = Manifest.load(manifest_path)
    installer = PersonaInstaller(repo_root=repo_root)
    plan = installer.plan(
        manifest,
        persona,
        harness_override="codex",
        root=Path("/"),
    )
    if plan.account != account or plan.persona != persona or plan.root != "/":
        raise FixtureRefused("resolved plan escaped the synthetic fixture binding")
    actor = ActorContext(
        uid=os.geteuid(),
        persona=persona,
        role=plan.role,
        instance=plan.instance,
        capabilities=_all_capabilities(plan),
    )
    backend = LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    applied = installer.apply(plan, plan.digest, backend, actor, manifest=manifest)
    verified = installer.verify(plan, backend)
    success = (
        applied.status in {"applied", "noop"}
        and verified.status == "verified"
        and verified.evidence_scope == "live"
        and verified.live_acceptance
    )
    payload = {
        "schema": REPORT_SCHEMA,
        "success": success,
        "fixture": {
            "account": account,
            "persona": persona,
            "uid": os.geteuid(),
            "auth_material_present": False,
            **push,
        },
        "plan": {
            "digest": plan.digest,
            "instance": plan.instance,
            "role": plan.role,
            "root": plan.root,
            "source_revision": plan.provenance.get("source_revision", ""),
        },
        "apply": _jsonable(applied),
        "verify": _jsonable(verified),
    }
    _atomic_write(
        report_path,
        (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    print(
        f"apply={applied.status} verify={verified.status} "
        f"scope={verified.evidence_scope} live={verified.live_acceptance}"
    )
    return 0 if success else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    marker = commands.add_parser("write-marker")
    marker.add_argument("--path", type=Path, required=True)
    marker.add_argument("--account", default=FIXTURE_ACCOUNT)
    marker.add_argument("--persona", default=FIXTURE_PERSONA)
    marker.add_argument("--run-id", required=True)

    field = commands.add_parser("marker-field")
    field.add_argument("--path", type=Path, required=True)
    field.add_argument("--field", required=True)

    cleanup = commands.add_parser("check-cleanup")
    cleanup.add_argument("--controller-marker", type=Path, required=True)
    cleanup.add_argument("--mailbox-marker", type=Path, required=True)
    cleanup.add_argument("--account", default=FIXTURE_ACCOUNT)
    cleanup.add_argument("--persona", default=FIXTURE_PERSONA)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--repo-root", type=Path, required=True)
    prepare.add_argument("--source-manifest", type=Path, required=True)
    prepare.add_argument("--fixture-manifest", type=Path, required=True)
    prepare.add_argument("--account", default=FIXTURE_ACCOUNT)
    prepare.add_argument("--persona", default=FIXTURE_PERSONA)
    prepare.add_argument("--evidence", type=Path, required=True)

    run = commands.add_parser("run")
    run.add_argument("--repo-root", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--account", default=FIXTURE_ACCOUNT)
    run.add_argument("--persona", default=FIXTURE_PERSONA)

    write_probe = commands.add_parser("probe-mailbox-write")
    write_probe.add_argument("--directory", type=Path, required=True)
    write_probe.add_argument("--direction", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "write-marker":
            write_marker(args.path, args.account, args.persona, args.run_id)
            return 0
        if args.command == "marker-field":
            print(marker_field(args.path, args.field))
            return 0
        if args.command == "check-cleanup":
            document = check_cleanup_markers(
                args.controller_marker,
                args.mailbox_marker,
                args.account,
                args.persona,
            )
            print(json.dumps(document, sort_keys=True))
            return 0
        if args.command == "prepare":
            evidence = prepare_fixture(
                args.repo_root,
                args.source_manifest,
                args.fixture_manifest,
                args.persona,
                args.account,
            )
            _atomic_write(
                args.evidence,
                (json.dumps(evidence, sort_keys=True, indent=2) + "\n").encode(),
                0o600,
            )
            print(json.dumps(evidence, sort_keys=True))
            return 0
        if args.command == "run":
            return run_live_acceptance(
                args.repo_root,
                args.manifest,
                args.persona,
                args.report,
                args.account,
            )
        if args.command == "probe-mailbox-write":
            evidence = probe_mailbox_write(args.directory, args.direction)
            print(json.dumps(evidence, sort_keys=True))
            return 0
    except (FixtureRefused, OSError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
