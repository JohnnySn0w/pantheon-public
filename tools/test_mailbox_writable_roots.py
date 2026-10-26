"""T-4963: mailbox_writable_roots refuses '//'-prefixed paths.

POSIX os.path.normpath keeps exactly two leading slashes and pathlib anchors
Path('//x') at '//', so lexical containment and equality checks treat '//x'
as distinct from '/x' while the kernel opens the same file. The resolver and
the maildir probe refuse such a path with MailboxWritableRootError, never a
bare ValueError. All paths live under tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.mailbox_writable_roots import (
    MailboxWritableRootError,
    resolve_mailbox_writable_roots,
    verify_maildir_writable_root,
)

_MANIFEST = {
    "harness_runtime": {
        "roles": {"implementer": {"codex": {"mailbox_writable_roots": [{"kind": "bound-mailbox"}]}}}
    }
}


def _resolve(hub: Path, *, root: Path = Path("/")):
    return resolve_mailbox_writable_roots(
        _MANIFEST,
        role="implementer",
        harness="codex",
        bound_persona="p1",
        bound_mailbox=hub / "p1",
        mailbox_hub=hub,
        root=root,
    )


def _maildir(path: Path) -> Path:
    for sub in ("tmp", "new", "cur"):
        (path / sub).mkdir(parents=True)
    return path


def test_resolve_refuses_double_slash_hub(tmp_path):
    with pytest.raises(MailboxWritableRootError):
        _resolve(Path("/" + str(tmp_path / "hub")))


def test_resolve_refuses_double_slash_hub_under_root_typed(tmp_path):
    # Before T-4963 this raised a bare ValueError from relative_to("/").
    with pytest.raises(MailboxWritableRootError):
        _resolve(Path("//hub"), root=tmp_path)


def test_resolve_accepts_single_slash_hub(tmp_path):
    hub = tmp_path / "hub"
    (root,) = _resolve(hub)
    assert root.path == hub / "p1"
    (rooted,) = _resolve(Path("/hub"), root=tmp_path)
    assert rooted.path == tmp_path / "hub" / "p1"


def test_verify_refuses_double_slash(tmp_path):
    inbox = _maildir(tmp_path / "inbox")
    with pytest.raises(MailboxWritableRootError):
        verify_maildir_writable_root(Path("/" + str(inbox)))


def test_verify_accepts_single_slash(tmp_path):
    inbox = _maildir(tmp_path / "inbox")
    assert verify_maildir_writable_root(inbox)["root"] == str(inbox)
