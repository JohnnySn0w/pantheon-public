"""T-5397: kino's mute dropbox (Recovery side). Temp base, health dir and registry only.

Run: python3 -m unittest tools.test_kino_mute_dropbox
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import kino_mute_dropbox as kmd  # noqa: E402
from tools import notify_policy as np  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.base = root / "kino-mute"
        (self.base / "requests").mkdir(parents=True)
        self.health = root / "health"
        self.health.mkdir()
        for p in ("specialist", "orchestrator", "_exporter"):
            (self.health / f"{p}.json").write_text(json.dumps({"persona": p}))
        self.reg = root / "notify_mute.json"
        self.logs: list[str] = []

    def drop(self, name, doc=None, raw=None, mode=0o644):
        path = self.base / "requests" / name
        path.write_bytes(raw if raw is not None else json.dumps(doc).encode())
        os.chmod(path, mode)
        return path

    def req(self, rid, op, persona=None, **extra):
        doc = {"id": rid, "op": op, **extra}
        if persona is not None:
            doc["persona"] = persona
        return self.drop(f"{rid}.json", doc)

    def apply(self):
        # Fake time: the temp wait advances a skew instead of sleeping for real.
        skew = [0.0]

        def sleep(secs):
            skew[0] += secs
        return kmd.apply(self.base, health_dir=self.health, registry=self.reg,
                         log=self.logs.append, sleep=sleep,
                         clock=lambda: time.time() + skew[0])

    def status(self):
        return json.loads((self.base / "status.json").read_text())

    def leftover(self):
        return sorted(os.listdir(self.base / "requests"))


class ApplyTests(Base):
    def test_mute_records_kino_as_the_requester_and_reports_status(self):
        self.req("r-0001", "mute", "specialist", reason="noisy during upgrade", requested_by="operator")
        res = self.apply()
        self.assertEqual([(r["ok"], r["result"]) for r in res], [(True, "muted")])
        entry = np.list_muted(self.reg)["specialist"]
        self.assertEqual((entry["by"], entry["reason"]), ("kino:operator", "noisy during upgrade"))
        st = self.status()
        self.assertEqual(st["schema"], kmd.SCHEMA)
        self.assertTrue(st["registry_ok"])
        self.assertIn("specialist", st["muted"])
        self.assertEqual(st["results"][0]["id"], "r-0001")
        self.assertEqual(self.leftover(), [], "processed requests are deleted")

    def test_unmute_and_list(self):
        np.mute("specialist", by="t", registry=self.reg)
        self.req("r-0002", "unmute", "specialist")
        self.req("r-0003", "list")
        res = self.apply()
        self.assertEqual([r["result"] for r in res], ["unmuted", "listed"])
        self.assertEqual(self.status()["muted"], {})

    def test_repeat_mute_and_unmute_are_idempotent(self):
        self.req("a1", "mute", "orchestrator")
        self.req("a2", "mute", "orchestrator")
        self.req("a3", "unmute", "specialist")
        self.assertEqual([r["result"] for r in self.apply()],
                         ["muted", "already muted", "was not muted"])

    def test_status_results_are_newest_first_and_bounded(self):
        for i in range(kmd.KEEP_RESULTS + 5):
            self.req(f"l{i:03d}", "list")
            self.apply()
        res = self.status()["results"]
        self.assertEqual(len(res), kmd.KEEP_RESULTS)
        self.assertEqual(res[0]["id"], f"l{kmd.KEEP_RESULTS + 4:03d}")


class ValidationTests(Base):
    def refused(self):
        res = self.apply()
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]["ok"])
        self.assertEqual(self.leftover(), [], "a refused request is still deleted")
        self.assertEqual(np.list_muted(self.reg), {}, "nothing was muted")
        return res[0]["error"]

    def test_unknown_op(self):
        self.req("v1", "delete-everything", "specialist")
        self.assertIn("op must be", self.refused())

    def test_persona_must_match_the_regex(self):
        self.req("v2", "mute", "../../etc")
        self.assertEqual(self.refused(), "bad persona")

    def test_persona_must_be_published_by_the_health_plane(self):
        self.req("v3", "mute", "nobody")
        self.assertIn("not published", self.refused())

    def test_exporter_record_is_not_a_persona(self):
        self.req("v4", "mute", "_exporter")
        self.assertEqual(self.refused(), "bad persona")

    def test_only_per_persona_records_are_personas(self):
        """orchestrator: the T-5395 aggregate fleet-health.json passes PERSONA_RE by name."""
        (self.health / "fleet-health.json").write_text(
            json.dumps({"schema": "pantheon-fleet-health/1", "entries": []}))
        (self.health / "imposter.json").write_text(json.dumps({"persona": "specialist"}))
        (self.health / "broken.json").write_text("{nope")
        (self.health / "listy.json").write_text(json.dumps(["persona"]))
        outside = Path(self._tmp.name) / "zed.json"  # self-consistent, so only NOFOLLOW stops it
        outside.write_text(json.dumps({"persona": "zed"}))
        os.symlink(outside, self.health / "zed.json")
        (self.health / "big.json").write_text(
            json.dumps({"persona": "big", "pad": "x" * kmd.HEALTH_FILE_MAX_BYTES}))
        self.assertEqual(kmd.known_personas(self.health), {"specialist", "orchestrator"})
        self.req("v21", "mute", "fleet-health")
        self.assertIn("not published", self.refused())

    def test_id_must_match_the_file_name(self):
        self.drop("v5.json", {"id": "other", "op": "list"})
        self.assertIn("does not match", self.refused())

    def test_reason_and_requester_are_bounded(self):
        self.req("v6", "mute", "specialist", reason="x" * (kmd.MAX_REASON + 1))
        self.assertIn("reason", self.refused())
        self.req("v7", "mute", "specialist", requested_by="a b")
        self.assertEqual(self.refused(), "bad requested_by")
        self.req("v8", "mute", "specialist", reason="line\nbreak")
        self.assertIn("reason", self.refused())

    def test_oversized_file(self):
        self.drop("v9.json", raw=b"{" + b" " * kmd.MAX_BYTES + b"}")
        self.assertIn("larger than", self.refused())

    def test_not_json_and_not_an_object(self):
        self.drop("v10.json", raw=b"{nope")
        self.assertIn("unreadable request", self.refused())
        self.drop("v11.json", raw=b"[1, 2]")
        self.assertIn("not a JSON object", self.refused())

    def test_symlink_is_never_followed(self):
        secret = Path(self._tmp.name) / "secret.json"
        secret.write_text(json.dumps({"id": "v12", "op": "mute", "persona": "specialist"}))
        os.symlink(secret, self.base / "requests" / "v12.json")
        self.assertIn("unreadable request", self.refused())
        self.assertTrue(secret.exists(), "the link target is untouched")

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.base / "requests" / "v15.json")
        self.assertIn("not a regular file", self.refused())

    def test_directory_request_is_refused_and_does_not_crash_the_run(self):
        (self.base / "requests" / "v16.json").mkdir()
        self.req("v17", "list")
        res = self.apply()
        self.assertEqual([r["id"] for r in res], ["v16", "v17"])
        self.assertIn("not a regular file", res[0]["error"])
        self.assertTrue(res[1]["ok"], "a later request still runs")
        self.assertEqual(self.leftover(), [], "an empty directory is rmdir'd")

    def test_unremovable_directory_is_left_and_logged(self):
        d = self.base / "requests" / "v18.json"
        d.mkdir()
        (d / "x").write_text("")
        self.assertIn("not a regular file", self.apply()[0]["error"])
        self.assertEqual(self.leftover(), ["v18.json"])
        self.assertTrue(any("cannot remove" in line for line in self.logs))

    def test_non_json_name_is_refused_and_dotfiles_ignored(self):
        self.drop("v13.txt", {"id": "v13", "op": "list"})
        self.assertIn("not a .json request", self.refused())
        self.drop(".tmp-v14.json", {"id": "v14", "op": "mute", "persona": "specialist"})
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.leftover(), [], "a temp still there after the wait is deleted")
        self.assertEqual(np.list_muted(self.reg), {}, "a temp is never applied")

    def test_fresh_temp_is_waited_out_then_left_for_its_rename(self):
        tmp = self.drop(".tmp-v19.json", {"id": "v19", "op": "list"})
        naps = []

        def sleep(s):  # kino finishes its write during the first nap
            naps.append(s)
            os.rename(tmp, self.base / "requests" / "v19.json")
        self.assertEqual(kmd.apply(self.base, health_dir=self.health, registry=self.reg,
                                   log=self.logs.append, sleep=sleep), [])
        self.assertEqual(naps, [1])
        self.assertEqual(self.leftover(), ["v19.json"], "the renamed request re-fires the watch")

    def test_stale_temp_is_deleted_without_waiting(self):
        tmp = self.drop(".tmp-v20.json", {"id": "v20", "op": "list"})
        old = time.time() - kmd.STALE_TMP_SECS - 5
        os.utime(tmp, (old, old))
        kmd.apply(self.base, health_dir=self.health, registry=self.reg, log=self.logs.append,
                  sleep=lambda s: self.fail("must not wait for a stale temp"))
        self.assertEqual(self.leftover(), [])

    def test_per_run_cap(self):
        for i in range(kmd.MAX_PER_RUN + 3):
            self.req(f"c{i:03d}", "list")
        self.assertEqual(len(self.apply()), kmd.MAX_PER_RUN)
        self.assertEqual(len(self.leftover()), 3)


class RefreshTests(Base):
    """orchestrator (T-5401): a mute made outside kino must reach status.json without a request."""

    def test_refresh_rewrites_status_with_a_mute_made_elsewhere(self):
        self.req("r1", "list")
        self.apply()
        np.mute("orchestrator", by="cli:someone", reason="x", registry=self.reg)
        self.assertEqual(kmd.apply(self.base, health_dir=self.health, registry=self.reg,
                                   log=self.logs.append), [])
        self.assertNotIn("orchestrator", self.status()["muted"], "no request, no refresh: stale")
        self.assertEqual(kmd.apply(self.base, health_dir=self.health, registry=self.reg,
                                   log=self.logs.append, refresh=True), [])
        st = self.status()
        self.assertIn("orchestrator", st["muted"])
        self.assertEqual([r["id"] for r in st["results"]], ["r1"], "a refresh adds no result")

    def test_refresh_on_an_unreadable_registry_fails_the_unit(self):
        self.reg.write_text("{not json")
        rc = kmd.main(["apply", "--base", str(self.base), "--health-dir", str(self.health),
                       "--registry", str(self.reg), "--refresh"])
        self.assertEqual(rc, 1)
        self.assertFalse(self.status()["registry_ok"])
        self.assertEqual(self.status()["results"], [], "the synthetic result is not published")

    def test_cli_refresh_on_a_good_registry_is_rc0(self):
        rc = kmd.main(["apply", "--base", str(self.base), "--health-dir", str(self.health),
                       "--registry", str(self.reg), "--refresh"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.status()["registry_ok"])


class RegistryFaultTests(Base):
    def test_unreadable_registry_writes_nothing_and_fails_the_unit(self):
        self.reg.write_text('{"schema": "x", "muted": {"specialist": {}}')   # torn
        torn = self.reg.read_text()
        self.req("f1", "mute", "orchestrator")
        with mock.patch.object(kmd, "apply", wraps=kmd.apply):
            rc = kmd.main(["apply", "--base", str(self.base), "--health-dir", str(self.health),
                           "--registry", str(self.reg)])
        self.assertEqual(rc, 1, "an unreadable registry pages Recovery via OnFailure")
        self.assertEqual(self.reg.read_text(), torn, "T-5396: never written over")
        st = self.status()
        self.assertFalse(st["registry_ok"])
        self.assertEqual(st["muted"], {})
        self.assertIn("registry unreadable", st["results"][0]["error"])

    def test_refused_requests_do_not_fail_the_unit(self):
        self.req("f2", "mute", "nobody")
        rc = kmd.main(["apply", "--base", str(self.base), "--health-dir", str(self.health),
                       "--registry", str(self.reg)])
        self.assertEqual(rc, 0)


class InitTests(Base):
    def test_init_sets_owner_only_modes_and_the_kino_acls(self):
        calls = []
        base = Path(self._tmp.name) / "fresh"
        kmd.init(base, kino_user="kino", recovery_user="claude-fallback",
                 run=lambda argv, **k: calls.append(argv))
        self.assertEqual(oct(base.stat().st_mode & 0o7777), oct(0o2750))
        self.assertEqual(oct((base / "requests").stat().st_mode & 0o7777), oct(0o2700))
        self.assertIn(["setfacl", "-m", "u:kino:r-x", str(base)], calls)
        self.assertIn(["setfacl", "-m", "d:u:kino:r", str(base)], calls)
        self.assertIn(["setfacl", "-m", "u:kino:rwx", str(base / "requests")], calls)
        self.assertIn(["setfacl", "-m", "d:u:claude-fallback:rw", str(base / "requests")], calls)
        kmd.init(base, kino_user="kino", recovery_user="claude-fallback", run=lambda *a, **k: None)

    def test_units_watch_the_dropbox_and_run_apply(self):
        repo = Path(kmd.__file__).resolve().parent
        path_unit = (repo / "systemd" / "pantheon-kino-mute.path").read_text()
        svc = (repo / "systemd" / "pantheon-kino-mute.service").read_text()
        self.assertIn(f"DirectoryNotEmpty={kmd.DEFAULT_BASE}/requests", path_unit)
        self.assertIn("kino_mute_dropbox.py apply", svc)
        self.assertIn("sg pantheon-mailbox", svc)
        self.assertIn("OnFailure=", svc)
        self.assertIn("apply --refresh", svc, "path and timer fires both refresh status")
        timer = (repo / "systemd" / "pantheon-kino-mute.timer").read_text()
        self.assertIn("OnCalendar=*:0/5", timer)
        self.assertIn("Unit=pantheon-kino-mute.service", timer)


class NotifyPolicyByFlagTests(unittest.TestCase):
    def test_cli_mute_by_flag(self):
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "m.json"
            np.main(["--registry", str(reg), "mute", "specialist", "--by", "kino:operator"])
            self.assertEqual(np.list_muted(reg)["specialist"]["by"], "kino:operator")
            np.main(["--registry", str(reg), "mute", "orchestrator"])
            self.assertTrue(np.list_muted(reg)["orchestrator"]["by"].startswith("cli:"))


if __name__ == "__main__":
    unittest.main()
