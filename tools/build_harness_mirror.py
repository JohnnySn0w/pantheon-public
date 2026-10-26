#!/usr/bin/env python3
"""Generate a release mirror's payload tree from a pinned pantheon-config sha (T-4768).

T-4750 C5 / ORG-091 phase 5: the public harness mirrors are GENERATED from one
pantheon-config commit instead of being hand-maintained on a genericize branch.
Recovery's publish leg is unchanged: this tool produces a payload TREE; recovery wraps it
with commit-tree (orphan-squash) and pushes it with an ordinary non-force push.

Usage:
    build_harness_mirror.py build --mirror claude --source-sha <sha>
        [--write-tree [--object-repo <repo>]] [--out-dir <dir>] [--allow-stale]
    build_harness_mirror.py check --mirror claude --source-sha <sha> [--expect-tree <tree>]
    build_harness_mirror.py diff  --mirror claude --source-sha <sha> --against <ref|tree>
        [--against-repo <repo>]
    build_harness_mirror.py scan  --mirror claude --source-sha <sha> --tree <tree>
        [--tree-repo <repo>]
  Common: [--repo <pantheon-config checkout>] [--manifest-path <file>]

Inputs are read only from git objects (`git ls-tree` / `git cat-file` at the
source sha): sorted paths, git modes, no timestamps, no working-tree reads. So
the same sha always yields the same tree sha (ORG-091's "bundle digest").
`--manifest-path` is the one exception: it reads the mirror config (manifest,
overlay/, shadows/) from a directory on disk while CONTENT still comes from the
source sha. The frozen-input acceptance test uses it; the default is the
manifest committed at the source sha.

Writes: `build`, `check` and `diff` write nothing (the tree sha is computed in
Python). `build --write-tree` writes loose blob + tree objects into
--object-repo (default: --repo) and NEVER touches an index or a ref; recovery points
it at the mirror clone, where commit-tree needs the objects.

Manifest (harness/<h>/mirror/manifest.yaml), strict schema, default-deny:
  files[]   the WHOLE payload, each path one of
              generated  source blob -> literal rules -> protect/subs
              shadow     a whole-file mirror version pinned to the source blob it
                         was reconciled against; a different source blob = STALE
              overlay    mirror-only file at <mirror dir>/overlay/<path>
  exclude[] every other blob under source_root needs a matching exclude glob,
            else it is UNCLASSIFIED (fail), so nothing joins or drops silently.
            Globs are fnmatch (`*` crosses `/`) relative to source_root. Lint:
            a glob's FIRST path segment must be a literal (no `*`, `?`, `[`),
            so `*`, `**`, `*.md` are refused (they could swallow the whole
            root); and no files[] path may match an exclude glob.
  protect   literals masked before subs (e.g. operator-Scott); the mask uses
            private-use-area sentinels, which no manifest string or source may hold
  subs      ordered token substitutions, boundary helpers from neutralize_seed
  rules     per-file exact literals with expect_count (fail closed), or append
  acceptance  the last published tree as a self-verifying ls-tree fixture plus
            a justified allowlist (path, published_blob, added_lines).

  leak_allowlist  exact leak-gate exemptions {path, literal, count, reason}
            (see tools/mirror_leak_gate.py).

LEAK GATE (T-4768 PR4): every build is scanned by tools/mirror_leak_gate.py
before anything is printed or written; there is no bypass flag. `build`,
`check` and `diff` exit 4 on a leak, and print the LEAKGATE verdict line (to
stderr for `build`, whose stdout is the tree sha). `scan --tree` runs the same
gate over an existing tree (recovery's publish leg: the exact tree being pushed),
with pattern sources and allowlist from --source-sha.

LEAK OUTPUT IS REDACTED (T-4821): the CLI prints each finding as its pattern
class plus path:line only, never the matched literal (an allowlist line shows
the path only), and a path that is itself a path-name hit is withheld in EVERY
printed line (mirror_leak_gate.redact_leak_lines, the same redaction the weekly
sweep's alert uses). A finding line the redactor does not recognise is withheld
whole (fail closed). The LEAKGATE verdict line is printed unchanged; it carries
no match text. `--show-matches` prints the raw findings for local debugging
only: it is never used in an SOP step, and its output is never pasted into a
mail, ticket or handoff. Library callers (build(), run_gate(), check()) still
get the raw res.leaks.

Exit codes: 0 ok, 1 tree mismatch or allowlist violation, 2 config / rule /
coverage error (incl. a missing shadow or overlay file, a malformed manifest, an
unreadable --manifest-path, a symlinked config path, a missing leak-gate pattern
source), 3 STALE shadow, 4 LEAK (the leak gate failed; it wins over 3).
`diff` is informational on stale: it prints STALE lines but exits 0 on stale (2
on a config error, 4 on a leak); gate on `check`, not `diff`.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import neutralize_seed as ns  # noqa: E402  (boundary helpers only, not its tables)
import mirror_leak_gate as gate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirror name -> committed manifest path.
MIRRORS = {
    "claude": "harness/claude/mirror/manifest.yaml",
    "codex": "harness/codex/mirror/manifest.yaml",
}

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_CONFIG = 2
EXIT_STALE = 3
EXIT_LEAK = 4

BOUNDARIES = {"L": ns.L, "LN": ns.LN, "LP": ns.LP, "LC": ns.LC}
FILE_MODES = ("100644", "100755")
CLASSES = ("generated", "shadow", "overlay")

TOP_KEYS = {"schema", "name", "published_repo", "source_root", "exclude", "protect",
            "subs", "files", "acceptance", "leak_allowlist"}
TOP_REQUIRED = {"schema", "name", "source_root", "files"}
EXCLUDE_KEYS = {"glob", "reason"}
SUB_KEYS = {"token", "boundary", "replace"}
FILE_KEYS = {
    "generated": ({"path", "class"}, {"source", "rules", "note"}),
    "shadow": ({"path", "class", "shadow", "source_blob"}, {"source", "note"}),
    "overlay": ({"path", "class"}, {"note"}),
}
RULE_REPLACE_KEYS = {"find", "replace", "expect_count"}
RULE_APPEND_KEYS = {"append"}
ACCEPT_KEYS = {"source_sha", "published_tree", "published_entries", "allowlist"}
ALLOW_KEYS = {"path", "reason", "published_blob", "added_lines"}

_HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_ENTRY = re.compile(r"^(100644|100755) [0-9a-f]{40}([0-9a-f]{24})? [^ ].*$")

# Protect-mask sentinels: private-use-area code points only (no digits/letters a
# sub token could match). Manifest strings and sources may not contain any.
_PUA_LO, _PUA_HI = 0xE000, 0xF8FF
_MASK_EDGE = chr(_PUA_HI)


def _mask_key(i: int) -> str:
    if i >= _PUA_HI - _PUA_LO:
        raise ManifestError("too many protect literals")
    return _MASK_EDGE + chr(_PUA_LO + i) + _MASK_EDGE


def _has_pua(s: str) -> bool:
    return any(_PUA_LO <= ord(c) <= _PUA_HI for c in s)


class ManifestError(ValueError):
    """The manifest is malformed (exit 2)."""


# Everything that is not a tree verdict is a config/usage error: exit 2, never the
# exit 1 that means "tree mismatch" (PR5's drift sweep keys on the difference).
CONFIG_ERRORS = (ManifestError, RuntimeError, OSError, ValueError, TypeError)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _git(repo, *args, input_bytes=None, env=None) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input_bytes,
                          capture_output=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


class GitSource:
    """Read-only view of one commit: `git ls-tree -r` once, blobs by oid."""

    def __init__(self, repo, sha: str):
        self.repo = str(repo)
        self.sha = _git(self.repo, "rev-parse", "--verify", "--quiet",
                        f"{sha}^{{commit}}").decode().strip()
        self.object_format = _git(self.repo, "rev-parse",
                                  "--show-object-format").decode().strip()
        self._entries: dict[str, tuple[str, str, str]] = {}
        out = _git(self.repo, "ls-tree", "-r", "-z", "--full-tree", self.sha)
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, path = rec.split(b"\t", 1)
            mode, typ, oid = meta.decode().split(" ")
            self._entries[path.decode("utf-8")] = (mode, typ, oid)

    def entry(self, path: str):
        """(mode, oid) for a regular-file blob, None if absent; raise on odd types."""
        got = self._entries.get(path)
        if got is None:
            return None
        mode, typ, oid = got
        if typ != "blob" or mode not in FILE_MODES:
            raise ManifestError(f"{path}: unsupported git entry {mode} {typ} "
                                "(only regular files are mirrored)")
        return mode, oid

    def read_blob(self, oid: str) -> bytes:
        return _git(self.repo, "cat-file", "blob", oid)

    def paths_under(self, prefix: str):
        prefix = prefix.rstrip("/") + "/"
        return sorted(p for p in self._entries if p.startswith(prefix))


class GitConfig:
    """Mirror config (manifest dir) read from the source commit."""

    def __init__(self, src: GitSource, mirror_dir: str):
        self.src = src
        self.mirror_dir = mirror_dir.rstrip("/")
        self.label = f"{src.sha[:12]}:{self.mirror_dir}"

    def get(self, rel: str):
        got = self.src.entry(f"{self.mirror_dir}/{rel}")
        if got is None:
            return None
        mode, oid = got
        return mode, self.src.read_blob(oid)


class FsConfig:
    """Mirror config read from a directory on disk (--manifest-path)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.label = str(self.root)

    def get(self, rel: str):
        """(mode, bytes), None if absent. A symlink in ANY component of rel, or a
        path that resolves outside the root, is a config error (git mode has no
        on-disk symlink equivalent we would publish, and following one escapes
        the reviewed config)."""
        root = self.root.resolve()
        cur = self.root
        for part in Path(rel).parts:
            cur = cur / part
            if cur.is_symlink():
                raise ManifestError(f"{self.label}/{rel}: symlink in config path "
                                    f"({cur.relative_to(self.root)})")
        p = self.root / rel
        resolved = p.resolve()
        if resolved != root and root not in resolved.parents:
            raise ManifestError(f"{self.label}/{rel}: resolves outside {root}")
        if not p.is_file():
            return None
        mode = "100755" if os.stat(p).st_mode & 0o111 else "100644"
        return mode, p.read_bytes()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _check_keys(where: str, obj, required: set, optional: set = frozenset()):
    if not isinstance(obj, dict):
        raise ManifestError(f"{where}: expected a mapping")
    missing = required - obj.keys()
    unknown = obj.keys() - required - optional
    if missing:
        raise ManifestError(f"{where}: missing key(s) {sorted(missing)}")
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {sorted(unknown)}")


