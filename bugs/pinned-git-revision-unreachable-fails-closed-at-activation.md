---
symptom: "A source-policy binding pinning a git revision hash (revision + git_blob + sha256 + byte_length) contains a fabricated or otherwise unreachable revision. The blob/sha256/byte_length may still be correct — but the revision doesn't exist in the repo's object graph. At activation time (SessionStart / resume / clear / compact), `_git_object_bytes()` runs `git rev-parse \"{revision}:{path}\"` literally on the pinned field and raises `ValueError('source git object unavailable')` — deploy fails closed every lifecycle event, indefinitely."
fix: "Ship the source-policy binding with a standing test that runs `git merge-base --is-ancestor <revision> HEAD` + `git rev-parse <revision>:<path>` as real subprocess calls (check=True) and asserts the resolved blob matches the pinned `git_blob`. This is a build-time regression guard against any future fabricated/stale revision — the test fails CI immediately instead of the defect reaching an activation-time crash. When updating the pinned revision (e.g. after AGENTS.md content changes), recompute POC_SOURCE_POLICY_MANIFEST_SHA256 from the real tree — it's derived, don't hand-edit."
domain: pantheon, codex, source-policy, hash-chain, git-object-binding, activation-lifecycle, persona-orientation
---

## Symptom

A source-policy binding in codex-config `codex_telegram/persona_orientation.py` (or any analogous `PoCSourcePolicy(source_bindings=[...])` structure) has a `SourceBinding.revision` field that doesn't exist in the repo's object graph:

```python
SourceBinding(
    source_id="codex-config-agents",
    repository="codex-config",
    relative_path="AGENTS.md",
    revision="366b03bda35efebedc5baba21d081e4955368d79",  # ← NONEXISTENT COMMIT
    git_blob="77a0307d51b56d39be53c1986ac392bc45d49a5f",   # ← CORRECT
    sha256="21fd2d662cead1486a2091eddc425c19b5b0499032c798ab6bef19feeea84cf6",  # ← CORRECT
    byte_length=7324,                                       # ← CORRECT
    required=True,
    load_mechanism="git-object",
)
```

At activation time (SessionStart / resume / clear / compact / any lifecycle event that runs `collect_source_bindings()`), `_git_object_bytes()` runs the pinned revision literally:

```
$ git -C /home/codex/codex-config rev-parse "366b03bda35efebedc5baba21d081e4955368d79:AGENTS.md"
fatal: path 'AGENTS.md' exists on disk, but not in '366b03bda35efebedc5baba21d081e4955368d79'
```

Raised as:

```
ValueError: source git object unavailable
```

Deploy fails closed every lifecycle event — the orchestrator-identity activation the source-policy binding exists to deliver would fail every time. Not intermittent: deterministic on every activation.

## Cause

`SourceBinding.revision`, `SourceBinding.git_blob`, `SourceBinding.sha256`, `SourceBinding.byte_length`, and `POC_SOURCE_POLICY_MANIFEST_SHA256` are pinned literals in code. When the underlying source content changes (e.g. AGENTS.md is rewritten in the same commit that updates the manifest), all four blob-derived fields must be regenerated to match the new content, AND `revision` must point to a real reachable commit (typically the same commit that introduced the new blob, or an ancestor of HEAD reachable in the target repo).

