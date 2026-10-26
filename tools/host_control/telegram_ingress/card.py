"""Bounded approval-card rendering and callback tags (T-2919).

The card is rendered from the ``Present`` arguments ONLY (which the
approval service renders from its pending record only) plus the fixed
template below. Plain text, no ``parse_mode``: summary text can never
inject markup or links.

Callback data (Bot API: ``callback_data`` is 1-64 BYTES) is
``"v1:" + d + tag`` where ``d`` is ``g`` (approve) or ``d`` (deny) and
``tag = base64url(SHA-256(domain | review_handle | nonce | d))`` without
padding (43 chars) -> 47 bytes. The tag binds review handle, nonce, and
decision together; the nonce itself never travels through Telegram.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

from tools.host_control.telegram_ingress import wire

CALLBACK_VERSION = "v1:"
_TAG_DOMAIN = b"pantheon-tg-cb/v1"
DECISION_BY_CODE = {"g": "granted", "d": "denied"}
MAX_CARD_TEXT = 3500  # UTF-16 code units (Bot API: text 1-4096 after entity parsing)
MAX_EFFECTS_CHARS = 2000  # UTF-16 code units
MAX_EFFECTS_LINES = 40
_TRUNCATED = "[truncated]"


class CardError(ValueError):
    """The Present arguments are outside the closed card contract."""


def _hex64(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise CardError(f"invalid {label}")
    return value


def callback_tag(review_handle: str, nonce: str, code: str) -> str:
    if code not in DECISION_BY_CODE:
        raise CardError("invalid decision code")
    digest = hashlib.sha256(
        b"|".join(
            (_TAG_DOMAIN, review_handle.encode(), nonce.encode(), code.encode())
        )
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def callback_data(review_handle: str, nonce: str, code: str) -> str:
    return CALLBACK_VERSION + code + callback_tag(review_handle, nonce, code)


def parse_callback_data(data: object) -> tuple[str, str] | None:
    """Return (decision code, tag) or None for anything off-contract."""

    if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
        return None
    if not data.startswith(CALLBACK_VERSION) or len(data) != 47:
        return None
    code, tag = data[3], data[4:]
    if code not in DECISION_BY_CODE:
        return None
    return code, tag


def tag_matches(review_handle: str, nonce: str, code: str, tag: str) -> bool:
    return hmac.compare_digest(
        callback_tag(review_handle, nonce, code).encode(), tag.encode()
    )


def _clean(text: str) -> str:
    # strip control characters except newline; plain text only
    return "".join(
        ch for ch in text if ch == "\n" or (ch.isprintable() and ch != "\x7f")
    )


def _bounded_effects(text: str) -> str:
    cleaned = _clean(text)
    lines = cleaned.split("\n")
    truncated = False
    if len(lines) > MAX_EFFECTS_LINES:
        lines = lines[:MAX_EFFECTS_LINES]
        truncated = True
    out = "\n".join(lines)
    if utf16_len(out) > MAX_EFFECTS_CHARS:
        out = truncate_utf16(out, MAX_EFFECTS_CHARS)
        truncated = True
    if truncated:
        out = out.rstrip() + "\n" + _TRUNCATED
    return out


def parse_present(
    review_handle: object,
    expected_digest: object,
    summary_json: object,
    callback_nonce: object,
    expires_at: object,
    *,
    now: datetime,
    max_ttl_seconds: int,
) -> dict:
    """Validate every ``Present`` argument against the closed contract
    and return the parsed card fields."""

    review_handle = _hex64(review_handle, "review handle")
    expected_digest = _hex64(expected_digest, "plan digest")
    nonce = _hex64(callback_nonce, "callback nonce")
    if (
        not isinstance(summary_json, str)
        or len(summary_json.encode("utf-8")) > wire.MAX_SUMMARY_JSON_BYTES
    ):
        raise CardError("invalid summary")
    try:
        summary = json.loads(summary_json)
    except json.JSONDecodeError:
        raise CardError("invalid summary") from None
    if not isinstance(summary, dict) or set(summary) != wire.CARD_KEYS:
        raise CardError("invalid summary keys")
    if summary["schema"] != wire.CARD_SCHEMA:
        raise CardError("invalid summary schema")
    if summary["review_handle"] != review_handle:
        raise CardError("summary review mismatch")
    if summary["plan_digest"] != expected_digest:
        raise CardError("summary digest mismatch")
    if summary["expires_at"] != expires_at or not isinstance(expires_at, str):
        raise CardError("summary expiry mismatch")
    target = summary["target_seat_id"]
    families = summary["operation_families"]
    effects = summary["redacted_effects"]
    if not isinstance(target, str) or not 1 <= len(target) <= 128:
        raise CardError("invalid target")
    if (
        not isinstance(families, list)
        or not families
        or len(families) > 32
        or not all(isinstance(f, str) and 1 <= len(f) <= 128 for f in families)
    ):
        raise CardError("invalid families")
    if (
        not isinstance(effects, str)
        or not 1 <= len(effects) <= wire.MAX_CARD_SUMMARY_LENGTH
    ):
        raise CardError("invalid effects")
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        raise CardError("invalid expiry") from None
    if expiry.tzinfo is None:
        raise CardError("invalid expiry")
    expiry = expiry.astimezone(timezone.utc)
    if expiry <= now or (expiry - now).total_seconds() > max_ttl_seconds:
        raise CardError("expiry outside policy")
    return {
        "review_handle": review_handle,
        "plan_digest": expected_digest,
        "nonce": nonce,
        "target": target,
        "families": tuple(families),
        "effects": effects,
        "expires_at": expiry,
    }


def render_card(fields: dict) -> tuple[str, dict]:
    """Return (text, reply_markup) for one card."""

    expiry = fields["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
    text = "\n".join(
        (
            "Pantheon host approval request",
            f"Review: {fields['review_handle'][:12]}",
            f"Target seat: {_clean(fields['target'])}",
            "Operation families: "
            + ", ".join(_clean(f) for f in fields["families"]),
            f"Plan digest: {fields['plan_digest']}",
            f"Expires: {expiry}",
            "",
            "Redacted effects:",
            _bounded_effects(fields["effects"]),
        )
    )
    if utf16_len(text) > MAX_CARD_TEXT:
        text = (
            truncate_utf16(text, MAX_CARD_TEXT - len(_TRUNCATED) - 1)
            + "\n"
            + _TRUNCATED
        )
    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "Approve",
                    "callback_data": callback_data(
                        fields["review_handle"], fields["nonce"], "g"
                    ),
                },
                {
                    "text": "Deny",
                    "callback_data": callback_data(
                        fields["review_handle"], fields["nonce"], "d"
                    ),
                },
            ]
        ]
    }
    return text, markup


def utf16_len(text: str) -> int:
    """Message length as the Bot API counts it: UTF-16 code units (an
    astral-plane character such as most emoji counts as 2)."""

    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    out = []
    units = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > max_units:
            break
        out.append(ch)
        units += width
    return "".join(out)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def closed_text(outcome: str) -> str:
    return {
        "granted": "Pantheon host approval: APPROVED (recorded).",
        "denied": "Pantheon host approval: DENIED (recorded).",
        "refused": "Pantheon host approval: not accepted; card closed.",
        "expired": "Pantheon host approval: card expired.",
        # T-5518 r1 B1: the service could not be asked after expiry
        "unknown": (
            "Pantheon host approval: decision status unknown; "
            "check the approval service."
        ),
    }.get(outcome, "Pantheon host approval: card closed.")


# Keyboard removal: an edit carrying an EMPTY inline keyboard (chosen per
# the coordinator's Q2 note; whether real Telegram removes the buttons
# this way is UNSUPPORTED under gate 8 — correctness never depends on it:
# a stale button is refused by card_closed and the spent nonce).
EMPTY_MARKUP = {"inline_keyboard": []}
