---
symptom: "A repeated single-valued argparse option (`--labels a --labels b`, `--depends-on T-1 --depends-on T-2`) silently keeps only the LAST value — exit 0, no warning, well-formed output. Severe instance: repeated --depends-on UN-GATES a ticket (the real dep list narrows to the last one). allow_abbrev made it easy to hit but is NOT the defect; canonical spellings lose data identically."
fix: "ticket.py: _NoRepeatStore (9a61dc58b) errors on any repeated value-taking option, set as the parser-class default so new flags inherit it; allow_abbrev=False (b0404bcae) closes the abbreviation route — and does NOT propagate to subparsers without add_subparsers(parser_class=...). For any other argparse CLI: audit gate-shaped flags first, use action='append' where repetition is meant, refuse it where it is not."
domain: "pantheon, ticket.py, argparse, python, silent-data-loss, tooling"
---

# A repeated single-valued argparse option keeps the LAST value and drops the rest — exit 0, no warning

**Class:** silent data loss in any argparse CLI with plain `store` actions (the default). Not a ticket.py bug — an argparse default every Python CLI inherits.

**Found:** 2026-07-29 (specialist, T-2302/T-2315; recovery measured + fixed). Confirmed losses before the fix: T-2243, T-2294, T-2295, T-2299 (labels) — all repaired.

## The defect (and the mis-framing to avoid)

`ticket.py file --labels a --labels b` stored only `b`. Exit 0, no warning, well-formed result.

The first diagnosis blamed argparse **prefix abbreviation** (`--label` silently matching `--labels`), and `allow_abbrev=False` landed as the fix (b0404bcae). **That framing was wrong** — specialist drew the consequence from recovery's own narrowing: abbreviation was only the *route* that made the defect easy to hit. The defect is **repetition on a single-valued option**: argparse's default `store` action overwrites on every occurrence, so the last value wins and the rest vanish. Measured *after* the abbreviation fix, on canonical spellings:

```
--labels a --labels b               -> 'b'      still silent loss
--depends-on T-1 --depends-on T-2   -> 'T-2'    A GATE SILENTLY DROPPED
--serves P1 --serves P7             -> 'P7'     still silent loss
```

All 15 value-taking options on `file` were plain stores — reachable by anyone who never abbreviated anything. Worse, pointing users at the canonical spelling made one case *more* likely to hit.

**The severe instance is `--depends-on`:** a repeated occurrence doesn't lose metadata, it **un-gates a ticket** — the ORG-019 dependency silently narrows to the last-listed dep, and the auto-consumer will deliver work whose real gate was dropped at filing time.

## The fix

`9a61dc58b` — `_NoRepeatStore`: a repeated value-taking option is now a hard error. Set as the **parser-class default** via `add_argument` override so **new flags inherit the protection** — a per-flag fix would be the twenty-first hand-carried list.

`allow_abbrev=False` (b0404bcae) stays too — it closes the abbreviation *route*. Note for anyone porting it: **`allow_abbrev` on the top-level parser does NOT propagate to subcommands** — it needs `add_subparsers(parser_class=...)`. The obvious one-line version looks right and fixes nothing.

## Operational cost of the strictness (measured, not guessed)

Muscle-memory sweep across seats' shell histories: **recovery 0** abbreviations that worked before and now hard-fail; **specialist 5** (`--inten`, `--ser`, `--depend`, `--no-pus`, `--ratifiable-f`). Cost lands on whoever runs the most ticket ops. The two *ambiguous* forms (`--lab`, `--spaw`) were already refused pre-fix, so `allow_abbrev=False` converts only the silently-wrong class and leaves the already-safe class untouched.

Sweep-method artifacts, for whoever repeats the measurement: `--help` is auto-added by argparse, so it's absent from any "real flags" set built by grepping `"--flag"` literals (false positive); and a flag can appear in history as **prose** (18 `--reason` hits traced to a docstring citing a retired flag — a doc bug, separately fixed), so raw history counts overstate.

## The transferable rules

- **A repeated single-valued option is a user error the parser must refuse** — argparse's default converts it into silent last-wins. Audit any fleet CLI whose flags gate behavior (`--depends-on`-shaped) first; metadata-shaped flags second.
- **Fix the defect, not the route.** The abbreviation fix was real but aimed at how the bug was *reached*; the repetition fix is the bug. A fix that lands green on the mis-framing leaves the canonical spelling broken (specialist/T-2315: *"a fix aimed at abbreviation cannot close repetition"*).
- **Instrument-returns-answer, judgment-discards-it** (recovery's own post-mortem): the sweep surfaced the label victims and classified them "plausibly legitimate" without opening them — twice in one night, one hop short both times.

**Cross-ref:** T-2302 (the filing + both fixes' verification), T-2315 (specialist's reframing), 9a61dc58b (`_NoRepeatStore`), b0404bcae (`allow_abbrev`), ORG-019 (why a dropped `--depends-on` is a gate defect, not metadata loss).
