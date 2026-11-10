"""Git-backed ``ExternalResolver`` (ORG-081 §7, T-5113): an external repo's contract at a pinned sha.

Read-only and fail-closed. For each (repo, 40-hex revision) it makes a throwaway bare repo in a
private scratch directory, fetches exactly that one commit (``--depth 1 <url> <sha>``), requires
the fetched commit to BE the pin, and materialises ONLY the module's directory into the scratch
directory (the contract checks read its siblings: planner, handlers, invariant list). Blobs are
written with ``cat-file`` from ``ls-tree`` entries: no checkout, so no smudge/clean filter and no
hook can run, and every git call also sets ``core.hooksPath=/dev/null``. A symlink or gitlink
entry in the directory is refused. For a module at the repo root only the top-level files are
written (a root scope must not pull the whole repo). Nothing outside the scratch directory is
written, no existing checkout is touched, and no credential is read here: git uses whatever its
own configuration already gives the URL's host, as the repos.yaml row implies.

Anything short of a verified, symlink-free tree is an ``ExternalResolveError``. The one soft case
is a module that is simply absent at the revision, which returns None (the protocol's contract).

The scratch directory lives until ``close()`` (use the context manager): callers read the returned
path after ``resolve()`` returns.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from tools.germinate.registry.schema import REVISION_RE

DEFAULT_TIMEOUT_SECS = 60


class ExternalResolveError(Exception):
    """The external contract could not be fetched and verified at its pinned revision."""


class GitExternalResolver:
    def __init__(self, url_for: Callable[[str], str], *, timeout: int = DEFAULT_TIMEOUT_SECS):
        """``url_for(repo)`` returns the git URL to fetch (production: through ``resolve_remote``)."""

        self._url_for = url_for
        self._timeout = timeout
        self._scratch: tempfile.TemporaryDirectory | None = None
        self._trees: dict[tuple[str, str, str], Path] = {}

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self) -> "GitExternalResolver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None
        self._trees.clear()

    # -- ExternalResolver --------------------------------------------------------------

    def resolve(self, repo: str, revision: str, module: str) -> Path | None:
        if not (isinstance(revision, str) and REVISION_RE.match(revision)):
            raise ExternalResolveError(f"revision {revision!r} is not a 40-hex commit sha; only a full sha is a pin")
        rel = Path(module)
        if not module or rel.is_absolute() or ".." in rel.parts:
            raise ExternalResolveError(f"module {module!r} must be a relative path without '..'")
        tree = self._tree(repo, revision, module)
        target = tree / rel
        return target if target.is_file() else None

    # -- internals ---------------------------------------------------------------------

    def _tree(self, repo: str, revision: str, module: str) -> Path:
        key = (repo, revision, Path(module).parent.as_posix())
        if key in self._trees:
            return self._trees[key]
        try:
            url = self._url_for(repo)
        except Exception as exc:  # any failure to name a URL is a refusal, never a pass
            raise ExternalResolveError(f"no fetch URL for {repo!r}: {exc}") from exc
        if not isinstance(url, str) or not url or url.startswith("-"):
            raise ExternalResolveError(f"refusing fetch URL {url!r} for {repo!r}")
        if self._scratch is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="pantheon-ext-resolve-")
        base = Path(self._scratch.name) / f"{len(self._trees)}"
        git_dir, tree = base / "git", base / "tree"
        tree.mkdir(parents=True)
        self._git("init", "--bare", "--quiet", str(git_dir))
        self._git("-c", "protocol.ext.allow=never", "--git-dir", str(git_dir), "fetch", "--quiet", "--depth", "1", "--no-tags", url, revision)
        got = self._git("--git-dir", str(git_dir), "rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
        if got != revision:
            raise ExternalResolveError(f"{repo}: fetched commit {got} is not the pinned {revision}")
        self._materialise(repo, revision, module, git_dir, tree)
        self._trees[key] = tree
        return tree

    def _materialise(self, repo: str, revision: str, module: str, git_dir: Path, tree: Path) -> None:
        where = f"{repo}@{revision}"
        directory = Path(module).parent.as_posix()
        at_root = directory == "."
        spec = [] if at_root else [directory + "/"]
        listing = self._git("--git-dir", str(git_dir), "ls-tree", *([] if at_root else ["-r"]), "-z", revision, "--", *spec, text=False)
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, kind, blob = meta.decode().split(" ")
            path = raw_path.decode("utf-8", "surrogateescape")
            if mode == "120000":
                raise ExternalResolveError(f"{where}: symlink {path!r} in the external tree is refused")
            if kind != "blob":
                if mode == "160000" and not at_root:
                    raise ExternalResolveError(f"{where}: gitlink/submodule {path!r} in the external tree is refused")
                continue  # root scope lists only top-level entries; trees and gitlinks there are not written
            parts = Path(path).parts
            if ".." in parts or Path(path).is_absolute() or (not at_root and parts[: len(Path(directory).parts)] != Path(directory).parts):
                raise ExternalResolveError(f"{where}: path {path!r} escapes {directory!r}")
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._git("--git-dir", str(git_dir), "cat-file", "blob", blob, text=False))

    def _git(self, *args: str, text: bool = True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        try:
            done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=text, timeout=self._timeout, env=env, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ExternalResolveError(f"git {' '.join(a for a in args if not a.startswith(('-', '/')))[:40]} timed out after {self._timeout}s") from exc
        except OSError as exc:
            raise ExternalResolveError(f"git could not run: {exc}") from exc
        if done.returncode != 0:
            err = done.stderr if text else done.stderr.decode("utf-8", "replace")
            raise ExternalResolveError(f"git failed ({done.returncode}): {err.strip()[:300]}")
        return done.stdout
