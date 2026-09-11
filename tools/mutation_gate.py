#!/usr/bin/env python3
"""tools/mutation_gate.py — targeted-mutation gate for Amendment H falsification.

Purpose: prove that a test's green depends on specific validation-code lines in
the impl, not on the impl file's mere existence. Closes the T-3341 Arc B gap
where whole-file-stash Amendment H falsification passed without proving the
test asserted anything about the validation code path.

Corpus: this is a scoped mutation-testing tool per Lipton 1971 / PITest /
Stryker. It does not implement full AST-mutation-operator coverage — it does
the ONE thing Amendment H needs: line-targeted removal + verify-red.

Usage:
    mutation_gate.py --source <file> --test-cmd '<shell>' --lines N,M,K
    mutation_gate.py --source <file> --test-cmd '<shell>' --diff <commit>
    mutation_gate.py --source <file> --test-cmd '<shell>' --auto

Exit: 0 if every targeted mutation was KILLED (test suite went red for each);
      1 if any mutation SURVIVED (test suite stayed green — a gap).

The three targeting modes:
- --lines N,M,K — explicit line numbers in the source file
- --diff <ref>  — mutate every non-blank/non-comment line in the diff of
                  <ref>..HEAD on the source file (the "test the fix" mode)
- --auto        — mutate every statement-line in the source file
                  (comprehensive scan; slow)

Design notes:
- Line-based, not AST-based. Simplicity wins for the Amendment H use case:
  we're not trying to catch every subtle mutation; we're trying to prove the
  test doesn't just pass on file-existence.
- Sequential (not parallel) — clean state matters more than throughput for a
  falsification gate. If the test suite is slow, use --lines instead of
  --auto to target the specific lines you care about.
- Restores the file even on Ctrl-C / crash via try/finally + shutil.copy2 of
  the original. No git required; works on any editable file.
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MutationResult:
    line_number: int
    original_line: str
    mutation_kind: str  # "commented-out" or "removed"
    test_outcome: str   # "red" (killed) or "green" (survived)
    test_stderr_tail: str = ""

    @property
    def killed(self) -> bool:
        return self.test_outcome == "red"


def run_test(test_cmd: str, cwd: Path | None = None) -> tuple[bool, str]:
    """Return (is_green, stderr_tail). is_green=True iff exit code 0."""
    # T-3462: PYTHONDONTWRITEBYTECODE=1 prevents new .pyc writes so stale
    # bytecode from an EARLIER iteration's mutation cannot be picked up by a
    # LATER iteration's "restored" source when both share the same mtime
    # (shutil.copy2 preserves mtime, so restored-to-original.mtime matches a
    # cached mutated pyc's source_mtime). Paired with restore()'s os.utime
    # bump: even if a .pyc IS present, source.mtime advances monotonically
    # across iterations and invalidates any historical pyc.
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        test_cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    tail = (result.stderr or result.stdout).splitlines()[-20:]
    return result.returncode == 0, "\n".join(tail)


def is_mutable_line(line: str) -> bool:
    """Cheap heuristic: skip blank lines, comments, decorators, closing brackets, docstring boundaries."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith("@"):
        return False
    if stripped in ("...", "pass", '"""', "'''", ")", "}", "]"):
        return False
    return True


def load_line_numbers(source: Path, mode: str, arg: str) -> list[int]:
    lines = source.read_text().splitlines()
    if mode == "lines":
        return [int(x) for x in arg.split(",") if x.strip()]
    if mode == "auto":
        return [i + 1 for i, line in enumerate(lines) if is_mutable_line(line)]
    if mode == "diff":
        # git diff <ref>..HEAD -- source, extract added/context lines' numbers
        # For prototype: use git blame + git diff --unified=0 to identify changed lines
        result = subprocess.run(
            ["git", "diff", "--unified=0", f"{arg}..HEAD", "--", str(source)],
            capture_output=True, text=True, check=True,
        )
        line_nums = []
        current_line = 0
        for diff_line in result.stdout.splitlines():
            if diff_line.startswith("@@"):
                # @@ -old,c +new,c @@ — parse new-file line-start
                try:
                    new_part = diff_line.split("+", 1)[1].split(" ", 1)[0]
                    current_line = int(new_part.split(",")[0])
                except (IndexError, ValueError):
                    continue
            elif diff_line.startswith("+") and not diff_line.startswith("+++"):
                if current_line <= len(lines) and is_mutable_line(lines[current_line - 1]):
                    line_nums.append(current_line)
                current_line += 1
            elif diff_line.startswith(" "):
                current_line += 1
        return line_nums
    raise ValueError(f"unknown targeting mode: {mode}")


