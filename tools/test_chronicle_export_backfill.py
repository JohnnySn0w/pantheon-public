"""Tests for `session_chronicle.py export-backfill` (T-6118 PR-2): the FIRST DROP.

tmp roots only. The sources are the committed fixtures in tools/fixtures/chronicle/ copied into tmp
trees; the export tree and the Chronicle are tmp_path stand-ins; the group is the test process's own gid.
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from tools import chronicle, chronicle_export as cx
from tools import session_chronicle as sc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chronicle"
GID = os.getegid()
ME = __import__("pwd").getpwuid(os.getuid()).pw_name
SINCE = "2026-11-01"


class World:
    def __init__(self, tmp: Path, agent: str = "artisan") -> None:
        self.tmp, self.agent = tmp, agent
        self.root = tmp / "chronicle"
        self.export = tmp / "export"
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.export / agent).mkdir(parents=True, exist_ok=True)
        os.chown(self.export / agent, -1, GID)
        self.n = 0

    def source(self, name: str = "claude_top_level.jsonl", fixture: str = "claude_top_level.jsonl") -> Path:
        path = self.tmp / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, path)
        return path

    def record(self, ts: str, source: Path | None, *, harness: str = "claude", md_mode: int = 0o600,
               with_md: bool = True, pointer: dict | None = None, uuid: str | None = None,
               md_text: str = "# handoff\n") -> str:
        self.n += 1
        uuid = uuid or f"uuid-{self.n}"
        stem = f"{chronicle._fs_safe_ts(ts)}-{uuid}"
        day = self.root / self.agent / ts[:10]
        day.mkdir(parents=True, exist_ok=True)
        with (self.root / self.agent / "index.md").open("a") as fh:
            fh.write(f"- {ts} | session-end/death | {uuid} | hello\n")
        if with_md:
            md = day / f"{stem}.md"
            md.write_text(md_text)
            os.chmod(md, md_mode)
        meta = json.loads((FIXTURES / "session_record.session.json").read_text())
        meta.update({"agent": self.agent, "persona": self.agent, "harness": harness})
        if pointer is None and source is not None:
            data = source.read_bytes()
            pointer = {"path": str(source), "session_id": "sess-1", "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest(), "candidates": 1}
        meta["native_transcript"] = pointer
        meta_path = day / f"{stem}.session.json"
        meta_path.write_text(json.dumps(meta))
        os.chmod(meta_path, 0o600)
        return stem

    def run(self, **kw):
        kw.setdefault("export_root", self.export)
        kw.setdefault("account_home", self.home)
        return sc.export_backfill(self.agent, SINCE, chronicle_root=self.root, **kw)

    def tree(self, *roots: Path) -> dict:
        out = {}
        for root in roots or (self.root, self.export):
            for dirpath, dirs, files in os.walk(root):
                for name in dirs + files:
                    p = Path(dirpath) / name
                    st = p.lstat()
                    out[str(p)] = (st.st_mtime_ns, st.st_size, st.st_mode)
        return out


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def kinds(result) -> list[str]:
    return [line[0] for line in result["lines"]]


# ------------------------------------------------------------------ dry-run
def test_dry_run_writes_nothing_creates_no_date_dir_and_prints_the_exact_format(w):
    src = w.source()
    stem = w.record("2026-11-03T17:16:01Z", src)
    before = w.tree()
    result = w.run()
    assert w.tree() == before and not (w.export / "artisan" / "2026-11-03").exists()
    assert result["exit"] == 0 and result["mode"] == "DRY-RUN"
    out = sc._format_backfill(result)
    size = src.stat().st_size
    assert out[0] == (f"MODE\tDRY-RUN\tpersona=artisan\tseat=artisan\tsince={SINCE}\texport_root={w.export}"
                      "\texport_root_state=provisioned")
    assert out[1] == f"would-archive\t{stem}\t{src}\t-> {stem}.transcript.jsonl.gz\t{size}B"
    assert out[2] == f"would-fix-mode\t{stem}.md\t0600->0640"
    exports = [line for line in out if line.startswith("would-export")]
    assert exports[0].startswith(f"would-export\tartisan/2026-11-03/{stem}.transcript.jsonl.gz\t~")
    assert exports[0].endswith("B")
    assert exports[1] == f"would-export\tartisan/2026-11-03/{stem}.md\t{len('# handoff') + 1}B"
    assert exports[2].startswith(f"would-export\tartisan/2026-11-03/{stem}.session.json\t")
    assert out[-1] == "SUMMARY\tmode=DRY-RUN\trecords=1\tarchive=1\tfix-mode=1\texport=3\tsupersede=0\tskip=0\tfail=0\tunprovisioned=0\thandoff-only=0\thandoff-identical=0"
    assert all(line.count("\t") >= 2 for line in out)


def test_dry_run_with_an_absent_export_root_previews_everything_and_writes_nothing(w, tmp_path):
    src = w.source()
    stem = w.record("2026-11-03T17:16:01Z", src)
    absent = tmp_path / "absent"
    before = w.tree(w.root, tmp_path / "src")
    result = w.run(export_root=absent)
    assert w.tree(w.root, tmp_path / "src") == before and not absent.exists()
    assert result["exit"] == 0 and result["export_root_state"] == "unprovisioned"
    out = sc._format_backfill(result)
    size = src.stat().st_size
    assert out[1] == f"would-archive\t{stem}\t{src}\t-> {stem}.transcript.jsonl.gz\t{size}B"
    assert out[2] == f"would-fix-mode\t{stem}.md\t0600->0640"
    exports = [line for line in out if line.startswith("would-export")]
    assert [e.split("\t")[1] for e in exports] == [
        f"artisan/2026-11-03/{stem}.transcript.jsonl.gz", f"artisan/2026-11-03/{stem}.md",
        f"artisan/2026-11-03/{stem}.session.json"]
    notes = [line.split("\t") for line in out if line.startswith("note")]
    assert len(notes) == 1 and notes[0][:3] == ["note", stem, "export-unprovisioned"] and notes[0][3]
    assert not [line for line in out if line.startswith("skip")]
    assert out[-1] == ("SUMMARY\tmode=DRY-RUN\trecords=1\tarchive=1\tfix-mode=1\texport=3\tsupersede=0"
                       "\tskip=0\tfail=0\tunprovisioned=1\thandoff-only=0\thandoff-identical=0")


def test_dry_run_with_an_absent_seat_dir_is_unprovisioned_too(w, tmp_path):
    w.record("2026-11-03T17:16:01Z", w.source())
    result = w.run(export_root=tmp_path / "emptyroot")
    assert result["export_root_state"] == "unprovisioned" and not (tmp_path / "emptyroot").exists()
    root2 = tmp_path / "root2"
    root2.mkdir()
    result = w.run(export_root=root2)
    assert result["export_root_state"] == "unprovisioned" and list(root2.iterdir()) == []
    assert "would-export" in kinds(result)


def test_dry_run_unprovisioned_still_applies_the_source_checks(w, tmp_path):
    src = w.source()
    w.record("2026-11-03T17:16:01Z", src)
    src.write_text("changed\n")
    result = w.run(export_root=tmp_path / "absent")
    assert kinds(result) == ["skip"] and result["lines"][0][2] == "source-changed"
    assert result["counts"]["unprovisioned"] == 0


def test_dry_run_unprovisioned_computes_supersedes_against_the_same_run_plan(w, tmp_path):
    src = w.source()
    prefix = src.read_bytes()
    first = w.record("2026-11-02T10:00:00Z", src)
    day = w.root / "artisan" / "2026-11-02"
    old_gz = day / f"{first}.transcript.jsonl.gz"
    old_gz.write_bytes(gzip.compress(prefix))
    os.chmod(old_gz, 0o640)
    meta_path = day / f"{first}.session.json"
    meta = json.loads(meta_path.read_text())
    meta["native_transcript"] = {"path": str(old_gz), "source_path": str(src), "bytes": old_gz.stat().st_size,
                                 "sha256": hashlib.sha256(old_gz.read_bytes()).hexdigest()}
    meta_path.write_text(json.dumps(meta))
    with src.open("a") as fh:
        fh.write('{"turn": "later"}\n')
    second = w.record("2026-11-03T10:00:00Z", src)
    result = w.run(export_root=tmp_path / "absent")
    sup = [l for l in result["lines"] if l[0] == "would-supersede"]
    assert sup == [["would-supersede", f"artisan/2026-11-02/{old_gz.name}", f"{old_gz.stat().st_size}B"]]
    assert result["counts"]["supersede"] == 1
    assert [l[1] for l in result["lines"] if l[0] == "would-archive"] == [second]
    assert not (tmp_path / "absent").exists()


def test_execute_with_an_absent_export_root_still_skips_and_writes_nothing(w, tmp_path):
    w.record("2026-11-03T17:16:01Z", w.source())
    absent = tmp_path / "absent"
    before = w.tree(w.root, tmp_path / "src")
    result = w.run(export_root=absent, execute=True)
    assert kinds(result) == ["skip"] and result["lines"][0][2] == "export-unprovisioned"
    assert w.tree(w.root, tmp_path / "src") == before and not absent.exists()
    assert result["counts"]["skip"] == 1 and result["counts"]["archive"] == 0 and result["exit"] == 0
    assert not list((w.root / "artisan").glob("*/*.gz"))


def test_header_and_summary_carry_the_export_root_state(w, tmp_path):
    w.record("2026-11-03T17:16:01Z", w.source())
    prov = sc._format_backfill(w.run())
    assert prov[0].endswith(f"\texport_root={w.export}\texport_root_state=provisioned")
    assert prov[-1].endswith("\tfail=0\tunprovisioned=0\thandoff-only=0\thandoff-identical=0")
    absent = tmp_path / "absent"
    unprov = sc._format_backfill(w.run(export_root=absent))
    assert unprov[0] == (f"MODE\tDRY-RUN\tpersona=artisan\tseat=artisan\tsince={SINCE}\texport_root={absent}"
                         "\texport_root_state=unprovisioned")
    assert unprov[-1].endswith("\tunprovisioned=1\thandoff-only=0\thandoff-identical=0")


def test_a_counter_named_archive_is_recognised_as_already_archived(w):
    src = w.source()
    stem = w.record("2026-11-03T17:16:01Z", src)
    day = w.root / "artisan" / "2026-11-03"
    gz = day / f"{stem}.transcript.jsonl.2.gz"
    gz.write_bytes(gzip.compress(src.read_bytes()))
    os.chmod(gz, 0o640)
    meta_path = day / f"{stem}.session.json"
    meta = json.loads(meta_path.read_text())
    meta["native_transcript"] = {"path": str(gz), "source_path": str(src), "bytes": gz.stat().st_size,
                                "sha256": hashlib.sha256(gz.read_bytes()).hexdigest()}
    meta_path.write_text(json.dumps(meta))
    result = w.run(records=(gz.name,))
    assert result["exit"] == 0 and "would-archive" not in kinds(result)
    assert f"artisan/2026-11-03/{gz.name}" in [l[1] for l in result["lines"] if l[0] == "would-export"]
    done = w.run(execute=True)
    assert done["exit"] == 0 and (w.export / "artisan" / "2026-11-03" / gz.name).exists()


def test_records_are_processed_oldest_first_and_since_filters(w):
    s1, s2 = w.source("a.jsonl"), w.source("b.jsonl")
    late = w.record("2026-11-04T01:00:00Z", s2)
    early = w.record("2026-11-02T01:00:00Z", s1)
    w.record("2026-10-20T01:00:00Z", s1)  # before --since
    stems = [line[1] for line in w.run()["lines"] if line[0] == "would-archive"]
    assert stems == [early, late]


# ------------------------------------------------------------------ skip reasons
def test_every_skip_reason(w, monkeypatch):
    src = w.source()
    no_md = w.record("2026-11-02T01:00:00Z", src, with_md=False)
    bare = w.record("2026-11-02T02:00:00Z", None, with_md=False)
    no_meta = w.record("2026-11-02T03:00:00Z", None)
    (w.root / "artisan" / "2026-11-02" / f"{no_meta}.session.json").unlink()
    gone = w.record("2026-11-02T04:00:00Z", w.tmp / "src" / "missing.jsonl",
                    pointer={"path": str(w.tmp / "src" / "missing.jsonl"), "sha256": "ab" * 32, "bytes": 1})
    changed_src = w.source("changed.jsonl")
    changed = w.record("2026-11-02T05:00:00Z", changed_src)
    changed_src.write_text("grown after the record\n")
    reasons = {line[1]: line[2] for line in w.run()["lines"] if line[0] == "skip"}
    assert reasons[no_md] == "no-md"
    assert reasons[bare] == "no-md"  # the old no-native-transcript skip is gone
    assert reasons[no_meta] == "no-session-json"
    assert "no-native-transcript" not in sc.SKIP_REASONS and "harness-not-archived" not in sc.SKIP_REASONS
    assert reasons[gone] == "source-missing"
    assert reasons[changed] == "source-changed"


def test_stem_unparseable_not_owner_and_seat_unresolved(w, tmp_path, monkeypatch):
    src = w.source()
    w.record("not-a-timestamp", src)
    assert [l[2] for l in w.run()["lines"] if l[0] == "skip"] == ["stem-unparseable"]
    w2 = World(tmp_path / "two")
    w2.record("2026-11-03T01:00:00Z", w2.source())
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    assert [l[2] for l in w2.run()["lines"] if l[0] == "skip"] == ["not-owner"]
    monkeypatch.undo()
    w3 = World(tmp_path / "three")
    w3.record("2026-11-03T01:00:00Z", w3.source("r.jsonl", "codex_rollout_top_level.jsonl"), harness="codex")
    repo = tmp_path / "repo"
    (repo / "harness" / "codex").mkdir(parents=True)
    (repo / "harness" / "codex" / "appserver_seats.json").write_text(
        json.dumps({"seats": {"a": {"account": ME}, "b": {"account": ME}}}))
    skips = [l for l in w3.run(repo_root=repo)["lines"] if l[0] == "skip"]
    assert skips[0][2] == "seat-unresolved" and "found ['a', 'b']" in skips[0][3]


def test_an_artisan_codex_record_exports_under_the_persona_when_no_seat_matches(tmp_path):
    w = World(tmp_path)
    stem = w.record("2026-11-01T15:00:00Z", w.source("r.jsonl", "codex_rollout_top_level.jsonl"), harness="codex")
    repo = tmp_path / "repo"
    (repo / "harness" / "codex").mkdir(parents=True)
    (repo / "harness" / "codex" / "appserver_seats.json").write_text(
        json.dumps({"seats": {"consultant": {"account": f"not-{ME}"}}}))
    result = w.run(repo_root=repo)
    assert result["seat"] == "artisan"
    assert any(l[0] == "would-export" and l[1].startswith(f"artisan/2026-11-01/{stem}") for l in result["lines"])


def test_a_codex_account_with_one_seat_exports_under_that_seat(tmp_path):
    w = World(tmp_path)
    (w.export / "consultant").mkdir()
    os.chown(w.export / "consultant", -1, GID)
    w.record("2026-11-01T15:00:00Z", w.source("r.jsonl", "codex_rollout_top_level.jsonl"), harness="codex")
    repo = tmp_path / "repo"
    (repo / "harness" / "codex").mkdir(parents=True)
    (repo / "harness" / "codex" / "appserver_seats.json").write_text(
        json.dumps({"seats": {"consultant": {"account": ME}}}))
    result = w.run(repo_root=repo, execute=True)
    assert result["seat"] == "consultant" and result["exit"] == 0, result
    assert list((w.export / "consultant" / "2026-11-01").glob("*.transcript.jsonl.gz"))


# ------------------------------------------------------------------ execute
def test_execute_archives_fixes_mode_links_rewrites_and_links_session_json_last(w, monkeypatch):
    src = w.source()
    raw = src.read_bytes()
    stem = w.record("2026-11-03T17:16:01Z", src)
    order = []
    real_link = os.link
    monkeypatch.setattr(os, "link", lambda s, d, *a, **k: (order.append(Path(d).name), real_link(s, d, *a, **k))[1]
                        if k.get("dst_dir_fd") is not None else real_link(s, d, *a, **k))
    seen = {}
    real_archive = chronicle.archive_native_transcript
    monkeypatch.setattr(chronicle, "archive_native_transcript",
                        lambda *a, **k: (seen.update(k), real_archive(*a, **k))[1])
    result = w.run(execute=True)
    assert result["exit"] == 0 and result["counts"]["fail"] == 0, result
    day = w.root / "artisan" / "2026-11-03"
    gz = day / f"{stem}.transcript.jsonl.gz"
    assert gz.is_file() and seen["compresslevel"] == 9 and seen["jsonl_uuid"] == "uuid-1"
    assert gzip.decompress(gz.read_bytes()) == raw and src.read_bytes() == raw  # source kept
    md, meta_path = day / f"{stem}.md", day / f"{stem}.session.json"
    for f in (gz, md, meta_path):
        assert stat.S_IMODE(f.stat().st_mode) == 0o640 and f.stat().st_gid == GID
        assert (w.export / "artisan" / "2026-11-03" / f.name).stat().st_ino == f.stat().st_ino
    assert order == [gz.name, md.name, meta_path.name]
    meta = json.loads(meta_path.read_text())
    pointer = meta["native_transcript"]
    assert pointer["path"] == str(gz) and pointer["backfilled"] is True
    assert pointer["sha256"] == hashlib.sha256(gz.read_bytes()).hexdigest() and pointer["bytes"] == gz.stat().st_size
    assert pointer["source_path"] == str(src) and pointer["source_bytes"] == len(raw)
    assert pointer["source_sha256"] == hashlib.sha256(raw).hexdigest() == pointer["pre_archive_sha256"]
    assert "source_changed_during_archive" not in pointer
    assert meta["export"]["status"] == "exported" and meta["export"]["seat"] == "artisan"
    assert meta["supersedes"] == [] and meta["harness"] == "claude"
    out = sc._format_backfill(result)
    assert any(l.startswith(f"archive\t{stem}\t") for l in out) and any(l.startswith("fix-mode\t") for l in out)
    assert any(l == f"export\tartisan/2026-11-03/{stem}.md\t{md.stat().st_size}B" for l in out)
    assert out[-1].startswith("SUMMARY\tmode=EXECUTE\trecords=1\tarchive=1\tfix-mode=1\texport=3")


def test_rerun_is_already_exported_and_changes_nothing(w):
    w.record("2026-11-03T17:16:01Z", w.source())
    w.run(execute=True)
    before = w.tree()
    again = w.run(execute=True)
    assert [l[2] for l in again["lines"] if l[0] == "skip"] == ["already-exported"]
    assert again["exit"] == 0 and w.tree() == before


def test_a_record_archived_but_never_exported_is_relinked_without_a_second_archive(w):
    stem = w.record("2026-11-03T17:16:01Z", w.source())
    w.run(execute=True)
    for f in (w.export / "artisan" / "2026-11-03").iterdir():
        f.unlink()  # the export links vanished (or the first run was unprovisioned)
    again = w.run(execute=True)
    assert "archive" not in kinds(again) and kinds(again).count("export") == 3 and again["exit"] == 0, again
    assert len(list((w.root / "artisan" / "2026-11-03").glob("*.transcript.jsonl*"))) == 1
    assert (w.export / "artisan" / "2026-11-03" / f"{stem}.session.json").exists()


def test_a_partial_earlier_run_reuses_its_gz_instead_of_minting_a_dot_2(w):
    src = w.source()
    stem = w.record("2026-11-03T17:16:01Z", src)
    day = w.root / "artisan" / "2026-11-03"
    gz = day / f"{stem}.transcript.jsonl.gz"
    gz.write_bytes(gzip.compress(src.read_bytes()))
    os.chmod(gz, 0o640)
    result = w.run(execute=True)
    assert result["exit"] == 0 and not list(day.glob("*.2.gz")), result


def test_a_link_failure_is_a_fail_line_exit_1_and_the_other_records_continue(w, monkeypatch):
    s1, s2 = w.source("a.jsonl"), w.source("b.jsonl")
    bad = w.record("2026-11-02T01:00:00Z", s1)
    good = w.record("2026-11-03T01:00:00Z", s2)
    real = os.link

    def flaky(src, dst, *a, **k):
        if bad in str(src) and k.get("dst_dir_fd") is not None:
            raise OSError(errno.EXDEV, "cross-device")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", flaky)
    result = w.run(execute=True)
    assert result["exit"] == 1 and result["counts"]["fail"] == 1
    fails = [l for l in result["lines"] if l[0] == "fail"]
    assert fails[0][1] == bad and "EXDEV" in fails[0][2]
    assert (w.export / "artisan" / "2026-11-03" / f"{good}.session.json").exists()
    assert not (w.export / "artisan" / "2026-11-02" / f"{bad}.session.json").exists()  # marker never linked


def test_backfill_r3_a_source_that_grows_while_archiving_gets_an_honest_pointer(w, monkeypatch):
    src = w.source()
    stem = w.record("2026-11-03T17:16:01Z", src)
    real = chronicle.archive_native_transcript

    def grow(*a, **k):
        with src.open("a") as fh:
            fh.write('{"late": true}\n')
        return real(*a, **k)

    monkeypatch.setattr(chronicle, "archive_native_transcript", grow)
    w.run(execute=True)
    pointer = json.loads((w.root / "artisan" / "2026-11-03" / f"{stem}.session.json").read_text())["native_transcript"]
    grown = src.read_bytes()
    assert pointer["source_changed_during_archive"] is True
    assert pointer["source_sha256"] == hashlib.sha256(grown).hexdigest() and pointer["source_bytes"] == len(grown)
    assert pointer["pre_archive_sha256"] != pointer["source_sha256"]


# ------------------------------------------------------------------ supersede
def test_an_earlier_exported_prefix_copy_is_superseded_and_never_deleted(w):
    src = w.source()
    first = w.record("2026-11-02T10:00:00Z", src)
    w.run(execute=True)
    old_gz = w.root / "artisan" / "2026-11-02" / f"{first}.transcript.jsonl.gz"
    with src.open("a") as fh:
        fh.write('{"turn": "later"}\n')
    second = w.record("2026-11-03T10:00:00Z", src)
    dry = w.run(records=(second,))
    sup = [l for l in dry["lines"] if l[0] == "would-supersede"]
    assert sup == [["would-supersede", f"artisan/2026-11-02/{old_gz.name}", f"{old_gz.stat().st_size}B"]]
    done = w.run(records=(second,), execute=True)
    assert done["exit"] == 0 and done["counts"]["supersede"] == 1, done
    meta = json.loads((w.root / "artisan" / "2026-11-03" / f"{second}.session.json").read_text())
    assert meta["supersedes"] == [{"file": f"artisan/2026-11-02/{old_gz.name}", "size": old_gz.stat().st_size,
                                   "sha256": hashlib.sha256(old_gz.read_bytes()).hexdigest()}]
    assert old_gz.exists() and (w.export / "artisan" / "2026-11-02" / old_gz.name).exists()


# ------------------------------------------------------------------ --record and the CLI
def test_record_selection_by_stem_and_basename_and_an_unmatched_record_exits_2(w):
    s1, s2 = w.source("a.jsonl"), w.source("b.jsonl")
    one = w.record("2026-11-02T01:00:00Z", s1)
    w.record("2026-11-03T01:00:00Z", s2)
    by_stem = w.run(records=(one,))
    assert by_stem["counts"]["records"] == 1 and by_stem["exit"] == 0
    assert w.run(records=(f"{one}.md",))["counts"]["records"] == 1
    assert w.run(records=(f"{one}.transcript.jsonl.gz",))["counts"]["records"] == 1
    missing = w.run(records=(one, "nope-1234"))
    assert missing["exit"] == 2 and missing["unmatched"] == ["nope-1234"]


def test_cli_prints_the_tsv_and_the_exit_codes(w, monkeypatch, capsys):
    stem = w.record("2026-11-03T17:16:01Z", w.source())
    monkeypatch.setenv("PANTHEON_CHRONICLE_ROOT", str(w.root))
    argv = ["export-backfill", "--persona", "artisan", "--since", SINCE, "--export-root", str(w.export),
            "--account-home", str(w.home)]
    assert sc.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("MODE\tDRY-RUN\tpersona=artisan\tseat=artisan") and out[-1].startswith("SUMMARY\t")
    assert any(stem in line for line in out)
    assert sc.main(argv + ["--record", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
    assert sc.main(argv + ["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "DRY-RUN" and doc["counts"]["records"] == 1
    assert sc.main(["export-backfill", "--persona", "Bad Name", "--since", SINCE]) == 2
    assert sc.main(["export-backfill", "--persona", "artisan", "--since", "11/01"]) == 2
    assert sc.main(argv + ["--execute"]) == 0


def test_backfill_reads_the_chronicle_root_resolved_at_call_time(w, monkeypatch):
    w.record("2026-11-03T17:16:01Z", w.source())
    monkeypatch.setenv("PANTHEON_CHRONICLE_ROOT", str(w.tmp / "other-chronicle"))
    result = sc.export_backfill("artisan", SINCE, export_root=w.export, account_home=w.home)
    assert result["counts"]["records"] == 0  # reads the resolved root, which is empty here


# ------------------------------------------------------------------ HANDOFF-only records (archivist decision)
def lines_of(result, kind):
    return [l for l in result["lines"] if l[0] == kind]


def tree_files(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_handoff_only_previews_two_exports_and_dedupes_within_the_run(w):
    first = w.record("2026-11-02T01:00:00Z", None, md_text="same\n", md_mode=0o600)
    twin = w.record("2026-11-02T02:00:00Z", None, md_text="same\n")
    third = w.record("2026-11-02T03:00:00Z", None, md_text="different\n", harness="hermes")
    before = (w.tree(), tree_files(w.export))
    result = w.run()
    assert (w.tree(), tree_files(w.export)) == before and result["exit"] == 0
    out = sc._format_backfill(result)
    assert f"would-fix-mode\t{first}.md\t0600->0640" in out
    assert [l[1] for l in lines_of(result, "would-export")] == [
        f"artisan/2026-11-02/{first}.md", f"artisan/2026-11-02/{first}.session.json",
        f"artisan/2026-11-02/{third}.md", f"artisan/2026-11-02/{third}.session.json"]
    assert f"skip\t{twin}\thandoff-identical\tartisan/2026-11-02/{first}.md" in out
    assert not lines_of(result, "would-archive")
    assert out[-1].endswith("export=4\tsupersede=0\tskip=1\tfail=0\tunprovisioned=0\thandoff-only=2\thandoff-identical=1")


def test_dry_run_dedupes_against_the_export_tree_too(w):
    prev = w.export / "artisan" / "2026-10-30"
    prev.mkdir()
    (prev / "2026-10-30T00-00-00Z-prev.md").write_text("same\n")
    (prev / "2026-10-30T00-00-00Z-prev.session.json").write_text("{}\n")  # committed: the marker is there
    stem = w.record("2026-11-02T01:00:00Z", None, md_text="same\n")
    result = w.run()
    assert lines_of(result, "skip") == [["skip", stem, "handoff-identical", "artisan/2026-10-30/2026-10-30T00-00-00Z-prev.md"]]
    assert not lines_of(result, "would-export")


def test_execute_exports_handoff_only_with_the_session_json_last_and_a_verifiable_manifest(w, monkeypatch):
    stem = w.record("2026-11-02T01:00:00Z", None, md_text="hello handoff\n")
    order = []
    real_link = os.link
    monkeypatch.setattr(os, "link", lambda s, d, *a, **k: (order.append(Path(d).name), real_link(s, d, *a, **k))[1]
                        if k.get("dst_dir_fd") is not None else real_link(s, d, *a, **k))
    result = w.run(execute=True)
    assert result["exit"] == 0 and result["counts"]["handoff-only"] == 1, result
    assert order == [f"{stem}.md", f"{stem}.session.json"]
    day = w.export / "artisan" / "2026-11-02"
    meta = json.loads((w.root / "artisan" / "2026-11-02" / f"{stem}.session.json").read_text())
    assert meta["transcript"] == "absent" and meta["transcript_absent_reason"] == "none-resolved"
    assert meta["native_transcript"] is None and meta["export"]["status"] == "exported"
    assert (day / f"{stem}.session.json").stat().st_ino == (w.root / "artisan" / "2026-11-02" / f"{stem}.session.json").stat().st_ino
    listed = {f["name"] for f in meta["export"]["files"]}
    assert listed == {p.name for p in day.iterdir()} - {f"{stem}.session.json"}
    for f in meta["export"]["files"]:
        data = (day / f["name"]).read_bytes()
        assert f["size"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()


def test_execute_skips_identical_handoffs_across_the_run_and_writes_nothing_to_the_tree(w):
    a = w.record("2026-11-02T01:00:00Z", None, md_text="same\n")
    b = w.record("2026-11-02T02:00:00Z", None, md_text="same\n")
    c = w.record("2026-11-02T03:00:00Z", None, md_text="other\n")
    d = w.record("2026-11-02T04:00:00Z", None, md_text="other\n")
    result = w.run(execute=True)
    assert result["exit"] == 0
    names = sorted(p.name for p in (w.export / "artisan" / "2026-11-02").iterdir())
    assert names == sorted([f"{a}.md", f"{a}.session.json", f"{c}.md", f"{c}.session.json"])
    assert [l[1:] for l in lines_of(result, "skip")] == [
        [b, "handoff-identical", f"artisan/2026-11-02/{a}.md"], [d, "handoff-identical", f"artisan/2026-11-02/{c}.md"]]
    meta = json.loads((w.root / "artisan" / "2026-11-02" / f"{b}.session.json").read_text())
    assert meta["export"]["status"] == "handoff-identical" and meta["export"]["identical_to"] == f"artisan/2026-11-02/{a}.md"
    assert meta["transcript"] == "absent" and meta["export"]["files"] == []
    assert result["counts"]["handoff-only"] == 2 and result["counts"]["handoff-identical"] == 2
    again = w.run(execute=True)  # idempotent: the exported ones are already-exported, the twins skip again
    assert {l[2] for l in lines_of(again, "skip")} == {"already-exported", "handoff-identical"}
    assert sorted(p.name for p in (w.export / "artisan" / "2026-11-02").iterdir()) == names


def test_a_record_with_a_transcript_is_not_deduped_in_backfill(w):
    src = w.source()
    prev = w.export / "artisan" / "2026-10-30"
    prev.mkdir()
    (prev / "2026-10-30T00-00-00Z-prev.md").write_text("# handoff\n")  # same bytes as the record's md
    w.record("2026-11-02T01:00:00Z", src)
    result = w.run()
    assert not lines_of(result, "skip") and len(lines_of(result, "would-export")) == 3
    assert result["counts"]["handoff-only"] == 0 and result["counts"]["handoff-identical"] == 0


def test_backfill_does_not_follow_a_symlink_planted_as_the_newest_md(w, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("same\n")
    day = w.export / "artisan" / "2026-11-01"
    day.mkdir()
    os.symlink(target, day / "2026-11-01T23-00-00Z-planted.md")
    stem = w.record("2026-11-02T01:00:00Z", None, md_text="same\n")
    result = w.run(execute=True)
    assert not lines_of(result, "skip") and (w.export / "artisan" / "2026-11-02" / f"{stem}.md").is_file()
