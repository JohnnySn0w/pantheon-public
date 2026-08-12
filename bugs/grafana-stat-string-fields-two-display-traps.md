---
symptom: "Grafana stat panels backed by Flux/InfluxDB2 queries returning a string _value silently render as noValue/blank in two independent ways: (1) reduceOptions.fields set to anything other than the literal \"/.*/\" only selects numeric fields, dropping string-valued cards, and (2) a final map() to {_value} on a table that still carries its original group-key columns (dropping both _time and group keys in one step) returns an empty result frame with no error."
fix: "Diagnose by running the panel query directly via POST /api/ds/query first — if rows come back the bug is panel-side, not data; for trap 1 keep reduceOptions.fields as \"/.*/\" and remove _time from the query output instead of filtering fields display-side; for trap 2 keep _time through the map() and drop it in a separate subsequent step (map(fn: (r) => ({_time: r._time, _value: <cond-string>})) |> drop(columns: [\"_time\"]))."
domain: grafana, flux, influxdb2, sentienthome, dashboards, observability
---

# Grafana stat panel + Flux strings — two display-side traps (bit twice 2026-07-12)

**Stack:** Grafana 12.3.1 @ dashboard.letsfixit.net, InfluxDB2/Flux datasource. Surface: SentientHome homeowner dashboards (`senthome-ac-health`), any stat card whose query returns a **string** `_value`.

## Trap 1 — `reduceOptions.fields` regex silently drops string fields

A stat panel with `reduceOptions.fields` set to anything other than the literal `"/.*/"` (e.g. `"/^_value$/"`) only selects from **numeric** fields — `"/.*/"` is a special-cased "include all field types". String-valued cards render as `noValue` ("—") while numeric+range-mapping cards on the same dashboard keep working, which makes it look data-side. It is not: `/api/ds/query` returns the rows fine.

Bit twice on the same dashboard in one day: v10 (footer-card iteration, reverted) and v13 (re-graft of the same panel options). Both times exactly the string cards (Cooling Now, Filter Health, Workload, Capacitor Health) went blank.

**Fix pattern:** never filter fields display-side to hide `_time`. Remove `_time` from the **query output** instead and keep `fields: "/.*/"`.

## Trap 2 — final `map()` to `{_value}` on a still-grouped table returns empty

Sibling of the known "conditional string-maps on aggregate tables silently return empty" trap (HANDOFF 7/12). When the final `map()` drops **both** `_time` and all group-key columns in one step on a table that still carries its original group key (e.g. `from |> filter |> map({r with ...}) |> last()`), the result frame is empty — no error. Tables already un-grouped by an earlier explicit-record `map()` are fine, which is why 7 of 8 cards survived the same edit.

**Fix pattern (verified):** keep `_time` through the map, then drop it:

```flux
|> last()
|> map(fn: (r) => ({_time: r._time, _value: <cond-string>}))
|> drop(columns: ["_time"])
```

(`|> group() |> last() |> map({_value})` was tested and is ALSO empty — group() before last() does not rescue it.)

## Diagnosis discipline

Blank stat card ≠ empty query. Run the panel query via `POST /api/ds/query` first (isolating test). If rows come back, the bug is panel-side field selection/typing, not data. Evidence: senthome-ac-health v13→v14, TG 5726–5732.

## Keywords

Grafana, stat panel, Flux, InfluxDB2, reduceOptions.fields, noValue, blank card,
string _value, numeric field selection, group(), last(), map(), _time, drop columns,
POST /api/ds/query, dashboard.letsfixit.net, senthome-ac-health, Cooling Now,
Filter Health, Workload, Capacitor Health, empty result frame, aggregate table,
conditional string-maps, display-side field filtering