def _check_rel(where: str, path) -> None:
    if (not isinstance(path, str) or not path or path.startswith("/")
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ManifestError(f"{where}: bad relative path {path!r}")


def _str(where: str, v, nonempty: bool = True) -> str:
    if not isinstance(v, str) or (nonempty and not v):
        raise ManifestError(f"{where}: expected a {'non-empty ' if nonempty else ''}string, "
                            f"got {v!r}")
    if _has_pua(v):
        raise ManifestError(f"{where}: private-use-area characters are reserved "
                            "(protect-mask sentinels)")
    return v


def _int(where: str, v, minimum: int) -> int:
    if type(v) is not int or v < minimum:  # noqa: E721  (bool is an int subclass: refuse it)
        raise ManifestError(f"{where}: expected an integer >= {minimum}, got {v!r}")
    return v


def _list(where: str, v, optional: bool = True) -> list:
    if v is None and optional:
        return []
    if not isinstance(v, list):
        raise ManifestError(f"{where}: expected a list, got {type(v).__name__}")
    return v


def _hex(where: str, v) -> str:
    if not isinstance(v, str) or not _HEX.match(v):
        raise ManifestError(f"{where}: full hex object id required, got {v!r}")
    return v


def _lint_glob(where: str, glob: str) -> None:
    first = glob.split("/", 1)[0]
    if not first or any(c in first for c in "*?["):
        raise ManifestError(f"{where}: exclude glob {glob!r} must start with a literal path "
                            "segment (no wildcard in the first segment)")


def validate_manifest(m) -> dict:
    _check_keys("manifest", m, TOP_REQUIRED, TOP_KEYS - TOP_REQUIRED)
    if type(m["schema"]) is not int or m["schema"] != 1:  # noqa: E721
        raise ManifestError(f"manifest: unsupported schema {m['schema']!r}")
    _str("name", m["name"])
    if "published_repo" in m:
        _str("published_repo", m["published_repo"])
    _check_rel("source_root", m["source_root"])
    globs = []
    for i, ex in enumerate(_list("exclude", m.get("exclude"))):
        _check_keys(f"exclude[{i}]", ex, EXCLUDE_KEYS)
        _lint_glob(f"exclude[{i}]", _str(f"exclude[{i}].glob", ex["glob"]))
        _str(f"exclude[{i}].reason", ex["reason"])
        globs.append(ex["glob"])
    for i, lit in enumerate(_list("protect", m.get("protect"))):
        _str(f"protect[{i}]", lit)
    for i, sub in enumerate(_list("subs", m.get("subs"))):
        _check_keys(f"subs[{i}]", sub, SUB_KEYS)
        _str(f"subs[{i}].token", sub["token"])
        _str(f"subs[{i}].replace", sub["replace"], nonempty=False)
        if sub["boundary"] not in BOUNDARIES:
            raise ManifestError(f"subs[{i}]: boundary must be one of {sorted(BOUNDARIES)}")
    seen = set()
    for i, f in enumerate(_list("files", m["files"], optional=False)):
        if not isinstance(f, dict) or f.get("class") not in CLASSES:
            raise ManifestError(f"files[{i}]: class must be one of {CLASSES}")
        req, opt = FILE_KEYS[f["class"]]
        _check_keys(f"files[{i}] ({f.get('path')})", f, req, opt)
        _check_rel(f"files[{i}].path", f["path"])
        _str(f"files[{i}].path", f["path"])
        if f["path"] in seen:
            raise ManifestError(f"files[{i}]: duplicate path {f['path']}")
        seen.add(f["path"])
        hit = [g for g in globs if fnmatch.fnmatchcase(f["path"], g)]
        if hit:
            raise ManifestError(f"files[{i}]: path {f['path']} matches exclude glob {hit[0]!r}")
        if "note" in f:
            _str(f"files[{i}].note", f["note"])
        if "source" in f:
            _check_rel(f"files[{i}].source", f["source"])
        if f["class"] == "shadow":
            _check_rel(f"files[{i}].shadow", f["shadow"])
            _hex(f"files[{i}].source_blob", f["source_blob"])
        for j, r in enumerate(_list(f"files[{i}].rules", f.get("rules"))):
            where = f"files[{i}].rules[{j}] ({f['path']})"
            if isinstance(r, dict) and "append" in r:
                _check_keys(where, r, RULE_APPEND_KEYS)
                _str(f"{where}.append", r["append"])
            else:
                _check_keys(where, r, RULE_REPLACE_KEYS)
                _str(f"{where}.find", r["find"])
                _str(f"{where}.replace", r["replace"], nonempty=False)
                _int(f"{where}.expect_count", r["expect_count"], 1)
    try:
        allow = gate.validate_allowlist(m.get("leak_allowlist"))
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    for i, a in enumerate(allow):
        _check_rel(f"leak_allowlist[{i}].path", a["path"])
        _str(f"leak_allowlist[{i}].literal", a["literal"])
    acc = m.get("acceptance")
    if acc is not None:
        _check_keys("acceptance", acc, ACCEPT_KEYS)
        _hex("acceptance.source_sha", acc["source_sha"])
        _hex("acceptance.published_tree", acc["published_tree"])
        for i, ln in enumerate(_list("acceptance.published_entries",
                                     acc["published_entries"], optional=False)):
            if not isinstance(ln, str) or not _ENTRY.match(ln):
                raise ManifestError(f"acceptance.published_entries[{i}]: expected "
                                    f"'<100644|100755> <oid> <path>', got {ln!r}")
        for i, a in enumerate(_list("acceptance.allowlist", acc["allowlist"])):
            where = f"acceptance.allowlist[{i}]"
            _check_keys(where, a, ALLOW_KEYS)
            _check_rel(f"{where}.path", a["path"])
            _str(f"{where}.reason", a["reason"])
            _hex(f"{where}.published_blob", a["published_blob"])
            for k, ln in enumerate(_list(f"{where}.added_lines", a["added_lines"],
                                         optional=False)):
                _str(f"{where}.added_lines[{k}]", ln, nonempty=False)
    return m


def load_manifest_text(text: bytes, where: str) -> dict:
    try:
        m = yaml.safe_load(text.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{where}: unparseable: {exc}") from exc
    return validate_manifest(m)


def open_mirror(repo, source_sha: str, mirror: str, manifest_path=None,
                manifest_rel: str | None = None):
    """Return (GitSource, config reader, manifest)."""
    src = GitSource(repo, source_sha)
    if manifest_path is not None:
        manifest_path = Path(manifest_path)
        cfg = FsConfig(manifest_path.parent)
        text = manifest_path.read_bytes()
        where = str(manifest_path)
    else:
        rel = manifest_rel or MIRRORS.get(mirror)
        if rel is None:
            raise ManifestError(f"unknown mirror {mirror!r}; known: {sorted(MIRRORS)}")
        cfg = GitConfig(src, rel.rsplit("/", 1)[0])
        got = src.entry(rel)
        if got is None:
            raise ManifestError(f"no manifest {rel} at {src.sha[:12]}")
        text = src.read_blob(got[1])
        where = f"{src.sha[:12]}:{rel}"
    m = load_manifest_text(text, where)
    if m["name"] != mirror:
        raise ManifestError(f"{where}: manifest name {m['name']!r} != --mirror {mirror!r}")
    return src, cfg, m


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

def apply_rules(text: str, rules, path: str, errors: list) -> str:
    for j, r in enumerate(rules or []):
        if "append" in r:
            text += r["append"]
            continue
        n = text.count(r["find"])
        if n != r["expect_count"]:
            errors.append(f"RULE {path} rules[{j}]: expect_count {r['expect_count']}, "
                          f"found {n}: {r['find'][:60]!r}")
            continue
        text = text.replace(r["find"], r["replace"])
    return text


def apply_subs(text: str, protect, subs) -> str:
    if _has_pua(text):
        raise ManifestError("private-use-area character in a text source "
                            "(reserved for the protect mask)")
    masks = []
    for i, lit in enumerate(protect or []):
        key = _mask_key(i)
        masks.append((key, lit))
        text = text.replace(lit, key)
    for sub in subs or []:
        pat = re.compile(BOUNDARIES[sub["boundary"]](re.escape(sub["token"])))
        rep = sub["replace"]
        text = pat.sub(lambda m, rep=rep: rep + (m.groupdict().get("ps") or ""), text)
    for key, lit in masks:
        text = text.replace(key, lit)
    return text


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    data: bytes
    cls: str
    provenance: str


@dataclass
class BuildResult:
    entries: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # exit 2
    stale: list = field(default_factory=list)    # exit 3
    leaks: list = field(default_factory=list)    # exit 4 (leak gate)
    verdict: str = ""                            # the LEAKGATE line
    object_format: str = "sha1"


def _source_path(m: dict, f: dict) -> str:
    return f.get("source") or f"{m['source_root'].rstrip('/')}/{f['path']}"


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def payload_paths(m: dict, mirror_rel: str | None = None) -> list:
    """The pantheon-config paths whose content can change this mirror's payload tree:
    source_root, every files[].source outside it (shadow / generated sources that
    live elsewhere, e.g. pinned-rules/), and the mirror config dir if it lives
    outside source_root. Sorted, de-duplicated, no path nested under another.
    The drift sweep's commit counts and the cadence report (T3) use this list as
    the git pathspec, so a payload input outside source_root is never undercounted.
    Leak-gate pattern sources are not listed: they change the verdict, not the tree."""
    root = m["source_root"].rstrip("/")
    out = {root}
    for f in m["files"]:
        s = f.get("source")
        if s and not _under(s, root):
            out.add(s)
    if mirror_rel:
        mdir = mirror_rel.rsplit("/", 1)[0] if mirror_rel.endswith(".yaml") else mirror_rel
        if not _under(mdir, root):
            out.add(mdir.rstrip("/"))
    return sorted(p for p in out if not any(q != p and _under(p, q) for q in out))


def build(src: GitSource, cfg, m: dict, gate_src: GitSource | None = None) -> BuildResult:
    """Build the payload, then run the leak gate over ALL of it (never optional).
    gate_src: where pattern sources b-e are read; default the source commit. Only
    the frozen-input acceptance tests pass another (their source sha predates the
    gate)."""
    res = BuildResult(object_format=src.object_format)
    claimed = set()
    for f in m["files"]:
        path, cls = f["path"], f["class"]
        if cls == "overlay":
            got = cfg.get(f"overlay/{path}")
            if got is None:
                res.errors.append(f"MISSING overlay {cfg.label}/overlay/{path}")
                continue
            res.entries.append(Entry(path, got[0], got[1], cls, f"overlay/{path}"))
            continue
        spath = _source_path(m, f)
        claimed.add(spath)
        if cls == "shadow":
            got = cfg.get(f["shadow"])
            if got is None:
                res.errors.append(f"MISSING shadow {cfg.label}/{f['shadow']} (for {path})")
                continue
            sentry = src.entry(spath)
            if sentry is None:
                res.stale.append(f"STALE {path}: source {spath} is gone "
                                 f"(pinned {f['source_blob']})")
            elif sentry[1] != f["source_blob"]:
                res.stale.append(f"STALE {path}: source {spath} is {sentry[1]}, "
                                 f"shadow pinned to {f['source_blob']}")
            res.entries.append(Entry(path, got[0], got[1], cls, f["shadow"]))
            continue
        sentry = src.entry(spath)
        if sentry is None:
            res.errors.append(f"MISSING source {spath} (for {path})")
            continue
        try:
            text = src.read_blob(sentry[1]).decode("utf-8")
        except UnicodeDecodeError:
            res.errors.append(f"NOT UTF-8 {spath}")
            continue
        n_err = len(res.errors)
        text = apply_rules(text, f.get("rules"), path, res.errors)
        if len(res.errors) != n_err:
            continue
        text = apply_subs(text, m.get("protect"), m.get("subs"))
        res.entries.append(Entry(path, sentry[0], text.encode("utf-8"), cls, spath))
    # Default-deny coverage: every blob under source_root is claimed or excluded.
    root = m["source_root"].rstrip("/")
    globs = [e["glob"] for e in m.get("exclude") or []]
    for p in src.paths_under(root):
        if p in claimed:
            continue
        rel = p[len(root) + 1:]
        if not any(fnmatch.fnmatchcase(rel, g) for g in globs):
            res.errors.append(f"UNCLASSIFIED {p}: add it to files[] or exclude[]")
    res.entries.sort(key=lambda e: e.path)
    if not res.errors:
        run_gate(res, src, cfg, m, gate_src or src)
    return res


def _tool_id() -> str:
    here = Path(__file__).resolve().parent
    return ",".join(blob_oid((here / n).read_bytes())[:12]
                    for n in ("build_harness_mirror.py", "mirror_leak_gate.py"))


def load_gate_patterns(gate_src: GitSource, cfg) -> "gate.PatternSet":
    def read_source(rel):
        got = gate_src.entry(rel)
        return None if got is None else gate_src.read_blob(got[1])

    def read_config(rel):
        got = cfg.get(rel)
        return None if got is None else got[1]

    return gate.load_patterns(read_source, read_config)


def redact_output(res: BuildResult) -> None:
    """T-4821: rewrite res for printing. Leak findings become class + path:line
    (unrecognised lines withheld whole), and every path-name-hit path is scrubbed
    from the error and STALE lines too. res.verdict is never touched. CLI only:
    library callers (the sweep) redact the raw findings themselves."""
    if not res.leaks:
        return
    res.leaks, withheld = gate.redact_leak_lines(res.leaks)
    res.errors = [gate.scrub_paths(s, withheld) for s in res.errors]
    res.stale = [gate.scrub_paths(s, withheld) for s in res.stale]


def run_gate(res: BuildResult, src: GitSource, cfg, m: dict, gate_src: GitSource,
             files=None) -> None:
    """Scan the output (or `files`, for `scan --tree`); fill res.leaks/verdict.
    A missing or unusable pattern source is a config error (exit 2)."""
    try:
        ps = load_gate_patterns(gate_src, cfg)
    except gate.GateSourceError as exc:
        res.errors.append(f"LEAKGATE {exc}")
        return
    if files is None:
        files = [(e.path, e.data) for e in res.entries]
        tree = tree_sha(listing_of(res.entries, res.object_format), res.object_format)
    else:
        files, tree = files
    g = gate.scan(files, ps, m.get("leak_allowlist") or [])
    res.leaks = g.leaks
    res.verdict = gate.verdict(g, ps, m["name"], src.sha, tree, _tool_id())


# ---------------------------------------------------------------------------
# Git object hashing (pure Python) and object writing
# ---------------------------------------------------------------------------

def _hash_object(kind: str, body: bytes, fmt: str = "sha1") -> str:
    h = hashlib.new("sha1" if fmt == "sha1" else "sha256")
    h.update(f"{kind} {len(body)}\0".encode() + body)
    return h.hexdigest()


def blob_oid(data: bytes, fmt: str = "sha1") -> str:
    return _hash_object("blob", data, fmt)


def _nest(listing):
    """[(mode, oid, path)] -> nested {name: (mode, oid) | dict}."""
    root: dict = {}
    for mode, oid, path in listing:
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ManifestError(f"path collision at {path}")
        if parts[-1] in node:
            raise ManifestError(f"duplicate path {path}")
        node[parts[-1]] = (mode, oid)
    return root


def _tree_records(node: dict, fmt: str, emit):
    """Serialize a nested node in git order; emit(body) returns the tree oid."""
    items = []
    for name, val in node.items():
        if isinstance(val, dict):
            items.append((name.encode() + b"/", "40000", name,
                          _tree_records(val, fmt, emit)))
        else:
            items.append((name.encode(), val[0], name, val[1]))
    items.sort(key=lambda t: t[0])
    body = b"".join(f"{mode} {name}".encode() + b"\0" + bytes.fromhex(oid)
                    for _, mode, name, oid in items)
    return emit(body, items)


def tree_sha(listing, fmt: str = "sha1") -> str:
    return _tree_records(_nest(listing), fmt,
                         lambda body, _items: _hash_object("tree", body, fmt))


def listing_of(entries, fmt: str = "sha1"):
    return [(e.mode, blob_oid(e.data, fmt), e.path) for e in entries]


def write_tree(entries, object_repo, fmt: str = "sha1") -> str:
    """Write blobs + trees as loose objects into object_repo (no index, no refs)."""
    listing = []
    for e in entries:
        oid = _git(object_repo, "hash-object", "-w", "--no-filters", "--stdin",
                   input_bytes=e.data).decode().strip()
        listing.append((e.mode, oid, e.path))

    def emit(_body, items):
        lines = "".join(f"{mode} {'tree' if mode == '40000' else 'blob'} {oid}\t{name}\n"
                        for _, mode, name, oid in items)
        return _git(object_repo, "mktree", input_bytes=lines.encode()).decode().strip()

    written = _tree_records(_nest(listing), fmt, emit)
    computed = tree_sha(listing, fmt)
    if written != computed:
        raise RuntimeError(f"mktree {written} != computed tree {computed}")
    return written


# ---------------------------------------------------------------------------
# Check / diff
# ---------------------------------------------------------------------------

def parse_entries(lines):
    out = []
    for ln in lines:
        mode, oid, path = ln.split(" ", 2)
        out.append((mode, oid, path))
    return out


def _ls_tree(repo, treeish):
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", treeish)
    listing = []
    for rec in out.split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, _typ, oid = meta.decode().split(" ")
            listing.append((mode, oid, path.decode("utf-8")))
    return listing


def _without_lines(data: bytes, lines) -> bytes:
    text = data.decode("utf-8")
    for ln in lines:
        needle = ln + "\n"
        idx = text.find(needle)
        if idx < 0 or (idx > 0 and text[idx - 1] != "\n"):
            return b""
        text = text[:idx] + text[idx + len(needle):]
    return text.encode("utf-8")


def check(res: BuildResult, m: dict, expect_tree: str | None = None,
          against_repo=None) -> tuple[int, list]:
    """Compare a build with the expected tree. Returns (exit code, report lines)."""
    report = list(res.errors) + list(res.stale)
    if res.errors:
        return EXIT_CONFIG, report
    report += list(res.leaks) + [res.verdict]
    if res.leaks:
        return EXIT_LEAK, report
    if res.stale:
        return EXIT_STALE, report
    fmt = res.object_format
    acc = m.get("acceptance") or {}
    if expect_tree is None:
        expect_tree = acc.get("published_tree")
        if expect_tree is None:
            return EXIT_CONFIG, report + ["no --expect-tree and no acceptance.published_tree"]
    allow = {}
    got_listing = listing_of(res.entries, fmt)
    got_tree = tree_sha(got_listing, fmt)
    if expect_tree == acc.get("published_tree"):
        expected = parse_entries(acc["published_entries"])
        fixture = tree_sha(expected, fmt)
        if fixture != expect_tree:
            return EXIT_CONFIG, report + [
                f"FIXTURE acceptance.published_entries hash to {fixture}, not {expect_tree}"]
        allow = {a["path"]: a for a in acc.get("allowlist") or []}
    elif got_tree == expect_tree:
        return EXIT_OK, report + [f"OK tree {got_tree}"]
    else:
        expected = _ls_tree(against_repo, expect_tree)
    if got_tree == expect_tree and not allow:
        return EXIT_OK, report + [f"OK tree {got_tree}"]
    exp_map = {p: (mode, oid) for mode, oid, p in expected}
    got_map = {p: (mode, oid) for mode, oid, p in got_listing}
    data = {e.path: e.data for e in res.entries}
    code = EXIT_OK
    for path in sorted(exp_map.keys() | got_map.keys()):
        if exp_map.get(path) == got_map.get(path):
            continue
        a = allow.get(path)
        if a and path in exp_map and path in got_map \
                and exp_map[path][0] == got_map[path][0] \
                and exp_map[path][1] == a["published_blob"] \
                and blob_oid(_without_lines(data[path], a["added_lines"]), fmt) \
                == a["published_blob"]:
            report.append(f"ALLOWLISTED {path}: {a['reason']}")
            continue
        code = EXIT_MISMATCH
        if path not in exp_map:
            report.append(f"MISMATCH {path}: not in expected tree")
        elif path not in got_map:
            report.append(f"MISMATCH {path}: missing from build")
        else:
            report.append(f"MISMATCH {path}: expected {exp_map[path][0]} {exp_map[path][1][:12]}"
                          f", built {got_map[path][0]} {got_map[path][1][:12]}"
                          + (" (allowlist entry does not hold)" if a else ""))
    for path in sorted(allow.keys() - (exp_map.keys() & got_map.keys())):
        code = EXIT_MISMATCH
        report.append(f"ALLOWLIST {path}: entry names no path in both trees")
    for path, a in sorted(allow.items()):
        if path in exp_map and exp_map.get(path) == got_map.get(path):
            code = EXIT_MISMATCH
            report.append(f"ALLOWLIST {path}: unused (build matches); remove the entry")
    report.append(f"{'OK' if code == EXIT_OK else 'FAIL'} built {got_tree} "
                  f"vs expected {expect_tree}")
    return code, report


def diff(res: BuildResult, against_repo, against: str) -> list:
    fmt = res.object_format
    expected = {p: (mode, oid) for mode, oid, p in _ls_tree(against_repo, against)}
    got = {e.path: e for e in res.entries}
    out = []
    for path in sorted(expected.keys() | got.keys()):
        e = got.get(path)
        x = expected.get(path)
        if e and x and x == (e.mode, blob_oid(e.data, fmt)):
            continue
        if x is None:
            out.append(f"+++ only in build: {path} ({e.mode})")
            continue
        if e is None:
            out.append(f"--- only in {against}: {path} ({x[0]})")
            continue
        if x[0] != e.mode:
            out.append(f"mode {path}: {x[0]} -> {e.mode}")
        if x[1] != blob_oid(e.data, fmt):
            old = _git(against_repo, "cat-file", "blob", x[1]).decode("utf-8", "replace")
            new = e.data.decode("utf-8", "replace")
            out.extend(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                            f"{against}:{path}", f"build:{path}",
                                            lineterm=""))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _materialize(entries, out_dir: Path) -> None:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"--out-dir {out_dir} is not empty")
    for e in entries:
        p = out_dir / e.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(e.data)
        os.chmod(p, 0o755 if e.mode == "100755" else 0o644)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "check", "diff", "scan"):
        p = sub.add_parser(name)
        p.add_argument("--mirror", required=True)
        p.add_argument("--source-sha", required=True)
        p.add_argument("--repo", default=str(REPO_ROOT))
        p.add_argument("--manifest-path", default=None,
                       help="read the mirror config from disk (content still from --source-sha)")
        p.add_argument("--show-matches", action="store_true",
                       help="print raw LEAK findings with the matched literal and path-hit "
                            "paths (local debugging ONLY; never used in an SOP step, never "
                            "pasted anywhere). Default: redacted to class + path:line.")
        if name == "build":
            p.add_argument("--write-tree", action="store_true")
            p.add_argument("--object-repo", default=None)
            p.add_argument("--out-dir", default=None)
            p.add_argument("--allow-stale", action="store_true")
        if name == "check":
            p.add_argument("--expect-tree", default=None)
            p.add_argument("--against-repo", default=None)
        if name == "diff":
            p.add_argument("--against", required=True)
            p.add_argument("--against-repo", default=None)
        if name == "scan":
            p.add_argument("--tree", required=True)
            p.add_argument("--tree-repo", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "scan":
        return _scan_cli(args)
    try:
        src, cfg, m = open_mirror(args.repo, args.source_sha, args.mirror, args.manifest_path)
        res = build(src, cfg, m)
    except CONFIG_ERRORS as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not args.show_matches:
        redact_output(res)   # T-4821: before anything is printed

    if args.cmd == "check":
        try:
            code, report = check(res, m, args.expect_tree, args.against_repo or args.repo)
        except CONFIG_ERRORS as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return EXIT_CONFIG
        print("\n".join(report))
        return code

    for line in res.errors + res.stale + res.leaks:
        print(line, file=sys.stderr)
    if res.errors:
        return EXIT_CONFIG
    print(res.verdict, file=sys.stderr)
    if res.leaks:
        return EXIT_LEAK
    if args.cmd == "diff":
        try:
            print("\n".join(diff(res, args.against_repo or args.repo, args.against)))
        except CONFIG_ERRORS as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return EXIT_CONFIG
        return EXIT_OK
    if res.stale and not args.allow_stale:
        return EXIT_STALE
    try:
        if args.out_dir:
            _materialize(res.entries, Path(args.out_dir))
        if args.write_tree:
            print(write_tree(res.entries, args.object_repo or args.repo, res.object_format))
        else:
            print(tree_sha(listing_of(res.entries, res.object_format), res.object_format))
    except CONFIG_ERRORS as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK


def _scan_cli(args) -> int:
    try:
        src, cfg, m = open_mirror(args.repo, args.source_sha, args.mirror, args.manifest_path)
        repo = args.tree_repo or args.repo
        tree = _git(repo, "rev-parse", "--verify", f"{args.tree}^{{tree}}").decode().strip()
        files = [(path, _git(repo, "cat-file", "blob", oid))
                 for _mode, oid, path in _ls_tree(repo, tree)]
        res = BuildResult(object_format=src.object_format)
        run_gate(res, src, cfg, m, src, files=(files, tree))
    except CONFIG_ERRORS as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not args.show_matches:
        redact_output(res)   # T-4821: before anything is printed
    for line in res.errors + res.leaks:
        print(line)
    if res.errors:
        return EXIT_CONFIG
    print(res.verdict)
    return EXIT_LEAK if res.leaks else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
