"""T-5113: the repos.yaml `revision` pin (ORG-081 §7) and its use by registry validate."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from tools import remote_resolve as RR
from tools.germinate.engine.tests import helpers as H
from tools.germinate.registry.git_resolver import GitExternalResolver
from tools.germinate.registry.tests import fixtures as F
from tools.germinate.registry.tests.test_git_resolver import MODULE, Fixture
from tools.germinate.registry.validate import check_contracts, errors

SHA = "a" * 40


def _msgs(findings) -> str:
    return "\n".join(str(f) for f in errors(findings))


def _repos_yaml(root: Path, *, revision=SHA, extra=None) -> None:
    row = {"name": "echo-prismatic", "grants": {"specialist": "ro"}, **(extra or {})}
    if revision is not None:
        row["revision"] = revision
    (root / "repos.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "repos": [{"name": "pantheon-config"}, row]}), encoding="utf-8"
    )


# --- schema: remote_resolve.load_manifest -----------------------------------------


def _load(path: Path):
    return RR.load_manifest(path, signature_verifier=lambda p, c: True)


def _manifest(root: Path, revision) -> Path:
    row = {"name": "x", "remotes": [{"role": "primary", "url": "https://h/x.git", "auth": "t"}]}
    if revision is not None:
        row["revision"] = revision
    path = root / "repos.yaml"
    path.write_text(yaml.safe_dump({"auth_refs": {"t": {"allowed_hosts": ["h"]}}, "repos": [row]}), encoding="utf-8")
    return path


def test_manifest_reads_a_full_sha_pin(tmp_path):
    assert _load(_manifest(tmp_path, SHA)).repos["x"].revision == SHA


def test_manifest_without_a_pin_has_none(tmp_path):
    assert _load(_manifest(tmp_path, None)).repos["x"].revision is None


@pytest.mark.parametrize("bad", ["main", "v1.2", "abc1234", "A" * 40, "a" * 39, 12345, ["a" * 40], ""])
def test_malformed_pin_is_refused_by_the_schema(tmp_path, bad):
    with pytest.raises(RR.ManifestIntegrityError, match="revision"):
        _load(_manifest(tmp_path, bad))


# --- every repos.yaml reader tolerates the new field ----------------------------------


def test_every_repos_yaml_reader_accepts_a_pinned_row(tmp_path):
    from tools import context_fill_monitor, provision_repos, repo_registry, ticket_lint
    from tools.germinate.components.harness_manager_remove import _repo_entries
    from tools.germinate.registry.validate import known_repos

    _repos_yaml(tmp_path)
    reg = repo_registry.load_repo_registry(tmp_path)
    assert any(r.get("name") == "echo-prismatic" and r.get("revision") == SHA for r in reg["repos"])
    # the shared loader backs ticket_lint, provision_repos, inbox_consumer and context_fill_monitor
    assert ticket_lint._load_repo_registry(tmp_path) == reg
    assert provision_repos._load_registry(tmp_path) == reg
    assert context_fill_monitor._load_repo_registry_for_audit(tmp_path) == reg
    assert ticket_lint.validate_repos_field(["echo-prismatic:ro"], "specialist", reg) is None
    assert "echo-prismatic" in known_repos(tmp_path)
    assert [e["name"] for e in _repo_entries(reg)] == ["echo-prismatic"]  # the pin adds no entry of its own


# --- validate: only a pinned row resolves ----------------------------------------------


class _Fake:
    def __init__(self, path):
        self.path, self.calls = path, []

    def resolve(self, repo, revision, module):
        self.calls.append((repo, revision, module))
        return self.path


def _ext_doc(digest, revision=SHA):
    return F.doc(
        F.component(
            id="comp-a",
            contract={"repo": "echo-prismatic", "module": "pantheon-component.yaml", "digest": digest, "repo_revision": revision},
        )
    )


def test_unpinned_external_row_stays_fail_closed_even_with_a_resolver(tmp_path):
    _repos_yaml(tmp_path, revision=None)
    fake = _Fake(None)
    msgs = _msgs(check_contracts(_ext_doc("sha256:" + "a" * 64), tmp_path, fake))
    assert "no revision pin" in msgs and fake.calls == []


def test_no_resolver_message_names_the_missing_pin(tmp_path):
    _repos_yaml(tmp_path, revision=None)
    msgs = _msgs(check_contracts(_ext_doc("sha256:" + "a" * 64), tmp_path))
    assert "no resolver is configured" in msgs and "no revision pin" in msgs


def test_contract_revision_must_equal_the_repos_yaml_pin(tmp_path):
    _repos_yaml(tmp_path, revision=SHA)
    fake = _Fake(None)
    msgs = _msgs(check_contracts(_ext_doc("sha256:" + "a" * 64, revision="b" * 40), tmp_path, fake))
    assert "!= repos.yaml pin" in msgs and fake.calls == []


def test_malformed_repos_yaml_pin_is_an_error(tmp_path):
    _repos_yaml(tmp_path, revision="main")
    msgs = _msgs(check_contracts(_ext_doc("sha256:" + "a" * 64), tmp_path, _Fake(None)))
    assert "40-hex" in msgs


def test_pinned_row_resolves_through_the_git_resolver_end_to_end(tmp_path):
    fx = Fixture(tmp_path / "fx")
    root = tmp_path / "checkout"
    root.mkdir()
    _repos_yaml(root, revision=fx.sha)
    doc = F.doc(F.component(id="comp-a", contract={"repo": "echo-prismatic", "module": MODULE, "digest": fx.digest, "repo_revision": fx.sha}))
    with GitExternalResolver(lambda repo: fx.url) as resolver:
        assert _msgs(check_contracts(doc, root, resolver)) == ""


def test_resolver_failure_is_a_finding_not_a_crash(tmp_path):
    fx = Fixture(tmp_path / "fx")
    root = tmp_path / "checkout"
    root.mkdir()
    gone = "c" * 40
    _repos_yaml(root, revision=gone)
    doc = F.doc(F.component(id="comp-a", contract={"repo": "echo-prismatic", "module": MODULE, "digest": fx.digest, "repo_revision": gone}))
    with GitExternalResolver(lambda repo: fx.url) as resolver:
        assert "cannot resolve" in _msgs(check_contracts(doc, root, resolver))


# --- CLI: opt-in only ---------------------------------------------------------------------


def test_cli_default_builds_no_resolver(monkeypatch, tmp_path):
    from tools.germinate.registry import cli

    seen = {}
    monkeypatch.setattr(cli, "validate_repo", lambda root, **kw: seen.update(kw) or [])
    args = cli.argparse.Namespace(repo_root=str(tmp_path), registry=None, today=None, resolve_external=False)
    assert cli._cmd_validate(args) == 0
    assert seen["resolver"] is None


def test_cli_flag_wires_the_git_resolver(monkeypatch, tmp_path):
    from tools.germinate.registry import cli

    seen = {}
    monkeypatch.setattr(cli, "validate_repo", lambda root, **kw: seen.update(kw) or [])
    monkeypatch.setattr(cli, "load_production_manifest", lambda **kw: object())
    args = cli.argparse.Namespace(repo_root=str(tmp_path), registry=None, today=None, resolve_external=True)
    assert cli._cmd_validate(args) == 0
    assert isinstance(seen["resolver"], GitExternalResolver)


def test_cli_flag_with_a_malformed_manifest_pin_is_a_clean_error(tmp_path, capsys):
    from tools.germinate.registry import cli

    (tmp_path / "repos.yaml").write_text(
        yaml.safe_dump({"repos": [{"name": "x", "revision": "main", "remotes": [{"role": "primary", "url": "https://h/x.git", "auth": "t"}]}]}),
        encoding="utf-8",
    )
    args = cli.argparse.Namespace(repo_root=str(tmp_path), registry=None, today=None, resolve_external=True)
    assert cli._cmd_validate(args) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "revision" in err and "Traceback" not in err
