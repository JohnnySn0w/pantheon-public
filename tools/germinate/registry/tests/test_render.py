"""ORG-081 §5 render: ORG-068 column order, glyphs, row order, determinism."""

from __future__ import annotations

from tools.germinate.registry import render
from tools.germinate.registry.tests import fixtures as F


def _doc():
    legacy = F.component(
        id="old",
        title="Old | System",
        legacy_coverage={
            "install": F.leg("covered"),
            "configure": F.leg("partial"),
            "health": F.leg("partial_by_design", waiver="E9.C3 waived"),
            "remove": F.leg("not_applicable"),
        },
    )
    return F.doc(legacy, F.adopted(id="new", note="the paradigm"), F.external())


def test_header_columns_in_org068_order():
    out = render.render_matrix(_doc())
    assert (
        "| Major system | Steward | Install | Configure | Health-mon | Update | Remove | Realization / cross-ref |"
        in out
    )


def test_glyphs_legacy_marker_and_unrecorded_cells():
    out = render.render_matrix(_doc())
    row = next(line for line in out.splitlines() if "(`old`)" in line)
    cells = [c.strip() for c in row.strip("|").split(" | ")]
    assert cells[2:7] == ["✓ᴸ", "◐ᴸ", "◑ᴸ", "—", "n/aᴸ"]
    assert "Old \\| System" in row  # pipes in prose cannot break the table


def test_adopted_cells_have_no_legacy_marker():
    out = render.render_matrix(_doc())
    row = next(line for line in out.splitlines() if "(`new`)" in line)
    assert "ᴸ" not in row
    assert "`adopted`; ORG-081; the paradigm" in row


def test_rows_follow_registry_order():
    out = render.render_matrix(_doc())
    assert out.index("(`old`)") < out.index("(`new`)")


def test_external_planes_render_separately():
    out = render.render_matrix(_doc())
    section = out[out.index("## External planes"):]
    assert "(`plane-a`)" in section
    assert "build: T-1234" in section
    assert "(`plane-a`)" not in out[: out.index("## External planes")]


def test_render_is_deterministic():
    assert render.render_matrix(_doc()) == render.render_matrix(_doc())
    assert render.render_matrix(_doc()).endswith("|\n")


def test_show_lists_every_row():
    out = render.render_show(_doc())
    assert [line.split("\t")[0] for line in out.splitlines()] == ["old", "new", "plane-a"]
