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


# --- T-5989: the module forms must run, never exit 0 silently ----------------------


def _module(module, *args, cwd):
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    return subprocess.run([sys.executable, "-m", module, *args], cwd=repo_root if cwd is None else cwd,
                          capture_output=True, text=True, timeout=120)


def test_registry_module_form_runs_the_registry_group(tmp_path):
    """`python3 -m tools.germinate.registry.cli validate` used to import and exit 0 without running."""

    root = F.make_repo(tmp_path, F.doc(F.component(contract_adoption="maybe")))
    proc = _module("tools.germinate.registry.cli", "validate", "--repo-root", str(root), cwd=None)
    assert proc.returncode == 1 and "contract_adoption 'maybe'" in proc.stderr
    clean = F.make_repo(tmp_path / "clean")
    ok = _module("tools.germinate.registry.cli", "validate", "--repo-root", str(clean), "--today", "2026-10-17", cwd=None)
    assert ok.returncode == 0 and ok.stdout.startswith("ok:")


def test_engine_module_form_runs_the_engine_group():
    proc = _module("tools.germinate.engine.cli", "--help", cwd=None)
    assert proc.returncode == 0 and "usage: germinate" in proc.stdout and "plan" in proc.stdout
    bad = _module("tools.germinate.engine.cli", "frobnicate", cwd=None)
    assert bad.returncode == 2


# --- T-6042: repin recomputes invariant_list_digest ---------------------------


def _invariant_repo(tmp_path, verify_ref=None, emitter_ref=None, **overrides):
    import yaml

    from tools.germinate.engine.tests import helpers as H

    repo = tmp_path / "repo"
    repo.mkdir()
    fields = {"invariant_list_id": "org.pantheon.test/v1", "invariant_list_digest": H.LIST_DIGEST}
    fields.update(overrides)
    fields = {k: v for k, v in fields.items() if v is not None}
    if verify_ref or emitter_ref:
        fields["legs"] = {"install": "plan_install(manifest, registry, scope) -> [Operation]", "remove": "plan_remove"}
        if verify_ref:
            fields["legs"]["verify"] = {"invariant_list_ref": verify_ref}
        if emitter_ref:
            fields["emitter"] = {"bus": "session", "interface": "org.pantheon.Test1", "invariant_list_ref": emitter_ref}
    _p, digest = H.write_contract(repo, "comp-a", **fields)
    F.make_repo(repo, F.doc(H.row("comp-a", digest)))
    lst = repo / "tools" / "germinate" / "components" / "comp-a.invariants.yaml"
    contract = lst.with_name("comp-a.yaml")
    return repo, lst, contract, yaml


def _edit_list(lst, yaml):
    doc = yaml.safe_load(lst.read_text(encoding="utf-8"))
    doc["invariant_list"]["invariants"].append({"id": "T2", "class": "presence-mode"})
    lst.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _snapshot(repo):
    return {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}


def test_repin_rewrites_invariant_list_digest_and_cascades(tmp_path, capsys):
    repo, lst, contract, yaml = _invariant_repo(tmp_path)
    base = ["--repo-root", str(repo)]
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 0
    _edit_list(lst, yaml)
    rc, _, err = _run(["registry", "repin", "--check", *base], capsys)
    assert rc == 1 and "invariant_list_digest:" in err
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0 and "invariant_list_digest" in out and "contract_digest" in out and "registry comp-a.contract.digest" in out
    assert _run(["registry", "validate", *base, "--today", "2026-10-26"], capsys)[0] == 0
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0 and "already pinned" in out


def test_repin_check_reports_invariant_list_drift_and_writes_nothing(tmp_path, capsys):
    repo, lst, _contract, yaml = _invariant_repo(tmp_path)
    _edit_list(lst, yaml)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "--check", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "invariant_list_digest:" in err
    assert _snapshot(repo) == before


def test_repin_leaves_a_contract_without_the_digest_line_alone(tmp_path, capsys):
    repo, lst, contract, yaml = _invariant_repo(tmp_path, invariant_list_id=None, invariant_list_digest=None)
    lst.write_text("not: a list\n", encoding="utf-8")  # irrelevant: the contract declares no list
    before = _snapshot(repo)
    rc, _, _ = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 0
    assert _snapshot(repo) == before
    assert "invariant_list_digest" not in contract.read_text(encoding="utf-8")


