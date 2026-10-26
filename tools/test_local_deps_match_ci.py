"""The test runner this seat uses must match the versions CI pins (T-4540).

ci.yml's "Install test dependencies" step pins pyyaml, pytest and pytest-xdist
(T-4415). A seat that drifts from those pins gets failures CI never sees: on
2026-10-04 a seat on pytest 9.0.3 (CI: 9.1.1) hit three execnet DumpErrors
under xdist+subtests in every full smoke, so every local run read red.

This reads the pins from ci.yml itself (no second copy to keep in sync) and
compares them with what is installed, so drift fails here with the fix in the
message instead of surfacing as unrelated-looking test failures. In CI it
passes by construction.
"""
from __future__ import annotations

import re
import unittest
from importlib import metadata
from pathlib import Path

CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
_PIN = re.compile(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+-]*)")


def ci_pins(text: str) -> dict[str, str]:
    """The `name==version` pins on ci.yml's `pip install` run line(s)."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        if "pip install" in line:
            pins.update((name.lower(), ver) for name, ver in _PIN.findall(line))
    return pins


class LocalTestDepsMatchCi(unittest.TestCase):
    def test_ci_pins_are_found(self) -> None:
        pins = ci_pins(CI_YML.read_text(encoding="utf-8"))
        self.assertIn("pytest", pins, "no pytest==X pin found on ci.yml's pip install line")

    def test_installed_versions_match_ci_pins(self) -> None:
        pins = ci_pins(CI_YML.read_text(encoding="utf-8"))
        drift = []
        for name, want in sorted(pins.items()):
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                have = "not installed"
            if have != want:
                drift.append(f"{name}: installed {have}, CI pins {want}")
        if drift:
            fix = "python3 -m pip install --user " + " ".join(f"{n}=={v}" for n, v in sorted(pins.items()))
            self.fail("local test deps differ from ci.yml; local results may not match CI:\n  "
                      + "\n  ".join(drift) + f"\nfix: {fix}")

    def test_parser_reads_a_pinned_install_line(self) -> None:
        line = "        run: python -m pip install --upgrade pip pyyaml==6.0.3 pytest==9.1.1 pytest-xdist==3.8.0"
        self.assertEqual(ci_pins(line), {"pyyaml": "6.0.3", "pytest": "9.1.1", "pytest-xdist": "3.8.0"})


if __name__ == "__main__":
    unittest.main()
