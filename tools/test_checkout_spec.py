"""tools/test_checkout_spec.py — T-5071: target_path must be a normalised absolute path.

Mirrors the T-2917 D32 idiom (tools/host_control/registry.py): a `//` prefix, any
`..` component, or any path where os.path.normpath(p) != p is refused at load.
Trailing `/` and `.` segments count as un-normalised and are refused too, so the
stored value is byte-identical to what a consumer would compute.

Hermetic: dict fixtures go straight to parse_checkout_file; the one end-to-end
test writes its checkouts dir under pytest's tmp_path. No real repo/state path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.checkout_spec import (
    RepositoryLifecycleError,
    load_checkout_spec,
    parse_checkout_file,
)


def _data(target_path: str) -> dict:
    return {
        "schema_version": 1,
        "checkouts": [
            {
                "checkout_id": "x",
                "logical_repo": "demo",
                "remote_ref": "manifest",
                "target_account": "acct",
                "target_path": target_path,
                "mode": {"tracking": "main"},
                "update_policy": "ff-only",
            }
        ],
    }


@pytest.mark.parametrize("bad", ["/home/x/../demo", "/home/x/..", "/.."])
def test_refuses_dotdot_segments(bad: str) -> None:
    with pytest.raises(RepositoryLifecycleError, match="normalised"):
        parse_checkout_file(_data(bad), source_file="t.yaml")


@pytest.mark.parametrize("bad", ["//home/x/demo", "///home/x"])
def test_refuses_double_slash_prefix(bad: str) -> None:
    # "//home/x/demo" survives normpath unchanged on POSIX; refused explicitly.
    with pytest.raises(RepositoryLifecycleError, match="normalised"):
        parse_checkout_file(_data(bad), source_file="t.yaml")


def test_refuses_trailing_slash() -> None:
    with pytest.raises(RepositoryLifecycleError, match="normalised"):
        parse_checkout_file(_data("/home/x/demo/"), source_file="t.yaml")


@pytest.mark.parametrize("bad", ["/home/x/./demo", "/home/x/demo/."])
def test_refuses_dot_segments(bad: str) -> None:
    with pytest.raises(RepositoryLifecycleError, match="normalised"):
        parse_checkout_file(_data(bad), source_file="t.yaml")


def test_refuses_inner_double_slash() -> None:
    with pytest.raises(RepositoryLifecycleError, match="normalised"):
        parse_checkout_file(_data("/home/x//demo"), source_file="t.yaml")


def test_accepts_normalised_absolute_path() -> None:
    (entry,) = parse_checkout_file(_data("/home/x/demo"), source_file="t.yaml")
    assert entry.target_path == "/home/x/demo"


def test_relative_path_keeps_absolute_message() -> None:
    with pytest.raises(RepositoryLifecycleError, match="absolute POSIX path"):
        parse_checkout_file(_data("relative/path"), source_file="t.yaml")


def test_load_checkout_spec_refuses_dotdot_end_to_end(tmp_path: Path) -> None:
    d = tmp_path / "checkouts"
    d.mkdir()
    (d / "global.yaml").write_text(
        "schema_version: 1\n"
        "checkouts:\n"
        "  - checkout_id: demo-one\n"
        "    logical_repo: demo-repo\n"
        "    remote_ref: manifest\n"
        "    target_account: acct\n"
        "    target_path: /home/x/../demo\n"
        "    mode:\n"
        "      tracking: main\n"
        "    update_policy: ff-only\n",
        encoding="utf-8",
    )
    with pytest.raises(RepositoryLifecycleError) as exc:
        load_checkout_spec(d)
    msg = str(exc.value)
    assert "normalised" in msg
    assert "global.yaml" in msg
    assert "demo-one" in msg