def test_repin_refuses_a_list_id_mismatch(tmp_path, capsys):
    repo, lst, _contract, yaml = _invariant_repo(tmp_path, invariant_list_id="org.pantheon.other/v1")
    _edit_list(lst, yaml)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "org.pantheon.other/v1" in err and "org.pantheon.test/v1" in err
    assert _snapshot(repo) == before


@pytest.mark.parametrize("content", (None, "plain: mapping\n", "{unclosed: [\n"))
def test_repin_refuses_an_unreadable_or_unsupported_list(tmp_path, capsys, content):
    repo, lst, _contract, _yaml = _invariant_repo(tmp_path)
    if content is None:
        lst.unlink()
    else:
        lst.write_text(content, encoding="utf-8")
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 1 and err.startswith("error:")
    assert _snapshot(repo) == before


def test_repin_refuses_a_digest_without_a_list_id(tmp_path, capsys):
    repo, _lst, _contract, _yaml = _invariant_repo(tmp_path, invariant_list_id=None)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "invariant_list_digest declared without invariant_list_id" in err
    assert _snapshot(repo) == before


_REF_ID = "org.pantheon.test/v1"


def _ref(digest):
    return f"{_REF_ID}@{digest}"


def test_repin_rewrites_the_verify_and_emitter_refs_with_the_digest(tmp_path, capsys):
    from tools.germinate.engine.tests import helpers as H

    good = _ref(H.LIST_DIGEST)
    repo, lst, contract, yaml = _invariant_repo(tmp_path, verify_ref=good, emitter_ref=good)
    base = ["--repo-root", str(repo)]
    validate = ["registry", "validate", *base, "--today", "2026-10-26"]
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 0
    _edit_list(lst, yaml)
    rc, _, err = _run(["registry", "repin", "--check", *base], capsys)
    assert rc == 1 and "legs.verify.invariant_list_ref:" in err and "emitter.invariant_list_ref:" in err
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0
    new = yaml.safe_load(contract.read_text(encoding="utf-8"))["component"]
    assert new["legs"]["verify"]["invariant_list_ref"] == new["emitter"]["invariant_list_ref"] == _ref(new["invariant_list_digest"])
    assert new["invariant_list_digest"] != H.LIST_DIGEST
    assert _run(validate, capsys)[0] == 0
    rc, out, _ = _run(["registry", "repin", "comp-a", *base], capsys)
    assert rc == 0 and "already pinned" in out


def test_repin_check_reports_a_stale_ref_even_with_a_current_digest(tmp_path, capsys):
    from tools.germinate.engine.tests import helpers as H

    stale = _ref("sha256:" + "1" * 64)
    repo, _lst, contract, _yaml = _invariant_repo(tmp_path, verify_ref=_ref(H.LIST_DIGEST), emitter_ref=stale)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "--check", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "emitter.invariant_list_ref:" in err and "legs.verify" not in err
    assert "invariant_list_digest:" not in err
    assert _snapshot(repo) == before


@pytest.mark.parametrize("bad", ("org.pantheon.other/v1@sha256:" + "2" * 64, "org.pantheon.test/v1", "org.pantheon.test/v1@sha256:xyz"))
def test_repin_refuses_a_malformed_or_wrong_id_ref(tmp_path, capsys, bad):
    from tools.germinate.engine.tests import helpers as H

    repo, lst, _contract, yaml = _invariant_repo(tmp_path, verify_ref=_ref(H.LIST_DIGEST), emitter_ref=bad)
    _edit_list(lst, yaml)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "emitter.invariant_list_ref" in err
    assert _snapshot(repo) == before


def _comment_refs(contract, base, capsys):
    """Append `  # pinned` to every ref line, then repin so the fixture starts consistent."""

    text = contract.read_text(encoding="utf-8")
    lines = [ln + "  # pinned" if ln.lstrip().startswith("invariant_list_ref:") else ln for ln in text.split("\n")]
    contract.write_text("\n".join(lines), encoding="utf-8")
    assert _run(["registry", "repin", "comp-a", *base], capsys)[0] == 0


