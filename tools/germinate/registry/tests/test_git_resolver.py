"""T-5113: the git-backed ORG-081 §7 ExternalResolver, against a LOCAL bare repo (no network)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.germinate.engine.tests import helpers as H
from tools.germinate.registry.git_resolver import ExternalResolveError, GitExternalResolver

MODULE = "tools/germinate/components/comp-a.yaml"
_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Fixture:
    def __init__(self, root: Path):
        self.work = root / "work"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-q", "-b", "main")
        _path, self.digest = H.write_contract(self.work, "comp-a")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "one")
        self.sha = git(self.work, "rev-parse", "HEAD")
        self.bare = root / "origin.git"
        git(root, "clone", "-q", "--bare", str(self.work), str(self.bare))
        git(self.bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.url = f"file://{self.bare}"
        # a commit that exists in the bare repo but is reachable from no ref
        git(self.work, "checkout", "-q", "-b", "tmp")
        (self.work / "extra.txt").write_text("x", encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "orphan")
        self.orphan = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", str(self.bare), "tmp")
        git(self.bare, "update-ref", "-d", "refs/heads/tmp")


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


@pytest.fixture
def resolver(fx):
    with GitExternalResolver(lambda repo: fx.url) as r:
        yield r


def test_pinned_revision_resolves_to_the_module_bytes(fx, resolver):
    path = resolver.resolve("echo-prismatic", fx.sha, MODULE)
    assert path is not None and path.read_bytes() == (fx.work / MODULE).read_bytes()
    assert path.with_suffix(".py").is_file()  # siblings the contract checks read come along


def test_a_sha_that_does_not_exist_fails_closed(fx, resolver):
    with pytest.raises(ExternalResolveError):
        resolver.resolve("echo-prismatic", "c" * 40, MODULE)


@pytest.mark.parametrize("bad", ["main", "v1.0", "abc1234", "B" * 40, "", "a" * 41])
def test_a_revision_that_is_not_a_full_lowercase_sha_is_refused(fx, resolver, bad):
    with pytest.raises(ExternalResolveError, match="40-hex"):
        resolver.resolve("echo-prismatic", bad, MODULE)


def test_module_absent_at_the_revision_returns_none(fx, resolver):
    assert resolver.resolve("echo-prismatic", fx.sha, "nope.yaml") is None


@pytest.mark.parametrize("bad", ["../x.yaml", "/etc/passwd", "a/../../b"])
def test_module_path_escape_is_refused(fx, resolver, bad):
    with pytest.raises(ExternalResolveError, match="relative"):
        resolver.resolve("echo-prismatic", fx.sha, bad)


def test_unreachable_url_fails_closed(fx, tmp_path):
    with GitExternalResolver(lambda repo: f"file://{tmp_path}/missing.git") as r:
        with pytest.raises(ExternalResolveError):
            r.resolve("echo-prismatic", fx.sha, MODULE)


def test_url_lookup_failure_is_an_external_resolve_error(fx):
    def boom(repo):
        raise RuntimeError("no remote")

    with GitExternalResolver(boom) as r:
        with pytest.raises(ExternalResolveError, match="no remote"):
            r.resolve("echo-prismatic", fx.sha, MODULE)


def test_timeout_fails_closed(fx, monkeypatch):
    import tools.germinate.registry.git_resolver as G

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(G.subprocess, "run", hang)
    with GitExternalResolver(lambda repo: fx.url, timeout=1) as r:
        with pytest.raises(ExternalResolveError, match="timed out"):
            r.resolve("echo-prismatic", fx.sha, MODULE)


def test_unreachable_sha_resolves_when_the_server_allows_any_sha(fx, resolver):
    # the pin is a content address; reachability from a ref is the server's policy, not ours
    assert resolver.resolve("echo-prismatic", fx.orphan, "extra.txt") is not None


def test_unreachable_sha_fails_closed_when_the_server_refuses_it(fx, resolver, monkeypatch):
    # protocol v2 servers serve any object they hold; under v0 the server's own policy applies
    git(fx.bare, "config", "uploadpack.allowAnySHA1InWant", "false")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.version")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")
    with pytest.raises(ExternalResolveError):
        resolver.resolve("echo-prismatic", fx.orphan, "extra.txt")


def test_a_symlink_in_the_external_tree_is_refused(fx, resolver):
    git(fx.work, "checkout", "-q", "main")
    (fx.work / "tools/germinate/components/link").symlink_to("/etc/passwd")
    git(fx.work, "add", "-A")
    git(fx.work, "commit", "-q", "-m", "link")
    sha = git(fx.work, "rev-parse", "HEAD")
    git(fx.work, "push", "-q", str(fx.bare), "main")
    with pytest.raises(ExternalResolveError, match="symlink"):
        resolver.resolve("echo-prismatic", sha, MODULE)


def test_no_write_to_any_existing_checkout(fx, resolver):
    before = subprocess.run(["git", "status", "--porcelain"], cwd=fx.work, capture_output=True, text=True).stdout
    resolver.resolve("echo-prismatic", fx.sha, MODULE)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=fx.work, capture_output=True, text=True).stdout
    assert before == after


def test_scratch_is_removed_on_close(fx):
    r = GitExternalResolver(lambda repo: fx.url)
    path = r.resolve("echo-prismatic", fx.sha, MODULE)
    assert path.exists()
    r.close()
    assert not path.exists()
    r.close()  # idempotent


# --- T-5113 review fixes: no checkout, so no hook and no filter can run -----------------------


def _commit_all(fx, msg: str) -> str:
    git(fx.work, "checkout", "-q", "main")
    git(fx.work, "add", "-A")
    git(fx.work, "commit", "-q", "-m", msg)
    git(fx.work, "push", "-q", str(fx.bare), "main")
    return git(fx.work, "rev-parse", "HEAD")


def test_fetch_head_that_is_not_the_pin_is_refused(fx, resolver, monkeypatch):
    real = GitExternalResolver._git

    def lying(self, *args, **kw):
        out = real(self, *args, **kw)
        return fx.orphan + "\n" if "rev-parse" in args else out

    monkeypatch.setattr(GitExternalResolver, "_git", lying)
    with pytest.raises(ExternalResolveError, match="is not the pinned"):
        resolver.resolve("echo-prismatic", fx.sha, MODULE)


def test_a_symlinked_directory_in_the_module_dir_is_refused(fx, resolver):
    (fx.work / "tools/germinate/components/sub").symlink_to("../..")
    sha = _commit_all(fx, "dirlink")
    with pytest.raises(ExternalResolveError, match="symlink"):
        resolver.resolve("echo-prismatic", sha, MODULE)


def test_a_submodule_entry_in_the_module_dir_is_refused(fx, resolver):
    git(fx.work, "checkout", "-q", "main")
    git(fx.work, "update-index", "--add", "--cacheinfo", f"160000,{fx.sha},tools/germinate/components/vendored")
    git(fx.work, "commit", "-q", "-m", "gitlink")
    git(fx.work, "push", "-q", str(fx.bare), "main")
    sha = git(fx.work, "rev-parse", "HEAD")
    with pytest.raises(ExternalResolveError, match="gitlink"):
        resolver.resolve("echo-prismatic", sha, MODULE)


def test_only_the_module_directory_is_materialised(fx, resolver):
    (fx.work / "tools/germinate/components/helpers").mkdir()
    (fx.work / "tools/germinate/components/helpers/h.py").write_text("H = 1\n", encoding="utf-8")
    (fx.work / "unrelated.txt").write_text("u", encoding="utf-8")
    sha = _commit_all(fx, "helpers")
    path = resolver.resolve("echo-prismatic", sha, MODULE)
    assert (path.parent / "helpers" / "h.py").read_text() == "H = 1\n"  # local helpers come along
    assert path.with_name("comp-a.invariants.yaml").exists() or path.with_suffix(".py").exists()
    assert not any(p.name == "unrelated.txt" for p in path.parents[3].rglob("*"))


def test_a_global_post_checkout_hook_does_not_fire(fx, tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-fired"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho fired > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    with GitExternalResolver(lambda repo: fx.url) as r:
        assert r.resolve("echo-prismatic", fx.sha, MODULE) is not None
    assert not marker.exists()


def test_a_configured_smudge_filter_selected_by_gitattributes_does_not_fire(fx, tmp_path, monkeypatch):
    (fx.work / ".gitattributes").write_text("* filter=x\n", encoding="utf-8")
    sha = _commit_all(fx, "attrs")
    marker = tmp_path / "smudge-fired"
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[filter \"x\"]\n\tsmudge = sh -c 'echo hit > {marker}; cat'\n\trequired = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    with GitExternalResolver(lambda repo: fx.url) as r:
        path = r.resolve("echo-prismatic", sha, MODULE)
        assert path.read_bytes() == (fx.work / MODULE).read_bytes()
    assert not marker.exists()
