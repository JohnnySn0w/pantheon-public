"""ORG-083 coverage adapter: the unchanged-test mutation driver and receipts (T-5971, proposal 10a / 12.5).

NOT collected by `pytest tools/`. Run explicitly, with the tests exactly as committed:

    nice -n 10 python3 tools/org083_coverage_adapter_assets/tests/mutants.py --out <fresh dir outside the repo> [--only ID ...]

For every mutant: a TEXTUAL patch is applied to a TEMP COPY of tools/org083_coverage_adapter.py (the
committed file is never edited; its sha256 is recorded before and after), the mutant's tests run against
that copy (ORG083_ADAPTER_UNDER_TEST), and the mutant is KILLED only if at least one targeted test FAILS
BY ASSERTION (a collection or import error does not count). A batch run of all targeted tests against the
unmutated adapter then records the restored green and the RA-1..RA-6 results of every row test.
Receipts (one JSON per mutant plus summary.json) go to <OUT>, are reported to the orchestrator and are NOT
committed (evidence under knowledge/episodic belongs to BASELINE-PC).
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ADAPTER = REPO / "tools" / "org083_coverage_adapter.py"
ASSETS = REPO / "tools" / "org083_coverage_adapter_assets"
REF = "tools/test_org083_coverage_adapter_refusals.py"
SEM = "tools/test_org083_coverage_adapter_semantics.py"


def row(*ids: str) -> list[str]:
    return [f"{REF}::test_refusal_row[{i}]" for i in ids]


def sem(*names: str) -> list[str]:
    return [f"{SEM}::{n}" for n in names]


def ref(*names: str) -> list[str]:
    return [f"{REF}::{n}" for n in names]


@dataclass
class Mutant:
    id: str
    hunks: list[tuple[str, str]]
    tests: list[str]
    note: str = ""


def off(guard: str) -> tuple[str, str]:
    """Turn `<guard>` off by prefixing `False and` (guards of the form `if X and not seams.off(...)`)."""
    return guard, guard.replace("if ", "if False and ", 1)


def kill(old: str, new: str) -> tuple[str, str]:
    return old, new


M: list[Mutant] = []


def add(id_, hunks, tests, note=""):
    M.append(Mutant(id_, hunks if isinstance(hunks, list) else [hunks], tests, note))


# ---- section 10a rows ---------------------------------------------------------------------------
add("R1a", off('    if pytest_rc != 0 and not seams.off("R1a"):'), row("R1a"), "drop the rc check")
add("R1b", off('    if failures and not seams.off("R1b"):'), row("R1b"), "count failures as passes")
add("R1c", kill('errors = sum(1 for c in root.iter("testcase") if c.find("error") is not None)', "errors = 0"), row("R1c"),
    "count only failure, not error")
add("R2a", kill('        if not seams.off("R2a"):', "        if False:"), row("R2a"), "treat a missing report as zero tests, not a refusal")
add("R2b", kill('        elif not seams.off("R2b"):', "        elif False:"), row("R2b"), "swallow the parse exception")
add("R2c", off('    if counts["executed"] == 0 and not seams.off("R2c"):'), row("R2c"), "drop the executed > 0 check")
add("R2d", kill('            if not seams.off("R2d"):', "            if False:"), row("R2d"), "as R2c, for an empty suite")
add("R3a", kill('    elif rc != 0 and not seams.off("R3a"):', "    elif False:"), row("R3a"), "ignore the checker's exit code")
add("R3b", kill('        if not seams.off("R3b"):', "        if False:"), row("R3b"), "never refuse a checker that was not run")
add("R4a", off('    if untracked and not seams.off("R4a"):'), row("R4a"), "check only tracked modifications")
add("R4b", off('    if modified and not seams.off("R4b"):'), row("R4b"), "check only untracked files")
add("R4c", off('    if head != requested and not seams.off("R4c"):'), row("R4c"), "skip the revision comparison")
add("R5a", off('    if obs.version != pin.version and not seams.off("R5a"):'), row("R5a"), "drop the version check")
add("R5b", off('    if not obs.ctracer and not seams.off("R5b"):'), row("R5b"), "drop the CTracer check")
add("R5c", kill('    if obs.core != "ctrace" and not seams.off("R5c"):', '    if not obs.ctracer and not seams.off("R5c"):'), row("R5c"),
    "check that CTracer is available, not the effective core")
add("R5d", kill('    if readable and not seams.off("R5d") and not all(i.has_arcs for i in readable):', "    if False:"), row("R5d"), "drop the has_arcs check")
add("R6a", kill('    if len(infos) < 1 + workers and not seams.off("R6a"):', '    if len(infos) < 1 and not seams.off("R6a"):'), row("R6a"), "compare against 1, not 1 + N")
add("R6b", kill("non_controller_full = [i for i in readable if not i.controller and i.measured_files > 0]",
                "non_controller_full = [i for i in readable if not i.controller]"), row("R6b"), "count files without checking their contents")
add("R6c", off('    if sum(i.executed_rows for i in readable) == 0 and not seams.off("R6c"):'), row("R6c"), "drop the zero-numerator check")
add("R6d", off('    if not any(i.controller for i in readable) and not seams.off("R6d"):'), row("R6d"), "count any 1 + N files regardless of origin")
add("R7a", [kill('    if (report.errored or re.search(r"\\d+ files? errored", report.summary)) and not seams.off("R7a"):', "    if False:"),
            kill('    post += [Fire("R7a", f"data file unreadable: {i.reason}", i.name) for i in infos if not i.readable and not seams.off("R7a")]',
                 "    pass")], row("R7a"), "ignore the combine error count and unreadable data files")
add("R7b", kill("unreadable = [(b, r) for b, r in report.errored if _UNREADABLE_REASON.search(r)]", "unreadable = []"), row("R7b"),
    "accept 'skipped' without a reason")
add("R7c", off('    if report.other_skips and not seams.off("R7c"):'), row("R7c"), "treat every skip as a duplicate")
add("R8", off('    if report.mixed and not seams.off("R8"):'), row("R8"), "catch the combine exception and continue")
add("R9a", kill('            if not seams.off("R9a"):', "            if False:"), row("R9a"), "drop the root-containment check")
add("R9b", [kill('                if not seams.off("R9b"):', "                if False:"), off('        if len(spellings) > 1 and not seams.off("R9b"):')],
    row("R9b"), "skip alias detection")
add("R9c", off('        if real != lexical and not seams.off("R9c"):'), row("R9c"), "compare unresolved paths only")
add("R9d", kill('            if not seams.off("R9d"):', "            if False:"), row("R9d"), "accept control characters")
add("R10a", kill('    if not seams.off("R10a"):', "    if False:"), row("R10a"), "check only the missing direction")
add("R10b", kill('    if not seams.off("R10b"):', "    if False:"), row("R10b"), "check only the extra direction")
add("R10c", kill('    if not seams.off("R10c"):', "    if False:"), row("R10c"), "skip stale-record detection")
add("R10d", kill('    if not seams.off("R10d"):', "    if False:"), row("R10d"), "skip record-path validation")
add("R10e", kill("    if len(set(entries)) != len(entries):", "    if False:"), row("R10e"), "de-duplicate silently")
add("R11a", off('            if slug == "no-data-collected" and not seams.off("R11a"):'), row("R11a"), "drop that warning from the refuse list")
add("R11b", kill('not seams.off("R11b") and warning_names_in_scope_module(message)', "False"), row("R11b"),
    "treat every module as out of scope")
add("R11c", off('            elif slug == "couldnt-parse" and not seams.off("R11c"):'), row("R11c"), "drop that warning")
add("R11d", off('            elif slug == "include-ignored" and not seams.off("R11d"):'), row("R11d"), "drop that warning")
add("R11e", kill("    for label, text in sources.items():", '    for label, text in [(k, v) for k, v in sources.items() if k == "controller"]:'),
    row("R11e"), "scan only the controller's stderr")
add("R11f-overrefuse", kill('not seams.off("R11b") and warning_names_in_scope_module(message)', 'not seams.off("R11b")'),
    ref("test_r11f_out_of_scope_module_not_measured_is_not_refused"), "refuse every module-not-measured (the over-refusal mutation)")
add("R12a", off('        if den == 0 and not seams.off("R12a"):'), row("R12a"), "drop the zero-denominator check")
add("R12b", off('        if num > den and not seams.off("R12b"):'), row("R12b"), "drop the bound check")
add("R12c", kill('            if not seams.off("R12c"):', "            if False:"), row("R12c"), "accept any number")
add("R13a", off('    if tuple(paths) != DEFAULT_PATHS and not seams.off("R13a"):'), row("R13a"), "mark admissible regardless of scope")
add("R13b", kill('    if not seams.off("R13b"):', "    if False:"), row("R13b"), "inspect only --paths")
add("R14a", kill("    left = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []", "    left = []"), row("R14a"), "skip the start-state check")
add("R14b", off('    if old and not seams.off("R14b"):'), row("R14b"), "check only presence at start")
add("R14c", off('    if junit.stat().st_mtime < start - START_SLACK_SECONDS and not seams.off("R14c"):'), row("R14c"), "skip the junit freshness check")
add("R15a", kill('''            write_refusal(out, [Fire("R15a", "the adapter was signalled (SIGTERM) mid-run: partial data", "SIGTERM")])
            rc = EXIT_REFUSED''', '''            write_refusal(out, [Fire("R15a", "the adapter was signalled (SIGTERM) mid-run: partial data", "SIGTERM")])
            rc = EXIT_OK'''), row("R15a"), "exit 0 on the signal path")
add("R15b", kill('''        raise Refusal([Fire("R15b", f"the measured run exceeded the {args.timeout}s timeout and was killed", str(exc))]) from exc''',
                 "        pytest_rc = 0"), row("R15b"), "treat a timeout like a normal exit")
add("R15c", off('    if pytest_rc == 2 and not seams.off("R15c"):'), row("R15c"), "map rc 2 onto the R1a path")
add("R16a", kill("    if actual != pinned_sha:", "    if False:"), row("R16a"), "skip the config digest check")
add("R16b", kill('    bad = sorted(k for k in environ if k.startswith("COVERAGE_"))', "    bad = []"), row("R16b"),
    "unset the variables silently instead of refusing")
add("R17a", kill('    if not seams.off("R17a") and sha256_bytes(raw) != pinned_sha:', "    if False:"), row("R17a") + sem("test_sm6_edited_static_map_without_updating_the_pin_is_refused"),
    "drop the pin check (SM-6)")
add("R17b", kill('    if not seams.off("R17b") and set(fields) != set(COMPARABILITY_CHECKED_FIELDS):', "    if False:"), row("R17b"),
    "drop the key-set check (SM-5)")
add("R17c", [kill('    if observed_core is not None and not seams.off("R17c.core") and observed_core != bs.get("core"):', "    if False:"),
             kill('    if has_arcs is not None and not seams.off("R17c.arcs") and bool(has_arcs) != bool(bs.get("arcs")):', "    if False:")],
    row("R17c", "R17c.arcs") + sem("test_sm8_contradictions_are_refused"), "drop the contradiction checks (SM-8)")

# ---- RA-1..RA-6 shared-helper mutations -----------------------------------------------------------
REP = row("R1a", "R8", "R16b")
add("RA-1", kill('''", ".join(sorted({f.id for f in refusal.fires})), file=sys.stderr)
            rc = EXIT_REFUSED''', '''", ".join(sorted({f.id for f in refusal.fires})), file=sys.stderr)
            rc = EXIT_OK'''), REP, "the refusal path exits 0")
add("RA-2", kill('[{"id": f.id,', '[{"id": "",'), REP, "refusal.json omits the predicate id")
add("RA-3", kill('    write_bytes(out / "refusal.json", dump_json(refusal_payload(fires)))',
                 '    write_bytes(out / "refusal.json", dump_json(refusal_payload(fires)))\n    write_bytes(out / "run-manifest.json", b"{}")'), REP,
    "the refusal path still writes the manifest")
add("RA-4", kill('    write_bytes(diagnostics / "accounting.json", dump_json(accounting))', '    write_bytes(out / "accounting.json", dump_json(accounting))'),
    row("R1a", "R2c", "R12a"), "diagnostics are published outside the quarantine")
add("RA-5", kill("    if out.exists() and any(out.iterdir()):", "    if False:"), ref("test_ra5_out_dir_must_be_fresh"), "reuses a non-empty <OUT>")
add("RA-6", kill("        if st.worktree is not None:\n            soak_leg.remove_worktree(repo_root, st.worktree, runner=runner)",
                 "        if False:\n            soak_leg.remove_worktree(repo_root, st.worktree, runner=runner)"), REP, "the worktree cleanup is skipped")

# ---- environment strip union (T-5971 addendum) ----------------------------------------------------
add("EU-a", kill("    return sorted(set(SECTION6_UNSET) | set(soak_leg.STRIPPED_ENV))", "    return sorted(set(SECTION6_UNSET))"),
    sem("test_env_union_exact_list"), "drop soak_leg.STRIPPED_ENV from the union")
add("EU-b", kill("    redirect = soak_leg.repo_redirect_env(runner=runner)", '    redirect = frozenset({"GIT_DIR", "GIT_NAMESPACE"})'),
    sem("test_env_redirect_vars_are_asked_of_git_stripped_and_fingerprinted"), "hard-code the redirect vars")
add("EU-c", kill('        "env_unset_names": normalize_set(env_unset),', '        "env_unset_names": [],'),
    sem("test_command_fingerprint_is_sensitive_to_exactly_the_name_set", "test_env_redirect_vars_are_asked_of_git_stripped_and_fingerprinted"),
    "omit the unset names from command_fingerprint")

# ---- SM-1..SM-10 ---------------------------------------------------------------------------------
CHANGED_OLD = '''    changed = [key for key in COMPARABILITY_CHECKED_FIELDS
               if canonical_bytes(base_c.get(key)) != canonical_bytes(cur_c.get(key))]'''
SM2 = sem(*(f"test_sm2_single_key_exact[{k}]" for k in (
    "collector.adapter_id", "collector.tool_name", "collector.tool_version", "collector.command_fingerprint", "collector.config_digest",
    "collector.runner_fingerprint", "scope.include_selectors", "scope.exclusion_ids", "scope.generated_source_ids", "scope.scope_fingerprint",
    "dimension", "unit", "denominator", "branch_semantics", "function_semantics", "test_selection")))
SM3 = sem("test_sm3a_changed_branch_semantics_declaration_is_detected", "test_sm3b_changed_function_semantics_declaration_is_detected",
          "test_sm3c_deselect_in_the_argv_is_detected", "test_sm3d_other_coverage_version_is_detected", "test_sm3e_env_allowlist_without_ci_is_detected")
add("SM-always-empty", kill(CHANGED_OLD, "    changed = []"), SM2[:3] + SM3 + sem("test_sm3y_added_in_scope_file_resets_scope", "test_sm4_three_keys_exactly"),
    "comparator always returns []")
add("SM-adapter-id-only", kill(CHANGED_OLD, '''    changed = [key for key in COMPARABILITY_CHECKED_FIELDS if key == "collector.adapter_id"
               and canonical_bytes(base_c.get(key)) != canonical_bytes(cur_c.get(key))]'''), SM2[1:] + SM3,
    "comparator compares only collector.adapter_id")
add("SM-observations-in-diff", kill(CHANGED_OLD, CHANGED_OLD + '''
    if maps[0].get("observations") != maps[1].get("observations"):
        changed = changed + ["observations"]'''), sem("test_sm2b_observation_only_differences_do_not_reset", "test_sm3x_content_only_edit_does_not_reset"),
    "comparator includes observations in the diff")
add("SM-scope-with-contents", kill("    fp_scope = scope_fingerprint(in_scope_paths=in_scope, unreported_paths=unreported, exclusion_regexes=regexes)",
                                   "    fp_scope = scope_fingerprint(in_scope_paths=[p + ':' + sha256_file(wt / p) for p in in_scope], unreported_paths=unreported, exclusion_regexes=regexes)"),
    sem("test_sm3x_content_only_edit_does_not_reset"), "scope_fingerprint built with file contents")
add("SM-scope-drops-paths", kill('        "in_scope_paths": normalize_set(paths),', '        "in_scope_paths": [],'),
    sem("test_sm3y_added_in_scope_file_resets_scope", "test_fc1_path_set_order_independence"), "scope_fingerprint drops the path set")
add("SM-keyset-check", kill('            if set(resolved.get("compared", {})) != expected:', "            if False:"), sem("test_sm5_resolved_map_key_set_must_equal_the_validator_tuple"),
    "key-set check removed")
add("SM-manifest-digest", kill('    if not seams.off("SM7.manifest") and sha256_bytes(map_bytes) != member.get("sha256"):', "    if False:"),
    sem("test_sm7_retained_digest_checks_manifest_and_artifact_record_separately"), "manifest digest check removed")
add("SM-record-digest", kill('        if rec is None or sha256_bytes(map_bytes) != rec.get("digest"):', "        if False:"),
    sem("test_sm7_retained_digest_checks_manifest_and_artifact_record_separately"), "ArtifactRecord digest check removed")
add("SM-key-list-hardcoded", kill("    expected = set(COMPARABILITY_CHECKED_FIELDS)", "    expected = set(" + repr(tuple(
    ["collector.adapter_id", "collector.tool_name", "collector.tool_version", "collector.command_fingerprint", "collector.config_digest",
     "collector.runner_fingerprint", "scope.include_selectors", "scope.exclusion_ids", "scope.generated_source_ids", "scope.scope_fingerprint",
     "dimension", "unit", "denominator", "branch_semantics", "function_semantics", "test_selection"])) + ")"),
    sem("test_sm9_key_source_is_the_imported_validator_tuple"), "key list hard-coded")
add("SM-config-folds-resolved-map", kill("    resolved = build_resolved_map(static_map, static_sha, instances, observations)",
                                         "    resolved = build_resolved_map(static_map, static_sha, instances, observations)\n    fp_config = digest({'c': fp_config, 'm': digest(resolved)})"),
    sem("test_sm10_config_digest_has_no_circularity"), "resolved-map digest folded back into config_digest")

# ---- FC-1..FC-8 ----------------------------------------------------------------------------------
add("FC-1", kill('        "in_scope_paths": normalize_set(paths),', '        "in_scope_paths": list(paths),'), sem("test_fc1_path_set_order_independence"),
    "hash the list without sorting")
add("FC-2", kill('        "env_unset_names": normalize_set(env_unset),', '        "env_unset_names": list(env_unset),'), sem("test_fc2_env_order_independence"),
    "iterate the names without sorting")
add("FC-3a", kill("sort_keys=True, separators", "sort_keys=False, separators"), sem("test_fc3_stable_key_order_and_unicode_normalization"), "drop sort_keys")
add("FC-3b", kill("    return json.dumps(_nfc(obj),", "    return json.dumps(obj,"), sem("test_fc3_stable_key_order_and_unicode_normalization"), "skip NFC normalization")
add("FC-4", kill('        "argv": [placeholderize(a, mapping) for a in argv],', '        "argv": list(argv),'), sem("test_fc4_absolute_path_normalization_across_checkouts"),
    "hash before placeholder substitution")
add("FC-5", kill("        real = os.path.realpath(absolute)", "        real = os.path.abspath(absolute)"), sem("test_fc5_realpath_not_abspath_and_no_double_counting"),
    "abspath instead of realpath")
add("FC-6", kill('        "workers": int(workers),', '        "workers": int(workers), "t": time.time_ns(),'), sem("test_fc6_determinism_across_runs"),
    "a timestamp in a fingerprint input")
add("FC-7", kill('        "workers": int(workers),', '        "workers": int(workers), "home": os.environ.get("HOME", ""),'), sem("test_fc7_determinism_across_runners"),
    "read $HOME into a non-runner fingerprint")
add("FC-8a", kill("    if len(set(seen)) != len(seen):", "    if False:"), sem("test_fc8_duplicates_and_control_characters_are_refused"), "de-duplicate silently")
add("FC-8b", kill("        if has_control_char(rel):", "        if False:"), sem("test_fc8_duplicates_and_control_characters_are_refused"), "accept control characters")


def apply(src: str, hunks) -> str:
    for old, new in hunks:
        if src.count(old) != 1:
            raise SystemExit(f"mutant anchor must match exactly once (found {src.count(old)}): {old[:90]!r}")
        src = src.replace(old, new)
    return src


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pytest(tests: list[str], junit: Path, env_extra: dict, jobs: int) -> tuple[int, dict]:
    env = {**os.environ, **env_extra}
    argv = [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", "-x" if jobs == 0 else "-q"]
    if jobs:
        argv += ["-n", str(jobs)]
    r = subprocess.run(["nice", "-n", "10", *argv], cwd=REPO, env=env, capture_output=True, text=True)
    outcome = {"failed": [], "errored": [], "passed": 0}
    try:
        for case in ET.parse(junit).getroot().iter("testcase"):
            tid = f"{case.get('classname')}::{case.get('name')}"
            if case.find("failure") is not None:
                outcome["failed"].append(tid)
            elif case.find("error") is not None:
                outcome["errored"].append(tid)
            else:
                outcome["passed"] += 1
    except (OSError, ET.ParseError):
        outcome["errored"].append("<no junit>")
    return r.returncode, outcome


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=2)
    args = ap.parse_args()
    out = args.out.resolve()
    if str(out).startswith(str(REPO)) or (out.exists() and any(out.iterdir())):
        raise SystemExit("--out must be a fresh directory outside the repository")
    (out / "receipts").mkdir(parents=True)
    chosen = [m for m in M if not args.only or m.id in args.only]
    before = sha(ADAPTER)
    all_tests = sorted({t for m in chosen for t in m.tests})
    ra_log = out / "ra-results.jsonl"
    rc, base = run_pytest(all_tests, out / "baseline-green.xml", {"ORG083_RA_LOG": str(ra_log)}, args.jobs)
    print(f"unmutated baseline: rc={rc} {base['passed']} passed, failed={base['failed']}, errored={base['errored']}", flush=True)
    if rc != 0:
        raise SystemExit("the unmutated adapter must be green before any mutant is taken")
    ra = {}
    if ra_log.exists():
        for line in ra_log.read_text().splitlines():
            rec = json.loads(line)
            ra[rec["test"].split(" ")[0]] = rec["ra"]
    summary = []
    for m in chosen:
        mdir = out / "mutants" / m.id
        mdir.mkdir(parents=True)
        src = ADAPTER.read_text(encoding="utf-8")
        mutated = apply(src, m.hunks)
        target = mdir / "org083_coverage_adapter.py"
        target.write_text(mutated, encoding="utf-8")
        os.symlink(ASSETS, mdir / "org083_coverage_adapter_assets")
        diff = "".join(difflib.unified_diff(src.splitlines(True), mutated.splitlines(True), "committed", f"mutant-{m.id}", n=1))
        rc, res = run_pytest(m.tests, mdir / "red.xml", {"ORG083_ADAPTER_UNDER_TEST": str(target)}, 0 if len(m.tests) == 1 else args.jobs)
        killed = bool(res["failed"]) and not res["errored"]
        receipt = {"id": m.id, "note": m.note, "diff": diff, "tests": m.tests, "red_tests": res["failed"], "collection_or_import_errors": res["errored"],
                   "killed": killed, "mutant_sha256": sha(target)}
        (out / "receipts" / f"{m.id}.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        summary.append(receipt)
        print(f"{'KILLED' if killed else 'SURVIVED'} {m.id}: red={res['failed'][:2]} errors={res['errored'][:1]}", flush=True)
    rc, green = run_pytest(all_tests, out / "restored-green.xml", {}, args.jobs)
    restored = rc == 0 and not green["failed"] and not green["errored"]
    after = sha(ADAPTER)
    final = {"mutants": len(summary), "killed": sum(r["killed"] for r in summary), "survivors": [r["id"] for r in summary if not r["killed"]],
             "restored_green": restored, "restored_green_counts": green, "committed_adapter_sha256_before": before,
             "committed_adapter_sha256_after": after, "committed_file_untouched": before == after,
             "ra_results_unmutated": ra, "red_test_by_mutant": {r["id"]: r["red_tests"][:1] for r in summary}}
    (out / "summary.json").write_text(json.dumps(final, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in final.items() if k not in ("ra_results_unmutated", "red_test_by_mutant")}, indent=2))
    return 0 if not final["survivors"] and restored and before == after else 1


if __name__ == "__main__":
    sys.exit(main())
