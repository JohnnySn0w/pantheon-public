"""Tests for tools/build_harness_mirror.py (T-4768, T-4750 C5, ORG-091 phase 5).

The acceptance tests are FROZEN INPUT (artisan ruling B3): content comes from
acceptance.source_sha, the manifest/overlay/shadows from this working tree, and the
result must reproduce the published claude-code-config / codex-config-template tree
except the allowlist. Everything else runs against throwaway git repos.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_harness_mirror as bhm  # noqa: E402

REPO = bhm.REPO_ROOT
CLAUDE_MANIFEST = REPO / "harness/claude/mirror/manifest.yaml"
CODEX_MANIFEST = REPO / "harness/codex/mirror/manifest.yaml"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


# Leak-gate pattern sources (T-4768 PR4), copied at test time from this checkout into
# each throwaway repo so the gate runs on every scratch build exactly as on a real one.
GATE_SOURCES = ("instance.yaml", "tools/verify_seed.py", "tools/agent_role_map.py",
                "tools/_git_utils.py", "harness/leak-gate/instance-leak-patterns.txt")
GATE_LEAKSCAN = "harness/claude/mirror/overlay/provisioning/leak-scan.sh"


def gate_source_files() -> dict:
    out = {rel: (REPO / rel).read_text(encoding="utf-8") for rel in GATE_SOURCES}
    out["harness/x/mirror/overlay/provisioning/leak-scan.sh"] = \
        (REPO / GATE_LEAKSCAN).read_text(encoding="utf-8")
    return out


def git(repo, *args, input_bytes=None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=input_bytes, env=GIT_ENV,
                          capture_output=True, check=True).stdout.decode().strip()


class Scratch(unittest.TestCase):
    """A throwaway repo with a tiny mirror 'x' at harness/x."""

    MANIFEST_REL = "harness/x/mirror/manifest.yaml"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.files = {
            "harness/x/agents/a.md": "Ask the operator. operator-Scott queues. finite-operator-attention.\n",
            "harness/x/agents/b.md": "keep /home/claude/x here\n",
            "harness/x/install.sh": "#!/bin/sh\necho seat\n",
            "harness/x/tests/t.py": "pass\n",
            "harness/x/mirror/overlay/tools/run.sh": "#!/bin/sh\necho overlay\n",
            "harness/x/mirror/shadows/install.sh": "#!/bin/sh\necho mirror\n",
            **gate_source_files(),
        }
        self.exec_paths = {"harness/x/install.sh", "harness/x/mirror/overlay/tools/run.sh",
                           "harness/x/mirror/shadows/install.sh"}
        self.manifest = {
            "schema": 1, "name": "x", "source_root": "harness/x",
            "exclude": [{"glob": "mirror/*", "reason": "config"},
                        {"glob": "tests/*", "reason": "ci"}],
            "protect": ["operator-Scott"],
            "subs": [{"token": "the operator", "boundary": "L", "replace": "the operator"}],
            "files": [
                {"path": "agents/a.md", "class": "generated"},
                {"path": "agents/b.md", "class": "generated", "rules": [
                    {"find": "/home/claude/x", "replace": "<repo root>", "expect_count": 1},
                    {"append": "appended\n"}]},
                {"path": "install.sh", "class": "shadow", "shadow": "shadows/install.sh",
                 "source_blob": None},
                {"path": "tools/run.sh", "class": "overlay"},
            ],
            "leak_allowlist": [{"path": "agents/a.md", "literal": "operator-Scott", "count": 1,
                                "reason": "the queue algorithm"}],
        }
        self.sha = self.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text, executable=False):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.chmod(p, 0o755 if executable else 0o644)

    def commit(self) -> str:
        for rel, text in self.files.items():
            self.write(rel, text, rel in self.exec_paths)
        for f in self.manifest["files"]:
            if f["class"] == "shadow" and f["source_blob"] is None:  # pin the first commit's blob
                f["source_blob"] = bhm.blob_oid(self.files["harness/x/install.sh"].encode())
        self.write(self.MANIFEST_REL, yaml.safe_dump(self.manifest, sort_keys=False))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "c")
        return git(self.repo, "rev-parse", "HEAD")

    def build(self, sha=None):
        src, cfg, m = bhm.open_mirror(self.repo, sha or self.sha, "x",
                                      manifest_rel=self.MANIFEST_REL)
        return bhm.build(src, cfg, m), m

    def cli(self, *args):
        return bhm.main([args[0], "--mirror", "x", "--repo", str(self.repo),
                         "--manifest-path", str(self.repo / self.MANIFEST_REL), *args[1:]])


class TestTransforms(Scratch):
    def test_subs_protect_rules_and_classes(self):
        res, _ = self.build()
        self.assertEqual(res.errors, [])
        self.assertEqual(res.stale, [])
        got = {e.path: e for e in res.entries}
        self.assertEqual(sorted(got), ["agents/a.md", "agents/b.md", "install.sh", "tools/run.sh"])
        self.assertEqual(got["agents/a.md"].data.decode(),
                         "Ask the operator. operator-Scott queues. finite-the operator-attention.\n")
        self.assertEqual(got["agents/b.md"].data.decode(), "keep <repo root> here\nappended\n")
        self.assertEqual(got["install.sh"].data.decode(), "#!/bin/sh\necho mirror\n")
        self.assertEqual(got["install.sh"].mode, "100755")
        self.assertEqual(got["tools/run.sh"].mode, "100755")
        self.assertEqual(got["agents/a.md"].mode, "100644")

    def test_excluded_and_config_paths_never_ship(self):
        res, _ = self.build()
        paths = [e.path for e in res.entries]
        self.assertFalse(any(p.startswith(("tests/", "mirror/")) for p in paths))

    def test_expect_count_zero_fails_closed(self):
        self.files["harness/x/agents/b.md"] = "the path moved\n"
        res, m = self.build(self.commit())
        self.assertTrue(any(e.startswith("RULE agents/b.md") and "found 0" in e
                            for e in res.errors), res.errors)
        self.assertEqual(bhm.check(res, m, "0" * 40)[0], bhm.EXIT_CONFIG)
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)

    def test_expect_count_two_fails_closed(self):
        self.files["harness/x/agents/b.md"] = "/home/claude/x and /home/claude/x\n"
        res, _ = self.build(self.commit())
        self.assertTrue(any("found 2" in e for e in res.errors), res.errors)

    def test_unclassified_source_fails(self):
        self.files["harness/x/agents/new.md"] = "new persona\n"
        res, _ = self.build(self.commit())
        self.assertIn("UNCLASSIFIED harness/x/agents/new.md: add it to files[] or exclude[]",
                      res.errors)

    def test_missing_overlay_and_source(self):
        del self.files["harness/x/mirror/overlay/tools/run.sh"]
        (self.repo / "harness/x/mirror/overlay/tools/run.sh").unlink()
        res, _ = self.build(self.commit())
        self.assertTrue(any(e.startswith("MISSING overlay") for e in res.errors), res.errors)

    def test_symlink_source_rejected(self):
        os.symlink("a.md", self.repo / "harness/x/agents/link.md")
        self.manifest["files"].append({"path": "agents/link.md", "class": "generated"})
        with self.assertRaises(bhm.ManifestError):
            self.build(self.commit())


class TestShadows(Scratch):
    def test_changed_source_blob_is_stale(self):
        # the manifest keeps pinning the v1 blob while the source moves to v2
        self.files["harness/x/install.sh"] = "#!/bin/sh\necho seat v2\n"
        res, man = self.build(self.commit())
        self.assertEqual(len(res.stale), 1, res.stale)
        self.assertIn("STALE install.sh", res.stale[0])
        self.assertEqual(bhm.check(res, man, "0" * 40)[0], bhm.EXIT_STALE)
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_STALE)
        self.assertEqual(self.cli("build", "--source-sha", "HEAD", "--allow-stale"), bhm.EXIT_OK)

    def test_deleted_source_is_stale(self):
        del self.files["harness/x/install.sh"]
        (self.repo / "harness/x/install.sh").unlink()
        res, _ = self.build(self.commit())
        self.assertTrue(res.stale and "is gone" in res.stale[0], res.stale)

    def test_missing_shadow_is_config_error(self):
        del self.files["harness/x/mirror/shadows/install.sh"]
        (self.repo / "harness/x/mirror/shadows/install.sh").unlink()
        res, _ = self.build(self.commit())
        self.assertEqual(len(res.errors), 1, res.errors)
        self.assertTrue(res.errors[0].startswith("MISSING shadow "))
        self.assertIn("shadows/install.sh (for install.sh)", res.errors[0])
        self.assertEqual(self.cli("check", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)


class TestSchema(Scratch):
    def _load(self, mutate):
        m = yaml.safe_load(yaml.safe_dump(self.manifest))
        mutate(m)
        return bhm.validate_manifest(m)

    def test_valid(self):
        self._load(lambda m: None)

    def test_rejects(self):
        cases = [
            lambda m: m.update(extra=1),
            lambda m: m["files"][0].update(rules=[], shadow="x"),
            lambda m: m["files"][2].pop("source_blob"),
            lambda m: m["files"][2].update(source_blob="abc"),
            lambda m: m["files"][0].update({"class": "verbatim"}),
            lambda m: m["files"].append({"path": "agents/a.md", "class": "generated"}),
            lambda m: m["files"][1]["rules"][0].update(expect_count=0),
            lambda m: m["files"][1]["rules"][0].update(typo=1),
            lambda m: m["subs"][0].update(boundary="WORD"),
            lambda m: m["files"][0].update(path="../escape.md"),
            lambda m: m.update(schema=2),
        ]
        for i, mutate in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(bhm.ManifestError):
                self._load(mutate)

    def test_name_must_match_mirror(self):
        with self.assertRaises(bhm.ManifestError):
            bhm.open_mirror(self.repo, self.sha, "y", manifest_rel=self.MANIFEST_REL)


class TestTreeAndDeterminism(Scratch):
    def test_write_tree_equals_python_hash_and_git(self):
        self.files["harness/x/mirror/overlay/a.b"] = "1\n"
        self.files["harness/x/mirror/overlay/a-b"] = "2\n"
        self.files["harness/x/mirror/overlay/a/c"] = "3\n"
        for p in ("a.b", "a-b", "a/c"):
            self.manifest["files"].append({"path": p, "class": "overlay"})
        res, _ = self.build(self.commit())
        self.assertEqual(res.errors, [])
        computed = bhm.tree_sha(bhm.listing_of(res.entries))
        objrepo = self.tmp / "objects-only"
        git(self.tmp, "init", "-q", "--bare", str(objrepo))
        written = bhm.write_tree(res.entries, objrepo)
        self.assertEqual(written, computed)
        listed = git(objrepo, "ls-tree", "-r", written).splitlines()
        self.assertIn(f"100755 blob {bhm.blob_oid(b'#!/bin/sh' + chr(10).encode() + b'echo mirror' + chr(10).encode())}"
                      "\tinstall.sh", listed)
        self.assertEqual(git(objrepo, "for-each-ref"), "")

    def test_same_sha_same_tree_across_builds_and_clones(self):
        a, _ = self.build()
        b, _ = self.build()
        clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", str(self.repo), str(clone))
        src, cfg, m = bhm.open_mirror(clone, self.sha, "x", manifest_rel=self.MANIFEST_REL)
        c = bhm.build(src, cfg, m)
        trees = {bhm.tree_sha(bhm.listing_of(r.entries)) for r in (a, b, c)}
        self.assertEqual(len(trees), 1)

    def test_build_and_check_write_nothing(self):
        def snapshot():
            objs = sorted(str(p) for p in (self.repo / ".git/objects").rglob("*") if p.is_file())
            idx = self.repo / ".git/index"
            return (git(self.repo, "for-each-ref"), git(self.repo, "rev-parse", "HEAD"),
                    idx.stat().st_mtime_ns, objs)
        before = snapshot()
        res, m = self.build()
        tree = bhm.tree_sha(bhm.listing_of(res.entries))
        self.assertEqual(bhm.check(res, m, tree)[0], bhm.EXIT_OK)
        self.assertEqual(self.cli("check", "--source-sha", "HEAD", "--expect-tree", tree),
                         bhm.EXIT_OK)
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_OK)
        self.assertEqual(self.cli("diff", "--source-sha", "HEAD", "--against", "HEAD"), bhm.EXIT_OK)
        self.assertEqual(snapshot(), before)


class TestCheckAndDiff(Scratch):
    def _published(self, mutate=None):
        """Publish the build into a separate repo; return (repo, tree, entries)."""
        res, _ = self.build()
        entries = list(res.entries)
        if mutate:
            entries = mutate(entries)
        pub = self.tmp / "pub"
        if not pub.exists():
            git(self.tmp, "init", "-q", "--bare", str(pub))
        return pub, bhm.write_tree(entries, pub), entries

    def test_check_ok_against_identical_tree(self):
        pub, tree, _ = self._published()
        res, m = self.build()
        code, report = bhm.check(res, m, tree, pub)
        self.assertEqual(code, bhm.EXIT_OK, report)

    def test_check_mismatch_and_diff(self):
        def drop_line(entries):
            return [bhm.Entry(e.path, e.mode, b"Ask the operator.\n", e.cls, e.provenance)
                    if e.path == "agents/a.md" else e for e in entries]
        pub, tree, _ = self._published(drop_line)
        res, m = self.build()
        code, report = bhm.check(res, m, tree, pub)
        self.assertEqual(code, bhm.EXIT_MISMATCH)
        self.assertTrue(any(r.startswith("MISMATCH agents/a.md") for r in report), report)
        out = "\n".join(bhm.diff(res, pub, tree))
        self.assertIn("+Ask the operator. operator-Scott queues.", out)

    def _with_acceptance(self, published_entries, allowlist):
        tree = bhm.tree_sha(bhm.parse_entries(published_entries))
        self.manifest["acceptance"] = {"source_sha": self.sha, "published_tree": tree,
                                       "published_entries": published_entries,
                                       "allowlist": allowlist}
        return tree

    def test_allowlist_holds_only_for_the_declared_lines(self):
        res, _ = self.build()
        old_a = b"Ask the operator. operator-Scott queues. finite-the operator-attention.\n"
        published = []
        for mode, oid, path in bhm.listing_of(res.entries):
            if path == "agents/a.md":
                oid = bhm.blob_oid(b"extra\n" + old_a)
            published.append(f"{mode} {oid} {path}")
        # 'extra' was dropped upstream: allowlisting it as an ADDED line must not hold
        self._with_acceptance(published, [{"path": "agents/a.md", "reason": "r",
                                           "published_blob": bhm.blob_oid(b"extra\n" + old_a),
                                           "added_lines": ["extra"]}])
        res, m = self.build(self.commit())
        code, report = bhm.check(res, m)
        self.assertEqual(code, bhm.EXIT_MISMATCH, report)
        self.assertTrue(any("allowlist entry does not hold" in r for r in report), report)

    def test_allowlist_added_line_passes_and_unused_entry_fails(self):
        res, _ = self.build()
        listing = bhm.listing_of(res.entries)
        new_a = next(e.data for e in res.entries if e.path == "agents/a.md")
        # the build gains a line the published file lacks: build data minus that line
        published_a = new_a
        self.files["harness/x/agents/a.md"] = "effort: low\n" + self.files["harness/x/agents/a.md"]
        published = [f"{m} {bhm.blob_oid(published_a) if p == 'agents/a.md' else o} {p}"
                     for m, o, p in listing]
        self._with_acceptance(published, [{"path": "agents/a.md", "reason": "W3",
                                           "published_blob": bhm.blob_oid(published_a),
                                           "added_lines": ["effort: low"]}])
        res, m = self.build(self.commit())
        code, report = bhm.check(res, m)
        self.assertEqual(code, bhm.EXIT_OK, report)
        self.assertIn("ALLOWLISTED agents/a.md: W3", report)
        # source reverts: the allowlist entry is now unused -> fail so it gets removed
        self.files["harness/x/agents/a.md"] = "Ask the operator. operator-Scott queues. finite-operator-attention.\n"
        res, m = self.build(self.commit())
        code, report = bhm.check(res, m)
        self.assertEqual(code, bhm.EXIT_MISMATCH, report)
        self.assertTrue(any("unused" in r for r in report), report)

    def test_corrupt_fixture_is_config_error(self):
        res, _ = self.build()
        published = [f"{m} {o} {p}" for m, o, p in bhm.listing_of(res.entries)]
        self._with_acceptance(published, [])
        self.manifest["acceptance"]["published_tree"] = "f" * 40
        res, m = self.build(self.commit())
        code, report = bhm.check(res, m)
        self.assertEqual(code, bhm.EXIT_CONFIG)
        self.assertTrue(any(r.startswith("FIXTURE") for r in report), report)


class TestConfigErrorsExitTwo(Scratch):
    """r1 MINOR 1: every config/usage error exits 2, never 1 (= tree mismatch)."""

    def _cli_with(self, mutate, *extra):
        m = yaml.safe_load(yaml.safe_dump(self.manifest))
        mutate(m)
        self.write(self.MANIFEST_REL, yaml.safe_dump(m, sort_keys=False))  # disk only
        return self.cli("check", "--source-sha", "HEAD", *extra)

    def _acceptance(self, m):
        res, _ = self.build()
        m["acceptance"] = {
            "source_sha": self.sha,
            "published_tree": bhm.tree_sha(bhm.listing_of(res.entries)),
            "published_entries": [f"{a} {b} {c}" for a, b, c in bhm.listing_of(res.entries)],
            "allowlist": []}

    def test_missing_manifest_path(self):
        code = bhm.main(["check", "--mirror", "x", "--repo", str(self.repo), "--source-sha",
                         "HEAD", "--manifest-path", str(self.tmp / "nope/manifest.yaml")])
        self.assertEqual(code, bhm.EXIT_CONFIG)

    def test_non_string_exclude_glob(self):
        self.assertEqual(self._cli_with(lambda m: m["exclude"][0].update(glob=7)), bhm.EXIT_CONFIG)

    def test_null_replace(self):
        self.assertEqual(self._cli_with(lambda m: m["files"][1]["rules"][0].update(replace=None)),
                         bhm.EXIT_CONFIG)

    def test_malformed_published_entries_line(self):
        def mutate(m):
            self._acceptance(m)
            m["acceptance"]["published_entries"][0] = "100644 not-an-oid"
        self.assertEqual(self._cli_with(mutate), bhm.EXIT_CONFIG)

    def test_valid_acceptance_still_ok(self):
        self.assertEqual(self._cli_with(self._acceptance), bhm.EXIT_OK)

    def test_bools_are_not_ints(self):
        self.assertEqual(self._cli_with(lambda m: m.update(schema=True)), bhm.EXIT_CONFIG)
        self.assertEqual(
            self._cli_with(lambda m: m["files"][1]["rules"][0].update(expect_count=True)),
            bhm.EXIT_CONFIG)

    def test_non_list_sections(self):
        self.assertEqual(self._cli_with(lambda m: m.update(subs="the operator")), bhm.EXIT_CONFIG)
        self.assertEqual(self._cli_with(lambda m: m.update(files={"a": 1})), bhm.EXIT_CONFIG)

    def test_out_dir_not_empty_exits_two(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / "x").write_text("x")
        self.assertEqual(self.cli("build", "--source-sha", "HEAD", "--out-dir", str(out)),
                         bhm.EXIT_CONFIG)


class TestExcludeLint(Scratch):
    """r1 MINOR 3: the first glob segment is literal; files[] never match an exclude."""

    def _load(self, mutate):
        m = yaml.safe_load(yaml.safe_dump(self.manifest))
        mutate(m)
        return bhm.validate_manifest(m)

    def test_wildcard_first_segment_refused(self):
        for glob in ("*", "**", "*.md", "*/tests/*", "a?/x", "[ab]/x", "/abs"):
            with self.subTest(glob=glob), self.assertRaises(bhm.ManifestError):
                self._load(lambda m, g=glob: m["exclude"].append({"glob": g, "reason": "r"}))

    def test_literal_first_segment_accepted(self):
        for glob in ("mirror/*", "install.ps1", "mcp/pantheon-mcp-codex/*", "tests/**"):
            with self.subTest(glob=glob):
                self._load(lambda m, g=glob: m["exclude"].append({"glob": g, "reason": "r"}))

    def test_file_path_matching_an_exclude_refused(self):
        with self.assertRaisesRegex(bhm.ManifestError, "matches exclude glob"):
            self._load(lambda m: m["exclude"].append({"glob": "agents/*", "reason": "r"}))


class TestSymlinksAndSubmodules(Scratch):
    """r1 MINOR 2 + NIT 7: no symlink in any FsConfig component; no gitlinks."""

    def _fs_build(self):
        src = bhm.GitSource(self.repo, self.sha)
        cfg = bhm.FsConfig(self.repo / "harness/x/mirror")
        return bhm.build(src, cfg, self.manifest)

    def test_symlinked_overlay_subdir_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "run.sh").write_text("#!/bin/sh\necho evil\n")
        overlay_tools = self.repo / "harness/x/mirror/overlay/tools"
        (overlay_tools / "run.sh").unlink()
        overlay_tools.rmdir()
        os.symlink(elsewhere, overlay_tools)
        with self.assertRaisesRegex(bhm.ManifestError, "symlink in config path"):
            self._fs_build()
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)

    def test_symlinked_overlay_file_refused(self):
        target = self.tmp / "target.sh"
        target.write_text("#!/bin/sh\n")
        f = self.repo / "harness/x/mirror/overlay/tools/run.sh"
        f.unlink()
        os.symlink(target, f)
        with self.assertRaisesRegex(bhm.ManifestError, "symlink in config path"):
            self._fs_build()
        self.assertEqual(self.cli("check", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)

    def test_plain_fs_config_builds(self):
        res = self._fs_build()
        self.assertEqual(res.errors, [])

    def test_submodule_source_refused(self):
        head = self.sha
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{head},harness/x/agents/sub")
        self.manifest["files"].append({"path": "agents/sub", "class": "generated"})
        self.write(self.MANIFEST_REL, yaml.safe_dump(self.manifest, sort_keys=False))
        git(self.repo, "add", self.MANIFEST_REL)
        git(self.repo, "commit", "-q", "-m", "gitlink")
        with self.assertRaisesRegex(bhm.ManifestError, "unsupported git entry 160000"):
            self.build(git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)


class TestMaskSentinelAndMessages(Scratch):
    def test_digit_token_cannot_break_protection(self):
        # r1 NIT 5: with the old "\\0<i>\\0" mask a sub token "0" rewrote the mask itself
        out = bhm.apply_subs("operator-Scott 0 the operator\n", ["operator-Scott"],
                             [{"token": "0", "boundary": "L", "replace": "zero"},
                              {"token": "the operator", "boundary": "L", "replace": "the operator"}])
        self.assertEqual(out, "operator-Scott zero the operator\n")
        self.assertFalse(bhm._has_pua(out))

    def test_pua_in_manifest_strings_refused(self):
        pua = chr(0xE000)
        for mutate in (lambda m: m["subs"][0].update(token="Mi" + pua),
                       lambda m: m["subs"][0].update(replace=pua),
                       lambda m: m["protect"].append("x" + pua)):
            m = yaml.safe_load(yaml.safe_dump(self.manifest))
            mutate(m)
            with self.subTest(), self.assertRaisesRegex(bhm.ManifestError, "private-use-area"):
                bhm.validate_manifest(m)

    def test_pua_in_source_exits_two(self):
        self.files["harness/x/agents/a.md"] = "x" + chr(0xF8FF) + "\n"
        self.commit()
        self.assertEqual(self.cli("build", "--source-sha", "HEAD"), bhm.EXIT_CONFIG)

    def test_stale_message_has_full_blob_ids(self):
        # r1 NIT 6
        self.files["harness/x/install.sh"] = "#!/bin/sh\necho seat v2\n"
        res, _ = self.build(self.commit())
        new = bhm.blob_oid(self.files["harness/x/install.sh"].encode())
        pinned = self.manifest["files"][2]["source_blob"]
        self.assertIn(new, res.stale[0])
        self.assertIn(pinned, res.stale[0])

    def test_diff_is_informational_on_stale(self):
        # r1 NIT 7: diff prints STALE but exits 0; check is the gate
        self.files["harness/x/install.sh"] = "#!/bin/sh\necho seat v2\n"
        self.commit()
        self.assertEqual(self.cli("diff", "--source-sha", "HEAD", "--against", "HEAD"), bhm.EXIT_OK)
        self.assertEqual(self.cli("check", "--source-sha", "HEAD"), bhm.EXIT_STALE)


class TestPayloadPaths(unittest.TestCase):
    """payload_paths (T-4768 PR5, orchestrator T3): the pathspec for payload-path commit counts."""

    def _m(self, files):
        return {"source_root": "harness/x/", "files": files}

    def test_root_plus_out_of_root_sources(self):
        m = self._m([{"path": "a.md", "class": "generated"},
                     {"path": "AGENTS.md", "class": "shadow", "source": "pinned-rules/x.md"},
                     {"path": "b.md", "class": "generated", "source": "harness/x/sub/b.md"}])
        self.assertEqual(bhm.payload_paths(m, "harness/x/mirror/manifest.yaml"),
                         ["harness/x", "pinned-rules/x.md"])

    def test_mirror_dir_outside_root_is_listed(self):
        self.assertEqual(bhm.payload_paths(self._m([]), "cfg/mirror/manifest.yaml"),
                         ["cfg/mirror", "harness/x"])

    def test_root_prefix_is_not_containment(self):
        m = self._m([{"path": "a", "class": "generated", "source": "harness/xy/a"}])
        self.assertEqual(bhm.payload_paths(m), ["harness/x", "harness/xy/a"])

    def test_real_manifests_list_their_shadow_sources(self):
        for name, root in (("claude", "harness/claude"), ("codex", "harness/codex")):
            m = yaml.safe_load((REPO / bhm.MIRRORS[name]).read_text(encoding="utf-8"))
            got = bhm.payload_paths(m, bhm.MIRRORS[name])
            self.assertIn(root, got)
            outside = [p for p in got if p != root]
            self.assertEqual(len(outside), 1, (name, got))  # one out-of-root shadow source each
            self.assertTrue(outside[0].startswith("pinned-rules/"), got)
        m = yaml.safe_load(CODEX_MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("pinned-rules/codex.md", bhm.payload_paths(m))


class TestClaudeManifest(unittest.TestCase):
    """The real claude mirror manifest, frozen-input acceptance (ruling B3)."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = bhm.load_manifest_text(CLAUDE_MANIFEST.read_bytes(), str(CLAUDE_MANIFEST))
        cls.acc = cls.manifest["acceptance"]

    def test_fixture_hashes_to_the_published_tree(self):
        entries = bhm.parse_entries(self.acc["published_entries"])
        self.assertEqual(len(entries), 24)
        self.assertEqual(bhm.tree_sha(entries), self.acc["published_tree"])
        self.assertEqual(self.acc["published_tree"], "fb497e8d0351e9084e8e34c9142c5af8f15a1979")

    def test_payload_is_exactly_the_published_paths(self):
        published = sorted(p for _m, _o, p in bhm.parse_entries(self.acc["published_entries"]))
        self.assertEqual(sorted(f["path"] for f in self.manifest["files"]), published)

    def test_codex_bridge_is_excluded(self):
        self.assertIn("mcp/pantheon-mcp-codex/*", [e["glob"] for e in self.manifest["exclude"]])

    def test_rules_are_exact_single_hits(self):
        for f in self.manifest["files"]:
            for r in f.get("rules") or []:
                if "find" in r:
                    self.assertEqual(r["expect_count"], 1, f["path"])

    def test_manifest_holds_no_contiguous_byte_sequence(self):
        needle = ("HERME" + "S.md").encode()
        self.assertNotIn(needle, CLAUDE_MANIFEST.read_bytes())

    def test_acceptance_frozen_input_reproduces_published_tree(self):
        # content AND mirror config (overlay, shadows, rules) from the frozen sha, so an
        # overlay edit awaiting its publish does not break the frozen input (T-5594);
        # leak-gate sources from HEAD (the frozen sha predates the gate). Checked
        # against the acceptance block on disk, which names the live publish.
        src, cfg, m = bhm.open_mirror(REPO, self.acc["source_sha"], "claude")
        res = bhm.build(src, cfg, m, gate_src=bhm.GitSource(REPO, "HEAD"))
        self.assertEqual(res.errors, [])
        self.assertEqual(res.stale, [])
        code, report = bhm.check(res, self.manifest)
        self.assertEqual(code, bhm.EXIT_OK, "\n".join(report))
        self.assertEqual(len(report), 2, report)
        self.assertTrue(report[0].startswith("LEAKGATE PASS mirror=claude "), report[0])
        self.assertEqual(report[-1], "OK tree fb497e8d0351e9084e8e34c9142c5af8f15a1979")
        self.assertEqual(self.acc["allowlist"], [])


