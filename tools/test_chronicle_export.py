"""Tests for tools/chronicle_export.py and the session-end export stage (T-6118 PR-2).

tmp roots only: the export tree, the Chronicle, the account home and the seats file are all tmp_path
stand-ins. Nothing here touches /var/lib/pantheon/chronicle-export or a real seat (conftest pins
PANTHEON_CHRONICLE_EXPORT_ROOT to an absent tmp path and the tripwire fails the run on a leak).
The group is the test process's own gid."""
from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import chronicle, chronicle_export as cx
from tools import session_chronicle as sc
from tools.test_session_chronicle import FIXTURES, NOW, OWN_GROUP, Seat, _codex_day_dir

GID = os.getegid()


def _mk(path: Path, data: bytes = b"x\n", mode: int = 0o640) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    os.chown(path, -1, GID)
    return path


def _tree(tmp: Path, seat: str = "specialist") -> Path:
    root = tmp / "export"
    (root / seat).mkdir(parents=True)
    os.chown(root / seat, -1, GID)
    return root


# ------------------------------------------------------------------ names and roots
def test_export_root_precedence_kwarg_then_env_then_default(monkeypatch, tmp_path):
    monkeypatch.delenv(cx.EXPORT_ROOT_ENV, raising=False)
    assert cx.export_root() == Path("/var/lib/pantheon/chronicle-export") == cx.DEFAULT_EXPORT_ROOT
    monkeypatch.setenv(cx.EXPORT_ROOT_ENV, str(tmp_path / "env"))
    assert cx.export_root() == tmp_path / "env"
    assert cx.export_root(tmp_path / "kw") == tmp_path / "kw"


def test_allowed_suffixes_and_the_pane_log_is_forbidden():
    assert cx.ALLOWED_SUFFIXES == (".md", ".summary.md", ".session.json")
    for ok in ("a.md", "a.session.json", "a.transcript.jsonl.gz", "x.transcript.jsonl.2.gz",
               "x.transcript.jsonl.13.gz"):
        assert cx.exportable(ok), ok
    for bad in ("a.pane.log.gz", "x.pane.log.2.gz", "x.transcript.jsonl.gz.tmp", "x.transcript.jsonl.2a.gz",
                "x.transcript.jsonl..gz", "x.transcript.jsonl.2.gz.tmp", "x.md.2", "a.txt", "../a.md", "d/a.md",
                "d/x.transcript.jsonl.2.gz", "", ".", "a.md\x00"):
        assert not cx.exportable(bad), bad


# ------------------------------------------------------------------ the dir-fd chain
def test_missing_root_and_missing_seat_are_unprovisioned_and_create_nothing(tmp_path):
    with pytest.raises(cx.ExportUnprovisioned):
        cx.ExportDir(tmp_path / "nope", "specialist", "2026-11-04")
    root = tmp_path / "export"
    root.mkdir()
    with pytest.raises(cx.ExportUnprovisioned):
        cx.ExportDir(root, "specialist", "2026-11-04")
    assert list(root.iterdir()) == []


def test_date_dir_is_created_2750_and_owned(tmp_path):
    root = _tree(tmp_path)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        st = os.fstat(target.fd)
    day = root / "specialist" / "2026-11-04"
    assert stat.S_IMODE(day.stat().st_mode) == 0o2750 and day.stat().st_uid == os.geteuid()
    assert st.st_gid == GID


def test_planted_symlinks_are_refused_at_every_level(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "export"
    root.mkdir()
    os.symlink(target, root / "specialist")
    with pytest.raises(cx.ExportRefused):
        cx.ExportDir(root, "specialist", "2026-11-04")
    (root / "specialist").unlink()
    (root / "specialist").mkdir()
    os.symlink(target, root / "specialist" / "2026-11-04")
    with pytest.raises(cx.ExportRefused):
        cx.ExportDir(root, "specialist", "2026-11-04")
    link_root = tmp_path / "linkroot"
    os.symlink(root, link_root)
    with pytest.raises(cx.ExportRefused):
        cx.ExportDir(link_root, "specialist", "2026-11-04")
    assert list(target.iterdir()) == []


def test_a_date_dir_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    (root / "specialist" / "2026-11-04").mkdir()
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(cx.ExportRefused):
        cx.ExportDir(root, "specialist", "2026-11-04")


def test_the_ingested_dir_and_bad_seat_names_are_not_export_seats(tmp_path):
    root = _tree(tmp_path, ".ingested")
    for seat in (".ingested", "..", "a/b", ""):
        with pytest.raises(cx.ExportRefused):
            cx.ExportDir(root, seat, "2026-11-04")


# ------------------------------------------------------------------ link
def _link_env(tmp_path, name="s.md"):
    root = _tree(tmp_path)
    src = _mk(tmp_path / "chron" / name, b"hello\n")
    return root, src


def test_link_hardlinks_to_the_final_name_sharing_the_inode(tmp_path):
    root, src = _link_env(tmp_path)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        res = target.link(src)
    dst = root / "specialist" / "2026-11-04" / "s.md"
    assert res.state == "linked" and res.size == 6
    assert dst.stat().st_ino == src.stat().st_ino and src.stat().st_nlink == 2
    assert not list(dst.parent.glob(".*"))  # no temp name was ever used


def test_link_is_idempotent_and_identical_bytes_at_another_inode_count_as_done(tmp_path):
    root, src = _link_env(tmp_path)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        assert target.link(src).state == "linked"
        assert target.link(src).state == "already"
    other = _mk(root / "specialist" / "2026-11-04" / "t.md", b"hello\n")
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        copy = _mk(tmp_path / "chron" / "t.md", b"hello\n")
        res = target.link(copy)
    assert res.state == "already" and other.stat().st_ino != copy.stat().st_ino


def test_different_bytes_under_the_final_name_are_refused_and_never_unlinked(tmp_path):
    root, src = _link_env(tmp_path)
    existing = _mk(root / "specialist" / "2026-11-04" / "s.md", b"DIFFERENT\n")
    ino = existing.stat().st_ino
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        with pytest.raises(cx.ExportRefused):
            target.link(src)
    assert existing.read_bytes() == b"DIFFERENT\n" and existing.stat().st_ino == ino and src.exists()


def test_same_size_different_bytes_under_the_final_name_are_refused_and_untouched(tmp_path):
    # Pins the sha comparison: size alone must never count as "already exported".
    root, src = _link_env(tmp_path)  # src is b"hello\n"
    existing = _mk(root / "specialist" / "2026-11-04" / "s.md", b"HELLO\n")
    assert existing.stat().st_size == src.stat().st_size
    ino = existing.stat().st_ino
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        with pytest.raises(cx.ExportRefused):
            target.link(src)
    assert existing.read_bytes() == b"HELLO\n" and existing.stat().st_ino == ino and src.read_bytes() == b"hello\n"


def test_link_refuses_a_symlink_source_and_a_non_group_readable_one(tmp_path):
    root, src = _link_env(tmp_path)
    sym = tmp_path / "chron" / "sym.md"
    os.symlink(src, sym)
    private = _mk(tmp_path / "chron" / "p.md", b"p", mode=0o600)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        with pytest.raises(cx.ExportRefused):
            target.link(sym)
        with pytest.raises(cx.ExportRefused):
            target.link(private)


def test_link_refuses_a_source_in_another_group_and_another_owner(tmp_path, monkeypatch):
    root, src = _link_env(tmp_path)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        target.gid = GID + 12345
        with pytest.raises(cx.ExportRefused):
            target.link(src)
        target.gid = GID
        real = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real + 1)
        with pytest.raises(cx.ExportRefused):
            target.link(src)


