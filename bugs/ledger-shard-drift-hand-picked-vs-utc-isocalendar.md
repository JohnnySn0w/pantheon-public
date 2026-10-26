---
symptom: "ledger/2026-W38/SPINE.md lists entry IDs (e.g. 2026-09-14T00:05:34Z-*) that do not exist in ledger/2026-W38/2026-W38.jsonl; grep finds them under ledger/2026-W37/2026-W37.jsonl instead"
fix: "when writing a new ledger entry, ALWAYS use tools/ledger_shard.py's current_write_target(ledger_dir, datetime.now(timezone.utc)) to resolve the shard path — never hand-pick the segment id from the commit message or from local time. isocalendar() uses UTC so an entry at 2026-09-14T00:05Z lands in W38 (Monday, week starts) even when local wall-clock (Chicago-19:05 CDT prior day) says Sunday-W37"
domain: pantheon, ledger, iso-week, shard-routing
---

## Symptom

`ledger/<Y>-W<NN>/SPINE.md` claims N entries and lists them by ID, but `wc -l` on `ledger/<Y>-W<NN>/<Y>-W<NN>.jsonl` returns fewer lines. Concretely observed 2026-09-15:

```
$ head -3 ledger/2026-W38/SPINE.md
# Ledger segment 2026-W38
Per-segment spine for `2026-W38/2026-W38.jsonl` (7 entries).
Auto-maintained by `tools/spine_index_groom.py`. ...

$ wc -l ledger/2026-W38/2026-W38.jsonl
2 ledger/2026-W38/2026-W38.jsonl

$ grep -l 2026-09-14T00:05:34Z-ho-carthing-emitter-v0-landed ledger/*/*.jsonl
ledger/2026-W37/2026-W37.jsonl    # <-- lives one shard over
```

Cross-shard reads still work (`iter_ledger_entry_files` unions every shard), so the ledger is functionally correct — the SPINE for the target week correctly names entries whose UTC timestamp falls in that week — but the entries physically live in a neighbouring shard's jsonl file. A reader browsing to the SPINE-referenced jsonl to hand-check an entry finds it missing.

## Cause

Two clocks in play. `tools/ledger_shard.py`'s `current_write_target(ledger_dir, now)` selects the shard by `now.isocalendar()` and EXPECTS `now` to already be UTC (documented at that function's docstring, ~line 109). `tools/spine_index_groom.py` also classifies entries by their UTC `ts` field's `isocalendar()` — so the groomer's assignment is UTC-derived.

**Recovery's fingerprint diagnosis (mailbox 01789470453069866296, 2026-09-15):** the drifted entries' 00:05-05:13Z clustering is the classic signature of a caller passing a naive-local `now` (e.g. `datetime.now()` with no `tz`) to `current_write_target` instead of `datetime.now(timezone.utc)`. In a behind-UTC zone (Chicago is UTC-5/-6), 2026-09-14T00:05Z is still 2026-09-13 19:05 local — Sunday, ISO W37. A naive-local `datetime.now()` computes W37; a UTC-aware `datetime.now(timezone.utc)` computes W38. `ledger_shard.py` itself is correct — the defect is at the call site.

The hand-picked-shard variant (naming `ledger/2026-W37/...` directly in a commit and writing to that shard without going through `current_write_target` at all) hits the same class through a different route: a human reading the UTC timestamp as "still Sunday" by local intuition. Recovery's fingerprint covers the naive-`datetime.now()` code path; both roads lead to the same one-week-off drift.

Observed instances (git-verified): prior Orchestrator session at commit `7aa737586` and 5 others in the same 00:05Z–05:13Z window on 2026-09-14 all landed in `ledger/2026-W37/2026-W37.jsonl`. Recovery's `spine_index_groom.py` (daily 12:30Z fire) correctly placed the SPINE entry under 2026-W38 (the UTC-derived shard).

## Fix

