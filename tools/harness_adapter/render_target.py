"""Per-harness render-target interface for the seat-install pipeline.

T-5005 (T-4924 plan §2.4, D-22). Each harness that the seat-install render
pipeline (``tools/persona_orientation/render.py`` + ``persona_installer``)
writes for gets one render target, which lives next to its adapter (ORG-031 §i).

The SOP-skill side of the registry (``in_all``, claude/codex/hermes targets,
per-target bindings) lives in ``tools/persona_orientation/render.py``
``RENDER_TARGETS`` (T-5011 PR-C). render.py runs as a bare script on every seat
and does not import this package (whose ``__init__`` pulls the adapter and
runtime-integration chain), so it carries the few hermes layout constants
itself; a test pins them to ``hermes_render``. This Protocol remains the full
per-seat interface the hermes target implements.
"""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Any, Literal, Mapping, Protocol, TypedDict

from tools.harness_adapter.types import HarnessLaunchConfig


ArtifactKind = Literal["project-context", "identity", "skill-root", "runtime-config"]


class SkillEntry(TypedDict, total=False):
    """One ``sop-skills.yaml`` entry, as render.py validates it."""

    slug: str
    sop: str
    description: str
    short_description: str
    body: Mapping[str, Any]


class HarnessRenderTarget(Protocol):
    harness: str
    in_all: bool

    def harness_home(self, account_home: Path) -> Path:
        ...

    def install_path(self, kind: ArtifactKind, *, account_home: Path) -> Path:
        ...

    def skill_frontmatter(self, entry: SkillEntry) -> Mapping[str, Any]:
        ...

    def skill_relpath(self, slug: str) -> PurePath:
        ...

    def render_standing(self, source_text: str, *, persona: str) -> bytes:
        ...

    def render_runtime_config(
        self,
        profile: HarnessLaunchConfig,
        defaults: Mapping[str, Any],
        *,
        account_home: Path,
    ) -> bytes:
        ...

    def forbidden_targets(self, *, account_home: Path) -> tuple[Path, ...]:
        ...

    def host_defaults(self, defaults: Mapping[str, Any]) -> tuple[Path, bytes] | None:
        ...

    def bindings_dir(self, *, repo_root: Path, account_home: Path) -> Path:
        ...