def test_link_refuses_the_pane_log_and_non_exportable_names(tmp_path):
    root, _ = _link_env(tmp_path)
    pane = _mk(tmp_path / "chron" / "s.pane.log.gz", b"pane")
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        with pytest.raises(cx.ExportRefused):
            target.link(pane)
        with pytest.raises(cx.ExportRefused):
            target.link(_mk(tmp_path / "chron" / "s.md"), "../escape.md")
    assert not list((root / "specialist" / "2026-11-04").iterdir())


def test_exdev_fails_with_no_copy_fallback(tmp_path, monkeypatch):
    root, src = _link_env(tmp_path)

    def cross(*a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", cross)
    with cx.ExportDir(root, "specialist", "2026-11-04") as target:
        with pytest.raises(cx.ExportFailed, match="EXDEV"):
            target.link(src)
    assert not list((root / "specialist" / "2026-11-04").iterdir())


# ------------------------------------------------------------------ ensure_group_readable
def test_ensure_group_readable_fixes_0600_in_place_so_existing_links_see_it(tmp_path):
    src = _mk(tmp_path / "a.md", b"a", mode=0o600)
    twin = tmp_path / "twin.md"
    os.link(src, twin)
    assert cx.ensure_group_readable(src, GID) is True
    assert stat.S_IMODE(twin.stat().st_mode) == 0o640 and twin.stat().st_ino == src.stat().st_ino
    assert cx.ensure_group_readable(src, GID) is False


def test_ensure_group_readable_refuses_a_symlink_and_a_foreign_file(tmp_path, monkeypatch):
    src = _mk(tmp_path / "a.md")
    sym = tmp_path / "sym.md"
    os.symlink(src, sym)
    with pytest.raises(cx.ExportRefused):
        cx.ensure_group_readable(sym, GID)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(cx.ExportRefused):
        cx.ensure_group_readable(src, GID)


# ------------------------------------------------------------------ export_files
def test_export_files_vocabulary(tmp_path):
    root = _tree(tmp_path)
    a, b = _mk(tmp_path / "c" / "a.md"), _mk(tmp_path / "c" / "b.transcript.jsonl.gz", b"gz")
    done = cx.export_files((b, a, None), root=root, seat="specialist", date="2026-11-04")
    assert done.status == cx.STATUS_EXPORTED and [f["name"] for f in done.files] == [b.name, a.name]
    assert done.as_meta().keys() == {"status", "seat", "date", "files", "root", "detail"}
    pane = _mk(tmp_path / "c" / "x.pane.log.gz")
    c = _mk(tmp_path / "c" / "c.md")
    partial = cx.export_files((c, pane), root=root, seat="specialist", date="2026-11-04")
    assert partial.status == cx.STATUS_PARTIAL and "not an exportable" in partial.detail
    refused = cx.export_files((pane,), root=root, seat="specialist", date="2026-11-04")
    assert refused.status == cx.STATUS_REFUSED
    unprov = cx.export_files((a,), root=tmp_path / "absent", seat="specialist", date="2026-11-04")
    assert unprov.status == cx.STATUS_UNPROVISIONED
    assert cx.ALERT_STATUSES == {cx.STATUS_PARTIAL, cx.STATUS_SEAT_UNRESOLVED, cx.STATUS_REFUSED, cx.STATUS_FAILED}


def test_export_files_failed_status_on_exdev(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    a = _mk(tmp_path / "c" / "a.md")
    monkeypatch.setattr(os, "link", lambda *x, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")))
    out = cx.export_files((a,), root=root, seat="specialist", date="2026-11-04")
    assert out.status == cx.STATUS_FAILED and "EXDEV" in out.detail


def test_export_files_fixes_a_0600_md_before_linking(tmp_path):
    root = _tree(tmp_path)
    md = _mk(tmp_path / "c" / "a.md", b"m", mode=0o600)
    assert cx.export_files((md,), root=root, seat="specialist", date="2026-11-04").status == cx.STATUS_EXPORTED
    assert stat.S_IMODE((root / "specialist" / "2026-11-04" / "a.md").stat().st_mode) == 0o640


# ------------------------------------------------------------------ seat resolution
def _seats_repo(tmp_path, seats: dict) -> Path:
    path = tmp_path / "repo" / "harness" / "codex" / "appserver_seats.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"seats": seats}))
    return tmp_path / "repo"


def _me() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def test_claude_exports_under_the_persona(tmp_path):
    assert cx.resolve_seat(persona="specialist", harness="claude", account_home=tmp_path) == ("specialist", None)


def test_codex_one_match_gives_the_appserver_seat(tmp_path):
    repo = _seats_repo(tmp_path, {"consultant": {"account": _me()}})
    assert cx.resolve_seat(persona="codex", harness="codex", account_home=tmp_path, repo_root=repo) == ("consultant", None)


def test_codex_zero_matches_falls_back_to_the_persona(tmp_path):
    repo = _seats_repo(tmp_path, {"consultant": {"account": "someone-else"}})
    assert cx.resolve_seat(persona="artisan", harness="codex", account_home=tmp_path, repo_root=repo) == ("artisan", None)


def test_codex_ambiguous_or_unreadable_is_unresolved(tmp_path):
    repo = _seats_repo(tmp_path, {"a": {"account": _me()}, "b": {"account": _me()}})
    seat, why = cx.resolve_seat(persona="codex", harness="codex", account_home=tmp_path, repo_root=repo)
    assert seat is None and "found ['a', 'b']" in why
    seat, why = cx.resolve_seat(persona="codex", harness="codex", account_home=tmp_path, repo_root=tmp_path / "none")
    assert seat is None and "cannot read" in why


# ------------------------------------------------------------------ find_supersedes
class _World:
    """A chronicle + export tree with prior exported records for one source path."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "chron"
        self.export = _tree(tmp)
        self.agent = "specialist"
        self.source = "/home/x/.claude/projects/p/s.jsonl"
        self.n = 0

    def record(self, payload: bytes, *, ts: str, source: str | None = None, status: str = "exported",
               export_file: bool = True) -> Path:
        self.n += 1
        uuid = f"u{self.n}"
        stem = f"{chronicle._fs_safe_ts(ts)}-{uuid}"
        day = self.root / self.agent / ts[:10]
        gz = _mk(day / f"{stem}.transcript.jsonl.gz", gzip.compress(payload))
        index = self.root / self.agent / "index.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a") as fh:
            fh.write(f"- {ts} | session-end/death | {uuid} | x\n")
        if export_file:
            dst = self.export / "specialist" / ts[:10] / gz.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(gz, dst)
        meta = {"native_transcript": {"path": str(gz), "source_path": source or self.source},
                "export": {"status": status, "seat": "specialist", "date": ts[:10], "files": [{"name": gz.name}]}}
        (day / f"{stem}.session.json").write_text(json.dumps(meta))
        return gz

    def new_gz(self, payload: bytes) -> Path:
        return _mk(self.root / "new" / "n.transcript.jsonl.gz", gzip.compress(payload))

    def find(self, new: Path, **kw):
        return cx.find_supersedes(agent=self.agent, chron_root=self.root, export_root_=self.export, new_gz=new,
                                  source_path=self.source, now=datetime(2026, 11, 4, tzinfo=timezone.utc), **kw)


def test_a_strict_prefix_copy_is_superseded_with_its_on_disk_size_and_sha(tmp_path):
    w = _World(tmp_path)
    old = w.record(b"line1\n", ts="2026-11-02T10:00:00Z")
    found, detail = w.find(w.new_gz(b"line1\nline2\n"))
    assert detail is None
    assert found == [{"file": f"specialist/2026-11-02/{old.name}", "size": old.stat().st_size,
                      "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}]
    assert old.exists() and (w.export / "specialist" / "2026-11-02" / old.name).exists()  # nothing deleted


def test_equal_diverged_and_foreign_copies_are_not_superseded(tmp_path):
    w = _World(tmp_path)
    w.record(b"same\n", ts="2026-11-01T10:00:00Z")
    w.record(b"other\n", ts="2026-11-01T11:00:00Z")
    w.record(b"same\n", ts="2026-11-01T12:00:00Z", source="/another/source.jsonl")
    w.record(b"same\n", ts="2026-11-01T13:00:00Z", status="export-unprovisioned", export_file=False)
    w.record(b"same\n", ts="2026-11-01T14:00:00Z", export_file=False)  # claims exported, file gone
    found, _ = w.find(w.new_gz(b"same\n"))  # equal bytes: not STRICT
    assert found == []
    found, _ = w.find(w.new_gz(b"same\nmore\n"))
    assert len(found) == 1  # only the first (same source, exported, still present)


def test_the_scan_is_bounded_by_days_and_records(tmp_path):
    w = _World(tmp_path)
    w.record(b"a\n", ts="2026-08-01T10:00:00Z")  # > 60 days before 2026-11-04
    assert w.find(w.new_gz(b"a\nb\n"))[0] == []
    w2 = _World(tmp_path / "w2")
    w2.record(b"a\n", ts="2026-11-01T10:00:00Z")
    w2.record(b"zz\n", ts="2026-11-02T10:00:00Z")
    assert w2.find(w2.new_gz(b"a\nb\n"), max_records=1)[0] == []  # only the newest record is looked at
    assert len(w2.find(w2.new_gz(b"a\nb\n"), max_records=2)[0]) == 1


def test_a_scan_past_its_deadline_ships_both_and_says_so(tmp_path):
    w = _World(tmp_path)
    w.record(b"a\n", ts="2026-11-01T10:00:00Z")
    ticks = iter(range(0, 1000, 100))
    found, detail = w.find(w.new_gz(b"a\nb\n"), clock=lambda: float(next(ticks)), deadline_seconds=20.0)
    assert found == [] and "deadline" in detail and "both copies ship" in detail


def test_strict_prefix_deadline_is_checked_inside_the_streaming_loop(tmp_path):
    # Inputs span more than one 1 MiB block; the clock expires after the first block is consumed.
    block = os.urandom(1 << 20)
    old = _mk(tmp_path / "old.gz", gzip.compress(block + block))
    new = _mk(tmp_path / "new.gz", gzip.compress(block + block + b"more\n"))
    assert cx._strict_prefix(old, new, 10.0, lambda: 0.0) is True  # control: no expiry, real strict prefix
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    assert cx._strict_prefix(old, new, 10.0, lambda: next(ticks)) is None  # safe direction: undetermined


def test_a_symlinked_export_copy_is_never_superseded_or_read_through(tmp_path, monkeypatch):
    # sha256_file also refuses symlinks, so `found == []` alone would survive removing the guard;
    # the read-through itself (_strict_prefix opening the link) is what must not happen.
    opened = []
    real = cx._strict_prefix
    monkeypatch.setattr(cx, "_strict_prefix", lambda *a, **k: (opened.append(a[0]), real(*a, **k))[1])
    w = _World(tmp_path)
    old = w.record(b"line1\n", ts="2026-11-02T10:00:00Z", export_file=False)
    real_prefix = _mk(tmp_path / "elsewhere" / "valid.gz", gzip.compress(b"line1\n"))  # a valid strict prefix
    link = w.export / "specialist" / "2026-11-02" / old.name
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_prefix, link)
    found, detail = w.find(w.new_gz(b"line1\nline2\n"))
    assert found == [] and detail is None and opened == []


def test_forbidden_suffix_holds_on_its_own_even_if_gz_were_allowed(monkeypatch):
    monkeypatch.setattr(cx, "ALLOWED_SUFFIXES", (".md", ".session.json", ".gz"))
    assert cx.exportable("x.pane.log.gz") is False
    assert cx.exportable("x.md") is True  # control: the patch did not break the allow path


# ------------------------------------------------------------------ session-end integration
def _ready(tmp_path, harness="claude", persona="specialist"):
    seat = Seat(tmp_path, persona=persona, harness=harness)
    src = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "1111-claude.jsonl"
    src.parent.mkdir(parents=True)
    src.write_text('{"turn": 1}\n')
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    root = _tree(tmp_path, persona)
    return seat, src, root


def test_session_end_exports_gz_md_and_session_json_as_hardlinks(tmp_path):
    seat, src, root = _ready(tmp_path)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "archived" and out["results"][0]["export"] == "exported", out
    (gz,) = seat.files(".transcript.jsonl.gz")
    (md,) = seat.files(".md")
    (meta_path,) = seat.files(".session.json")
    day = root / "specialist" / gz.parent.name
    assert sorted(p.name for p in day.iterdir()) == sorted([gz.name, md.name, meta_path.name])
    for original in (gz, md, meta_path):
        assert (day / original.name).stat().st_ino == original.stat().st_ino
        assert stat.S_IMODE(original.stat().st_mode) == 0o640
    meta = seat.meta()
    assert meta["export"]["status"] == "exported" and meta["export"]["seat"] == "specialist"
    assert meta["export"]["root"] == str(root) and [f["name"] for f in meta["export"]["files"]] == [gz.name, md.name]
    assert meta["supersedes"] == [] and out["results"][0]["export_session_json"] == "linked"
    assert not list(day.glob("*pane*")) and seat.alerts == []


def test_session_json_is_linked_last_the_commit_marker(tmp_path, monkeypatch):
    seat, _, root = _ready(tmp_path)
    order = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda s, d, *a, **k: (order.append(Path(d).name), real(s, d, *a, **k))[1]
                        if k.get("dst_dir_fd") is not None else real(s, d, *a, **k))
    seat.run("death", export_root=root)
    assert [n.rsplit(".", 2)[-1] if n.endswith("json") else n.split(".")[-1] for n in order] == ["gz", "md", "json"]
    assert order[-1].endswith(".session.json")


def test_an_unprovisioned_export_is_recorded_without_an_alert(tmp_path):
    seat, _, _ = _ready(tmp_path)
    out = seat.run("death", export_root=tmp_path / "absent")
    assert out["results"][0]["status"] == "archived"
    assert seat.meta()["export"]["status"] == "export-unprovisioned" and seat.alerts == []
    assert "export_session_json" not in out["results"][0]


def test_the_default_export_root_in_tests_is_the_absent_conftest_path(tmp_path):
    seat, _, _ = _ready(tmp_path)
    seat.run("death")
    meta = seat.meta()
    assert meta["export"]["status"] == "export-unprovisioned"
    assert meta["export"]["root"] == os.environ[cx.EXPORT_ROOT_ENV] != str(cx.DEFAULT_EXPORT_ROOT)


def _handoff_seat(tmp_path, harness="claude"):
    seat = Seat(tmp_path, harness=harness)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    return seat, _tree(tmp_path)


def _file_set(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_a_record_without_a_transcript_exports_the_md_then_the_session_json(tmp_path, monkeypatch):
    seat, root = _handoff_seat(tmp_path)
    order = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda s, d, *a, **k: (order.append(Path(d).name), real(s, d, *a, **k))[1]
                        if k.get("dst_dir_fd") is not None else real(s, d, *a, **k))
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "exported" and out["results"][0]["export_session_json"] == "linked"
    (md,) = seat.files(".md")
    (meta_path,) = seat.files(".session.json")
    assert order == [md.name, meta_path.name]  # exactly two files, the session.json last
    day = root / "specialist" / md.parent.name
    assert sorted(p.name for p in day.iterdir()) == sorted([md.name, meta_path.name])
    meta = seat.meta()
    assert meta["transcript"] == "absent" and meta["transcript_absent_reason"] == "none-resolved"
    assert meta["native_transcript"] is None
    (entry,) = meta["export"]["files"]
    assert entry["name"] == md.name and entry["size"] == md.stat().st_size
    assert entry["sha256"] == hashlib.sha256(md.read_bytes()).hexdigest()
    assert seat.alerts == []


def test_absent_reason_names_the_cause(tmp_path):
    seat, root = _handoff_seat(tmp_path / "a", harness="hermes")
    seat.run("death", export_root=root)
    assert seat.meta()["transcript_absent_reason"] == "harness-not-archived"
    seat, src, root = _ready(tmp_path / "b")
    seat.run("death", export_root=root, native_transcript={"path": str(src), "bytes": 1, "sha256": "0" * 64})
    meta = seat.meta()  # a pointer whose archive is not under root/agent: the archive did not happen
    assert meta["transcript"] == "absent" and meta["transcript_absent_reason"] == "archive-failed"
    assert meta["native_transcript"]["path"] == str(src)


def test_a_record_with_a_transcript_says_archived_and_has_no_reason(tmp_path):
    seat, _, root = _ready(tmp_path)
    seat.run("death", export_root=root)
    meta = seat.meta()
    assert meta["transcript"] == "archived" and "transcript_absent_reason" not in meta


def _plant_md(root: Path, seat: str, date: str, name: str, data: bytes, *, committed: bool = True) -> Path:
    """Plant an exported .md; a committed drop also has its sibling .session.json (the commit marker)."""
    if committed:
        _mk(root / seat / date / (name[:-3] + ".session.json"), b"{}\n")
    return _mk(root / seat / date / name, data)


HANDOFF_BYTES = b"# Handoff\nnext: ship T-5016\n"


def test_a_byte_identical_handoff_is_skipped_and_writes_nothing(tmp_path):
    seat, root = _handoff_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    before = _file_set(root)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "archived" and out["results"][0]["export"] == "handoff-identical"
    assert _file_set(root) == before and "export_session_json" not in out["results"][0]
    meta = seat.meta()
    assert meta["export"]["status"] == "handoff-identical"
    assert meta["export"]["identical_to"] == "specialist/2026-10-13/2026-10-13T10-00-00Z-prev.md"
    assert meta["transcript"] == "absent" and seat.alerts == []


def test_the_transcript_field_reuses_the_stage1_result(tmp_path, monkeypatch):
    seat, root = _handoff_seat(tmp_path)
    calls = []
    real = sc._transcript_gz_of

    def flaky(pointer, chron_root, agent):
        calls.append(1)
        return real(pointer, chron_root, agent) if len(calls) == 1 else Path("/nonexistent/second-call.gz")

    monkeypatch.setattr(sc, "_transcript_gz_of", flaky)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "exported"
    assert len(calls) == 1  # asked once, in stage 1
    meta = seat.meta()
    assert meta["transcript"] == "absent" and "transcript_absent_reason" in meta  # matches what was exported
    assert [f["name"] for f in meta["export"]["files"]] == [seat.files(".md")[0].name]


def test_a_differing_handoff_after_an_identical_one_exports(tmp_path):
    seat, root = _handoff_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-old.md", b"different\n")
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", b"# Handoff\nnext: other\n")
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "exported"  # only the NEWEST is compared; it differs
    seat2, seat2_root = _handoff_seat(tmp_path / "x")
    _plant_md(seat2_root, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-old.md", HANDOFF_BYTES)  # older identical
    _plant_md(seat2_root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", b"newer, different\n")
    assert seat2.run("death", export_root=seat2_root)["results"][0]["export"] == "exported"


def test_the_newest_is_by_date_dir_then_name_not_mtime(tmp_path):
    root = _tree(tmp_path)
    older = _plant_md(root, "specialist", "2026-10-12", "2026-10-12T23-00-00Z-a.md", b"same\n")
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T01-00-00Z-a.md", b"other\n")
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T02-00-00Z-b.md", b"same\n")
    os.utime(older, (9e9 - 1, 9e9 - 1))  # the oldest name has the newest mtime: irrelevant
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") == "specialist/2026-10-13/2026-10-13T02-00-00Z-b.md"
    md2 = _mk(tmp_path / "rec2.md", b"other\n")
    assert cx.identical_to_newest(root, "specialist", md2, date="2026-10-14") is None


def test_dedupe_compares_size_first_then_sha(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-a.md", b"aaaa\n")
    hashed = []
    real = cx._sha256_fd_or_path
    monkeypatch.setattr(cx, "_sha256_fd_or_path", lambda *a, **k: (hashed.append(a[0]), real(*a, **k))[1])
    assert cx.identical_to_newest(root, "specialist", _mk(tmp_path / "b.md", b"bbbbbb\n"), date="2026-10-14") is None
    assert hashed == []  # sizes differ: nothing hashed
    assert cx.identical_to_newest(root, "specialist", _mk(tmp_path / "c.md", b"cccc\n"), date="2026-10-14") is None
    assert hashed  # same size, different bytes: hashed, and not identical


def test_a_symlink_planted_as_the_newest_md_is_not_followed(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    target = _mk(tmp_path / "elsewhere" / "secret.md", b"same\n")
    day = root / "specialist" / "2026-10-13"
    day.mkdir(parents=True)
    os.symlink(target, day / "2026-10-13T23-00-00Z-planted.md")
    _plant_md(root, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-real.md", b"same\n")
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") is None  # no dedupe against a link
    assert not [o for o in opened if "secret.md" in o]  # the link target was never opened
    # and a symlinked date dir is refused, not walked
    other = _tree(tmp_path / "o")
    real_day = tmp_path / "realday"
    _plant_md(tmp_path, "realday", ".", "2026-10-13T10-00-00Z-x.md", b"same\n")
    os.symlink(real_day, other / "specialist" / "2026-10-13")
    assert cx.identical_to_newest(other, "specialist", md, date="2026-10-14") is None


def test_an_uncommitted_newest_md_does_not_cause_a_skip(tmp_path):
    # stage 2 failed or a concurrent drop is mid-way: no sibling .session.json, so it is not a drop yet
    root = _tree(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-a.md", b"same\n", committed=False)
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") is None


def test_the_comparison_skips_an_uncommitted_newer_md_and_uses_the_older_committed_one(tmp_path):
    root = _tree(tmp_path)
    _plant_md(root, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-old.md", b"same\n")  # committed, older date
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T09-00-00Z-c.md", b"same\n")  # committed, same day
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-u.md", b"other\n", committed=False)
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") == "specialist/2026-10-13/2026-10-13T09-00-00Z-c.md"
    root2 = _tree(tmp_path / "x")
    _plant_md(root2, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-old.md", b"same\n")
    _plant_md(root2, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-u.md", b"other\n", committed=False)
    assert cx.identical_to_newest(root2, "specialist", md, date="2026-10-14") == "specialist/2026-10-12/2026-10-12T10-00-00Z-old.md"
    # and no committed drop at all: export
    root3 = _tree(tmp_path / "y")
    _plant_md(root3, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-u.md", b"same\n", committed=False)
    assert cx.identical_to_newest(root3, "specialist", md, date="2026-10-14") is None


def test_a_symlinked_sibling_session_json_is_not_a_commit_marker(tmp_path):
    root = _tree(tmp_path)
    target = _mk(tmp_path / "elsewhere" / "real.session.json", b"{}\n")
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-a.md", b"same\n", committed=False)
    os.symlink(target, root / "specialist" / "2026-10-13" / "2026-10-13T10-00-00Z-a.session.json")
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") is None


def test_the_record_itself_already_in_the_tree_is_never_deduped(tmp_path):
    # A retry after a kill between the md link and the session.json link must still finish the drop.
    root = _tree(tmp_path)
    md = _mk(tmp_path / "2026-10-14T10-00-00Z-me.md", b"same\n")
    _plant_md(root, "specialist", "2026-10-14", md.name, b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14") is None


def test_the_dedupe_scan_is_bounded(tmp_path):
    root = _tree(tmp_path)
    for day in range(1, 12):
        (root / "specialist" / f"2026-09-{day:02d}").mkdir()
    _plant_md(root, "specialist", "2026-08-01", "2026-08-01T10-00-00Z-a.md", b"same\n")
    md = _mk(tmp_path / "rec.md", b"same\n")
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14", max_dates=5) is None
    assert cx.identical_to_newest(root, "specialist", md, date="2026-10-14", max_dates=30) is not None


def test_a_planned_md_counts_as_exported_and_wins_when_newer(tmp_path):
    root = _tree(tmp_path)
    _plant_md(root, "specialist", "2026-10-12", "2026-10-12T10-00-00Z-a.md", b"tree\n")
    planned_src = _mk(tmp_path / "planned.md", b"plan\n")
    planned = cx.ExportedMd.of("2026-10-13", "2026-10-13T10-00-00Z-p.md", planned_src)
    same = _mk(tmp_path / "rec.md", b"plan\n")
    assert cx.identical_to_newest(root, "specialist", same, date="2026-10-14", planned=planned) == \
        "specialist/2026-10-13/2026-10-13T10-00-00Z-p.md"
    assert cx.identical_to_newest(root, "specialist", _mk(tmp_path / "t.md", b"tree\n"), date="2026-10-14",
                                  planned=planned) is None  # the planned one is newer than the tree's


def test_export_manifest_entries_match_the_linked_bytes(tmp_path):
    seat, _, root = _ready(tmp_path)
    seat.run("death", export_root=root)
    meta = seat.meta()
    day = root / "specialist" / meta["export"]["date"]
    names = {f["name"] for f in meta["export"]["files"]}
    assert names == {p.name for p in day.iterdir()} - {p.name for p in day.glob("*.session.json")}
    for entry in meta["export"]["files"]:
        data = (day / entry["name"]).read_bytes()
        assert entry["size"] == len(data) and entry["sha256"] == hashlib.sha256(data).hexdigest(), entry


def test_a_record_with_a_transcript_is_never_deduped(tmp_path):
    seat, _, root = _ready(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "exported"
    (day,) = [d for d in (root / "specialist").iterdir() if d.name == seat.files(".md")[0].parent.name]
    assert len(list(day.iterdir())) == 3  # gz, md, session.json


def test_unresolved_seat_records_exports_nothing_and_alerts_as_degraded(tmp_path, monkeypatch):
    seat = Seat(tmp_path, persona="codex", harness="codex")
    rollout = _codex_day_dir(seat.home) / "rollout-2026-10-14T20-00-02-2222-codex.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"turn": 1}\n')
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    repo = _seats_repo(tmp_path, {"a": {"account": _me()}, "b": {"account": _me()}})
    real = cx.resolve_seat
    monkeypatch.setattr(cx, "resolve_seat", lambda **kw: real(**{**kw, "repo_root": repo}))
    out = seat.run("death", harness="codex", export_root=_tree(tmp_path, "codex"))
    assert out["results"][0]["alert_export"] == "routed" or seat.alerts
    meta = seat.meta()
    assert meta["export"]["status"] == "export-seat-unresolved" and meta["export"]["files"] == []
    assert len(seat.alerts) == 1 and "session end archived, but memex export seat unresolved" in seat.alerts[0][1]


def test_a_refused_file_makes_the_export_partial_and_alerts(tmp_path, monkeypatch):
    seat, _, root = _ready(tmp_path)
    real_link = cx.ExportDir.link

    def picky(self, src, name=None):  # the md is refused, the gz still ships
        if Path(src).name.endswith(".md"):
            raise cx.ExportRefused("md refused for the test")
        return real_link(self, src, name)

    monkeypatch.setattr(cx.ExportDir, "link", picky)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "archived" and out["results"][0]["export"] == "partial"
    assert seat.meta()["export"]["status"] == "partial" and len(seat.alerts) == 1
    assert "session end archived, but memex export partial" in seat.alerts[0][1]
    assert "export_session_json" not in out["results"][0]  # no commit marker for a partial export


def test_a_session_json_link_failure_alerts_and_does_not_raise(tmp_path, monkeypatch):
    seat, _, root = _ready(tmp_path)
    real = cx.link_one
    monkeypatch.setattr(cx, "link_one", lambda *a, **k: cx.ExportOutcome(cx.STATUS_FAILED, "specialist", "d", str(root),
                                                                        detail="EXDEV on the marker"))
    out = seat.run("death", export_root=root)
    res = out["results"][0]
    assert res["status"] == "archived" and res["export_session_json"] == "failed"
    assert any("session end archived, but memex export of" in a[1] for a in seat.alerts)
    assert real is not cx.link_one


def test_export_failure_is_fail_open(tmp_path, monkeypatch):
    seat, _, root = _ready(tmp_path)
    monkeypatch.setattr(sc.chronicle_export, "export_files", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "archived" and seat.meta()["export"]["status"] == "export-failed"
    assert len(seat.alerts) == 1


def test_swap_paired_md_fixed_to_0640_before_link(tmp_path):
    """R1: runtime_controller.py:8782 writes the swap md 0600 on its strict path; the export fixes it in place."""
    seat, _, root = _ready(tmp_path)
    swap_ts = "2026-10-14T20:30:00Z"  # after STARTED, so the session end pairs with it
    md = chronicle.archive_required(seat.agent, "# swap handoff\n", jsonl_uuid="swapuuid",
                                    reason="planned-runtime-swap",
                                    ts=swap_ts, root=seat.root)
    assert stat.S_IMODE(md.stat().st_mode) == 0o600
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "paired" and out["results"][0]["export"] == "exported", out
    assert stat.S_IMODE(md.stat().st_mode) == 0o640
    exported = root / "specialist" / "2026-10-14" / md.name
    assert exported.stat().st_ino == md.stat().st_ino


def test_a_counter_named_archive_is_exported_at_session_end(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    src = tmp_path / "rollout.jsonl"
    src.write_text('{"a": 1}\n')
    first = chronicle.archive_native_transcript(seat.agent, src, jsonl_uuid="rot", ts="2026-10-14T21:00:00Z",
                                                root=seat.root, group=OWN_GROUP)
    src.write_text('{"a": 1}\n{"a": 2}\n')
    second = chronicle.archive_native_transcript(seat.agent, src, jsonl_uuid="rot", ts="2026-10-14T21:00:00Z",
                                                 root=seat.root, group=OWN_GROUP)
    assert Path(first["path"]).name != Path(second["path"]).name
    assert Path(second["path"]).name.endswith(".transcript.jsonl.2.gz"), second["path"]
    second.update({"source_path": str(src)})
    root = _tree(tmp_path)
    out = seat.run("thread-rotation", harness="codex", native_transcript=dict(second), export_root=root,
                   export_seat="specialist")
    assert out["results"][0]["export"] == "exported", out
    assert (root / "specialist" / "2026-10-14" / Path(second["path"]).name).exists()


def test_the_caller_archived_t6115_gz_is_exported_when_it_sits_under_root_agent(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    src = tmp_path / "rollout.jsonl"
    src.write_text('{"a": 1}\n')
    pre = chronicle.archive_native_transcript(seat.agent, src, jsonl_uuid="rot", ts="2026-10-14T21:00:00Z",
                                              root=seat.root, group=OWN_GROUP)
    pre.update({"source_path": str(src)})
    root = _tree(tmp_path)
    out = seat.run("thread-rotation", harness="codex", native_transcript=dict(pre), export_root=root,
                   export_seat="specialist")
    assert out["results"][0]["export"] == "exported", out
    assert (root / "specialist" / "2026-10-14" / Path(pre["path"]).name).exists()
    foreign = _mk(tmp_path / "foreign" / "z.transcript.jsonl.gz", gzip.compress(b"q"))
    seat2 = Seat(tmp_path / "s2", harness="codex")
    seat2.runtime()
    seat2.write_handoff()
    seat2.live_log("bye")
    seat2.run("thread-rotation", harness="codex", export_root=_tree(tmp_path / "s2"), export_seat="specialist",
              native_transcript={"path": str(foreign), "bytes": 1, "sha256": "0" * 64})
    assert [f["name"] for f in seat2.meta()["export"]["files"]] == [seat2.files(".md")[0].name]  # foreign gz: never exported


def test_a_grown_session_supersedes_its_earlier_exported_copy(tmp_path):
    seat, src, root = _ready(tmp_path)
    seat.run("death", export_root=root)
    (first,) = seat.files(".transcript.jsonl.gz")
    src.write_text('{"turn": 1}\n{"turn": 2}\n')
    seat.runtime(started="2026-10-14T22:00:00.123456Z")
    seat.write_handoff()
    seat.live_log("again", stamp="2026-10-14 22:00:01")
    out = seat.run("death", now=NOW + timedelta(hours=2), export_root=root)
    assert out["results"][0]["status"] == "archived", out
    sup = [m for m in (json.loads(p.read_text()) for p in seat.files(".session.json")) if m["supersedes"]]
    assert len(sup) == 1
    (entry,) = sup[0]["supersedes"]
    assert entry["file"] == f"specialist/{first.parent.name}/{first.name}"
    assert entry["size"] == first.stat().st_size and entry["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.exists() and (root / entry["file"]).exists()  # never deleted


def test_r2_euid_zero_with_a_nonexistent_group_falls_back_ungrouped_and_keeps_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    seat, _, root = _ready(tmp_path)
    out = seat.run("death", archive_group="no-such-group-t6118-r2", export_root=tmp_path / "absent")
    assert out["results"][0]["status"] == "archived", out
    assert "does not exist" in seat.meta()["group_error"]
    assert len(seat.alerts) == 1


def test_r3_a_source_that_grows_between_the_resolver_and_the_archive_gets_an_honest_pointer(tmp_path, monkeypatch):
    seat, src, root = _ready(tmp_path)
    real = chronicle.archive_native_transcript

    def grow_then_archive(*a, **k):
        with src.open("a") as fh:
            fh.write('{"turn": 2}\n')
        return real(*a, **k)

    monkeypatch.setattr(chronicle, "archive_native_transcript", grow_then_archive)
    seat.run("death", export_root=root)
    pointer = seat.meta()["native_transcript"]
    grown = src.read_bytes()
    assert pointer["source_changed_during_archive"] is True
    assert pointer["source_bytes"] == len(grown) and pointer["source_sha256"] == hashlib.sha256(grown).hexdigest()
    assert gzip.decompress(Path(pointer["path"]).read_bytes()) == grown


def test_r3_an_unchanged_source_records_no_change_flag(tmp_path):
    seat, src, root = _ready(tmp_path)
    seat.run("death", export_root=root)
    assert "source_changed_during_archive" not in seat.meta()["native_transcript"]


def test_archive_native_transcript_returns_the_hash_and_count_of_the_bytes_it_compressed(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_bytes(b"abc\n" * 1000)
    out = chronicle.archive_native_transcript("specialist", src, jsonl_uuid="u", ts="2026-11-04T00:00:00Z",
                                              root=tmp_path / "c", group=OWN_GROUP)
    assert out["source_bytes"] == 4000 and out["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert set(out) >= {"path", "bytes", "sha256", "source_bytes"}  # the T-6115 keys are unchanged


def test_r4_failure_wording_is_unchanged_and_degraded_alerts_say_archived_but(tmp_path):
    seat = Seat(tmp_path)
    sc.alert_failure(seat.persona, "disk full", root=seat.root, agent=seat.agent, now=NOW,
                     route_fn=lambda *a: seat.alerts.append(a))
    assert seat.alerts[0][1] == (
        "session-chronicle specialist: a session end was NOT archived to the Chronicle (disk full). The restart "
        "was not blocked. Check ~/.local/state/pantheon/chronicle/specialist/.claims and re-run "
        "`python3 ~/pantheon-config/tools/session_chronicle.py coverage --persona specialist` as that seat.")
    sc.alert_failure(seat.persona, "native transcript not archived: x", root=seat.root, agent=seat.agent, now=NOW,
                     route_fn=lambda *a: seat.alerts.append(a), kind=sc.ALERT_KIND_DEGRADED)
    degraded = seat.alerts[1][1]
    assert degraded.startswith("session-chronicle specialist: session end archived, but native transcript not archived: x.")
    assert "NOT archived" not in degraded


def test_r4_a_degraded_alert_does_not_mute_a_real_failure(tmp_path):
    seat = Seat(tmp_path)
    route = lambda *a: seat.alerts.append(a)  # noqa: E731
    kw = dict(root=seat.root, agent=seat.agent, now=NOW, route_fn=route)
    assert sc.alert_failure(seat.persona, "a", kind=sc.ALERT_KIND_DEGRADED, **kw) == "routed"
    assert sc.alert_failure(seat.persona, "b", kind=sc.ALERT_KIND_DEGRADED, **kw) == "cooldown"
    assert sc.alert_failure(seat.persona, "c", **kw) == "routed"


def test_r4_group_transcript_and_pane_alerts_in_session_end_are_degraded(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise chronicle.ChronicleDurabilityError("nope")
    monkeypatch.setattr(chronicle, "archive_native_transcript", boom)
    seat, _, root = _ready(tmp_path)
    seat.run("death", export_root=root)
    assert seat.alerts and all("session end archived, but" in a[1] for a in seat.alerts)


def test_r5_a_raise_while_resolving_the_group_fails_the_claim_instead_of_leaving_it_claimed(tmp_path, monkeypatch):
    seat, _, root = _ready(tmp_path)
    monkeypatch.setattr(sc, "_group_for_export", lambda req: (_ for _ in ()).throw(RuntimeError("grp exploded")))
    out = seat.run("death", export_root=root)
    assert out["results"][0]["status"] == "failed", out
    claims = [json.loads(p.read_text()) for p in (seat.root / seat.agent / ".claims").glob("*.json")]
    assert [c["state"] for c in claims] == ["failed"]
    assert "NOT archived" in seat.alerts[0][1]


def test_cli_takes_session_id_and_export_root():
    ns = sc.build_parser().parse_args(["session-end", "--cause", "death", "--session-id", "abc-1",
                                       "--export-root", "/tmp/x"])
    assert ns.session_id == "abc-1" and ns.export_root == "/tmp/x"
    req = sc.SessionEndRequest(persona="x", cause="death")
    assert req.export_root is None and req.export_seat is None and req.session_id is None


# ------------------------------------------------------------------ T-6118 PR-4 (a): the summary in the export
def _summary_export_seat(tmp_path, **kw):
    seat = Seat(tmp_path, persona="specialist", harness="claude")
    src = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "1111-claude.jsonl"
    src.parent.mkdir(parents=True)
    src.write_bytes((FIXTURES / "summary_claude.jsonl").read_bytes())
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    return seat, _tree(tmp_path)


def test_summary_md_is_exportable_and_pane_log_still_refused():
    assert cx.exportable("2026-10-14T230000Z-u.summary.md") is True
    assert ".summary.md" in cx.ALLOWED_SUFFIXES
    assert cx.exportable("2026-10-14T230000Z-u.pane.log.gz") is False


def test_a_summary_is_linked_after_the_md_and_before_the_session_json_and_listed_in_meta(tmp_path, monkeypatch):
    seat, root = _summary_export_seat(tmp_path)
    order = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda s, d, *a, **k: (order.append(Path(d).name), real(s, d, *a, **k))[1]
                        if k.get("dst_dir_fd") is not None else real(s, d, *a, **k))
    out = seat.run("death", export_root=root, handoff_fresh=False)
    assert out["results"][0]["export"] == "exported" and out["results"][0]["export_session_json"] == "linked", out
    (summary,) = seat.files(".summary.md")
    (meta_path,) = seat.files(".session.json")
    assert order[-1] == meta_path.name and summary.name in order[:-1]
    day = root / "specialist" / summary.parent.name
    assert (day / summary.name).stat().st_ino == summary.stat().st_ino
    meta = seat.meta()
    names = [f["name"] for f in meta["export"]["files"]]
    assert summary.name in names and meta["summary"]["path"] == str(summary)
    assert seat.alerts == []


def test_a_fresh_handoff_export_is_unchanged_from_pr2(tmp_path):
    seat, root = _summary_export_seat(tmp_path)
    seat.run("death", export_root=root, handoff_fresh=True)
    (gz,) = seat.files(".transcript.jsonl.gz")
    (md,) = seat.files(".md")
    assert [f["name"] for f in seat.meta()["export"]["files"]] == [gz.name, md.name]
    assert seat.meta()["summary"] is None


def test_a_newer_summary_md_does_not_change_the_handoff_dedupe_answer(tmp_path):
    seat, root = _handoff_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    # a committed, NEWER, different .summary.md in the tree must not become "the newest .md"
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T11-00-00Z-later.summary.md", b"synthesized\n")
    before = _file_set(root)
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "handoff-identical", out
    assert _file_set(root) == before
    assert seat.meta()["export"]["identical_to"] == "specialist/2026-10-13/2026-10-13T10-00-00Z-prev.md"
    assert seat.files(".summary.md") == []  # a transcript-less record never has a summary


def test_a_summary_record_whose_gz_failed_exports_the_summary_without_an_identical_md(tmp_path, monkeypatch):
    seat, root = _summary_export_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    monkeypatch.setattr(chronicle, "archive_native_transcript",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gz boom")))
    out = seat.run("death", export_root=root, handoff_fresh=False)
    assert out["results"][0]["export"] == "exported", out
    (summary,) = seat.files(".summary.md")
    names = [f["name"] for f in seat.meta()["export"]["files"]]
    assert names == [summary.name] and seat.meta()["export"]["identical_to"].endswith("-prev.md")


# ------------------------------------------------------------------ T-6118 PR-4 fix 1: a stale HANDOFF dedupes even with a summary
def _day_names(root: Path, seat: str = "specialist") -> list:
    return sorted(p.name for p in (root / seat).glob("*/*") if p.is_file())


def test_a_stale_identical_handoff_with_a_transcript_exports_all_but_the_md(tmp_path):
    seat, root = _summary_export_seat(tmp_path)
    twin = "specialist/2026-10-13/2026-10-13T10-00-00Z-prev.md"
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    out = seat.run("death", export_root=root, handoff_fresh=False)
    assert out["results"][0]["export"] == "exported" and out["results"][0]["export_session_json"] == "linked", out
    (md,) = [p for p in seat.files(".md") if not p.name.endswith(".summary.md")]
    day = root / "specialist" / md.parent.name
    assert sorted(p.name for p in day.iterdir()) == sorted([
        seat.files(".transcript.jsonl.gz")[0].name, seat.files(".summary.md")[0].name,
        seat.files(".session.json")[0].name])
    assert not (day / md.name).exists()
    meta = seat.meta()["export"]
    assert meta["status"] == "exported" and meta["identical_to"] == twin
    assert md.name not in [f["name"] for f in meta["files"]] and len(meta["files"]) == 2
    assert seat.alerts == []


def test_a_stale_different_handoff_with_a_transcript_exports_all_four(tmp_path):
    seat, root = _summary_export_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", b"# an older, different handoff\n")
    out = seat.run("death", export_root=root, handoff_fresh=False)
    assert out["results"][0]["export"] == "exported", out
    day = root / "specialist" / seat.files(".session.json")[0].parent.name
    assert sorted(p.name for p in day.iterdir()) == sorted(
        p.name for p in seat.files(".md") + seat.files(".session.json") + seat.files(".transcript.jsonl.gz"))
    assert len(list(day.iterdir())) == 4
    meta = seat.meta()["export"]
    assert "identical_to" not in meta and len(meta["files"]) == 3


def test_a_later_session_with_only_a_summary_and_marker_still_dedupes_against_the_committed_md(tmp_path):
    seat, root = _handoff_seat(tmp_path)
    _plant_md(root, "specialist", "2026-10-13", "2026-10-13T10-00-00Z-prev.md", HANDOFF_BYTES)
    # a LATER date dir: a committed session (session.json) + summary, but no .md (the md was deduped away)
    _mk(root / "specialist" / "2026-10-13" / "2026-10-13T12-00-00Z-next.session.json", b"{}\n")
    _mk(root / "specialist" / "2026-10-13" / "2026-10-13T12-00-00Z-next.summary.md", b"synth\n")
    _mk(root / "specialist" / "2026-10-13" / "2026-10-13T12-00-00Z-next.transcript.jsonl.gz", b"gz")
    out = seat.run("death", export_root=root)
    assert out["results"][0]["export"] == "handoff-identical", out
    assert seat.meta()["export"]["identical_to"] == "specialist/2026-10-13/2026-10-13T10-00-00Z-prev.md"
    assert cx.identical_to_newest(root, "specialist", seat.handoff, date="2026-10-14") == \
        "specialist/2026-10-13/2026-10-13T10-00-00Z-prev.md"
