"""Strict root-owned seat and manifest registry."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tools.host_control.contracts import (
    ManifestRecord,
    RegistryError,
    SeatRecord,
    SubjectContext,
)
from tools.host_control.trusted_files import RootOwnedFileLoader


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_TOP_KEYS = frozenset(("schema", "seats", "manifests"))
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
    if os.path.normpath(value) != value or ".." in Path(value).parts:
        raise RegistryError(f"{context} must be normalized")
    return Path(value)


@dataclass(frozen=True)
class SeatRegistry:
    _seats: Mapping[str, SeatRecord]
    _subjects: Mapping[int, SubjectContext]
    _manifests: Mapping[str, ManifestRecord]
    _files: RootOwnedFileLoader

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        files: RootOwnedFileLoader,
    ) -> "SeatRegistry":
        raw = files.read_json(path, max_bytes=1024 * 1024)
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
        return cls(seats, subjects, manifests, files)

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
