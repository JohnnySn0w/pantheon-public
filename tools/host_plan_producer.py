"""Unprivileged plan producer for the ORG-052 host broker (T-5533, design (c)).

Intent (T-2652 / ORG-052 section 1 and 3, split protocol from privilege): the
privileged broker (``tools.host_control``) no longer computes installer plans.
At registration time (T-2920) the root installer runs THIS module, as an
unprivileged user, from the root-owned framework copy. It plans the seat with
``PersonaInstaller`` (so it may load the installer's agent-facing closure: the
Codex Telegram bridge, the harness adapter and the mailbox helpers), and emits
the plan's canonical JSON. The root installer places those bytes under a
trusted root and registers ``{seat_id, manifest_revision, path, sha256}`` in the
seat registry's ``plans`` list. The broker then only reads that file through
the root-owned loader and runs its checks
(``tools.host_control.planner.HostPlanService.plan``).

Determinism (T-5533 survey): the plan is a pure function of the manifest file,
the seat target and the framework tree once git's environment is scrubbed.
``PersonaInstaller.plan`` has no clock, uuid, random or hostname input; it does
run ``git rev-parse HEAD`` in the framework root (``provenance.source_revision``
and every unit banner), which honours ``GIT_*`` variables, and it re-reads the
manifest from its path. The producer therefore:

* refuses unless ``--framework-root`` is its own repo root, so the planning code
  that runs is the tree whose templates it reads;
* removes every ``GIT_*`` variable for the duration of the plan;
* refuses when the manifest bytes differ from the registered sha256, before AND
  after planning (the after check covers the installer's own re-reads);
* recomputes the framework artifact digest (``FRAMEWORK_CODE_ARTIFACTS`` plus
  the registered template ids, the installer's own ``_artifact_digest``) over
  ``--framework-root`` and refuses unless it equals the manifest record's
  ``framework_sha256`` AND the plan's ``source_artifact_sha256``: a manifest
  record carrying a stale digest never gets a plan (T-5533, orchestrator's scope add);
* refuses early when the plan's target or declared framework revision differ
  from the records (the broker re-checks all of them);
* refuses a plan whose effective broker order launches before containment
  (T-5470, ``host_control.planner.refuse_launch_before_containment``), and
  every other refusal of the broker partition that check runs (T-5617: the
  residue cap, the routed one-per-kind bound, an admitted op ordered after
  residue), as a clean ``refused`` with rc 2.

It also holds the frozen-manifest template binding check that used to run in
the broker (``validate_frozen_manifest``).

The planning closure is wider than the registered ``framework_sha256``, so the
plans must be regenerated and re-registered on EVERY framework-copy update.

CLI::

    python3 -I tools/host_plan_producer.py --framework-root ROOT \\
        --seat-record JSON --manifest-record JSON --out ABSOLUTE_PATH

Writes the plan bytes to ``--out`` (exclusive create, mode 0644) and prints the
registry plan record as one canonical JSON line.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.persona_installer import (  # noqa: E402
    Manifest,
    ManifestError,
    PersonaInstaller,
    _artifact_digest,
)
from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS, _canonical_json  # noqa: E402
from tools.host_control.contracts import PolicyDenied  # noqa: E402
from tools.host_control.planner import refuse_launch_before_containment  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The registry record field sets the producer accepts (the same records the
#: root installer writes into the seat registry; tools/host_control/registry.py).
SEAT_RECORD_KEYS = frozenset(
    ("seat_id", "uid", "persona", "role", "instance", "account", "harness", "install_root")
)
MANIFEST_RECORD_KEYS = frozenset(
    ("revision", "path", "sha256", "framework_revision", "framework_sha256", "template_ids")
)
MANIFEST_MAX_BYTES = 1024 * 1024
#: Bound on the broker-policy detail carried into a refusal message (T-5617).
REFUSAL_DETAIL_LIMIT = 200


class PlanProducerError(RuntimeError):
    """The producer refused to emit a plan."""


@contextlib.contextmanager
def git_environment_scrubbed() -> Iterator[None]:
    """Remove every ``GIT_*`` variable while the block runs, then restore them.

    ``PersonaInstaller.plan`` runs ``git -C <framework> rev-parse HEAD``; a
    ``GIT_DIR``/``GIT_WORK_TREE``/... in the environment would change the plan.
    """

    saved = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(saved)


def validate_frozen_manifest(manifest: Manifest, template_ids: tuple[str, ...]) -> None:
    """The manifest's systemd, quota, and MCP-audit templates must be exactly the registered
    ``template_ids`` (moved verbatim from the broker's planner, T-5533)."""

    systemd = manifest.installer.get("systemd")
    if not isinstance(systemd, dict):
        raise PlanProducerError("frozen manifest systemd section is invalid")
    quota_check = manifest.installer.get("quota_check")
    if quota_check is None:
        quota_check = {}
    if not isinstance(quota_check, dict):
        raise PlanProducerError("frozen manifest quota_check section is invalid")
    mcp_audit = manifest.installer.get("mcp_allowlist_audit")
    if mcp_audit is None:
        mcp_audit = {}
    if not isinstance(mcp_audit, dict):
        raise PlanProducerError("frozen manifest mcp_allowlist_audit section is invalid")
    actual = (
        systemd.get("service_template"),
        systemd.get("timer_template"),
        quota_check.get(
            "service_template",
            "tools/systemd/pantheon-persona-quota@.service",
        ),
        quota_check.get(
            "timer_template",
            "tools/systemd/pantheon-persona-quota@.timer",
        ),
        *(
            (mcp_audit.get("service_template"), mcp_audit.get("timer_template"))
            if mcp_audit else ()
        ),
    )
    if actual != template_ids:
        raise PlanProducerError("registered template binding mismatch")


def framework_artifact_digest(template_ids: tuple[str, ...]) -> str:
    """The framework digest the registry's ``framework_sha256`` is defined as:
    the installer's ``_artifact_digest`` over ``FRAMEWORK_CODE_ARTIFACTS`` plus
    the registered template ids, read from this producer's own repo root."""

    return _artifact_digest(
        REPO_ROOT,
        tuple(Path(item) for item in (*FRAMEWORK_CODE_ARTIFACTS, *template_ids)),
    )


def _record(value: object, keys: frozenset[str], context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PlanProducerError(f"invalid {context} record fields")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_manifest(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MANIFEST_MAX_BYTES + 1)
    if len(raw) > MANIFEST_MAX_BYTES:
        raise PlanProducerError("manifest exceeds size limit")
    return raw


def produce_plan(
    *,
    framework_root: Path,
    seat_record: Mapping[str, object],
    manifest_record: Mapping[str, object],
) -> bytes:
    """Plan ``seat_record`` from ``manifest_record`` and return the canonical
    JSON bytes of the full installer plan (the broker partitions it)."""

    if Path(framework_root).resolve() != REPO_ROOT:
        raise PlanProducerError("framework root is not the producer's own repo root")
    seat = _record(seat_record, SEAT_RECORD_KEYS, "seat")
    manifest = _record(manifest_record, MANIFEST_RECORD_KEYS, "manifest")
    manifest_path = manifest["path"]
    if not isinstance(manifest_path, str) or not os.path.isabs(manifest_path):
        raise PlanProducerError("manifest path must be absolute")
    template_ids = manifest["template_ids"]
    if not isinstance(template_ids, list) or not all(isinstance(item, str) for item in template_ids):
        raise PlanProducerError("invalid template ids")
    path = Path(manifest_path)
    registered_sha256 = manifest["sha256"]

    raw = _read_manifest(path)
    if _sha256(raw) != registered_sha256:
        raise PlanProducerError("manifest bytes differ from the registered sha256")
    frozen = Manifest.from_bytes(path, raw)
    validate_frozen_manifest(frozen, tuple(template_ids))
    framework_digest = framework_artifact_digest(tuple(template_ids))
    if framework_digest != manifest["framework_sha256"]:
        raise PlanProducerError(
            "registered framework_sha256 is stale against the framework copy"
        )

    with git_environment_scrubbed():
        plan = PersonaInstaller(REPO_ROOT).plan(
            frozen,
            str(seat["persona"]),
            harness_override=str(seat["harness"]),
            root=Path(str(seat["install_root"])),
        )

    # The installer re-reads the manifest from its path while planning
    # (_manifest_document, _role_runtime_references): the file must still be
    # the registered bytes afterwards.
    if _sha256(_read_manifest(path)) != registered_sha256:
        raise PlanProducerError("manifest drifted while planning")
    if not plan.validate_digest():
        raise PlanProducerError("installer returned an invalid plan digest")
    expected = {
        "instance": seat["instance"],
        "role": seat["role"],
        "persona": seat["persona"],
        "account": seat["account"],
        "selected_harness": seat["harness"],
        "root": seat["install_root"],
    }
    for field_name, wanted in expected.items():
        if getattr(plan, field_name) != wanted:
            raise PlanProducerError(f"target mismatch: {field_name}")
    if plan.provenance.get("declared_revision") != manifest["framework_revision"]:
        raise PlanProducerError("framework revision mismatch")
    if plan.provenance.get("source_artifact_sha256") != manifest["framework_sha256"]:
        raise PlanProducerError("framework digest mismatch")
    if plan.provenance.get("source_artifact_sha256") != framework_digest:
        raise PlanProducerError("framework copy changed while planning")
    # T-5470: the same launch-order invariant the broker applies, at registration.
    # It runs the broker's own partition (partition_installer_plan), so EVERY
    # partition refusal surfaces here: the out-of-envelope residue cap, the
    # routed one-per-kind bound (RoutedLaunchBoundExceeded), an admitted op
    # ordered after residue (OutOfEnvelopeKinds) and LaunchBeforeContainment.
    # All are PolicyDenied; T-5617 maps the parent to a clean refusal (rc 2).
    try:
        refuse_launch_before_containment(plan)
    except PolicyDenied as exc:
        raise PlanProducerError(str(exc)[:REFUSAL_DETAIL_LIMIT]) from None
    return _canonical_json(plan.to_dict()).encode("utf-8")


def plan_record(seat_id: str, manifest_revision: str, path: Path, plan_bytes: bytes) -> dict[str, str]:
    """The ``plans`` registry entry for bytes placed at ``path``."""

    return {
        "seat_id": seat_id,
        "manifest_revision": manifest_revision,
        "path": str(path),
        "sha256": _sha256(plan_bytes),
    }


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    try:
        # T-5533 review r1: the create mode is filtered by the umask (077 gives
        # 0600); set the registered 0644 explicitly on the new descriptor.
        os.fchmod(descriptor, 0o644)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--framework-root", required=True, type=Path)
    parser.add_argument("--seat-record", required=True, help="the seat's registry record, as JSON")
    parser.add_argument("--manifest-record", required=True, help="the manifest's registry record, as JSON")
    parser.add_argument("--out", required=True, type=Path, help="absolute path for the plan bytes (must not exist)")
    args = parser.parse_args(argv)
    try:
        if not args.out.is_absolute():
            raise PlanProducerError("--out must be absolute")
        try:
            seat = json.loads(args.seat_record)
            manifest = json.loads(args.manifest_record)
        except json.JSONDecodeError as exc:
            raise PlanProducerError(f"invalid record JSON: {exc}") from exc
        data = produce_plan(
            framework_root=args.framework_root,
            seat_record=seat,
            manifest_record=manifest,
        )
        _write_exclusive(args.out, data)
    except (PlanProducerError, ManifestError, OSError) as exc:
        print(f"host_plan_producer: refused: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(plan_record(str(seat["seat_id"]), str(manifest["revision"]), args.out, data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
