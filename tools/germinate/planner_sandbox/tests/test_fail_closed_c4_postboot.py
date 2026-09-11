"""C4 (partial, postboot dimension) red-before-green test: fail-closed
on postboot-indicator compat warning.

Tranche-specific file (NOT the shared ``test_fail_closed_c4.py`` the
T-3294 ticket body originally named) -- PO ratified the tranche-specific
convention across A/B/C/D during Phase 1 plan review to avoid a
4-parallel-branch merge-conflict surface on one shared file (see T-3294
DECISIONS). Each tranche gets its own ``test_fail_closed_c4_<tranche>.py``.
"""

from __future__ import annotations

import json

import pytest

from tools.germinate.planner_sandbox.harness.fixtures import temp_scratch_dir
from tools.germinate.planner_sandbox.postboot import enforcement, marker
from tools.germinate.planner_sandbox.postboot.exceptions import PrimitiveUnavailable


def test_c4_postboot_compat_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker with a reserved-for-future-use marker_version refuses
    fail-closed with the warning surface -- it must NOT degrade to
    "marker present, good enough".
    """

    with temp_scratch_dir() as scratch:
        marker_path = scratch / "postboot-complete.marker"
        monkeypatch.setattr(marker, "MARKER_PATH", marker_path)

        future_version = marker.CURRENT_MARKER_VERSION + 1
        body = json.dumps(
            {"marker_version": future_version, "boot_id": marker.current_boot_id()}
        )
        marker_path.write_text(body, encoding="utf-8")
        marker_path.chmod(0o644)

        with pytest.raises(PrimitiveUnavailable) as exc_info:
            enforcement.require_or_refuse()

        exc = exc_info.value
        assert exc.name == "postboot-marker"
        assert str(future_version) in exc.reason
