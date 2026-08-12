---
symptom: "A TSV written with `\"\\t\".join(row)` and read back with a default `csv.reader(delimiter='\\t')` does not round-trip, and no error is raised. Four distinct shapes, distinguished by what happens to the ROW COUNT: a field STARTING with a double-quote has its quotes silently stripped (count UNCHANGED, values corrupted); a field that is a lone double-quote swallows the entire remainder of the file into one field (count DOWN); a field containing a newline splits into extra rows (count UP); a field containing a tab adds a column (count UNCHANGED, columns shifted). Because the count moves in every direction including not at all, a row-count check is not a sufficient guard."
fix: "Write with `csv.writer(f, delimiter='\\t')` and never `'\\t'.join`. Then assert the ROUND TRIP, not the row count: read the file back with `csv.reader` and `assert back == rows`. A count-only check passes on the two most damaging shapes (leading-quote strip and embedded-tab column shift). Note the trigger is a LEADING quote, not any quote — a quote mid-field round-trips fine through the naive path, so a corpus scanned for `'\"' in field` overstates the affected rows and mis-locates the cause."
domain: pantheon, csv, tsv, python, data-integrity, corpus, qrels, labeling, round-trip
---

# `"\t".join` TSV is lossy — and the row count does not detect it

**Class:** silent data corruption at a serialization boundary. Not a Python bug — `csv.reader`
applies RFC-4180 quoting rules that `"\t".join` never applied on the way out, so the reader
"correctly" interprets quoting the writer did not intend.

**Found:** 2026-08-02 (T-2411, curating specialist's general-rules tier into committed homes). The
inherited rule reported the symptom on a real corpus — 158 of 1,639 snippets contained a `"` and a
default reader returned 1,577 of 1,639 rows. Re-measuring changed both the trigger and the guard.

## Symptom

You write a table with the obvious one-liner:

```python
open(p, "w").write("\n".join("\t".join(r) for r in rows) + "\n")
```

and read it back the obvious way:

```python
rows_back = list(csv.reader(open(p), delimiter="\t"))
```

`rows_back != rows`, and nothing raised. Measured, one bad field per case, 2 rows in:

| field content        | rows out | what happened                                  |
|----------------------|----------|------------------------------------------------|
| `he said "hi"`       | 2        | **round-trips fine** — mid-field quote is safe |
| `"quoted" thing`     | 2        | quotes **stripped** → `quoted thing`           |
| `"` (lone quote)     | **1**    | rest of file swallowed into one field          |
| `line1\nline2`       | **3**    | row **split** into two                         |
| `col1\tcol2`         | 2        | extra **column**; every later column shifts    |

## Why the row count is not a guard

The count goes **down** (lone quote), **up** (embedded newline), and **stays exactly right** while
the data is wrong (leading quote, embedded tab). The two count-preserving shapes are the dangerous
ones: a leading-quote strip changes values silently, and an embedded tab shifts every subsequent
column so a labeling surface silently re-associates labels with the wrong fields.

So `assert len(rows_back) == len(rows)` passes on precisely the cases you most need to catch. It is
an instrument whose form cannot express the failure — see
[`instrument-whose-form-cannot-express-the-failure.md`](instrument-whose-form-cannot-express-the-failure.md).

## Fix

```python
import csv
with open(p, "w", newline="") as f:
    csv.writer(f, delimiter="\t").writerows(rows)

with open(p, newline="") as f:
    back = list(csv.reader(f, delimiter="\t"))
assert back == rows, "TSV round-trip lost data"
```

`csv.writer` quotes and escapes on the way out, so the reader's rules and the writer's rules agree.
The round-trip assert is the part that matters — it is the only check that fails on all four shapes.

Anything writing a labeling, qrels, eval or retrieval-corpus surface should do this: those files are
read back by a different process, often much later, and a column shift there is indistinguishable
from a bad model.

## Corrected from the inherited rule

The rule this entry replaces attributed the loss to fields **containing** a `"`. Measurement says a
quote mid-field is harmless; the trigger is a quote in the **leading position** of a field (plus the
independent newline and tab shapes, which have nothing to do with quotes at all). Scanning a corpus
for `'"' in field` therefore overstates the affected row count and points at the wrong cause — which
matters when you are deciding whether an existing artifact needs regenerating.

## Related

- [`instrument-whose-form-cannot-express-the-failure.md`](instrument-whose-form-cannot-express-the-failure.md) — the row-count check is an instance.
- [`git-quotepath-escapes-a-path-out-of-its-own-prefix-test.md`](git-quotepath-escapes-a-path-out-of-its-own-prefix-test.md) — sibling shape: a quoting convention applied on one side of a boundary and not the other.
