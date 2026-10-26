#!/usr/bin/env python3
"""cited_hash_lint — find commit hashes cited in tickets that are NOT on origin/main.

WHY (T-2201 -> T-2272). A resolve note that cites a sha is evidence: the reader is
meant to run `git show <sha>` and check the claim. When the sha resolves to nothing
reachable, the note stops being evidence and becomes an assertion — and the failure
is invisible to everyone except a reader who actually tries. T-2201 hand-audited
seven citations and found four unreachable; a machine pass reproduced that 4/7
mapping exactly and then found three MORE the eye had missed.

⚠️ THE FAILURE IS CREATED BY THE PUSH, WHICH IS WHY THIS IS A SWEEP AND NOT A
PRE-RESOLVE GATE. This repo's pre-push hook rebases onto origin/main before pushing.
A rebase REWRITES commits, so a sha that was correct when the note was written
becomes unreachable afterwards. A check that runs before the push therefore passes
and cannot ever see the defect it is looking for. Only a later sweep can.
(That is also why `git rev-parse HEAD` must be read AFTER a push, never before.)

⚠️ UNREACHABLE ONLY. NAIVE HEX MATCHING IS ALMOST ALL NOISE, and a noisy gate gets
ignored — which is worse than no gate. The T-2201 pass found 81 distinct hex tokens
that resolve to no object at all: ACP session ids, UUID fragments, MD5 digests.
None are commits and none are defects. So:

    MISSING     — the object does not exist here. Almost always not a sha at all.
                  Reported as INFO, never a failure.
    UNREACHABLE — the object EXISTS and is not an ancestor of origin/main. This is
                  the real class: a commit that was written, cited, and then
                  rewritten or abandoned. THIS is what fails.

A token must also look CITED — inside backticks, or introduced by a word like
"commit"/"landed"/"at" — before it is considered at all. A bare hex string in prose
is not a citation and treating it as one is how the noise gets in.

Usage:
    python3 tools/cited_hash_lint.py                # sweep tickets/, exit 1 on UNREACHABLE
    python3 tools/cited_hash_lint.py --info         # also list MISSING (informational)
    python3 tools/cited_hash_lint.py --path P ...   # sweep specific files/dirs
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# 7-40 hex chars, not part of a longer alphanumeric run. Lower bound 7 because
# that is git's short-sha floor; below it the false-positive rate explodes.
_HEX = r"(?<![0-9a-zA-Z])([0-9a-f]{7,40})(?![0-9a-zA-Z])"

# A token only counts as a CITATION if it is quoted, or introduced by a word that
# means "this is a commit". Everything else is prose that happens to be hex.
_CITED = re.compile(
    "|".join((
        r"`" + _HEX + r"`",                                          # `abc1234`
        r"(?:commit|landed|landing|lands|at|sha|rev|in)\s+" + _HEX,   # landed abc1234
        r"(?:^|\s)@" + _HEX,                                         # @abc1234
    )),
    re.I | re.M,
)

STATUS_OK, STATUS_MISSING = "OK", "MISSING"
STATUS_UNREACHABLE, STATUS_UNLANDED = "UNREACHABLE", "UNLANDED"

# T-4537: where main() fetches GitHub PR heads (refs/pull/<N>/head).
PR_REFS = "refs/pr/*"


def _git(*args, cwd=REPO, input=None):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, input=input)


def cited_tokens(text: str) -> list[str]:
    """Distinct hex tokens that appear in a citation position, in order."""
    out, seen = [], set()
    for m in _CITED.finditer(text):
        tok = next((g for g in m.groups() if g), None)
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def classify(token: str, *, ref: str = "origin/main", cwd=REPO) -> str:
    """OK / MISSING / UNLANDED / UNREACHABLE for one token.

    ⚠️ THE UNLANDED SPLIT CAME OUT OF THE LINT'S OWN FIRST RUN and it matters for
    the reason this ticket exists at all — a noisy gate gets ignored. Of the first
    19 hits, `2e876bb5f` turned out to be a REAL commit sitting on
    origin/agent/T-2261: a reader CAN verify it, it simply has not landed. That is
    a different and much weaker finding than a commit reachable from no ref
    anywhere, which is the rebase-rewrote-it case the audit was built for.
    Collapsing the two would put recoverable citations in the same bucket as lost
    ones and make the gate cry wolf on every in-flight branch reference.

    T-4537: a PR head (refs/pr/*, fetched by main) counts as a ref here too. The
    fleet squash-merges and deletes agent/<ticket> branches, which orphaned every
    review-time citation of a branch head (157 NEW on 2026-10-04). GitHub keeps
    refs/pull/<N>/head forever, so the reader can still verify them via the PR.
    """
    return classify_many([token], ref=ref, cwd=cwd)[token]


def _rev_set(*revs, cwd=REPO) -> set[str]:
    r = _git("rev-list", *revs, cwd=cwd)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def classify_many(tokens, *, ref: str = "origin/main", cwd=REPO) -> dict[str, str]:
    """`classify` for many tokens in at most three git calls (T-4529).

    Per-token subprocesses (cat-file -e, merge-base --is-ancestor, branch -a
    --contains) cost ~15k tokens x 2-3 spawns, and the weekly run hit its 15min
    timeout. Same answers, batched:
      MISSING      `<tok>^{commit}` does not resolve (absent, ambiguous, or not a
                   commit), via one `cat-file --batch-check`;
      OK           the resolved sha is in `rev-list <ref>`, i.e. an ancestor of ref;
      UNLANDED     else it is in `rev-list --branches --remotes --glob=refs/pr/*`:
                   the commits `branch -a --contains` would find a branch for, plus
                   PR heads (T-4537);
      UNREACHABLE  otherwise.
    """
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        return {}
    r = _git("cat-file", "--batch-check", cwd=cwd,
             input="".join(f"{t}^{{commit}}\n" for t in tokens))
    lines = r.stdout.splitlines() if r.returncode == 0 else []
    resolved: dict[str, str] = {}
    for tok, line in zip(tokens, lines):
        parts = line.split()
        if len(parts) == 3 and parts[1] == "commit":
            resolved[tok] = parts[0]
    out = {t: STATUS_MISSING for t in tokens if t not in resolved}
    on_ref = _rev_set(ref, cwd=cwd) if resolved else set()
    off_ref = {t: sha for t, sha in resolved.items() if sha not in on_ref}
    out.update({t: STATUS_OK for t in resolved if t not in off_ref})
    if off_ref:
        on_branch = _rev_set("--branches", "--remotes", f"--glob={PR_REFS}", cwd=cwd)
        out.update({t: STATUS_UNLANDED if sha in on_branch else STATUS_UNREACHABLE
                    for t, sha in off_ref.items()})
    return out


# ---------------------------------------------------------------------------
# T-2401: standing vs NEW findings.
#
# The weekly unit exits 1 whenever any UNREACHABLE citation exists, and the
# standing set does not shrink: T-2347 audited it and REFUSED to rewrite
# append-only transition logs to make a lint go green, which is the correct
# call. So the number reads the same every week — and under T-2237(a) the
# unit-failure path is classified URGENT. A weekly urgent page for a static
# number teaches the reader to skip urgent pages, which un-earns exactly the
# attention T-2237 bought. The T-2360 stamper fix means NEW stale citations
# should now be rare, so new-vs-standing is precisely where the signal is.
#
# Visibility is not the problem; PAGING is. The standing set still prints in
# full on every run — it is only removed from the exit code.
BASELINE_PATH = REPO / "tools" / "cited_hash_lint_baseline.txt"


def load_baseline(path=None) -> set[tuple[str, str]]:
    """{(token, path)} accepted as standing, unfixable history.

    Keyed on the PAIR, matching cited_ticket_lint_baseline's reasoning: an
    exemption must name the exact citation it excuses. The same bad hash cited
    somewhere new is a new finding, and a file that moves loses its exemption
    and re-reports — the safe direction, since it fails toward showing a finding
    rather than toward hiding one.
    """
    p = pathlib.Path(path or BASELINE_PATH)
    out: set[tuple[str, str]] = set()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out  # absent baseline == nothing exempt, i.e. today's behaviour
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.add((parts[0], parts[1]))
    return out


def partition_baseline(findings, baseline):
    """(new, standing, stale_rows) — what fails, what only prints, what to prune.

    stale_rows are baseline entries NOT observed in this sweep: the citation was
    repaired, or the file moved. They are reported because a dead exemption is a
    loaded gun — it silently pre-authorises whatever later reoccupies that exact
    (token, path) pair. Reported, never fatal: pruning is housekeeping, and
    failing the weekly run over it would recreate the noise this ticket removes.

    T-4537: pairs compare with the ticket STATE dir folded out
    (tickets/{inbox,active,done}/T-N.md). A ticket changing state is the same
    document, so a baselined citation in an open ticket must not re-page when it
    resolves. A genuine move (any other path change) still loses its exemption.
    """
    def key(tok, path):
        return tok, _STATE_DIR.sub(r"\1*/", path)

    exempt = {key(tok, path) for tok, path in baseline}
    observed = {key(tok, path) for path, tok in findings}
    new = [(path, tok) for path, tok in findings if key(tok, path) not in exempt]
    standing = [(path, tok) for path, tok in findings if key(tok, path) in exempt]
    stale_rows = sorted(row for row in baseline if key(*row) not in observed)
    return new, standing, stale_rows


_STATE_DIR = re.compile(r"^(tickets/)(?:inbox|active|done)/")


def _write_baseline(path, findings) -> None:
    """Rewrite the baseline. Only ever from an explicit --update-baseline run."""
    header = (
        "# cited_hash_lint baseline — UNREACHABLE citations accepted as standing.\n"
        "#\n"
        "# These are REAL defects, not false positives. They are listed here because\n"
        "# the surfaces carrying them are append-only: T-2347 audited this set and\n"
        "# REFUSED to rewrite transition logs to make a lint go green, and that\n"
        "# refusal stands. Without this file the weekly sweep pages URGENT on the\n"
        "# same unchanging number forever (T-2237(a) classifies unit-failure as\n"
        "# urgent), which teaches the reader to skip urgent pages.\n"
        "#\n"
        "# A row does NOT declare a citation correct. It records that the citation\n"
        "# is wrong and unfixable in place, and removes it from the EXIT CODE only —\n"
        "# every row still prints in full on every run.\n"
        "#\n"
        "# DO NOT add rows to silence a NEW finding. A new unreachable hash means a\n"
        "# citation was written without re-reading the landed sha after the push;\n"
        "# fix the citation. Since the T-2360 stamper fix these should be rare, and\n"
        "# rarity is the whole signal this file protects.\n"
        "#\n"
        "# Keyed on (token, path): an exemption names the exact citation it excuses,\n"
        "# so the same bad hash cited somewhere new still reports, and a moved file\n"
        "# loses its exemption and re-reports.\n"
        "#\n"
        "# format: <token> <path>\n\n"
    )
    body = "".join(f"{tok} {path}\n" for path, tok in sorted(findings))
    pathlib.Path(path).write_text(header + body, encoding="utf-8")


def sweep(paths, *, ref: str = "origin/main", cwd=REPO) -> dict:
    files: list[pathlib.Path] = []
    for p in paths:
        p = pathlib.Path(p)
        files.extend(sorted(p.rglob("*.md")) if p.is_dir() else [p])
    findings = {STATUS_MISSING: [], STATUS_UNREACHABLE: [], STATUS_UNLANDED: []}
    cited: list[tuple[pathlib.Path, list[str]]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cited.append((f, cited_tokens(text)))
    status = classify_many([t for _, toks in cited for t in toks], ref=ref, cwd=cwd)
    for f, toks in cited:
        for tok in toks:
            st = status[tok]
            if st != STATUS_OK:
                findings[st].append((str(f.relative_to(cwd) if cwd in f.parents
                                         else f), tok))
    findings["files_scanned"] = len(files)
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", action="append", default=None,
                    help="file or dir to sweep (repeatable; default tickets/)")
    ap.add_argument("--info", action="store_true",
                    help="also list MISSING tokens (informational, never fails)")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--baseline", default=None,
                    help=f"baseline file (default {BASELINE_PATH.name})")
    ap.add_argument("--update-baseline", action="store_true",
                    help="rewrite the baseline from this run's findings. NEVER "
                         "wire this into the scheduled unit — a finding that "
                         "can become 'standing' without a reviewable commit is "
                         "the suppress-by-default failure this baseline exists "
                         "to avoid.")
    args = ap.parse_args(argv)

    paths = args.path or [REPO / "tickets"]
    # Reachability is measured against the REMOTE's view. Without this the answer
    # describes a stale local ref and can call a landed commit unreachable.
    _git("fetch", "-q", "origin")
    # T-4537: PR heads too, into a private namespace (not refs/remotes, so branch
    # listings stay clean). A squash-merged agent/<ticket> branch is deleted, but
    # its PR head is not, so review-time citations stay verifiable through it.
    _git("fetch", "-q", "--prune", "origin", f"+refs/pull/*/head:{PR_REFS}")
    f = sweep(paths, ref=args.ref)

    unreachable, missing = f[STATUS_UNREACHABLE], f[STATUS_MISSING]
    unlanded = f[STATUS_UNLANDED]
    print(f"[cited_hash_lint] {f['files_scanned']} file(s); "
          f"{len(unreachable)} UNREACHABLE, {len(unlanded)} UNLANDED, "
          f"{len(missing)} MISSING (info)")
    if unlanded:
        print(f"  ⚠️ UNLANDED — a real commit on another ref (a branch or a PR head), NOT on {args.ref}. "
              f"Verifiable, but do not describe it as landed:")
        for path, tok in unlanded:
            print(f"    {tok}  {path}")
    if args.info and missing:
        print("  MISSING (not objects here — usually session ids / UUIDs / md5s, "
              "not citation failures):")
        for path, tok in missing:
            print(f"    {tok}  {path}")
    baseline = load_baseline(args.baseline)
    new, standing, stale_rows = partition_baseline(unreachable, baseline)

    if standing:
        print(f"  ({len(standing)} STANDING — audited, in the baseline, NOT "
              f"failing this run. Real defects in append-only history that "
              f"T-2347 refused to rewrite; see cited_hash_lint_baseline.txt)")
        for path, tok in standing:
            print(f"    {tok}  {path}")
    if stale_rows:
        # Not fatal — see partition_baseline. Surfaced because an exemption that
        # outlives its finding pre-authorises whatever next occupies that pair.
        print(f"  ({len(stale_rows)} baseline row(s) no longer observed — the "
              f"citation was repaired or the file moved. Prune them:)")
        for tok, path in stale_rows:
            print(f"    {tok}  {path}")

    if args.update_baseline:
        _write_baseline(args.baseline or BASELINE_PATH, unreachable)
        print(f"  wrote {len(unreachable)} row(s) to "
              f"{args.baseline or BASELINE_PATH}. This is a DELIBERATE act: "
              f"review the diff and commit it, so nothing becomes 'standing' "
              f"without a reviewable change.")
        return 0

    if not new:
        print(f"  clean — no NEW unreachable citations "
              f"({len(standing)} standing, unchanged)")
        return 0
    print(f"  ⛔ {len(new)} NEW: cited but NOT an ancestor of {args.ref} — the "
          f"reader cannot verify these, so they are assertions rather than "
          f"evidence:")
    for path, tok in new:
        print(f"    {tok}  {path}")
    print("  Fix by re-reading the LANDED hash (`git rev-parse HEAD` AFTER the "
          "push — this repo's pre-push hook rebases and rewrites it) and "
          "correcting the citation.")
    print("  Do NOT baseline a new finding to silence it: the baseline records "
          "unfixable published history, not inconvenient results.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