class TestCodexManifest(unittest.TestCase):
    """The real codex mirror manifest, frozen-input acceptance (ruling B3, T-4768 PR3)."""

    # Zero STALE at acceptance.source_sha: recovery re-pinned the #811 drift as reviewed-not-
    # ported (2026-10-09). A reviewed-not-ported re-pin moves only source_blob. PORTING a
    # source change changes the shadow's bytes, so it also needs a new acceptance block
    # (source_sha, published_tree, entries) and the tree sha / file count asserted here.
    STALE_MSG = ("STALE shadow at the frozen source_sha: re-pin source_blob if reviewed-"
                 "not-ported; if ported, also refresh the acceptance block and this test")

    @classmethod
    def setUpClass(cls):
        cls.manifest = bhm.load_manifest_text(CODEX_MANIFEST.read_bytes(), str(CODEX_MANIFEST))
        cls.acc = cls.manifest["acceptance"]
        cls.by_path = {f["path"]: f for f in cls.manifest["files"]}

    def test_registered_mirror(self):
        self.assertEqual(REPO / bhm.MIRRORS["codex"], CODEX_MANIFEST)
        self.assertEqual(self.manifest["name"], "codex")

    def test_fixture_hashes_to_the_published_tree(self):
        entries = bhm.parse_entries(self.acc["published_entries"])
        self.assertEqual(len(entries), 43)
        self.assertEqual(bhm.tree_sha(entries), self.acc["published_tree"])
        self.assertEqual(self.acc["published_tree"], "4e7c6a420dad8bc0ed2039273d417a38625dc5bc")

    def test_payload_is_exactly_the_published_paths(self):
        published = sorted(p for _m, _o, p in bhm.parse_entries(self.acc["published_entries"]))
        self.assertEqual(sorted(self.by_path), published)

    def test_rules_are_exact_single_hits(self):
        for f in self.manifest["files"]:
            for r in f.get("rules") or []:
                if "find" in r:
                    self.assertEqual(r["expect_count"], 1, f["path"])

    def test_manifest_and_shadows_hold_no_contiguous_byte_sequence(self):
        needle = ("HERME" + "S.md").encode()
        self.assertNotIn(needle, CODEX_MANIFEST.read_bytes())
        for f in self.manifest["files"]:
            if f["class"] == "shadow":
                data = (CODEX_MANIFEST.parent / f["shadow"]).read_bytes()
                self.assertNotIn(needle, data, f["shadow"])

    def test_agents_md_tracks_pinned_rules_without_the_sticker_rule(self):
        # W4: 94ddf65's state-sticker section is reviewed and NOT ported; the shadow is
        # pinned to the post-94ddf65 pinned-rules/codex.md blob.
        f = self.by_path["AGENTS.md"]
        self.assertEqual((f["class"], f["source"]), ("shadow", "pinned-rules/codex.md"))
        self.assertEqual(f["source_blob"], "c0246d006b78cadf5a77dd1406f9eba4b401a0d6")
        self.assertNotIn(b"State stickers", (CODEX_MANIFEST.parent / f["shadow"]).read_bytes())

    def test_acceptance_frozen_input_reproduces_published_tree(self):
        src, cfg, m = bhm.open_mirror(REPO, self.acc["source_sha"], "codex",
                                      manifest_path=CODEX_MANIFEST)
        res = bhm.build(src, cfg, m, gate_src=bhm.GitSource(REPO, "HEAD"))
        self.assertEqual(res.errors, [])
        self.assertEqual(res.stale, [], self.STALE_MSG)
        code, report = bhm.check(res, m)
        self.assertEqual(code, bhm.EXIT_OK, "\n".join(report))
        self.assertEqual(len(report), 2, report)
        self.assertTrue(report[0].startswith("LEAKGATE PASS mirror=codex "), report[0])
        self.assertIn(" files=43 leaks=0 allowlisted=30 ", report[0])
        self.assertEqual(report[-1], "OK tree 4e7c6a420dad8bc0ed2039273d417a38625dc5bc")
        self.assertEqual(self.acc["allowlist"], [])

    def test_t6_payload_holds_no_contiguous_byte_sequence(self):
        # HEAD, not the frozen sha: the live payload is what must stay clean.
        needle = ("HERME" + "S.md").encode()
        src, cfg, m = bhm.open_mirror(REPO, "HEAD", "codex", manifest_path=CODEX_MANIFEST)
        res = bhm.build(src, cfg, m, gate_src=bhm.GitSource(REPO, "HEAD"))
        for e in res.entries:
            self.assertNotIn(needle, e.data, e.path)
            self.assertNotIn(b"HERMES", e.data, e.path)


if __name__ == "__main__":
    unittest.main()
