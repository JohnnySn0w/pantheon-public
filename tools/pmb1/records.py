"""Run records: schema ``pmb1-run/1``.

Rule 6.1.5: accuracy-only is rejected. A record missing its cost fields fails
validation (``validate_record``), and the gate refuses invalid records.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "pmb1-run/1"
COST_FIELDS = (
    "write_calls", "supersede_calls", "forget_calls", "retrieve_calls",
    "write_llm_calls", "write_dollars", "write_seconds", "retrieve_seconds", "tokens_returned",
)
RUN_COST_FIELDS = COST_FIELDS + ("backbone_calls",)
V2_SAMPLE_KEYS = ("A1.accuracy", "A-retrieve.recall", "E3.utility")
V3_SAMPLE_KEYS = ("GOV.leak", "A2.stale_evidence", "A2.new_value")  # v3 adds governance and supersede
V4_SAMPLE_KEYS = ("A2c.current", "A2c.asof", "A2c.stale_as_current", "A9.surfaced", "A9.false_conflict",
                  "C5.lineage", "GOV-derived.leak", "GOV-derived.provenance")  # v4 adds valid time, conflicts, lineage, derived relations


def validate_record(rec: object) -> list[str]:
    """Return a list of problems; empty means valid."""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["record is not an object"]
    if rec.get("schema") != SCHEMA:
        errs.append(f"schema is {rec.get('schema')!r}, want {SCHEMA!r}")
    if not isinstance(rec.get("gate"), bool):
        errs.append("gate must be a bool")
    sub = rec.get("substrate")
    if not isinstance(sub, dict) or not all(isinstance(sub.get(k), str) and sub.get(k) for k in ("name", "role", "version")):
        errs.append("substrate.describe() missing name/role/version")
    bb = rec.get("backbone")
    if not isinstance(bb, dict) or not (bb.get("name") and bb.get("family")):
        errs.append("backbone needs name and family")
    if "embedding_model" not in rec:
        errs.append("embedding_model missing (null when the substrate has none)")
    fx = rec.get("fixture")
    if not isinstance(fx, dict) or not (fx.get("id") and isinstance(fx.get("sha256"), str) and len(fx["sha256"]) == 64):
        errs.append("fixture needs id and a sha256")
    cost = rec.get("cost")
    if not isinstance(cost, dict):
        errs.append("cost block missing (rule 6.1.5: accuracy-only is rejected)")
    else:
        for k in RUN_COST_FIELDS:
            v = cost.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"cost.{k} missing or not numeric")
    tracks = rec.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        errs.append("no track scores")
    else:
        for name, t in tracks.items():
            if not isinstance(t, dict) or not isinstance(t.get("n"), int) or t["n"] < 0:
                errs.append(f"track {name}: n missing")
            elif not isinstance(t.get("metrics"), dict) or not t["metrics"]:
                errs.append(f"track {name}: metrics missing")
    if not isinstance(rec.get("not_run"), list):
        errs.append("not_run must list the tracks that did not run")
    if isinstance(fx, dict) and fx.get("version", 1) >= 2:
        errs += _validate_v2(rec)
    return errs


def _validate_v2(rec: dict) -> list[str]:
    """v2 records carry what the gate's intervals and honesty notes need."""
    errs: list[str] = []
    for k in ("reader_knows_lexicon", "embedder_is_lexical_stub"):
        if not isinstance(rec.get(k), bool):
            errs.append(f"{k} must be a bool on a v2 record")
    version = rec.get("fixture", {}).get("version", 1)
    if version >= 4 and not isinstance(rec.get("extractor_is_pattern_stub"), bool):
        errs.append("extractor_is_pattern_stub must be a bool on a v4 record")
    samples = rec.get("samples")
    if not isinstance(samples, dict):
        return errs + ["samples missing (v2 records carry per-probe values)"]
    keys = V2_SAMPLE_KEYS + (V3_SAMPLE_KEYS if version >= 3 else ()) + (V4_SAMPLE_KEYS if version >= 4 else ())
    for key in keys:
        v = samples.get(key)
        if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            errs.append(f"samples.{key} must be a non-empty list of numbers")
    return errs


def write_record(rec: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = lambda s: "".join(c if c.isalnum() or c in "-_." else "_" for c in s)  # noqa: E731
    p = out_dir / f"{safe(rec['substrate']['name'])}__{safe(rec['backbone']['name'])}.json"
    p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def load_records(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        out.append(json.loads(Path(p).read_text(encoding="utf-8")))
    return out
