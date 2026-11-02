"""T-5678: the ORG-105 security-ticket gate (pure logic; no I/O, no ticket.py import).

Arc intent: a security-shaped ticket is reviewed against the ratified threat model
(ORG-105 section 4) BEFORE it can enter the work queue, so fleet effort goes to real
threats (D1; P1 structural over prose, after T-5288's prose wiring failed).

A ticket is security-shaped when its labels hit SECURITY_LABELS or its title/ask hits
KEYWORD_RE. Filing one needs a complete chain (foothold, path, impact asset) in typed
slugs, or `--not-security` for a keyword-only hit. An accepted file lands
`security_gate: pending`; every pull path skips pending tickets until HO runs
`security-accept` (see `is_pending`).

The slug tables below are PINNED to ORG-105's text and cross-checked against the ADR by
tools/test_ticket_security_gate.py (AdrSyncTest). Changing the ADR without these fails
that test, which is the drift alarm.
"""

from __future__ import annotations

import re

SECURITY_LABELS = frozenset({
    "security", "privacy", "worker-guard", "threat-model", "vuln", "cve", "hardening",
    "security-surface", "security-relevant", "security-posture", "security-adjacent",
    "security-tooling",
})

# Whole-word, case-insensitive. Trailing \w* = prefix match (bypass*, escalat*, ...).
_KEYWORDS = ("su" + "do", r"bypass\w*", r"escalat\w*", r"privilege\w*", r"exploit\w*",
             r"inject\w*", r"leak\w*", r"exfil\w*", r"credential\w*", r"attacker\w*")
KEYWORD_RE = re.compile(r"\b(?:" + "|".join(_KEYWORDS) + r")\b|\bCVE-", re.IGNORECASE)

# ORG-105 section 2 footholds (slug -> ADR bullet title / section 7 matrix row label).
FOOTHOLDS = ("internet-attacker", "supply-chain", "untrusted-content",
             "compromised-account", "guest-voice", "provider-billing")
FOOTHOLD_TITLE = {
    "internet-attacker": "Opportunistic internet attackers",
    "supply-chain": "Supply chain",
    "untrusted-content": "Untrusted content steering an agent",
    "compromised-account": "A compromised trusted account or endpoint",
    "guest-voice": "Guests and other household voices",
    "provider-billing": "Provider policy and billing",
}
FOOTHOLD_ROW = {
    "internet-attacker": "Internet attacker",
    "supply-chain": "Supply chain",
    "untrusted-content": "Untrusted content steering an agent",
    "compromised-account": "Compromised trusted account or endpoint",
    "guest-voice": "Guest or household voice",
    "provider-billing": "Provider policy and billing",
}

# ORG-105 section 1 assets (slug -> ADR bullet title / matrix column label), ADR order.
ASSETS = ("money-credentials", "outward-identity", "home", "private-data",
          "integrity", "uptime")
ASSET_TITLE = {
    "money-credentials": "Money and credentials",
    "outward-identity": "Our outward identity",
    "home": "The home",
    "private-data": "the operator's private data leaving the box",
    "integrity": "The integrity of fleet code, config and automation",
    "uptime": "The fleet staying up",
}
ASSET_COLUMN = {
    "money-credentials": "Money & credentials",
    "outward-identity": "Outward identity",
    "home": "The home",
    "private-data": "Private data leaving",
    "integrity": "Integrity",
    "uptime": "Uptime",
}

_ROWS = {  # section 7 matrix, columns in ASSETS order
    "internet-attacker": "○——○○●",
    "supply-chain": "●●○●●○",
    "untrusted-content": "●●○●●○",
    "compromised-account": "●●●●●●",
    "guest-voice": "——●○—○",
    "provider-billing": "●○———●",
}
MATRIX = {(f, a): _ROWS[f][i] for f in FOOTHOLDS for i, a in enumerate(ASSETS)}
NO_PATH = "—"

RANKS = ("primary", "secondary")


class GateRefusal(ValueError):
    """The gate declined the filing; ticket.py re-raises it as a TicketRefusal."""


def label_hits(labels) -> list[str]:
    return sorted(l for l in (labels or []) if str(l).strip().lower() in SECURITY_LABELS)


def keyword_hits(*texts) -> list[str]:
    hits: list[str] = []
    for t in texts:
        if isinstance(t, str):
            hits += [m.group(0).lower() for m in KEYWORD_RE.finditer(t)]
    return sorted(set(hits))


