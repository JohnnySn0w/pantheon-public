#!/usr/bin/env python3
"""worker_guard_cutover_smoke: prove the T-4177 worker-guard shim is decision-identical, then time it.

WHY. tools/hooks/worker-guard.py became a thin entry shim (T-4177) in front of the guard body
in tools/hooks/worker_guard_impl.py. The settings.json command target is unchanged, so a `git pull`
on a live seat applies the shim instantly. This script is the pre-pull gate: run it from a
worktree of the branch, against the NEW files, BEFORE pulling main into a live checkout.

    python3 tools/worker_guard_cutover_smoke.py                  # layout + probes + isolation
    python3 tools/worker_guard_cutover_smoke.py --bench 60       # ... plus the interleaved benchmark
    python3 tools/worker_guard_cutover_smoke.py --migration --old-ref <sha>  # one-time cutover proof
    # re-check the LIVE files after a pull:
    python3 tools/worker_guard_cutover_smoke.py --hooks-dir ~/pantheon-config/tools/hooks

Exit 0 only if every step passes. The steps:
  layout      shim + impl colocated, shim executable, __pycache__ writability reported
  probes      the four T-4139 live probes (main-thread Read/Grep silent allow; subagent Read of a
              credential denied; subagent Read of an ordinary file allowed)
  isolation   the shim ALONE in a temp dir still allows main-thread payloads (=> the fast path never
              imports the impl) and fails loud (exit 1) on a subagent payload
  migration   (opt-in, --migration; T-5324) the pre-shim guard (git show <old-ref>:tools/hooks/worker-guard.py) vs the new shim over
              a corpus: curated matrix + every payload the existing test_worker_guard suite passes to
              decide() + a seeded fuzz. (returncode, stdout, stderr) must be byte-identical.
              Also asserts the impl body equals the old body modulo main() and the docstring.
              Only meaningful at the original cutover: origin/main now holds the shim, and the
              frozen pre-shim guard diverges on every legitimate guard edit since, so it is not run
              by default.
  bench       (optional) interleaved wall + child-CPU timing, old vs new vs bare python3.

The corpus builders, runners and fuzzer here are imported by tools/test_worker_guard_shim.py, which
holds the PERMANENT check (shim == impl-run-as-script). The old-monolith comparison lives only here:
against a frozen old guard it would go red on the next legitimate guard edit.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import copy
import dataclasses
import importlib.util
import io
import json
import math
import os
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOOKS_DIR = REPO_ROOT / "tools" / "hooks"
SHIM_NAME = "worker-guard.py"
IMPL_NAME = "worker_guard_impl.py"
# Last commit at which tools/hooks/worker-guard.py is the pre-shim monolith (T-4139 squash).
PRE_SHIM_SHA = "4897d8d5a88391d9a6981e6f50bd4715634bedcc"
SHIM_MARKER = "worker_guard_impl"

Case = tuple[str, bytes]


@dataclasses.dataclass(frozen=True)
class Outcome:
    rc: int
    out: bytes
    err: bytes

    def is_deny(self) -> bool:
        try:
            spec = json.loads(self.out)["hookSpecificOutput"]
        except Exception:
            return False
        return spec.get("permissionDecision") == "deny"


def run_script(script: Path, stdin: bytes, timeout: float = 120.0) -> Outcome:
    proc = subprocess.run(
        [sys.executable, str(script)], input=stdin, capture_output=True, timeout=timeout
    )
    return Outcome(proc.returncode, proc.stdout, proc.stderr)


# --- corpus -----------------------------------------------------------------------------------

_HARNESS_FIELDS = {"session_id": "s-4177", "hook_event_name": "PreToolUse", "tool_use_id": "toolu_x"}

# (label, payload-without-agent-fields). Chosen so that, WITH an agent_type, most of these DENY
# (the T-1345/T-1381/T-4135/T-4139 surfaces) and the rest are ordinary allows.
TOOL_PAYLOADS: list[tuple[str, dict]] = [
    ("bash-token-read", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.claude/channels/telegram/.env"}}),
    ("bash-bot-api", {"tool_name": "Bash", "tool_input": {"command": "curl https://api.telegram.org/bot1:x/sendMessage"}}),
    ("bash-git-push", {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}),
    ("bash-gh-pr", {"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}}),
    ("bash-sudo", {"tool_name": "Bash", "tool_input": {"command": "sudo ls"}}),
    ("bash-cred-cat", {"tool_name": "Bash", "tool_input": {"command": "cat /home/claude/gh_token.env"}}),
    ("bash-ssh-key", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_ed25519"}}),
    ("bash-ledger-write", {"tool_name": "Bash", "tool_input": {"command": "git add ledger/2026-W40/2026-W40.jsonl"}}),
    ("bash-git-config-global", {"tool_name": "Bash", "tool_input": {"command": "git config --global user.name x"}}),
    ("bash-gh-api-put", {"tool_name": "Bash", "tool_input": {"command": "gh api -X PUT repos/o/r/contents/x -f message=m"}}),
    ("bash-plain", {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}),
    ("bash-read-only-mention", {"tool_name": "Bash", "tool_input": {"command": "grep sudo /tmp/notes.txt"}}),
    ("bash-empty-command", {"tool_name": "Bash", "tool_input": {"command": ""}}),
    ("bash-no-input", {"tool_name": "Bash"}),
    ("bash-null-input", {"tool_name": "Bash", "tool_input": None}),
    ("bash-shared-cwd-identity", {"tool_name": "Bash", "tool_input": {"command": "git config user.email a@b.c"}, "cwd": "/home/claude/pantheon-config"}),
    ("read-root-env", {"tool_name": "Read", "tool_input": {"file_path": "/home/claude/root.env"}}),
    ("read-ssh-key", {"tool_name": "Read", "tool_input": {"file_path": "/home/claude/.ssh/id_ed25519"}}),
    ("read-claude-credentials", {"tool_name": "Read", "tool_input": {"file_path": "/home/claude/.claude/.credentials.json"}}),
    ("read-plain", {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.txt"}, "cwd": "/tmp"}),
    ("read-relative-cred", {"tool_name": "Read", "tool_input": {"file_path": "root.env"}, "cwd": "/tmp"}),
    ("read-codex-relative", {"tool_name": "Read", "tool_input": {"file_path": "auth.json"}, "cwd": "/home/x/.codex"}),
    ("read-empty-path", {"tool_name": "Read", "tool_input": {"file_path": ""}}),
    ("grep-cred-path", {"tool_name": "Grep", "tool_input": {"pattern": "tok", "path": "/home/claude/gh_token.env"}}),
    ("grep-dir-path", {"tool_name": "Grep", "tool_input": {"pattern": "tok", "path": "/home/claude"}}),
    ("grep-no-path", {"tool_name": "Grep", "tool_input": {"pattern": "tok"}}),
    ("edit-protected", {"tool_name": "Edit", "tool_input": {"file_path": "/home/claude/pantheon-config/sop/standard/x.md"}}),
    ("edit-code", {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x.py"}}),
    ("write-readme", {"tool_name": "Write", "tool_input": {"file_path": "/home/claude/pantheon-config/README.md", "content": "a\nb\n"}}),
    # T-4545: the ORG-094 paths (adr/ moved to architecture/adr/); the old spelling protects nothing.
    ("multiedit-adr", {"tool_name": "MultiEdit", "tool_input": {"file_path": "/tmp/pantheon-config-T-1/architecture/adr/ORG-1.md"}}),
    ("write-engineering-practices", {"tool_name": "Write", "tool_input": {"file_path": "/home/claude/pantheon-config/architecture/engineering-practices/x.md", "content": "a\n"}}),
    ("bash-write-architecture-adr", {"tool_name": "Bash", "tool_input": {"command": "sed -i s/a/b/ /home/claude/pantheon-config/architecture/adr/ORG-1.md"}}),
    ("notebook-protected", {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/home/claude/pantheon-config/notes/n.ipynb"}}),
    ("apply-patch-protected", {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: /home/claude/pantheon-config/notes/x.md\n@@\n-a\n+b\n*** End Patch\n"}}),
    ("apply-patch-plain", {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: /tmp/x.py\n+a\n*** End Patch\n"}}),
    ("task", {"tool_name": "Task", "tool_input": {"prompt": "do it"}}),
    ("mcp-tool", {"tool_name": "mcp__plugin_x__y", "tool_input": {"a": 1}}),
    ("unknown-tool", {"tool_name": "Frobnicate", "tool_input": {}}),
    ("empty-tool-name", {"tool_name": ""}),
    ("no-tool-name", {}),
]

# Cheap subset for the unit-test matrix: one deny per surface (Bash, Read, Edit).
CANARY_LABELS = ("bash-token-read", "read-root-env", "edit-protected")

AGENT_VARIANTS: list[tuple[str, dict]] = [
    ("agent-absent", {}),
    ("agent-null", {"agent_type": None}),
    ("agent-empty", {"agent_type": ""}),
    ("agent-zero", {"agent_type": 0}),
    ("agent-false", {"agent_type": False}),
    ("agent-empty-list", {"agent_type": []}),
    ("agent-empty-dict", {"agent_type": {}}),
    ("agent-implementer", {"agent_type": "implementer"}),
    ("agent-named-instance", {"agent_type": "t1336-impl2"}),
    ("agent-one", {"agent_type": 1}),
    ("agent-true", {"agent_type": True}),
    ("agent-list", {"agent_type": ["x"]}),
    ("agent-id-only", {"agent_id": "a1"}),
    ("agent-id-null-type", {"agent_type": None, "agent_id": "a2"}),
    ("agent-both", {"agent_type": "reviewer", "agent_id": "a3"}),
]
CORE_AGENT_LABELS = ("agent-absent", "agent-implementer")

_ESCAPED_KEY = (b'"agent_type"', b'"agent\\u005ftype"')


def _dump(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _payload(agent: dict, tool: dict) -> dict:
    merged: dict = {}
    merged.update(_HARNESS_FIELDS)
    merged.update(copy.deepcopy(tool))
    merged.update(agent)
    return merged


def raw_shape_cases() -> list[Case]:
    """Malformed, oddly-typed and extreme stdin, each of which the old guard must treat identically."""
    deep = lambda n, tail: b'{"agent_type":"implementer","tool_name":"Bash","tool_input":{"command":"git push"},"x":' + b"[" * n + b"]" * n + tail  # noqa: E731
    big_content = ("line\n" * 200_000)
    return [
        ("raw-empty", b""),
        ("raw-whitespace", b"  \n\t "),
        ("raw-brace", b"{"),
        ("raw-garbage", b"not json at all"),
        ("raw-null", b"null"),
        ("raw-true", b"true"),
        ("raw-number", b"123"),
        ("raw-string", b'"agent_type"'),
        ("raw-empty-list", b"[]"),
        ("raw-list-with-agent", b'[{"agent_type":"implementer","tool_name":"Bash"}]'),
        ("raw-empty-object", b"{}"),
        ("raw-bom-valid", b"\xef\xbb\xbf" + _dump({"agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": "git push"}})),
        ("raw-invalid-utf8", b'\xff\xfe{"agent_type":"implementer"}'),
        ("raw-invalid-utf8-in-value", b'{"agent_type":"implementer","tool_name":"Bash","tool_input":{"command":"\xff git push"}}'),
        ("raw-trailing-garbage", _dump({"agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": "git push"}}) + b" trailing"),
        ("raw-truncated", _dump({"agent_type": "implementer", "tool_name": "Bash"})[:-3]),
        ("raw-duplicate-key-last-wins-empty", b'{"agent_type":"implementer","agent_type":"","tool_name":"Bash","tool_input":{"command":"git push"}}'),
        ("raw-duplicate-key-last-wins-set", b'{"agent_type":"","agent_type":"implementer","tool_name":"Bash","tool_input":{"command":"git push"}}'),
        ("raw-nested-agent-type-only", _dump({"tool_name": "Bash", "tool_input": {"command": "git push", "agent_type": "implementer"}})),
        ("raw-escaped-key-deny", b'{"agent\\u005ftype":"implementer","tool_name":"Bash","tool_input":{"command":"git push"}}'),
        ("raw-escaped-key-empty", b'{"agent\\u005ftype":"","tool_name":"Bash","tool_input":{"command":"git push"}}'),
        ("raw-escaped-first-letter", b'{"\\u0061gent_type":"implementer","tool_name":"Bash","tool_input":{"command":"git push"}}'),
        ("raw-case-variant-key", _dump({"Agent_Type": "implementer", "tool_name": "Bash", "tool_input": {"command": "git push"}})),
        ("raw-deep-50-subagent", deep(50, b"}")),
        ("raw-deep-900-subagent", deep(900, b"}")),
        ("raw-deep-5000-subagent", deep(5000, b"}")),
        ("raw-deep-5000-main", b'{"tool_name":"Bash","x":' + b"[" * 5000 + b"]" * 5000 + b"}"),
        ("raw-big-main-write", _dump({"tool_name": "Write", "tool_input": {"file_path": "/tmp/big.txt", "content": big_content}})),
        ("raw-big-subagent-write", _dump({"agent_type": "implementer", "tool_name": "Write", "tool_input": {"file_path": "/tmp/big.txt", "content": big_content}})),
        ("raw-main-write-mentions-agent-type", _dump({"tool_name": "Write", "tool_input": {"file_path": "/tmp/g.py", "content": "d.get('agent_type')\n"}})),
        ("raw-main-content-unicode-escape", _dump({"tool_name": "Write", "tool_input": {"file_path": "/tmp/u.py", "content": "s = '\\u00e9'\n"}})),
    ]


def matrix_cases(*, full: bool) -> list[Case]:
    """agent-field variants x tool payloads.

    `full` = every pair. Otherwise (the unit-test size) the pairs where the agent shape is one of
    CORE_AGENT_LABELS or the tool payload is one of CANARY_LABELS. Every canary payload that carries
    an `agent_type` key also gets its escaped-key spelling (`agent\\u005ftype`), which decodes to
    the same key but defeats a bare substring test.
    """
    cases: list[Case] = []
    for a_label, agent in AGENT_VARIANTS:
        for t_label, tool in TOOL_PAYLOADS:
            if not full and a_label not in CORE_AGENT_LABELS and t_label not in CANARY_LABELS:
                continue
            raw = _dump(_payload(agent, tool))
            cases.append((f"{a_label}/{t_label}", raw))
            if _ESCAPED_KEY[0] in raw and t_label in CANARY_LABELS:
                cases.append((f"{a_label}/{t_label}/escaped-key", raw.replace(*_ESCAPED_KEY)))
    return cases


def curated_corpus(*, full: bool) -> list[Case]:
    return raw_shape_cases() + matrix_cases(full=full)


_FUZZ_KEYS = ["tool_name", "tool_input", "cwd", "session_id", "agent_id", "hook_event_name",
              "agent_type", "permission_mode", "transcript_path", "command", "file_path"]
_FUZZ_SCALARS = [None, True, False, 0, 1, -1, 3.5, "", "x", "implementer", "reviewer", "agent_type",
                 "\\u0061gent_type", "é", "日本", "a\nb", "\u2028", "Bash", "Read"]
_FUZZ_AGENT_VALUES = [None, "", 0, False, [], {}, "implementer", "t1336-impl2", 1, True, ["x"], {"a": 1}]


def _fuzz_value(rng: random.Random, depth: int) -> object:
    roll = rng.random()
    if depth <= 0 or roll < 0.55:
        return rng.choice(_FUZZ_SCALARS)
    if roll < 0.75:
        return [_fuzz_value(rng, depth - 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(_FUZZ_KEYS): _fuzz_value(rng, depth - 1) for _ in range(rng.randint(0, 3))}


def fuzz_payload(rng: random.Random) -> bytes:
    if rng.random() < 0.12:
        obj: object = _fuzz_value(rng, 3)  # arbitrary top-level JSON type
    else:
        obj = {}
        if rng.random() < 0.6:
            obj.update(copy.deepcopy(rng.choice(TOOL_PAYLOADS)[1]))  # type: ignore[union-attr]
        for _ in range(rng.randint(0, 4)):
            obj[rng.choice(_FUZZ_KEYS)] = _fuzz_value(rng, 2)  # type: ignore[index]
        if rng.random() < 0.5:
            obj["agent_type"] = rng.choice(_FUZZ_AGENT_VALUES)  # type: ignore[index]
    text = json.dumps(obj, ensure_ascii=rng.random() < 0.5)
    if rng.random() < 0.15:
        text = text.replace('"agent_type"', '"agent\\u005ftype"')
    raw = text.encode("utf-8")
    roll = rng.random()
    if roll < 0.08:
        raw = raw[: rng.randint(0, len(raw))]
    elif roll < 0.14 and raw:
        i = rng.randrange(len(raw))
        raw = raw[:i] + bytes([rng.randrange(256)]) + raw[i + 1:]
    elif roll < 0.17:
        raw = b"\xef\xbb\xbf" + raw
    elif roll < 0.20:
        raw = raw + b" trailing"
    return raw


def fuzz_corpus(n: int, seed: int) -> list[Case]:
    rng = random.Random(seed)
    return [(f"fuzz-{seed}-{i}", fuzz_payload(rng)) for i in range(n)]


def record_existing_suite_payloads() -> list[Case]:
    """Every payload the existing tools/test_worker_guard.py suite hands to decide(), verbatim.

    Recorded by spying on decide() while the real suite runs, so none of it is hand-invented and
    it tracks the suite as the suite grows. Raises if the suite itself is red.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools import test_worker_guard as twg  # noqa: PLC0415

    seen: dict[str, None] = {}
    original = twg.worker_guard.decide

    def spy(data):
        try:
            seen.setdefault(json.dumps(data, sort_keys=True), None)
        except (TypeError, ValueError):
            pass
        return original(data)

    twg.worker_guard.decide = spy
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(twg)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        twg.worker_guard.decide = original
    if not result.wasSuccessful():
        raise RuntimeError(f"tools.test_worker_guard is red ({len(result.failures)}F/{len(result.errors)}E); fix it first")
    return [(f"recorded-{i}", s.encode("utf-8")) for i, s in enumerate(seen)]