The v2 T-3338 defect: `git_blob` (77a0307d…), `sha256` (21fd2d66…), and `byte_length` (7324) were correctly regenerated from the actual new content — but `revision` was set to a fabricated 40-char hex string that doesn't exist anywhere in codex-config's object graph. The real commit that introduced the blob was `f60bfdd0d5be4824351bff44cf22c42f1e45578a` (Cairn's own docs-commit within the v2 bundle). Cause of the fabrication is unclear — a manual typo, a copy-paste from stale scratch, or an automation misfire; unrecoverable from the diff alone.

The manifest-level `POC_SOURCE_POLICY_MANIFEST_SHA256` is *derived* (computed via `policy_manifest_sha256(POC_SOURCE_POLICY)`), not independent — so it can be recomputed correctly *even from a fabricated revision*: the manifest-hash validation passes even when the underlying revision is nonexistent, because the manifest-hash covers the LITERAL contents of the source-policy structure, not the repo-reachability of the fields inside it. This is why the defect is not caught by `validate_policy_manifest()` alone.

## Fix

Ship the source-policy binding module (or an adjacent test file) with a standing reachability test that runs real git plumbing against the actual repo:

```python
class PolicySourceFalsifiers(unittest.TestCase):
    def test_codex_config_binding_is_reachable_and_resolves_expected_blob(self):
        repo = Path(__file__).resolve().parents[1]  # or actual repo root
        by_id = {
            source.source_id: source
            for source in po.POC_SOURCE_POLICY.source_bindings
        }
        binding = by_id["codex-config-agents"]

        # Fails if revision doesn't exist / is not reachable from HEAD.
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", binding.revision, "HEAD"],
            cwd=repo,
            check=True,
        )

        # Fails if revision:path doesn't resolve to expected blob.
        resolved = subprocess.run(
            ["git", "rev-parse", f"{binding.revision}:{binding.relative_path}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(resolved, binding.git_blob)
```

Because `check=True`, `subprocess.run()` raises on non-zero exit — a fabricated/unreachable revision fails on the `merge-base --is-ancestor` call before ever reaching the blob-match assertion. A stale-but-reachable revision that resolves to a different blob fails on the assertion. Together the two checks cover the class.

When you update a pinned revision (e.g. after AGENTS.md content changes):

1. Regenerate `git_blob` from the actual new blob (`git rev-parse HEAD:AGENTS.md`).
2. Regenerate `sha256` and `byte_length` from the actual bytes.
3. Set `revision` to a real reachable commit — typically the commit that introduces the new content, verified via `git rev-parse <sha>^{commit}` before committing.
4. Recompute `POC_SOURCE_POLICY_MANIFEST_SHA256` via `policy_manifest_sha256(POC_SOURCE_POLICY)` — it's derived, don't hand-edit.

## Verification

To reproduce the failure with any candidate binding in a checkout:

```bash
python3 -c "
from codex_telegram import persona_orientation as po
import subprocess, pathlib
repo = pathlib.Path('/path/to/target/repo')  # e.g. /home/codex/codex-config
by_id = {s.source_id: s for s in po.POC_SOURCE_POLICY.source_bindings}
for sid, s in by_id.items():
    try:
        subprocess.run(['git', '-C', str(repo), 'rev-parse', f'{s.revision}:{s.relative_path}'],
                       check=True, capture_output=True)
        print(f'{sid}: OK ({s.revision[:8]}:{s.relative_path})')
    except subprocess.CalledProcessError as e:
        print(f'{sid}: FAIL ({s.revision[:8]}:{s.relative_path}) -- {e.stderr.decode().strip()}')
"
```

A bugged binding prints `FAIL ... fatal: path '<path>' exists on disk, but not in '<revision>'`. A correct binding prints `OK`.

## Discovered

- 2026-08-30 T-3338 v2 REBOUND (postimpl reviewer `rev-t3338-v2`). v2 bundle head 9e033d2 added persona/role identity work including AGENTS.md rewrite and a manifest revision update in `codex_telegram/persona_orientation.py:236`. Reviewer independently ran `git cat-file -t 366b03bda35efebedc5baba21d081e4955368d79` against the full-history clone → "could not get object info". Cross-checked: the real commit that introduced the blob was `f60bfdd0d5be4824351bff44cf22c42f1e45578a` (bundle-internal). Standing reachability test was recommended as HO-tier followup, adopted in v3 fix commit `fe119d4` as `test_codex_config_binding_is_reachable_and_resolves_expected_blob` — CI-guard now in place for this specific binding; the pattern generalizes to any `SourceBinding` in any future `PoCSourcePolicy`.

- Full v2 verdict: `/tmp/claude-1000/-home-claude/9554c38b-318c-4867-be3f-29391913b770/scratchpad/t3338-v2-review-verdict.md` (session-local, non-durable). Ticket-side annotations at `tickets/done/T-3338.md` when Cairn resolves — search for "V2 POSTIMPL REVIEWER" and "V3 POSTIMPL REVIEWER APPROVE" for the audit chain.

Related pins: [[reference_cross_persona_identity_surface_review_via_bundle]] (how these reviews get done cross-account).