def is_pending(fields: dict) -> bool:
    """True iff the ticket awaits HO's security accept: every pull path skips it."""
    return str(fields.get("security_gate") or "").strip().lower() == "pending"


_KEY_SPLIT = re.compile(r";\s*(?=(?:foothold|impact|path)=)")


def parse_chain(raw: str) -> tuple[str, str, str]:
    """`foothold=<slug>;impact=<slug>;path=<free text>` (any order) -> (foothold, impact, path).

    A `;` inside the path text is kept: parts split only where a known key follows."""
    parts: dict[str, str] = {}
    for piece in _KEY_SPLIT.split(raw.strip()):
        key, sep, val = piece.partition("=")
        key = key.strip()
        if not sep or key not in ("foothold", "impact", "path") or key in parts:
            raise GateRefusal(_chain_help(f"bad --threat-chain part {piece.strip()[:40]!r}"))
        parts[key] = val.strip()
    if set(parts) != {"foothold", "impact", "path"}:
        raise GateRefusal(_chain_help("--threat-chain needs foothold, impact and path"))
    if parts["foothold"] not in FOOTHOLDS:
        raise GateRefusal(_chain_help(f"unknown foothold {parts['foothold']!r}"))
    if parts["impact"] not in ASSETS:
        raise GateRefusal(_chain_help(f"unknown impact {parts['impact']!r}"))
    if not parts["path"]:
        raise GateRefusal(_chain_help("empty path"))
    return parts["foothold"], parts["impact"], parts["path"]


def _chain_help(lead: str = "") -> str:
    return (
        (f"{lead}. " if lead else "")
        + "ORG-105 s4 needs foothold+path+impact: --threat-chain "
        "'foothold=<F>;impact=<I>;path=<text>' with "
        f"F in {{{','.join(FOOTHOLDS)}}}, I in {{{','.join(ASSETS)}}}. "
        "Keyword-only false hit: --not-security '<reason>'."
    )


def chain_fields(chain: str, path_justification: str | None) -> dict:
    """Validated chain -> frontmatter fields (classified pending). Raises GateRefusal."""
    foothold, impact, path = parse_chain(chain)
    just = (path_justification.strip()
            if isinstance(path_justification, str) and path_justification.strip() else None)
    if MATRIX[(foothold, impact)] == NO_PATH and not just:
        raise GateRefusal(
            f"ORG-105 s7 marks {foothold} x {impact} '{NO_PATH}' (no realistic path): "
            "add --path-justification '<why the path is real>'."
        )
    out = {"security_gate": "pending", "threat_foothold": foothold,
           "threat_impact": impact, "threat_path": path}
    if just:
        out["threat_justification"] = just
    return out


def is_unclassified(fields: dict) -> bool:
    """Pending with no chain: filed by an automated filer, HO supplies the chain at accept."""
    return str(fields.get("threat_unclassified") or "").strip().lower() == "true"


def evaluate(*, title: str, ask: str | None, labels, threat_chain: str | None = None,
             path_justification: str | None = None, not_security: str | None = None,
             automated_filer: str | None = None) -> dict:
    """Gate a filing. Returns frontmatter fields to merge ({} = not gated); raises GateRefusal.

    An automated filer (non-interactive, named) with a security-shaped filing and NO chain
    is not refused: it lands pending + unclassified so nothing is lost and nothing is
    pulled before HO reviews it (security-accept then requires the chain)."""
    lab = label_hits(labels)
    kw = keyword_hits(title, ask)
    chain = threat_chain.strip() if isinstance(threat_chain, str) and threat_chain.strip() else None
    ns = not_security if isinstance(not_security, str) else None
    filer = (automated_filer.strip()
             if isinstance(automated_filer, str) and automated_filer.strip() else None)

    if ns is not None and chain:
        raise GateRefusal("--not-security and --threat-chain are mutually exclusive: pick one.")
    if ns is not None:
        if not (lab or kw):
            return {}
        if lab:
            raise GateRefusal(
                f"--not-security cannot override the security label(s) {lab}: supply "
                "--threat-chain, or file without the label."
            )
        if not ns.strip():
            raise GateRefusal("--not-security needs a non-empty reason.")
        return {"not_security_reason": ns.strip()}

    if not (lab or kw or chain):
        return {}
    if chain:
        return chain_fields(chain, path_justification)
    if filer:
        return {"security_gate": "pending", "threat_unclassified": "true",
                "security_filer": filer}
    why = f"label {lab}" if lab else f"keyword {kw}"
    raise GateRefusal(f"Security-shaped ticket ({why}). " + _chain_help())