# --- comparison ---------------------------------------------------------------------------------


@dataclasses.dataclass
class Comparison:
    total: int
    denies: int
    mismatches: list[str]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _describe(o: Outcome) -> str:
    return f"rc={o.rc} out={o.out[:160]!r} err={o.err[:160]!r}"


# T-5817: the guard's own fail-closed clock (T-4389 DECIDE_DEADLINE_S, counted from process start)
# is real time, so on a loaded host a run in the 8-wide pool can hit it and print this deny while
# its twin did not: orchestrator's soak, 2026-11-01, 'fuzz-4177-1: ref[... T-4389 ... timed out] !=
# new[allow]'. That is the host, not a decision difference. Such a pair is re-run one case at a
# time; it still has to come out byte-identical, and one that keeps timing out stays a mismatch.
TIMED_OUT_MARKER = b"BLOCKED (worker-guard, T-4389): worker-guard timed out deciding"
TIMEOUT_RERUNS = 3


def _timed_out(o: Outcome) -> bool:
    return TIMED_OUT_MARKER in o.out


def _both_timed_out_alike(r: Outcome, n: Outcome) -> bool:
    return r == n and _timed_out(r)


def compare_scripts(ref: Path, new: Path, cases: list[Case], workers: int = 8,
                    timeout_reruns: int = TIMEOUT_RERUNS) -> Comparison:
    """Run `ref` and `new` over `cases`; (rc, stdout, stderr) must be byte-identical for every case.
    A pair where either side hit the guard's T-4389 deadline is re-run serially, up to
    `timeout_reruns` times (T-5817), until a re-run decides it with neither side timed out.

    T-5838: two identical timeouts are not a decision, so they cannot clear a pair that once
    differed: a pair first seen as a one-sided timeout passes only on a re-run that matches with
    neither side timed out. A pair that timed out on BOTH sides alike in the pool and never
    showed a difference stays a match if its re-runs keep timing out alike (both sides fail
    closed the same way; counting it would turn every slow-host run red)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        ref_out = list(pool.map(lambda c: run_script(ref, c[1]), cases))
        new_out = list(pool.map(lambda c: run_script(new, c[1]), cases))
    masked: dict[int, tuple[Outcome, Outcome]] = {}
    for i, (_label, raw) in enumerate(cases):
        r, n = ref_out[i], new_out[i]
        first_diff = None if _both_timed_out_alike(r, n) else (r, n)
        for _ in range(timeout_reruns):
            if not (_timed_out(r) or _timed_out(n)):
                break
            r, n = run_script(ref, raw), run_script(new, raw)
            if first_diff is None and r != n:
                first_diff = (r, n)
        ref_out[i], new_out[i] = r, n
        if first_diff is not None and _both_timed_out_alike(r, n):
            masked[i] = first_diff
    mismatches = []
    for i, ((label, _raw), r, n) in enumerate(zip(cases, ref_out, new_out)):
        if i in masked:
            fr, fn = masked[i]
            mismatches.append(f"{label}: ref[{_describe(fr)}] != new[{_describe(fn)}], and every "
                              f"re-run timed out on both sides (T-4389), so it was never cleared")
        elif r != n:
            mismatches.append(f"{label}: ref[{_describe(r)}] != new[{_describe(n)}]")
    return Comparison(len(cases), sum(r.is_deny() for r in ref_out), mismatches)


def load_shim(hooks_dir: Path = DEFAULT_HOOKS_DIR):
    """Import the shim as a module (its work sits under `if __name__ == "__main__"`, so this is inert)."""
    spec = importlib.util.spec_from_file_location("worker_guard_shim", hooks_dir / SHIM_NAME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body_identity_problems(old_src: str, impl_src: str) -> list[str]:
    """The impl must equal the old guard modulo the module docstring and main()."""

    def normalise(src: str) -> list[str]:
        body = ast.parse(src).body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        return [ast.dump(n) for n in body if not (isinstance(n, ast.FunctionDef) and n.name == "main")]

    old_nodes, new_nodes = normalise(old_src), normalise(impl_src)
    if old_nodes == new_nodes:
        return []
    return [f"impl differs from the old guard outside main()/docstring: {len(old_nodes)} vs {len(new_nodes)} top-level nodes"]


# --- steps ----------------------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


def resolve_old_guard(old_ref: str | None, dest: Path) -> tuple[Path, str]:
    """Write the pre-shim guard to `dest`; return (path, resolved-sha). Refuses a ref that is already the shim.

    With no explicit ref: origin/main, falling back to PRE_SHIM_SHA once origin/main has itself
    become the shim (post-merge re-runs).
    """
    candidates = [old_ref] if old_ref else ["origin/main", PRE_SHIM_SHA]
    for ref in candidates:
        blob = _git("show", f"{ref}:tools/hooks/{SHIM_NAME}")
        if blob.returncode != 0:
            continue
        if SHIM_MARKER in blob.stdout:
            if old_ref:
                raise RuntimeError(f"--old-ref {old_ref} already holds the shim, not the monolith")
            continue
        dest.write_text(blob.stdout, encoding="utf-8")
        sha = _git("rev-parse", ref).stdout.strip() or ref
        return dest, sha
    raise RuntimeError("could not read a pre-shim tools/hooks/worker-guard.py from git (fetch, or pass --old-ref)")


def rollback_command(sha: str, live_repo: str = "~/pantheon-config") -> str:
    """Atomic (temp file + rename) restore of the pre-shim guard at the LIVE hook path.

    Names the live clone, not the worktree this script runs from: the wired command is
    `<seat home>/pantheon-config/tools/hooks/worker-guard.py`. The sha must be in that clone's
    object store (it is, once the pull that brought the shim has fetched history).
    """
    tmp = "tools/hooks/.wg-rollback.tmp"
    return (
        f"cd {live_repo} && git show {sha}:tools/hooks/{SHIM_NAME} > {tmp} && chmod 755 {tmp} "
        f"&& mv {tmp} tools/hooks/{SHIM_NAME}"
    )


_MAIN_READ = {**_HARNESS_FIELDS, "cwd": "/home/claude", "permission_mode": "default",
              "transcript_path": "/home/claude/.claude/projects/-home-claude/s.jsonl",
              "tool_name": "Read", "tool_input": {"file_path": "/home/claude/pantheon-config/tools/hooks/README.md"}}
_SUB_READ = {**_MAIN_READ, "agent_id": "a1b2c3", "agent_type": "implementer"}

LIVE_PROBES: list[tuple[str, dict, str]] = [
    ("main-thread Read", {"tool_name": "Read", "tool_input": {"file_path": "/home/claude/root.env"}}, "allow"),
    ("main-thread Grep", {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "/home/claude/root.env"}}, "allow"),
    ("subagent Read of root.env", {"agent_type": "implementer", "tool_name": "Read", "tool_input": {"file_path": "/home/claude/root.env"}}, "deny"),
    ("subagent Read of ordinary file", {"agent_type": "implementer", "tool_name": "Read", "tool_input": {"file_path": "/tmp/ordinary.txt"}}, "allow"),
]


def check_layout(hooks_dir: Path) -> list[str]:
    problems = []
    shim, impl = hooks_dir / SHIM_NAME, hooks_dir / IMPL_NAME
    for path in (shim, impl):
        if not path.is_file():
            problems.append(f"missing {path}")
    if shim.is_file() and not os.access(shim, os.X_OK):
        problems.append(f"{shim} is not executable")
    if shim.is_file() and SHIM_MARKER not in shim.read_text(encoding="utf-8"):
        problems.append(f"{shim} does not reference {SHIM_MARKER}: still the monolith?")
    return problems


def report_pyc(hooks_dir: Path) -> str:
    """Informational: can this account cache the impl's bytecode? (silent recompile if not.)"""
    cache = hooks_dir / "__pycache__"
    target = cache if cache.is_dir() else hooks_dir
    writable = os.access(target, os.W_OK)
    dont = bool(os.environ.get("PYTHONDONTWRITEBYTECODE")) or sys.dont_write_bytecode
    cached = importlib.util.cache_from_source(str(hooks_dir / IMPL_NAME))
    return (f"{target} writable={'yes' if writable else 'NO'}; PYTHONDONTWRITEBYTECODE={'set' if dont else 'unset'}; "
            f"impl pyc present={'yes' if os.path.exists(cached) else 'no'} "
            f"(a non-writable dir or the env var just means the impl recompiles per subagent call, as before)")


