"""`germinate registry ...` entry point: exit codes and wiring, over tmp_path repos only."""

from __future__ import annotations

import pytest

from tools.germinate import cli
from tools.germinate.registry.tests import fixtures as F


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_validate_clean_repo_exits_0(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    rc, out, _ = _run(["registry", "validate", "--repo-root", str(root), "--today", "2026-10-17"], capsys)
    assert rc == 0 and out.startswith("ok:")


def test_validate_bad_registry_exits_1(tmp_path, capsys):
    root = F.make_repo(tmp_path, F.doc(F.component(contract_adoption="maybe")))
    rc, _, err = _run(["registry", "validate", "--repo-root", str(root)], capsys)
    assert rc == 1 and "contract_adoption 'maybe'" in err


def test_unknown_subcommand_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["registry", "frobnicate"])
    assert exc.value.code == 2


def test_render_prints_and_check_detects_drift(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    rc, out, _ = _run(["registry", "render", "--repo-root", str(root)], capsys)
    assert rc == 0 and "| Major system |" in out
    rendered = root / "registry" / "system-coverage.md"
    assert _run(["registry", "render", "--repo-root", str(root), "--check"], capsys)[0] == 1  # absent
    rendered.write_text(out, encoding="utf-8")
    assert _run(["registry", "render", "--repo-root", str(root), "--check"], capsys)[0] == 0
    rendered.write_text(out + "hand edit\n", encoding="utf-8")
    rc, _, err = _run(["registry", "render", "--repo-root", str(root), "--check"], capsys)
    assert rc == 1 and "is stale" in err


def test_render_unloadable_registry_exits_1(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    (root / "registry" / "systems.yaml").write_text("a: 1\na: 2\n", encoding="utf-8")
    rc, _, err = _run(["registry", "render", "--repo-root", str(root)], capsys)
    assert rc == 1 and "duplicate key" in err


def test_show_lists_rows(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    rc, out, _ = _run(["registry", "show", "--repo-root", str(root)], capsys)
    assert rc == 0 and out.splitlines()[0].startswith("comp-a\tcomponent\tnot_adopted")


def test_lint_adrs_missing_diff_file_exits_2_cleanly(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    missing = tmp_path / "absent.diff"
    rc, _, err = _run(["registry", "lint-adrs", "--repo-root", str(root), "--diff", str(missing)], capsys)
    assert rc == 2
    assert err.startswith("error: cannot read diff") and "Traceback" not in err


def test_lint_adrs_tree_and_diff(tmp_path, capsys):
    root = F.make_repo(tmp_path)
    adr = root / "architecture" / "adr" / "ORG-300-x.md"
    adr.write_text("---\nregisters_components: [ghost]\n---\n", encoding="utf-8")
    rc, _, err = _run(["registry", "lint-adrs", "--repo-root", str(root), "--tree"], capsys)
    assert rc == 1 and "ghost" in err
    adr.write_text("---\nregisters_components: [comp-a]\n---\n", encoding="utf-8")
    assert _run(["registry", "lint-adrs", "--repo-root", str(root)], capsys)[0] == 0
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/architecture/adr/ORG-301-n.md b/architecture/adr/ORG-301-n.md\n"
        "new file mode 100644\n--- /dev/null\n+++ b/architecture/adr/ORG-301-n.md\n"
        "@@ -0,0 +1,3 @@\n+---\n+id: ORG-301\n+---\n",
        encoding="utf-8",
    )
    rc, _, err = _run(["registry", "lint-adrs", "--repo-root", str(root), "--diff", str(diff)], capsys)
    assert rc == 1 and "a new ORG ADR must carry" in err


# --- T-5477: registry repin ----------------------------------------------------


def test_repin_check_passes_on_the_real_repo(capsys):
    """CI-red smoke: a handlers or helper edit merged without a re-pin fails here."""

    rc, out, err = _run(["registry", "repin", "--check"], capsys)
    assert rc == 0, err
    assert "harness-manager" in out and "host-substrate" in out and "pantheon-config" in out


def _repin_repo(tmp_path):
    from tools.germinate.engine.tests import helpers as H

    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a")
    handlers = repo / "tools" / "germinate" / "components" / "comp-a_handlers.py"
    handlers.write_text("HANDLERS = ()\n", encoding="utf-8")  # unpinned: repin must insert the field
    F.make_repo(repo, F.doc(H.row("comp-a", digest)))
    return repo, handlers


def test_repin_inserts_the_pin_cascades_and_then_validates(tmp_path, capsys):
    repo, handlers = _repin_repo(tmp_path)
    base = ["--repo-root", str(repo)]
    validate = ["registry", "validate", *base, "--today", "2026-10-26"]
    rc, _, err = _run(["registry", "repin", "--check", *base], capsys)
    assert rc == 1 and "handlers_digest: (missing)" in err and "registry repin comp-a" in err
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0 and "registry comp-a.contract.digest" in out
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 0
    assert _run(validate, capsys)[0] == 0

    handlers.write_text("HANDLERS = ()\nX = 1\n", encoding="utf-8")  # a later handler edit
    rc, _, err = _run(validate, capsys)
    assert rc == 1 and "handlers_digest" in err
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 1
    assert _run(["registry", "repin", "comp-a", *base], capsys)[0] == 0
    assert _run(validate, capsys)[0] == 0
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0 and "already pinned" in out


def test_repin_keeps_comments_and_layout(tmp_path, capsys):
    repo, handlers = _repin_repo(tmp_path)
    contract = handlers.with_name("comp-a.yaml")
    contract.write_text("# keep me\n" + contract.read_text(), encoding="utf-8")
    assert _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)[0] == 0
    text = contract.read_text()
    assert text.startswith("# keep me\n") and text.count("handlers_digest:") == 1


def test_repin_needs_an_id_unless_checking(tmp_path, capsys):
    repo, _ = _repin_repo(tmp_path)
    assert _run(["registry", "repin", "--repo-root", str(repo)], capsys)[0] == 2


def test_repin_refuses_unknown_and_unpinned_rows(tmp_path, capsys):
    repo, _ = _repin_repo(tmp_path)
    rc, _, err = _run(["registry", "repin", "nope", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "no such registry row" in err
    root = F.make_repo(tmp_path / "other")
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(root)], capsys)
    assert rc == 1 and "repin only maintains pinned rows" in err
