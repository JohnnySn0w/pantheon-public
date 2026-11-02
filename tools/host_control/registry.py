"""Strict root-owned seat, manifest and plan registry."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tools.host_control.contracts import (
    ManifestRecord,
    PlanRecord,
    RegistryError,
    SeatRecord,
    SubjectContext,
)
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_TOP_KEYS = frozenset(("schema", "seats", "manifests"))
# T-5533: ``plans`` is optional (absent == no registered plans); a registry with
# it has exactly these keys.
_TOP_KEYS_WITH_PLANS = _TOP_KEYS | {"plans"}
_PLAN_KEYS = frozenset(("seat_id", "manifest_revision", "path", "sha256"))
#: Registered plan size cap (a seat plan is ~65 KB today).
PLAN_MAX_BYTES = 2 * 1024 * 1024
#: Per-file cap when re-hashing a framework artifact (persona_installer.py is
#: the largest, ~0.5 MB).
FRAMEWORK_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024
_SEAT_KEYS = frozenset(
    (
        "seat_id",
        "uid",
        "persona",
        "role",
        "instance",
        "account",
        "harness",
        "install_root",
    )
)
_MANIFEST_KEYS = frozenset(
    (
        "revision",
        "path",
        "sha256",
        "framework_revision",
        "framework_sha256",
        "template_ids",
    )
)


def _exact_keys(
    value: object,
    expected: frozenset[str],
    context: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RegistryError(f"invalid {context} fields")
    return value


def _identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RegistryError(f"invalid {context}")
    return value


def _absolute_normalized(value: object, context: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise RegistryError(f"{context} must be absolute")
    # "//"-prefixed paths survive normpath unchanged (POSIX); refused (D32)
    if value.startswith("//") or os.path.normpath(value) != value or ".." in Path(value).parts:
        raise RegistryError(f"{context} must be normalized")
    return Path(value)


@dataclass(frozen=True)
class SeatRegistry:
    _seats: Mapping[str, SeatRecord]
    _subjects: Mapping[int, SubjectContext]
    _manifests: Mapping[str, ManifestRecord]
    _files: RootOwnedFileLoader
    _plans: Mapping[tuple[str, str], PlanRecord] = dataclasses.field(default_factory=dict)
    _framework_root: Path | None = None

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        files: RootOwnedFileLoader,
        framework_root: Path | None = None,
    ) -> "SeatRegistry":
        """``framework_root`` (T-5533): the root-owned framework copy, under the
        loader's trusted roots. ``check_framework_digest`` re-hashes it, so a plan
        registered against an older framework copy is refused; without it that
        check (and so ``HostPlanService.plan``) refuses. Optional here because
        callers that never plan (the executor, the e29 child, the harness MCP
        registration) load the registry too."""

        if framework_root is not None:
            framework_root = _absolute_normalized(str(framework_root), "framework root")
            if not files.is_allowed(framework_root / FRAMEWORK_CODE_ARTIFACTS[0]):
                raise RegistryError("framework root is outside trusted roots")
        raw = files.read_json(path, max_bytes=1024 * 1024)
        if isinstance(raw, dict) and "plans" in raw:
            _exact_keys(raw, _TOP_KEYS_WITH_PLANS, "registry")
        else:
            _exact_keys(raw, _TOP_KEYS, "registry")
        if raw["schema"] != "pantheon-host-seat-registry/v1":
            raise RegistryError("unsupported registry schema")
        seats_raw = raw["seats"]
        manifests_raw = raw["manifests"]
        if not isinstance(seats_raw, list) or not seats_raw:
            raise RegistryError("registry seats must be a non-empty list")
        if not isinstance(manifests_raw, list) or not manifests_raw:
            raise RegistryError("registry manifests must be a non-empty list")

        seats: dict[str, SeatRecord] = {}
        subjects: dict[int, SubjectContext] = {}
        for index, item in enumerate(seats_raw):
            value = _exact_keys(item, _SEAT_KEYS, f"seat {index}")
            seat_id = _identifier(value["seat_id"], "seat id")
            uid = value["uid"]
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise RegistryError("invalid seat uid")
            harness = value["harness"]
            if harness not in ("claude", "codex"):
                raise RegistryError("invalid seat harness")
            install_root = _absolute_normalized(
                value["install_root"],
                "seat install root",
            )
            record = SeatRecord(
                seat_id=seat_id,
                uid=uid,
                persona=_identifier(value["persona"], "seat persona"),
                role=_identifier(value["role"], "seat role"),
                instance=_identifier(value["instance"], "seat instance"),
                account=_identifier(value["account"], "seat account"),
                harness=harness,
                install_root=install_root,
            )
            if seat_id in seats or uid in subjects:
                raise RegistryError("duplicate seat id or uid")
            seats[seat_id] = record
            subjects[uid] = SubjectContext(
                uid=record.uid,
                seat_id=record.seat_id,
                persona=record.persona,
                role=record.role,
                instance=record.instance,
            )

        manifests: dict[str, ManifestRecord] = {}
        for index, item in enumerate(manifests_raw):
            value = _exact_keys(item, _MANIFEST_KEYS, f"manifest {index}")
            revision = _identifier(value["revision"], "manifest revision")
            manifest_path = _absolute_normalized(
                value["path"],
                "manifest path",
            )
            if not files.is_allowed(manifest_path):
                raise RegistryError("manifest path is outside trusted roots")
            manifest_sha256 = value["sha256"]
            framework_sha256 = value["framework_sha256"]
            if (
                not isinstance(manifest_sha256, str)
                or _HEX_256.fullmatch(manifest_sha256) is None
                or not isinstance(framework_sha256, str)
                or _HEX_256.fullmatch(framework_sha256) is None
            ):
                raise RegistryError("invalid registered digest")
            template_ids_raw = value["template_ids"]
            if (
                not isinstance(template_ids_raw, list)
                or not template_ids_raw
                or not all(
                    isinstance(template_id, str)
                    and 1 <= len(template_id) <= 256
                    and not Path(template_id).is_absolute()
                    and ".." not in Path(template_id).parts
                    for template_id in template_ids_raw
                )
                or len(set(template_ids_raw)) != len(template_ids_raw)
            ):
                raise RegistryError("invalid template ids")
            record = ManifestRecord(
                revision=revision,
                path=manifest_path,
                sha256=manifest_sha256,
                framework_revision=_identifier(
                    value["framework_revision"],
                    "framework revision",
                ),
                framework_sha256=framework_sha256,
                template_ids=tuple(template_ids_raw),
            )
            if revision in manifests:
                raise RegistryError("duplicate manifest revision")
            manifest_bytes = files.read_bytes(
                manifest_path,
                max_bytes=1024 * 1024,
            )
            if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
                raise RegistryError("registered manifest hash mismatch")
            manifests[revision] = record

        plans_raw = raw.get("plans", [])
        if not isinstance(plans_raw, list):
            raise RegistryError("registry plans must be a list")
        plans: dict[tuple[str, str], PlanRecord] = {}
        for index, item in enumerate(plans_raw):
            value = _exact_keys(item, _PLAN_KEYS, f"plan {index}")
            seat_id = _identifier(value["seat_id"], "plan seat id")
            revision = _identifier(value["manifest_revision"], "plan manifest revision")
            if seat_id not in seats:
                raise RegistryError("plan names an unregistered seat")
            if revision not in manifests:
                raise RegistryError("plan names an unregistered manifest revision")
            plan_path = _absolute_normalized(value["path"], "plan path")
            if not files.is_allowed(plan_path):
                raise RegistryError("plan path is outside trusted roots")
            plan_sha256 = value["sha256"]
            if not isinstance(plan_sha256, str) or _HEX_256.fullmatch(plan_sha256) is None:
                raise RegistryError("invalid registered plan digest")
            key = (seat_id, revision)
            if key in plans:
                raise RegistryError("duplicate plan for seat and manifest revision")
            plan_bytes = files.read_bytes(plan_path, max_bytes=PLAN_MAX_BYTES)
            if hashlib.sha256(plan_bytes).hexdigest() != plan_sha256:
                raise RegistryError("registered plan hash mismatch")
            plans[key] = PlanRecord(
                seat_id=seat_id,
                manifest_revision=revision,
                path=plan_path,
                sha256=plan_sha256,
            )
        return cls(seats, subjects, manifests, files, plans, framework_root)

    def subject_for_uid(self, authenticated_uid: int) -> SubjectContext:
        if not isinstance(authenticated_uid, int) or isinstance(
            authenticated_uid,
            bool,
        ):
            raise RegistryError("invalid authenticated uid")
        try:
            return self._subjects[authenticated_uid]
        except KeyError as exc:
            raise RegistryError("authenticated uid is not registered") from exc

    def seat_ids(self) -> tuple[str, ...]:
        """Every registered seat id, sorted (T-2917 5d.4: the broker's
        executor-reachability health leg covers each registered seat)."""

        return tuple(sorted(self._seats))

    def seat(self, seat_id: str) -> SeatRecord:
        try:
            return self._seats[seat_id]
        except (KeyError, TypeError) as exc:
            raise RegistryError("unknown target seat") from exc

    def manifest(self, revision: str) -> ManifestRecord:
        try:
            return self._manifests[revision]
        except (KeyError, TypeError) as exc:
            raise RegistryError("unknown manifest revision") from exc

    def read_manifest_bytes(self, record: ManifestRecord) -> bytes:
        registered = self.manifest(record.revision)
        if registered != record:
            raise RegistryError("manifest record is not registered")
        raw = self._files.read_bytes(record.path, max_bytes=1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != record.sha256:
            raise RegistryError("registered manifest hash drift")
        return raw

    def check_framework_digest(self, record: ManifestRecord) -> None:
        """T-5533 (orchestrator's scope add): refuse when the framework copy's artifact
        digest (``FRAMEWORK_CODE_ARTIFACTS`` plus the record's template ids, the
        same bytes and order as ``persona_installer._artifact_digest`` and the
        executor's source check) differs from the registered ``framework_sha256``.
        That is, the registered manifest and its plan are stale against the
        copy. Fail closed (T-5533 review r1, B1): a registry loaded without a
        framework root cannot prove its plans fresh, so this refuses. ``load``
        keeps ``framework_root`` optional because the executor, the e29 child and
        the harness MCP registration load the registry but never plan."""

        if self._framework_root is None:
            raise RegistryError("no framework root configured")
        artifacts = (*FRAMEWORK_CODE_ARTIFACTS, *record.template_ids)
        digest = hashlib.sha256()
        for relative in sorted(artifacts):
            data = self._files.read_bytes(
                self._framework_root / relative,
                max_bytes=FRAMEWORK_ARTIFACT_MAX_BYTES,
            )
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
        if digest.hexdigest() != record.framework_sha256:
            raise RegistryError("registered framework digest is stale against the framework copy")

    def plan_record(self, seat_id: str, manifest_revision: str) -> PlanRecord:
        """T-5533: the root-registered plan for ``(seat_id, manifest_revision)``."""

        try:
            return self._plans[(seat_id, manifest_revision)]
        except (KeyError, TypeError) as exc:
            raise RegistryError("no registered plan for seat and manifest revision") from exc

    def read_plan_bytes(self, record: PlanRecord) -> bytes:
        """Mirror of ``read_manifest_bytes`` for a registered plan: the record must
        equal the registered one, the bytes come through the root-owned loader
        (owner, mode and no-symlink checks), under ``PLAN_MAX_BYTES``, and must
        still hash to the registered sha256."""

        registered = self.plan_record(record.seat_id, record.manifest_revision)
        if registered != record:
            raise RegistryError("plan record is not registered")
        raw = self._files.read_bytes(record.path, max_bytes=PLAN_MAX_BYTES)
        if hashlib.sha256(raw).hexdigest() != record.sha256:
            raise RegistryError("registered plan hash drift")
        return raw