def test_repin_rewrites_a_commented_ref_and_keeps_the_comment(tmp_path, capsys):
    from tools.germinate.engine.tests import helpers as H

    good = _ref(H.LIST_DIGEST)
    repo, lst, contract, yaml = _invariant_repo(tmp_path, verify_ref=good, emitter_ref=good)
    base = ["--repo-root", str(repo)]
    _comment_refs(contract, base, capsys)
    _edit_list(lst, yaml)
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 1
    assert _run(["registry", "repin", "comp-a", *base], capsys)[0] == 0
    text = contract.read_text(encoding="utf-8")
    new = yaml.safe_load(text)["component"]
    assert text.count("  # pinned") == 2
    assert new["legs"]["verify"]["invariant_list_ref"] == _ref(new["invariant_list_digest"]) != good
    assert _run(["registry", "validate", *base, "--today", "2026-10-26"], capsys)[0] == 0
    assert _run(["registry", "repin", "--check", *base], capsys)[0] == 0


@pytest.mark.parametrize("odd", ("[a, b]", "{ref: x}", "|", "", "'org.pantheon.test/v1@sha256:" + "3" * 64))
def test_repin_refuses_a_ref_line_the_strict_form_cannot_parse(tmp_path, capsys, odd):
    from tools.germinate.engine.tests import helpers as H

    good = _ref(H.LIST_DIGEST)
    repo, lst, contract, yaml = _invariant_repo(tmp_path, verify_ref=good, emitter_ref=good)
    text = contract.read_text(encoding="utf-8")
    contract.write_text(text.replace(
        f"invariant_list_ref: {good}", f"invariant_list_ref: {odd}" if odd else "invariant_list_ref:", 1), encoding="utf-8")
    _edit_list(lst, yaml)
    before = _snapshot(repo)
    rc, _, err = _run(["registry", "repin", "comp-a", "--repo-root", str(repo)], capsys)
    assert rc == 1 and "invariant_list_ref" in err
    assert _snapshot(repo) == before


def test_repin_examines_every_ref_key_line(tmp_path, capsys):
    from tools.germinate.engine.tests import helpers as H
    from tools.germinate.registry import repin as R

    good = _ref(H.LIST_DIGEST)
    stale = _ref("sha256:" + "1" * 64)
    repo, _lst, contract, _yaml = _invariant_repo(tmp_path, verify_ref=good, emitter_ref=stale)
    _comment_refs(contract, ["--repo-root", str(repo)], capsys)
    keys = [ln for ln in contract.read_text(encoding="utf-8").split("\n") if ln.lstrip().startswith("invariant_list_ref:")]
    result = R.repin(repo, repo / "registry" / "systems.yaml", "comp-a")
    assert len(keys) == 2 == result.refs_seen


# --- T-6075: repin refuses a ref form the line scan cannot see ----------------


def _exotic(text, form):
    import re

    pat = re.compile(r"verify:\n([ ]+)invariant_list_ref: (\S+)")
    swaps = {
        "space-before-colon": lambda m: f"verify:\n{m.group(1)}invariant_list_ref : {m.group(2)}",
        "quoted-key": lambda m: f'verify:\n{m.group(1)}"invariant_list_ref": {m.group(2)}',
        "flow-style": lambda m: f"verify: {{invariant_list_ref: {m.group(2)}}}",
        "list-item": lambda m: f"verify:\n{m.group(1)}- invariant_list_ref: {m.group(2)}",
    }
    out, n = pat.subn(swaps[form], text, count=1)
    assert n == 1
    return out


@pytest.mark.parametrize("form", ("space-before-colon", "quoted-key", "flow-style", "list-item"))
def test_repin_refuses_a_ref_form_the_line_scan_cannot_see(tmp_path, capsys, form):
    from tools.germinate.engine.tests import helpers as H

    good = _ref(H.LIST_DIGEST)
    repo, lst, contract, yaml = _invariant_repo(tmp_path, verify_ref=good)
    contract.write_text(_exotic(contract.read_text(encoding="utf-8"), form), encoding="utf-8")
    _edit_list(lst, yaml)
    before = _snapshot(repo)
    for args in (["repin", "comp-a"], ["repin", "--check"]):
        rc, _, err = _run(["registry", *args, "--repo-root", str(repo)], capsys)
        assert rc == 1 and "1 refs in the parsed contract, 0 examined by the line scan" in err
        assert _snapshot(repo) == before


def test_repin_refuses_a_contract_whose_yaml_does_not_parse(tmp_path, capsys):
    from tools.germinate.registry import repin as R

    repo, _lst, contract, _yaml = _invariant_repo(tmp_path)
    contract.write_text(contract.read_text(encoding="utf-8") + "bad: [unclosed\n", encoding="utf-8")
    with pytest.raises(R.RepinError):
        R.repin(repo, repo / "registry" / "systems.yaml", "comp-a")
