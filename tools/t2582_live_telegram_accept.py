#!/usr/bin/env python3
"""tools/t2582_live_telegram_accept.py — gated live Telegram send-accept leg (T-2582).

Upgrades T-2568's mocked-contract ``telegram-outbound`` row to a genuine
"Telegram accepted a real message" proof: a real ``sendMessage`` call to the
dedicated non-operator test chat (``-5356823915``, group "Test group",
provisioned by the operator and captured 2026-08-08 — see ticket T-2582's
transition log) via the existing operating bot token, read from the isolated
``pantheon-test``-owned path so this never touches the live operating
``.env``.

Mirrors ``tools/t2382_live_acceptance.py``'s gate/skip shape (an explicit
opt-in env var, absent by default, missing it raises :class:`AcceptanceSkipped`
rather than silently no-op'ing) — same name/behavior, deliberately NOT
imported from that module: T-2382's ``AcceptanceSkipped``/``LiveLeg`` are
scoped to its own codex/claude/same-unit-swap/forced-cap leg family, and
importing them here would couple this small telegram-only leg to that
unrelated domain for no benefit.

SEND-ISOLATION (mirrors ``telegram_outbound.py``'s own crux): the always-run
suite (``python -m pytest tools/ -q``) never sets
``T2582_LIVE_TELEGRAM_ACCEPT``, so :func:`require_live_gate` always raises
:class:`AcceptanceSkipped` there and :func:`make_live_send_fn` is never
constructed. :func:`run_live_accept` takes an injected
:data:`~tools.bringup_acceptance.telegram_outbound.SendFn`, exactly like
``TelegramOutboundModule`` — the tests for this module exercise that function
with a fake ``SendFn`` and never touch the live wiring in :func:`main`.

Operator-refusal (belt-and-suspenders, mirrors ``telegram_outbound.py``'s own
``_REAL_OPERATOR_CHAT_ID`` guard): :func:`refuse_operator_chat` HardStops if
the target chat_id is ever the real operator chat ``424242424`` — an
independent module-level literal, not an import of that module's constant,
so a future change there cannot silently widen or narrow this guard.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path
from typing import Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bringup_acceptance.telegram_outbound import (
    SendFn,
    SendRequest,
    assert_chat_id_matches,
    make_live_send_fn,
)
from tools.bringup_acceptance.types import HardStop

GATE_ENV = "T2582_LIVE_TELEGRAM_ACCEPT"
_GATE_TRUE_VALUES = frozenset({"1", "true", "yes"})

# The dedicated non-operator test chat captured in T-2582's transition log
# (operator-provisioned group "Test group", 2026-08-08).
DEDICATED_TEST_CHAT_ID = "-5356823915"

# The real operator chat — this leg must NEVER be wired at it. Independent
# literal (see module docstring) rather than importing telegram_outbound's
# own constant.
_REAL_OPERATOR_CHAT_ID = "424242424"

# The isolated pantheon-test-owned token path (mode 600, not readable by the
# `claude` user without sudo — deliberate isolation from the live operating
# ~/.claude/channels/telegram/.env).
DEFAULT_TOKEN_PATH = Path("/home/pantheon-test/.config/telegram-test-bot/token")


class AcceptanceSkipped(RuntimeError):
    """Raised when the live gate env var is not explicitly set."""


@dataclasses.dataclass(frozen=True)
class LiveAcceptResult:
    """Ground truth from one real send-accept round-trip."""

    message_id: str
    chat_id: str


def require_live_gate(env: Mapping[str, str]) -> None:
    """Raise :class:`AcceptanceSkipped` unless ``GATE_ENV`` is explicitly truthy."""
    if env.get(GATE_ENV) not in _GATE_TRUE_VALUES:
        raise AcceptanceSkipped(
            f"missing explicit live gate {GATE_ENV}; justifies-via P1/P4: the "
            "telegram live-accept leg is operator-gated"
        )


def refuse_operator_chat(chat_id: str) -> None:
    """HardStop if ``chat_id`` is the real operator chat."""
    if chat_id == _REAL_OPERATOR_CHAT_ID:
        raise HardStop(
            f"refusing t2582 live telegram-accept leg against the REAL operator chat "
            f"{_REAL_OPERATOR_CHAT_ID} — chat_id must be the dedicated non-operator test chat"
        )


def run_live_accept(*, send: SendFn, chat_id: str) -> LiveAcceptResult:
    """Send one probe message and assert genuine send-accept.

    Mirrors ``TelegramOutboundModule._prove_send``'s assertions (ok,
    message_id present, chat_id echoed) via the injected ``send`` seam.
    """
    refuse_operator_chat(chat_id)
    request = SendRequest(chat_id=chat_id, text="T-2582 live telegram-accept probe")
    result = send(request)
    if not result.ok:
        raise HardStop(
            f"live send-accept: sendMessage did not report ok "
            f"(error_kind={result.error_kind!r} raw={result.raw!r})"
        )
    if not result.message_id:
        raise HardStop(
            f"live send-accept: sendMessage accepted but returned no message_id "
            f"(raw={result.raw!r})"
        )
    assert_chat_id_matches(result=result, intended_chat_id=chat_id)
    return LiveAcceptResult(message_id=result.message_id, chat_id=result.chat_id)


def read_token(token_path: Path) -> str:
    """Read + strip the isolated bot token file."""
    return token_path.read_text(encoding="utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", default=DEDICATED_TEST_CHAT_ID)
    parser.add_argument("--token-path", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args(argv)

    try:
        require_live_gate(os.environ)
    except AcceptanceSkipped as exc:
        print(f"SKIPPED: {exc}")
        return 0

    token = read_token(args.token_path)
    send = make_live_send_fn(token)
    result = run_live_accept(send=send, chat_id=args.chat_id)
    print(f"ACCEPTED: message_id={result.message_id} chat_id={result.chat_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