def run_probes(shim: Path) -> list[str]:
    problems = []
    for label, payload, expect in LIVE_PROBES:
        out = run_script(shim, _dump(payload))
        if out.rc != 0 or out.err:
            problems.append(f"{label}: rc={out.rc} err={out.err[:200]!r}")
        elif expect == "deny" and not out.is_deny():
            problems.append(f"{label}: expected deny, got {out.out[:120]!r}")
        elif expect == "allow" and out.out:
            problems.append(f"{label}: expected silent allow, got {out.out[:120]!r}")
    return problems


def run_isolation(shim: Path) -> list[str]:
    """Shim alone in a temp dir: main-thread => silent allow (impl never imported); subagent => fail loud."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="wg-shim-alone-") as tmp:
        lone = Path(tmp) / SHIM_NAME
        lone.write_text(shim.read_text(encoding="utf-8"), encoding="utf-8")
        for label, payload in (("main Read", _MAIN_READ), ("main Bash", {"tool_name": "Bash", "tool_input": {"command": "git push"}})):
            out = run_script(lone, _dump(payload))
            if out != Outcome(0, b"", b""):
                problems.append(f"{label} without the impl on disk: {_describe(out)} (the fast path imports the impl?)")
        sub = run_script(lone, _dump(_SUB_READ))
        if sub.rc != 1 or b"worker_guard_impl" not in sub.err:
            problems.append(f"subagent without the impl on disk should fail loud (rc 1, ImportError): {_describe(sub)}")
    return problems


def run_migration(shim: Path, impl: Path, old_ref: str | None, fuzz_n: int, seed: int, workers: int) -> tuple[list[str], list[str], str]:
    """Returns (problems, info-lines, resolved-old-sha)."""
    problems: list[str] = []
    info: list[str] = []
    with tempfile.TemporaryDirectory(prefix="wg-old-guard-") as tmp:
        old, sha = resolve_old_guard(old_ref, Path(tmp) / "old_worker_guard.py")
        info.append(f"old guard = tools/hooks/{SHIM_NAME} @ {sha[:12]}")
        problems += body_identity_problems(old.read_text(encoding="utf-8"), impl.read_text(encoding="utf-8"))
        curated = curated_corpus(full=True)
        recorded = record_existing_suite_payloads()
        fuzz = fuzz_corpus(fuzz_n, seed)
        info.append(f"corpus: {len(curated)} curated + {len(recorded)} recorded-from-existing-suite + {len(fuzz)} fuzz(seed={seed})")
        # impl-as-script is not compared to the old guard here: body_identity_problems() above proves
        # the impl body identical (AST) and the permanent unit test proves shim == impl-as-script.
        for name, cases in (("curated", curated), ("recorded", recorded), ("fuzz", fuzz)):
            cmp = compare_scripts(old, shim, cases, workers)
            info.append(f"  {name:<9} old vs shim {cmp.total:>5} payloads, {cmp.denies:>4} deny, {len(cmp.mismatches)} mismatches")
            problems += [f"[{name}] {m}" for m in cmp.mismatches[:5]]
    return problems, info, sha


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def run_bench(variants: dict[str, tuple[list[str], bytes]], rounds: int) -> list[str]:
    """Interleaved rounds (start position rotates) so drifting load hits every variant alike."""
    names = list(variants)
    wall: dict[str, list[float]] = {n: [] for n in names}
    cpu: dict[str, list[float]] = {n: [] for n in names}
    load_before = os.getloadavg()
    for r in range(rounds):
        k = r % len(names)
        for name in names[k:] + names[:k]:
            argv, stdin = variants[name]
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            t0 = time.perf_counter()
            subprocess.run(argv, input=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = (time.perf_counter() - t0) * 1000
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            wall[name].append(elapsed)
            cpu[name].append(((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)) * 1000)
    load_after = os.getloadavg()
    width = max(len(n) for n in names)
    lines = [f"bench: {rounds} interleaved rounds; loadavg before={load_before[0]:.2f} after={load_after[0]:.2f} (1-min)",
             f"  {'variant':<{width}}  wall p50   p90   min | child-CPU p50  (ms)"]
    for n in names:
        lines.append(f"  {n:<{width}}  {statistics.median(wall[n]):8.1f} {percentile(wall[n], 0.9):5.1f} {min(wall[n]):5.1f} | {statistics.median(cpu[n]):8.1f}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hooks-dir", type=Path, default=DEFAULT_HOOKS_DIR, help="dir holding worker-guard.py + worker_guard_impl.py (default: this checkout)")
    ap.add_argument("--old-ref", help="git ref holding the pre-shim guard (default: origin/main, else the pinned pre-shim sha)")
    ap.add_argument("--migration", action="store_true", help="also run the one-time old-vs-new corpus comparison (cutover only; see the docstring)")
    ap.add_argument("--skip-migration", action="store_true", help="accepted for old command lines; migration is already off by default (T-5324)")
    ap.add_argument("--live-repo", default="~/pantheon-config", help="clone named in the printed rollback command (default ~/pantheon-config)")
    ap.add_argument("--fuzz", type=int, default=400, help="fuzz payload count (default 400)")
    ap.add_argument("--seed", type=int, default=4177)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--bench", type=int, default=0, metavar="N", help="also run N interleaved benchmark rounds")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # a redirected log shows progress as steps finish

    hooks_dir = args.hooks_dir.expanduser().resolve()
    shim, impl = hooks_dir / SHIM_NAME, hooks_dir / IMPL_NAME
    failures = 0

    def step(name: str, problems: list[str], info: list[str] | None = None) -> None:
        nonlocal failures
        print(f"{'FAIL' if problems else 'PASS'}  {name}")
        for line in info or []:
            print(f"      {line}")
        for p in problems:
            print(f"      ! {p}")
        failures += bool(problems)

    print(f"worker-guard cutover smoke: hooks-dir={hooks_dir} python={sys.version.split()[0]} loadavg={os.getloadavg()[0]:.2f}")
    layout = check_layout(hooks_dir)
    step("layout", layout, [report_pyc(hooks_dir)] if not layout else None)
    if layout:
        return 1
    step("probes (T-4139 live-probe shapes)", run_probes(shim))
    step("isolation (fast path never imports the impl; broken impl fails loud)", run_isolation(shim))
    old_sha = PRE_SHIM_SHA
    if args.migration and not args.skip_migration:
        try:
            problems, info, old_sha = run_migration(shim, impl, args.old_ref, args.fuzz, args.seed, args.workers)
        except (RuntimeError, OSError) as exc:
            problems, info = [str(exc)], []
        step("migration (old guard vs new, byte-identical rc/stdout/stderr)", problems, info)
        print(f"      pyc after the runs: {report_pyc(hooks_dir)}")
    if args.bench:
        variants: dict[str, tuple[list[str], bytes]] = {
            "bare python3 -c pass": ([sys.executable, "-c", "pass"], b""),
            "new shim, main-thread Read": ([sys.executable, str(shim)], _dump(_MAIN_READ)),
            "new shim, subagent Read": ([sys.executable, str(shim)], _dump(_SUB_READ)),
        }
        with tempfile.TemporaryDirectory(prefix="wg-old-bench-") as old_dir:
            try:
                old_path, old_sha = resolve_old_guard(args.old_ref, Path(old_dir) / "old_worker_guard.py")
                variants["old guard, main-thread Read"] = ([sys.executable, str(old_path)], _dump(_MAIN_READ))
                variants["old guard, subagent Read"] = ([sys.executable, str(old_path)], _dump(_SUB_READ))
            except (RuntimeError, OSError) as exc:
                print(f"      (no old-guard variants in the benchmark: {exc})")
            for line in run_bench(variants, args.bench):
                print(line)
    print()
    print("ROLLBACK (atomic restore of the pre-shim guard at the live path; leaves the impl file orphaned, harmless):")
    print(f"  {rollback_command(old_sha, args.live_repo)}")
    print(f"RESULT: {'FAIL' if failures else 'PASS'} ({failures} failing step(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