**Preferred (Recovery's suggestion, mailbox 2026-09-15) — shard from the entry's own `ts`:**

```python
from tools.ledger_shard import shard_for_ts
target = shard_for_ts(entry["ts"])  # derives from the entry's own Z timestamp
```

This guarantees `shard == entry's UTC week` by construction — no `now` in the loop, no way for a caller to pass a naive-local clock. Robust against every failure mode in Cause.

**Acceptable fallback — pass UTC-aware `now` explicitly:**

```python
from datetime import datetime, timezone
from pathlib import Path
from tools.ledger_shard import current_write_target

now = datetime.now(timezone.utc)  # NOT datetime.now() — must carry tz
target = current_write_target(Path("ledger"), now)
```

**Shell one-liner variant** (the shape most HO sessions actually use), derive from entry-ts if you have it, else pass UTC-aware `now`:

```bash
# derive from the entry ts (most robust)
TS='2026-09-15T05:35:00Z'
TARGET=$(python3 -c "import sys; sys.path.insert(0,'tools'); from ledger_shard import shard_for_ts; print(shard_for_ts('$TS'))")
echo "$ENTRY_JSON" >> "$TARGET"

# or, UTC-aware now (fine if the entry ts and now are within seconds)
TARGET=$(python3 -c "from datetime import datetime, timezone; from pathlib import Path; from tools.ledger_shard import current_write_target; print(current_write_target(Path('ledger'), datetime.now(timezone.utc)))")
```

Never pick the shard from the commit message subject or from local wall-clock. UTC is authoritative because that is what `spine_index_groom.py` uses on the read side.

## Verification

Reproduce: read any two matching pairs of `SPINE.md` and `<shard>.jsonl` and cross-check the entry-id list against `head`/`grep` of the jsonl:

```bash
awk -F'`' '/^\| `/ {print $2}' ledger/2026-W38/SPINE.md | sort > /tmp/spine.ids
python3 -c "import json,sys; [print(json.loads(l)['id']) for l in open('ledger/2026-W38/2026-W38.jsonl')]" | sort > /tmp/jsonl.ids
diff /tmp/spine.ids /tmp/jsonl.ids
```

A drift-free segment produces an empty diff.

**Scope check (2026-09-15 fleet-wide scan across all week-shards):** the drift is NOT one-off. 195 entries drifted across 8 different weeks — W25 (45), W27 (28), W28 (25), W31 (9), W34 (23), W35 (20), W37 (39), W38 (6). Every case: an entry's UTC `ts` puts it in shard N but its physical home is shard N-1, matching the Sunday-Chicago-local-time hypothesis (Sunday evening CDT is Monday UTC, ISO week rolls over). This is a chronic HO-side write-pattern issue, not a fresh incident.

Scan script:

```bash
for spine in ledger/2026-W*/SPINE.md; do
    week=$(dirname "$spine" | xargs basename)
    jsonl="ledger/${week}/${week}.jsonl"
    [ -f "$jsonl" ] || continue
    spine_ids=$(awk -F'`' '/^\| `/ {print $2}' "$spine" | sort)
    jsonl_ids=$(python3 -c "import json; [print(json.loads(l)['id']) for l in open('$jsonl')]" 2>/dev/null | sort)
    drift=$(comm -23 <(echo "$spine_ids") <(echo "$jsonl_ids") | wc -l)
    [ "$drift" -gt 0 ] && echo "$week: $drift drifted entries"
done
```

## Disposition of the 195 accumulated drifts — LEAVE (T-2407 precedent)

**Physical relocation would edit an append-only chain, forbidden by [[ORG-001]] mutation-policy** ("Append-only. Entries are never edited or deleted. To correct an error, append a new entry that supersedes the old one." L40; "correcting structural mistakes requires supersedure entries, which adds noise. Acceptable: the noise IS the audit value." L202). This is the same class as **T-2407** (2026-08-01 S3* audit's 9 frozen dup-ids): "They stay. Repairing them means editing an append-only chain to fix an addressing defect, which forfeits exactly the property the addressing exists to serve." Shard-misplacement is an addressing defect; the T-2407 precedent governs.

Recovery flagged this before executing T-3720 (mailbox 01789436466888580826, 2026-09-15). Correct — my initial "Recovery-domain groomer pass is the right home for the cleanup" reading missed the ORG-001 + T-2407 constraint. Path chosen: **forward-only prevention** (this entry's Fix section on every future ledger append) + **leave the 195 documented** as this bug entry does. The drift is functional no-op (`iter_ledger_entry_files` unions every shard so cross-shard reads work), so the only hand-check ergonomic loss is: browse-from-SPINE-to-named-jsonl reports "not found" on drifted rows. That's ORG-001-consistent noise, exactly what T-2407 says to accept.

## Retirement

Retire this entry when `current_write_target` is called by every writer AND a lint blocks direct-shard-path edits at commit time, closing the surface — i.e. no new drift can be introduced. The accumulated 195 stay under any retirement path; they are audit chain content by ORG-001, not defects to sweep.

Do **not** retire on a "relocation happened" trigger — that path is closed by ORG-001 + T-2407 unless the operator explicitly ratifies a mutation-policy override for physical re-sharding (goal-level ledger-convention call; only the operator, and it would need its own ADR amendment). Recovery stands ready to build the relocation if that path clears; not filed as an operator-touch by default (bar-of-goal test: hand-check ergonomics do not warrant the mutation-policy override).
