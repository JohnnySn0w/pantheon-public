#!/usr/bin/env python3
"""ORG-102 H2 weekly blind sampler and reply parser.

The sampler writes one Telegram-ready outbox file for Orchestrator to relay. The
parser reads the operator's reply from a file Orchestrator populates later.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import subprocess
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import quant_delivery_collect as qd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("/var/lib/pantheon/quant")
DEFAULT_OUTCOMES = DEFAULT_OUT / "call_outcomes.jsonl"
SCHEMA_FILE = REPO_ROOT / "schemas" / "quant-delivery.schema.json"
PARAPHRASE_PROMPT_FILE = REPO_ROOT / "tools" / "quant_h2_paraphrase_prompt.txt"
PARAPHRASE_SCHEMA_FILE = REPO_ROOT / "tools" / "quant_h2_paraphrase.schema.json"
HOLD_DAYS = 14
DEFAULT_LIMIT = 10
SOURCE_EXCERPT_LIMIT = 2000
RENDERER_PROMPT_VERSION = "h2-paraphrase-v1"
DEFAULT_RENDERER_MAX_ATTEMPTS = 3

GUIDANCE_RE = re.compile(r"\b(?:P|D)\s*-?\s*\d+\b|\bORG\s*-?\s*\d{3,}\b|\bADR\s*-?\s*\d+\b", re.IGNORECASE)
HEX_RE = re.compile(r"\b[0-9a-fA-F]{7,}\b")
COMMIT_WORD_RE = re.compile(r"\bcommit\s+[0-9a-fA-F]{5,}\b", re.IGNORECASE)
DATETIME_RE = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}(?:T~?\d{1,2}:\d{2}Z?|[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?Z?)?\b|"
    r"\b\d{1,2}:\d{2}\s*(?:AM|PM|UTC|CST|CDT|PST|PDT)?\b",
    re.IGNORECASE,
)
WEEK_RE = re.compile(r"\b20\d{2}-W\d{2}\b", re.IGNORECASE)
RUN_RE = re.compile(r"\bRUN\s+\d{4,6}Z\b", re.IGNORECASE)
PR_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.-]+#\d+|(?:PR|pull\s+request)\s*#?\s*\d+)\b",
    re.IGNORECASE,
)
TG_RE = re.compile(r"\b(?:the operator\s+)?(?:TG|msg)[\s_-]*\d+\b", re.IGNORECASE)
TICKET_RE = re.compile(r"\bT\s*[- ]?\s*\d{3,6}\b", re.IGNORECASE)
PATH_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s(=\[{:,;]))(?!y/n\b)(?:/|[.~]*/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+/?",
    re.IGNORECASE,
)
JARGON_CHARS_RE = re.compile(r"[_`*#]+")
ALLOWED_REPLY_SEPARATORS_RE = re.compile(r"[\s,.;:/|\\-]*")
NUMBERED_REPLY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[\).:=/-]?\s*([yYnN])(?![A-Za-z])")
BAD_END_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "because",
    "but",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "via",
    "with",
}
BAD_START_WORDS = {"and", "but", "for", "or", "the", "to", "with"}
JUDGMENT_WORD_RE = re.compile(
    r"\b(?:correctly|wisely|obviously|clearly|appropriately|rightly|wrongly|mistakenly|"
    r"unfortunately|fortunately|should have|should've)\b",
    re.IGNORECASE,
)
ASK_SHAPED_CALL_RE = re.compile(
    r"^\s*(?:"
    r"decide|whether|should|choose|pick|select|can\s+we|could\s+we|do\s+we|"
    r"does\s+the\s+team|would\s+it|is\s+it|approve\s+or|reject\s+or"
    r")\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
BOILERPLATE_SITUATIONS = {
    "during fleet work the fleet made this call",
    "during ticket or PR work the fleet made this call",
}
NOISY_CALL_PREFIXES = (
    "audit-chain link",
    "b8 chain link",
    "committed",
    "resolved",
    "session reset",
)
PREFERRED_CALL_RE = re.compile(
    r"^(?:"
    r"confirmed|closed|defaults|evaluate|head caught|implementer applied|picked|plan is|"
    r"pushed back|ran through|recommendation|s3 audit|strict-set"
    r")\b",
    re.IGNORECASE,
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def week_start_for(value: dt.datetime | None = None) -> dt.date:
    value = value or qd.now_utc()
    return (value - dt.timedelta(days=value.weekday())).date()


def parse_week(value: str | None) -> str:
    if value:
        return dt.date.fromisoformat(value).isoformat()
    return week_start_for().isoformat()


def call_time(row: dict[str, Any]) -> dt.datetime | None:
    return qd.parse_time(row.get("call_occurred_at"))


def display_ref(row: dict[str, Any]) -> str | None:
    for value in [row.get("call_ref"), *(row.get("ticket_ids") or [])]:
        if isinstance(value, str):
            match = TICKET_RE.search(value)
            if match:
                digits = re.search(r"\d{3,6}", match.group(0))
                if digits:
                    return f"T-{int(digits.group(0)):04d}"
    return None


@lru_cache(maxsize=1)
def ledger_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted((REPO_ROOT / "ledger").glob("**/*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                entries.setdefault(entry_id, entry)
    return entries


def source_entry(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("call_source_type") != "ledger":
        return None
    ref = row.get("call_source_ref")
    return ledger_entries().get(str(ref)) if ref else None


def context_fragment(chunks: list[str]) -> str:
    context = " ".join(chunks[1:3] or chunks[:1])
    if "—" in context:
        context = context.split("—", 1)[1].strip()
    return context


def humanize_slug(value: str | None) -> str:
    text = re.sub(r"[_-]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sentence_chunks(value: str | None) -> list[str]:
    text = str(value or "")
    chunks = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+|;\s+", text) if chunk.strip()]
    return chunks


def scrub_text(text: str | None) -> str:
    text = str(text or "")
    text = TG_RE.sub("the operator ruling", text)
    text = PR_RE.sub("the PR", text)
    text = GUIDANCE_RE.sub("", text)
    text = WEEK_RE.sub("", text)
    text = RUN_RE.sub("", text)
    text = COMMIT_WORD_RE.sub("implementation commit", text)
    text = DATETIME_RE.sub("", text)
    text = HEX_RE.sub("", text)
    text = PATH_RE.sub("the file", text)
    text = text.replace("the file/", "the file")
    text = TICKET_RE.sub("", text)
    text = re.sub(r"\b[A-Za-z0-9_.-]+@", "", text)
    text = re.sub(r"\bruling\+\d+\b", "ruling", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = text.replace("(", "").replace(")", "")
    text = JARGON_CHARS_RE.sub(" ", text)
    text = re.sub(r"(?<=\s)-\s*([A-Za-z])", r"\1", text)
    text = re.sub(r"\bpost\s*-\s*,?", "post", text, flags=re.IGNORECASE)
    text = text.replace("->", " to ").replace("=>", " means ")
    text = re.sub(r"\bjustifies?\s+via\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bjustify-?via\b\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bvia\b\s*[:=-]?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" -;:,./—")
    return text


def word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", text))


def ends_badly(text: str) -> bool:
    words = re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", text.lower())
    return not words or words[-1] in BAD_END_WORDS


def starts_badly(text: str) -> bool:
    words = re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", text.lower())
    return not words or words[0] in BAD_START_WORDS


def is_boilerplate_situation(text: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9 ]+", "", text).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in BOILERPLATE_SITUATIONS


def preferred_call_chunk(chunks: list[str], fallback: str) -> str:
    candidates = [scrub_text(chunk) for chunk in chunks]
    presentable_candidates = [text for text in candidates if presentable(text)]
    for text in presentable_candidates:
        if PREFERRED_CALL_RE.search(text):
            return text
    for text in presentable_candidates:
        if not text.lower().startswith(NOISY_CALL_PREFIXES):
            return text
    return fallback


def presentable(text: str, *, min_words: int = 5) -> bool:
    if word_count(text) < min_words or starts_badly(text) or ends_badly(text):
        return False
    if text[:1] and not re.match(r"[A-Za-z0-9'\"“‘]", text[:1]):
        return False
    if re.search(r"^\d+\.", text):
        return False
    if re.search(r"^\d+\b", text):
        return False
    if text.count("'") % 2 or text.count('"') % 2:
        return False
    if re.search(r"\bfiles?\b|\bfile\s*=", text, re.IGNORECASE):
        return False
    if re.search(r":\.", text):
        return False
    if re.search(r"\b[A-Za-z]+\.[A-Za-z]", text):
        return False
    if re.search(r"[→⇢≡]", text):
        return False
    if re.search(r"/\d{2,}\b", text):
        return False
    if re.search(r"\b(?:it|this|that|these|those)\b", text, re.IGNORECASE):
        return False
    if re.search(r"\bclaimed active\b", text, re.IGNORECASE):
        return False
    if re.search(r"\b(?:to|for|from|with|via|the)\.\s+[A-Z]", text):
        return False
    if re.search(r"\b(?:impl|suite|run|ticket|thread)\s*[-:]?\s*\d+\b", text, re.IGNORECASE):
        return False
    if re.search(
        r"\bthe PR\b|\bTelegram\b|\boperator ruling\b|\bon branch\b|\bbranch\b|"
        r"\bsource-channel\b|\breview attribution\b|\bworker ticket\b|\btickets?\b|\bauto-[a-z-]+\b|"
        r"\bORG-NNN\b|\bARC-INTENT\b|\bJUSTIFIES\b|\bDECISIONS\b|\bterminal-state\b|"
        r"\bclone-currency\b|\bworker-guard\b|\bcodex-cli\b|\bTUI\b|\bclaude\b|\bSpecialist\b|"
        r"\bArchivist\b|\bRecovery\b|\bcrontab\b|\bsudo\b|\btrampoline\b|\bsystemd\b|"
        r"\binbox\b|\bmailbox\b|\bhandoff\b|\bpost-?restart\b|\bdispatch\b|\bAudit-chain\b|"
        r"\bpwa\b|\bqueue\b|\bcodex\b|\bseat\b|\bexecutor\b|\btauri\b|\bunit tests\b|"
        r"\bcarve\b|\bd-ac\b|\bmax-window\b|\btokenizer\b|\bsubagents\b|\becho-fixarc\b|"
        r"\bmemory pin\b|\bartifact paths\b|\bworkspace\b|\bfilesystem\b|\bgit HEAD\b|"
        r"\bPull-step\b|\bTODO\w*\b|\bRPI\b|\bSSH\b|\bmid-smoke\b|\bkeep-going poke\b|"
        r"\bbubble-up\b|\bdispatcher tier\b|\bconfig-control\b|\bTestNegationGuard\b|"
        r"\bPR held\b|\bincl\b|\bHO-tier\b|\brecs\b|\boverflow\b|\bBACKLOG\b|"
        r"\breviewer-caught\b|\bagent-pane\b|\breviewer\b|\bsmoke\b|\bdiff\b|\brebound\b|"
        r"\bunittest\b|\borchestrator\b|\bfull-repo\b|\bapproved paths\b|\badditive-only\b|"
        r"\bHERMES\b|\bTotal tests\b|\bSamsone\b|\boperator thread\b|\bB8\b|"
        r"\bFleet-artisan\b|\bSpeechmatics\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"\b[A-Za-z0-9_.-]+\.(?:md|py|rs|toml|json|yaml|yml)\b", text, re.IGNORECASE):
        return False
    if re.search(r"\bauto-filed\s+\d+\s+tickets\b", text, re.IGNORECASE):
        return False
    if re.search(r"\bauto-unblocked\s+\d+\b", text, re.IGNORECASE):
        return False
    if re.search(r"\bpantheon config sync\.py wrote state file\b", text, re.IGNORECASE):
        return False
    if re.search(r"\bfirst seen at\s*=", text, re.IGNORECASE):
        return False
    if re.search(r"[<>]", text):
        return False
    return not forbidden_blind_token(text)


def too_similar(left: str, right: str) -> bool:
    left_words = set(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", left.lower()))
    right_words = set(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", right.lower()))
    if not left_words or not right_words:
        return True
    overlap = len(left_words & right_words) / min(len(left_words), len(right_words))
    return overlap >= 0.75


def source_texts(row: dict[str, Any]) -> tuple[str, str]:
    entry = source_entry(row)
    if entry:
        action = humanize_slug(str(entry.get("summary") or entry.get("action") or row.get("call") or ""))
        body = str(entry.get("rationale") or entry.get("notes") or "")
        chunks = sentence_chunks(body)
        call_raw = preferred_call_chunk(chunks, chunks[0] if chunks else action)
        context = context_fragment(chunks)
        situation_raw = context or chunks[0] if chunks else action
        return situation_raw, call_raw

    title = row.get("source_title") or row.get("ticket_title") or row.get("pr_title")
    serves = row.get("serves") or row.get("rationale")
    call = row.get("call")
    situation_raw = str(title or serves or call or row.get("call_source_ref") or "")
    return situation_raw, str(call or serves or title or "")


def clean_excerpt(text: str | None) -> str:
    text = str(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SOURCE_EXCERPT_LIMIT].rstrip()


def source_excerpt(row: dict[str, Any]) -> dict[str, str] | None:
    """Return the auditable source text the renderer sees, or None if absent.

    This is intentionally broader than the old extractive renderer: ugly or
    insider-ish prose stays eligible so H2 does not bias toward neat sources.
    """
    entry = source_entry(row)
    parts: list[str] = []
    kind = str(row.get("call_source_type") or "unknown")
    if entry:
        kind = "ledger"
        for key in ("summary", "action", "rationale", "notes"):
            value = clean_excerpt(entry.get(key))
            if value:
                parts.append(f"{key}: {value}")
    else:
        for key in ("source_title", "ticket_title", "pr_title", "serves", "rationale", "call"):
            value = clean_excerpt(row.get(key))
            if value:
                parts.append(f"{key}: {value}")
    excerpt = clean_excerpt("\n".join(parts))
    if not excerpt:
        return None
    return {"source_excerpt": excerpt, "source_excerpt_kind": kind}


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 3)].rsplit(" ", 1)[0].strip(" ,;:-")
    cut = cut or text[: max(0, limit - 3)]
    while cut and ends_badly(cut):
        next_cut = cut.rsplit(" ", 1)[0].strip(" ,;:-")
        if not next_cut or next_cut == cut:
            break
        cut = next_cut
    return cut.rstrip() + "..."


def forbidden_blind_token(text: str) -> bool:
    return bool(
        GUIDANCE_RE.search(text)
        or HEX_RE.search(text)
        or PR_RE.search(text)
        or TG_RE.search(text)
        or TICKET_RE.search(text)
        or PATH_RE.search(text)
    )


def paraphrase_reject_reasons(situation: str, decision: str) -> list[str]:
    reasons: list[str] = []
    values = {"situation": str(situation or "").strip(), "decision": str(decision or "").strip()}
    for field, text in values.items():
        if word_count(text) < 5:
            reasons.append(f"{field}:too_short")
        if len(text) > 180:
            reasons.append(f"{field}:too_long")
        if text.count("\n"):
            reasons.append(f"{field}:multi_line")
        if not re.search(r"[.!?]$", text):
            reasons.append(f"{field}:not_sentence")
    combined = f"{values['situation']} {values['decision']}"
    if forbidden_blind_token(combined) or GUIDANCE_RE.search(combined) or URL_RE.search(combined):
        reasons.append("leak:source_ref")
    if re.search(r"/\d{2,}\b", combined):
        reasons.append("leak:slash_number")
    if re.search(r"\b(?:it|this|that|these|those)\b", combined, re.IGNORECASE):
        reasons.append("quality:vague_pronoun")
    if JUDGMENT_WORD_RE.search(combined):
        reasons.append("neutrality:judgment_word")
    if ASK_SHAPED_CALL_RE.search(values["decision"]) or values["decision"].endswith("?"):
        reasons.append("quality:ask_shaped_call")
    if too_similar(values["situation"], values["decision"]):
        reasons.append("quality:duplicate")
    return sorted(set(reasons))


class RendererUnavailable(RuntimeError):
    pass


class RendererFailed(RuntimeError):
    pass


class CodexParaphraseRenderer:
    def __init__(
        self,
        *,
        timeout: int = 120,
        prompt_file: Path = PARAPHRASE_PROMPT_FILE,
        schema_file: Path = PARAPHRASE_SCHEMA_FILE,
    ) -> None:
        self.timeout = timeout
        self.prompt_file = prompt_file
        self.schema_file = schema_file
        self.prompt = read_text(prompt_file)
        self.prompt_sha256 = sha256_text(self.prompt)
        self.codex_version = self._codex_version()

    def _codex_version(self) -> str:
        try:
            proc = subprocess.run(
                ["codex", "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RendererUnavailable(f"renderer_unavailable: codex --version failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RendererUnavailable(f"renderer_unavailable: codex --version rc={proc.returncode}: {detail}")
        return proc.stdout.strip() or "codex"

    def render(
        self,
        *,
        row: dict[str, Any],
        source: dict[str, str],
        attempt: int,
        prior_reject_reasons: list[str],
    ) -> dict[str, Any]:
        payload = {
            "prompt_version": RENDERER_PROMPT_VERSION,
            "source_excerpt": source["source_excerpt"],
            "prior_reject_reasons": prior_reject_reasons,
        }
        full_prompt = self.prompt.rstrip() + "\n\nINPUT_JSON:\n" + json.dumps(payload, sort_keys=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{row.get('call_id', 'call')}-{attempt}")[:160]
        safe_out_file = Path("/tmp") / f"quant-h2-render-{safe_name}.json"
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(self.schema_file),
            "--output-last-message",
            str(safe_out_file),
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=REPO_ROOT,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RendererUnavailable(f"renderer_unavailable: codex exec failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            raise RendererUnavailable(f"renderer_unavailable: codex exec rc={proc.returncode}: {detail}")
        try:
            data = json.loads(safe_out_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RendererFailed(f"renderer_invalid_json: {exc}") from exc
        return {
            "situation": str(data.get("situation") or "").strip(),
            "decision": str(data.get("decision") or "").strip(),
            "renderer_model": self.codex_version,
            "renderer_prompt_version": RENDERER_PROMPT_VERSION,
            "renderer_prompt_sha256": self.prompt_sha256,
            "renderer_command": " ".join(cmd[:-1] + ["-"]),
        }


def render_item(
    row: dict[str, Any],
    source: dict[str, str],
    renderer: Any,
    *,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, list[str], int]:
    reject_reasons: list[str] = []
    last_output: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            output = renderer.render(row=row, source=source, attempt=attempt, prior_reject_reasons=reject_reasons)
        except RendererFailed as exc:
            reject_reasons = [str(exc)]
            continue
        last_output = output
        reasons = paraphrase_reject_reasons(output.get("situation", ""), output.get("decision", ""))
        if not reasons:
            return output, [], attempt
        reject_reasons = reasons
    return last_output, reject_reasons or ["renderer_failed"], max_attempts


def blind_item(row: dict[str, Any]) -> dict[str, str] | None:
    situation_raw, call_raw = source_texts(row)
    situation = scrub_text(situation_raw)
    call = scrub_text(call_raw)
    if is_boilerplate_situation(situation):
        return None
    if not presentable(call):
        return None
    if len(situation) > 96 or len(call) > 112 or len(situation) + len(call) > 200:
        return None
    combined = f"{situation} {call}"
    if situation.lower() == call.lower() or too_similar(situation, call):
        return None
    if not presentable(situation) or not presentable(call):
        return None
    if forbidden_blind_token(combined):
        return None
    if TICKET_RE.search(combined):
        return None
    return {"blind_situation": situation, "blind_call": call, "display_ref": display_ref(row)}


def sampled_call_ids(out_dir: Path) -> set[str]:
    return {str(row.get("call_id")) for row in load_jsonl(out_dir / "call_h2_samples.jsonl") if row.get("call_id")}


def eligible_rows(
    outcomes: list[dict[str, Any]],
    *,
    generated_at: str,
    already_sampled: set[str],
) -> tuple[list[tuple[dict[str, Any], dict[str, str]]], dict[str, int]]:
    generated_dt = qd.parse_time(generated_at) or qd.now_utc()
    out: list[tuple[dict[str, Any], dict[str, str]]] = []
    skipped = defaultdict(int)
    for row in outcomes:
        if row.get("call_id") in already_sampled:
            skipped["already_sampled"] += 1
            continue
        if row.get("outcome") not in {"held", "reversed", "overridden"}:
            skipped["ineligible_outcome"] += 1
            continue
        occurred = call_time(row)
        if occurred is None or generated_dt - occurred < dt.timedelta(days=HOLD_DAYS):
            skipped["too_young_or_undated"] += 1
            continue
        source = source_excerpt(row)
        if source is None:
            skipped["no_source_text"] += 1
            continue
        out.append((row, source))
    return out, dict(sorted(skipped.items()))


def stratified_sample(
    candidates: list[tuple[dict[str, Any], dict[str, str]]],
    *,
    limit: int,
    seed: str,
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    rng = random.Random(seed)
    groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = defaultdict(list)
    for row, item in candidates:
        groups[str(row.get("agent") or "unknown")].append((row, item))
    agents = sorted(groups)
    for agent in agents:
        groups[agent].sort(key=lambda pair: str(pair[0].get("call_id")))
        rng.shuffle(groups[agent])
    rng.shuffle(agents)
    chosen: list[tuple[dict[str, Any], dict[str, str]]] = []
    while len(chosen) < limit and any(groups.values()):
        for agent in list(agents):
            if len(chosen) >= limit:
                break
            if groups[agent]:
                chosen.append(groups[agent].pop(0))
        agents = [agent for agent in agents if groups[agent]]
    return chosen


def make_sample_id(week_start: str, seed: str) -> str:
    alphabet = "ghjkmnpqrstuvwxyz"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    suffix = "".join(alphabet[byte % len(alphabet)] for byte in digest[:8])
    return f"h2-{week_start}-{suffix}"


def message_text(sample_id: str, week_start: str, records: list[dict[str, Any]]) -> str:
    lines = ["Weekly check: do you agree with each call? Reply y/n per item, like yynyn...", ""]
    for row in records:
        lines.append(f"{row['item_no']}. Situation: {row['blind_situation']}")
        lines.append(f"   Call: {row['blind_call']}")
    lines.extend(["", f"sample_id: {sample_id}", f"week_start: {week_start}"])
    return "\n".join(lines) + "\n"


def write_version(out_dir: Path, run: dict[str, Any]) -> None:
    write_json(
        out_dir / "schema" / "version.json",
        {
            "schema": "pantheon.quant.schema_manifest/v1",
            "generated_at": run["generated_at"],
            "run_id": run["run_id"],
            "schema_file": str(SCHEMA_FILE.relative_to(REPO_ROOT)),
            "feeds": {
                "call_h2_samples": "pantheon.quant.call_h2_sample/v1",
                "call_h2_replies": "pantheon.quant.call_h2_reply/v1",
                "call_h2_weekly": "pantheon.quant.call_h2_weekly/v1",
                "runs": "pantheon.quant.run/v1",
            },
        },
    )


def sample(args: argparse.Namespace, renderer: Any | None = None) -> dict[str, Any]:
    generated_at = qd.iso_z(qd.now_utc())
    run_id = f"quant-h2-{qd.now_utc().strftime('%Y%m%dT%H%M%SZ')}"
    week_start = parse_week(args.week_start)
    outcomes = load_jsonl(args.outcomes)
    seed = args.seed or f"{week_start}:{outcomes[-1].get('run_id') if outcomes else 'empty'}"
    sample_id = args.sample_id or make_sample_id(week_start, seed)
    existing = load_jsonl(args.out / "call_h2_samples.jsonl")
    if any(row.get("sample_id") == sample_id for row in existing):
        raise SystemExit(f"sample_id already exists: {sample_id}")
    candidates, skipped_raw = eligible_rows(outcomes, generated_at=generated_at, already_sampled=sampled_call_ids(args.out))
    skipped = defaultdict(int, skipped_raw)
    chosen = stratified_sample(candidates, limit=len(candidates), seed=seed)
    renderer = renderer or CodexParaphraseRenderer(timeout=args.renderer_timeout)
    records: list[dict[str, Any]] = []
    render_failures: list[dict[str, Any]] = []
    max_attempts = getattr(args, "renderer_max_attempts", DEFAULT_RENDERER_MAX_ATTEMPTS)
    for row, source in chosen:
        if len(records) >= args.limit:
            break
        item, reject_reasons, attempts = render_item(row, source, renderer, max_attempts=max_attempts)
        if reject_reasons or item is None:
            skipped["render_failed_postcheck"] += 1
            render_failures.append(
                {
                    "call_id": row.get("call_id"),
                    "call_source_type": row.get("call_source_type"),
                    "call_source_ref": row.get("call_source_ref"),
                    "attempts": attempts,
                    "reject_reasons": reject_reasons,
                }
            )
            continue
        idx = len(records) + 1
        records.append(
            {
                "schema": "pantheon.quant.call_h2_sample/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "sample_id": sample_id,
                "week_start": week_start,
                "seed": seed,
                "item_no": idx,
                "call_id": row["call_id"],
                "call_source_type": row.get("call_source_type"),
                "call_source_ref": row.get("call_source_ref"),
                "call_ref": row.get("call_ref"),
                "ticket_ids": row.get("ticket_ids") or [],
                "agent": str(row.get("agent") or "unknown"),
                "display_ref": display_ref(row),
                "outcome": row.get("outcome"),
                "call_occurred_at": row.get("call_occurred_at"),
                "hidden_via_ids": row.get("via_ids") or [],
                "hidden_evidence_kinds": [e.get("kind") for e in row.get("evidence") or []],
                "source_excerpt": source["source_excerpt"],
                "source_excerpt_kind": source["source_excerpt_kind"],
                "blind_situation": item["situation"],
                "blind_call": item["decision"],
                "renderer_model": item.get("renderer_model"),
                "renderer_prompt_version": item.get("renderer_prompt_version"),
                "renderer_prompt_sha256": item.get("renderer_prompt_sha256"),
                "renderer_command": item.get("renderer_command"),
                "renderer_attempts": attempts,
                "renderer_postcheck": "passed",
                "renderer_reject_reasons": [],
            }
        )
    append_jsonl(args.out / "call_h2_samples.jsonl", records)
    outbox = args.out / "h2" / "outbox" / f"{sample_id}.md"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.write_text(message_text(sample_id, week_start, records), encoding="utf-8")
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "collector": "h2-sampler",
        "days": args.days,
        "repos": [],
        "source_status_by_repo": {},
        "records": {"call_h2_samples": len(records)},
        "sample_id": sample_id,
        "week_start": week_start,
        "seed": seed,
        "outbox": str(outbox),
        "eligible": len(candidates),
        "skipped": skipped,
        "render_failures": render_failures,
    }
    append_jsonl(args.out / "runs" / f"{run_id}.jsonl", [run])
    write_version(args.out, run)
    return run


def answer_word(value: str) -> str:
    return "agree" if value.lower() == "y" else "disagree"


def parse_reply(raw: str, offered: int) -> dict[str, Any]:
    text = raw.strip()
    all_match = re.fullmatch(r"all\s+([yYnN])[\s.!,]*", text, flags=re.IGNORECASE)
    if all_match:
        answer = answer_word(all_match.group(1))
        return {
            "parse_status": "parsed",
            "answers": [{"item_no": idx, "answer": answer} for idx in range(1, offered + 1)],
            "invalid_chars_count": 0,
            "surplus_count": 0,
        }

    numbered = list(NUMBERED_REPLY_RE.finditer(text))
    if numbered:
        stripped = NUMBERED_REPLY_RE.sub("", text)
        if ALLOWED_REPLY_SEPARATORS_RE.fullmatch(stripped):
            seen: dict[int, str] = {}
            duplicates = 0
            surplus = 0
            for match in numbered:
                item_no = int(match.group(1))
                if 1 <= item_no <= offered:
                    if item_no in seen:
                        duplicates += 1
                    seen[item_no] = answer_word(match.group(2))
                else:
                    surplus += 1
            if duplicates:
                return {
                    "parse_status": "unparsed",
                    "answers": [],
                    "invalid_chars_count": 0,
                    "surplus_count": surplus + duplicates,
                }
            answers = [{"item_no": item_no, "answer": seen[item_no]} for item_no in sorted(seen)]
            return {
                "parse_status": "parsed" if len(answers) == offered else "partial",
                "answers": answers,
                "invalid_chars_count": 0,
                "surplus_count": surplus,
            }
        return {"parse_status": "unparsed", "answers": [], "invalid_chars_count": len(stripped), "surplus_count": 0}

    compact = re.sub(r"[\s,.;:/|\\-]+", "", text)
    if not compact or re.search(r"[^yYnN]", compact):
        return {"parse_status": "unparsed", "answers": [], "invalid_chars_count": len(compact), "surplus_count": 0}
    answers = [
        {"item_no": idx, "answer": answer_word(char)}
        for idx, char in enumerate(compact[:offered], start=1)
    ]
    return {
        "parse_status": "parsed" if len(answers) == offered else "partial",
        "answers": answers,
        "invalid_chars_count": 0,
        "surplus_count": max(0, len(compact) - offered),
    }


def latest_answers(sample_id: str, replies: list[dict[str, Any]]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for reply in replies:
        if reply.get("sample_id") != sample_id or reply.get("parse_status") == "unparsed":
            continue
        for answer in reply.get("answers") or []:
            answers[int(answer["item_no"])] = str(answer["answer"])
    return answers


def recompute_weekly(out_dir: Path, run_id: str, generated_at: str) -> list[dict[str, Any]]:
    samples = load_jsonl(out_dir / "call_h2_samples.jsonl")
    replies = load_jsonl(out_dir / "call_h2_replies.jsonl")
    by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in samples:
        by_sample[str(row["sample_id"])].append(row)
    rows: list[dict[str, Any]] = []
    graded_history: list[dict[str, Any]] = []
    for sample_id, items in sorted(by_sample.items(), key=lambda pair: (pair[1][0].get("week_start"), pair[0])):
        answers = latest_answers(sample_id, replies)
        answered = len(answers)
        agree = sum(1 for value in answers.values() if value == "agree")
        disagree = answered - agree
        interval = qd.wilson_interval(agree, answered)
        if answered:
            graded_history.append({"agree": agree, "answered": answered})
        rolling = graded_history[-4:]
        rolling_answered = sum(row["answered"] for row in rolling)
        rolling_agree = sum(row["agree"] for row in rolling)
        rolling_interval = qd.wilson_interval(rolling_agree, rolling_answered)
        low = bool(rolling_interval and rolling_interval["low"] < 0.70)
        if answered:
            graded_history[-1]["low"] = low
        consecutive = 0
        for hist in reversed(graded_history):
            if hist["low"]:
                consecutive += 1
            else:
                break
        verdict = "insufficient-n" if answered == 0 else "fail" if consecutive >= 4 else "pass"
        rows.append(
            {
                "schema": "pantheon.quant.call_h2_weekly/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "sample_id": sample_id,
                "week_start": items[0].get("week_start"),
                "offered": len(items),
                "answered": answered,
                "agree": agree,
                "disagree": disagree,
                "agreement_rate_wilson90": interval,
                "lower_bound_below_0_70": low,
                "rolling_4w_answered": rolling_answered,
                "rolling_4w_agree": rolling_agree,
                "rolling_4w_wilson90": rolling_interval,
                "consecutive_low_weeks": consecutive,
                "falsifier_triggered": consecutive >= 4,
                "verdict": verdict,
            }
        )
    qd.write_jsonl(out_dir / "call_h2_weekly.jsonl", rows)
    return rows


def ingest_reply(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = qd.iso_z(qd.now_utc())
    run_id = f"quant-h2-reply-{qd.now_utc().strftime('%Y%m%dT%H%M%SZ')}"
    samples = [row for row in load_jsonl(args.out / "call_h2_samples.jsonl") if row.get("sample_id") == args.sample_id]
    if not samples:
        raise SystemExit(f"unknown sample_id: {args.sample_id}")
    raw = args.reply_file.read_text(encoding="utf-8")
    parsed = parse_reply(raw, len(samples))
    reply = {
        "schema": "pantheon.quant.call_h2_reply/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "sample_id": args.sample_id,
        "reply_received_at": generated_at,
        "reply_file": str(args.reply_file),
        "raw_reply": raw.strip(),
        **parsed,
        "answered_count": len(parsed["answers"]),
    }
    append_jsonl(args.out / "call_h2_replies.jsonl", [reply])
    weekly = recompute_weekly(args.out, run_id, generated_at)
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "collector": "h2-reply",
        "days": 1,
        "repos": [],
        "source_status_by_repo": {},
        "records": {"call_h2_replies": 1, "call_h2_weekly": len(weekly)},
        "sample_id": args.sample_id,
        "parse_status": parsed["parse_status"],
        "answered_count": len(parsed["answers"]),
    }
    append_jsonl(args.out / "runs" / f"{run_id}.jsonl", [run])
    write_version(args.out, run)
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sample_p = sub.add_parser("sample")
    sample_p.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    sample_p.add_argument("--week-start")
    sample_p.add_argument("--seed")
    sample_p.add_argument("--sample-id")
    sample_p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sample_p.add_argument("--days", type=int, default=60)
    sample_p.add_argument("--renderer-timeout", type=int, default=120)
    sample_p.add_argument("--renderer-max-attempts", type=int, default=DEFAULT_RENDERER_MAX_ATTEMPTS)
    reply_p = sub.add_parser("ingest-reply")
    reply_p.add_argument("--sample-id", required=True)
    reply_p.add_argument("--reply-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "sample":
            result = sample(args)
        else:
            result = ingest_reply(args)
    except RendererUnavailable as exc:
        raise SystemExit(f"renderer_unavailable: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
