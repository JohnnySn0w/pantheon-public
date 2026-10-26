"""ORG-081 D-10 lint: registers_components / no_registry_row_waiver against registry ids."""

from __future__ import annotations

import pytest

from tools.germinate.registry import adr_link
from tools.germinate.registry.validate import errors
from tools.germinate.registry.tests import fixtures as F

REGISTRY = F.doc(F.component(id="comp-a"), F.external(id="plane-a"))


def _write_adr(root, name, frontmatter: str):
    path = root / "architecture" / "adr" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: x\n{frontmatter}---\n\n# body\n", encoding="utf-8")
    return path


def _tree(tmp_path, frontmatter: str, name: str = "ORG-200-test.md"):
    _write_adr(tmp_path, name, frontmatter)
    return adr_link.scan_tree(tmp_path, REGISTRY)


def _errs(findings) -> str:
    return "\n".join(str(f) for f in errors(findings))


# ---------------------------------------------------------------- tree mode


def test_registered_id_present_is_clean(tmp_path):
    assert errors(_tree(tmp_path, "registers_components: [comp-a]\n")) == []


def test_empty_list_is_clean(tmp_path):
    assert errors(_tree(tmp_path, "registers_components: []\n")) == []


def test_registered_id_absent_refused(tmp_path):
    assert "names 'ghost', which has no kind: component registry row" in _errs(
        _tree(tmp_path, "registers_components: [comp-a, ghost]\n")
    )


def test_external_plane_id_is_not_a_component(tmp_path):
    assert "names 'plane-a'" in _errs(_tree(tmp_path, "registers_components: [plane-a]\n"))


def test_both_fields_refused(tmp_path):
    fm = "registers_components: [comp-a]\nno_registry_row_waiver: decision-level\n"
    assert "mutually exclusive" in _errs(_tree(tmp_path, fm))


@pytest.mark.parametrize("value", ["none", "comp-a", "{a: 1}"])
def test_scalar_registers_components_refused(tmp_path, value):
    assert "must be a list" in _errs(_tree(tmp_path, f"registers_components: {value}\n"))


@pytest.mark.parametrize("value", ['""', "[]", "null"])
def test_waiver_must_be_non_empty_string(tmp_path, value):
    assert "must be a non-empty string" in _errs(_tree(tmp_path, f"no_registry_row_waiver: {value}\n"))


def test_waiver_alone_is_clean(tmp_path):
    assert errors(_tree(tmp_path, "no_registry_row_waiver: decision-level ADR\n")) == []


def test_neither_field_is_grandfathered_in_tree_mode(tmp_path):
    assert _tree(tmp_path, "") == []


def test_non_org_adrs_are_ignored(tmp_path):
    _write_adr(tmp_path, "ADR-500-project.md", "registers_components: none\n")
    assert adr_link.scan_tree(tmp_path, REGISTRY) == []


def test_duplicate_frontmatter_key_refused(tmp_path):
    fm = "registers_components: [ghost]\nregisters_components: [comp-a]\n"
    assert "duplicate key 'registers_components'" in _errs(_tree(tmp_path, fm))


def test_unparseable_frontmatter_reported(tmp_path):
    assert "not valid YAML" in _errs(_tree(tmp_path, "registers_components: [a\n"))


# ---------------------------------------------------------------- diff mode


def _new_file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    added = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}"
    )


def _modified_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " ---\n"
        "-status: Proposed\n"
        "+status: Ratified\n"
    )


NEW = "architecture/adr/ORG-300-new.md"


def test_new_adr_without_either_field_refused():
    diff = _new_file_diff(NEW, "---\nid: ORG-300\n---\n\n# x\n")
    assert "a new ORG ADR must carry" in _errs(adr_link.scan_diff(diff, REGISTRY))


def test_new_adr_with_waiver_is_clean():
    diff = _new_file_diff(NEW, "---\nid: ORG-300\nno_registry_row_waiver: decision-level\n---\n\n# x\n")
    assert adr_link.scan_diff(diff, REGISTRY) == []


def test_new_adr_registering_missing_row_refused():
    diff = _new_file_diff(NEW, "---\nid: ORG-300\nregisters_components: [ghost]\n---\n")
    assert "names 'ghost'" in _errs(adr_link.scan_diff(diff, REGISTRY))


def test_modified_existing_adr_is_grandfathered():
    assert adr_link.scan_diff(_modified_diff("architecture/adr/ORG-010-old.md"), REGISTRY) == []


def test_new_non_adr_file_is_ignored():
    diff = _new_file_diff("notes/ORG-300-new.md", "---\nid: x\n---\n")
    assert adr_link.scan_diff(diff, REGISTRY) == []