def mutate_line(source: Path, line_num: int, backup: Path) -> tuple[str, str]:
    """Mutate one line in-place. Returns (original_line, mutation_kind).

    mutation_kind is "commented-out" when a simple comment substitution keeps
    the file parse-able, else "replaced-with-pass" when the line was the sole
    body of a block and a bare `pass` had to be substituted to preserve syntax.
    A rare "unmutable" is returned when neither form parses (e.g. the line is
    a top-level `def` whose removal orphans downstream references — treat as
    "structural, skip").
    """
    lines = source.read_text().splitlines(keepends=True)
    original = lines[line_num - 1]
    stripped = original.lstrip()
    indent = original[: len(original) - len(stripped)]
    line_end = "\n" if original.endswith("\n") else ""

    # First attempt: comment out.
    trial_comment = f"{indent}# MUTATED-OUT: {stripped.rstrip()}{line_end}"
    lines[line_num - 1] = trial_comment
    trial_text = "".join(lines)
    if _parses(trial_text):
        source.write_text(trial_text)
        return original, "commented-out"

    # Second attempt: bare `pass` (for block-body-only lines).
    trial_pass = f"{indent}pass  # MUTATED-OUT: {stripped.rstrip()}{line_end}"
    lines[line_num - 1] = trial_pass
    trial_text = "".join(lines)
    if _parses(trial_text):
        source.write_text(trial_text)
        return original, "replaced-with-pass"

    # Give up on this line; restore + signal.
    source.write_text("".join(lines[: line_num - 1] + [original] + lines[line_num:]))
    return original, "unmutable"


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def restore(source: Path, backup: Path) -> None:
    shutil.copy2(backup, source)
    # T-3462: bump source.mtime to now so it monotonically advances across
    # bulk-mode iterations. shutil.copy2 preserves the backup's mtime, which
    # after restore matches the ORIGINAL mtime — that made any historical
    # .pyc from an earlier iteration's mutation "valid" for the CURRENT
    # iteration's "restored" source, letting the earlier mutated bytecode
    # get loaded when pytest should have re-compiled from the restored
    # source. utime(source, None) advances mtime past any cached pyc's
    # source_mtime, invalidating it. Paired with PYTHONDONTWRITEBYTECODE=1
    # in run_test (belt-and-suspenders).
    os.utime(source, None)


def gate(source: Path, test_cmd: str, line_numbers: list[int]) -> list[MutationResult]:
    """Run the mutation gate. Baseline must be green."""
    backup = source.with_suffix(source.suffix + ".mutation-backup")
    shutil.copy2(source, backup)
    results: list[MutationResult] = []
    try:
        # Baseline: must be green before we start mutating
        baseline_green, baseline_tail = run_test(test_cmd)
        if not baseline_green:
            print(f"[FATAL] baseline test suite is RED; cannot run mutation gate.", file=sys.stderr)
            print(f"        last 20 lines of output:\n{baseline_tail}", file=sys.stderr)
            sys.exit(2)
        print(f"[baseline] green — proceeding with {len(line_numbers)} mutations")

        for i, line_num in enumerate(line_numbers, 1):
            original, kind = mutate_line(source, line_num, backup)
            if kind == "unmutable":
                print(f"  [{i}/{len(line_numbers)}] line {line_num}: ⚪-UNMUTABLE — {original.strip()[:60]}")
                continue
            is_green, tail = run_test(test_cmd)
            restore(source, backup)
            outcome = "green" if is_green else "red"
            results.append(MutationResult(
                line_number=line_num,
                original_line=original.rstrip("\n"),
                mutation_kind=kind,
                test_outcome=outcome,
                test_stderr_tail=tail if is_green else "",
            ))
            symbol = "🟢-SURVIVED" if is_green else "🔴-KILLED"
            kind_tag = "" if kind == "commented-out" else f" [{kind}]"
            print(f"  [{i}/{len(line_numbers)}] line {line_num}: {symbol}{kind_tag} — {original.strip()[:60]}")
    finally:
        restore(source, backup)
        backup.unlink(missing_ok=True)
    return results


def report(results: list[MutationResult]) -> int:
    total = len(results)
    killed = sum(1 for r in results if r.killed)
    survived = total - killed
    print(f"\n=== MUTATION GATE REPORT ===")
    print(f"Total mutations: {total}")
    print(f"Killed (test detected):   {killed} ({100*killed//max(total,1)}%)")
    print(f"Survived (test missed):   {survived} ({100*survived//max(total,1)}%)")
    if survived:
        print(f"\nSURVIVORS (test did not detect these mutations — GAP):")
        for r in results:
            if not r.killed:
                print(f"  line {r.line_number}: {r.original_line.strip()}")
        print(f"\nEXIT 1 — Amendment H falsification gap. The test's green does not depend on {survived} of {total} mutated lines.")
        return 1
    print(f"\nEXIT 0 — every targeted line's removal broke the test suite. Amendment H falsification is behavior-bound.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, type=Path, help="source file to mutate")
    ap.add_argument("--test-cmd", required=True, help="shell command to run the test suite")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--lines", help="comma-separated line numbers to mutate")
    target.add_argument("--diff", help="git ref; mutate lines added in <ref>..HEAD")
    target.add_argument("--auto", action="store_true", help="mutate every statement-like line")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"source file not found: {args.source}", file=sys.stderr)
        return 2

    mode = "lines" if args.lines else ("diff" if args.diff else "auto")
    arg = args.lines if args.lines else (args.diff if args.diff else "")
    line_numbers = load_line_numbers(args.source, mode, arg)
    if not line_numbers:
        print("[FATAL] no mutable lines found for targeting mode:", mode, file=sys.stderr)
        return 2

    results = gate(args.source, args.test_cmd, line_numbers)
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
