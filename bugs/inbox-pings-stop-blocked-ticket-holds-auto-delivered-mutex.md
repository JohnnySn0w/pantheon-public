# Inbox pings stop / `skip:dispatch_locked` forever — a blocked ticket still holds the auto_delivered mutex

**Symptom(s)**
- An agent (orchestrator/specialist/recovery) stops receiving inbox auto-deliveries ("inbox pings"), silently, for hours.
- `journalctl --user -u pantheon-inbox-consumer@<agent>.service` shows every 10-min cycle ending in `[inbox_consumer] <agent>: skip:dispatch_locked`.
- No error — the service exits 0 each cycle; it's *deliberately* skipping.

**Root cause**
`check_dispatch_locked()` (T-0717 mutex) holds the lock whenever the agent has an
**unresolved `auto_delivered: true` ticket** in `inbox/` or `active/` that isn't yet
stale. When such a ticket gets **blocked / dep-gated** (`ticket.py block` sets
`depends_on`/`not_before`), its `auto_delivered` stamp is **not** cleared — so the
blocked ticket keeps holding the mutex even though it is no longer an in-flight
dispatch. Every consumer cycle then skips, suppressing *all* inbox delivery to that
agent. The staleness backstop (`AUTO_DELIVERED_STALE_SECONDS`) only fires hours later
(liveness = max(transition-log ts, file mtime)), and ongoing repo activity that
touches the file can keep refreshing the mtime and defer the backstop further.

**Immediate fix (30-second remediation)**
Find the culprit and clear its stamp:
```bash
grep -lE "auto_delivered:\s*true" tickets/inbox/*.md tickets/active/*.md \
  | xargs grep -lE "^target:\s*<agent>"        # the blocked one is the culprit
# remove `auto_delivered:` + `auto_delivered_at:` from its frontmatter (it stays blocked),
# commit, then verify:
python3 tools/inbox_consumer.py --agent <agent>   # should print "pulled T-XXXX", not skip:dispatch_locked
```

**Structural fix:** T-0864 — `ticket.py block` (and any dep-gate / not_before disposition
on an auto_delivered ticket) must clear the stamp, and/or `check_dispatch_locked` must
treat dep_blocked auto_delivered tickets as non-locking (a blocked ticket cannot be in-flight).

**Provenance:** 2026-06-06. T-0768 (dep-gated on T-0666) held orchestrator's lock ~7.6h, silently
suppressing all inbox delivery; the operator noticed the missing pings. Stamp cleared → consumer
immediately pulled T-0770. See ledger 2026-06-06T00:28:14Z-027.
