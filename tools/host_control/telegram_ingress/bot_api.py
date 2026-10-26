"""Minimal Telegram Bot API client for the approval ingress (T-2919).

Five methods only — ``getMe``, ``getUpdates``, ``sendMessage``,
``editMessageText``, ``answerCallbackQuery`` — over stdlib urllib, JSON
POST bodies, no webhook (the ingress has NO listening socket: updates are
provider-authenticated by the TLS channel to the Bot API, never by an
inbound request anyone could forge).

Gate 21: the bot token appears ONLY in the request URL path (Bot API
authentication) and is never logged, raised, chained, or put in a body.
Every failure becomes ``BotApiError(code)`` with a closed code and NO
chained cause (``from None``): exception text from urllib can carry the
request object, and a traceback must not walk back to the URL. The token
is held as ``bytes`` and excluded from ``repr``.

Per the verified Bot API reference (coordinator Q2 follow-up): every
response carries ``ok``; failures carry ``description`` and an Integer
``error_code`` whose contents are subject to change. The client fails
closed on ANY non-2xx status or ``ok`` != true; an auth-shaped failure
(HTTP 401/403 or error_code 401/403) is reported as ``auth`` so the
runner may re-lease once, but correctness never depends on that code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

TELEGRAM_API_BASE = "https://api.telegram.org"
_TEST_BASE_PREFIX = "http://127.0.0.1:"
MAX_RESPONSE_BYTES = 1 << 20
BOT_API_ERROR_CODES = frozenset(
    ("transport", "http_status", "auth", "malformed", "not_ok", "invalid_request")
)


class BotApiError(RuntimeError):
    """Closed, non-disclosing Bot API failure."""

    def __init__(self, code: str) -> None:
        if code not in BOT_API_ERROR_CODES:
            code = "transport"
        self.code = code
        super().__init__(f"bot api failure: {code}")


def _error_detail(method: str, url: str, exc: BaseException | None) -> str:
    """What a failure may say about itself: the method name only.

    The mutation suite neuters this (returning the URL) to prove the
    leak canaries catch a client that reports its request."""

    return method


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


class BotApiClient:
    def __init__(
        self,
        token: bytes,
        *,
        base_url: str = TELEGRAM_API_BASE,
        timeout_slack_seconds: float = 10.0,
    ) -> None:
        if not isinstance(token, bytes) or not token or b"/" in token or (
            any(byte < 0x21 or byte > 0x7E for byte in token)
        ):
            raise BotApiError("invalid_request")
        if base_url != TELEGRAM_API_BASE and not (
            base_url.startswith(_TEST_BASE_PREFIX)
            and base_url[len(_TEST_BASE_PREFIX):].isdigit()
        ):
            # only the real API, or a loopback fixture server in tests
            raise BotApiError("invalid_request")
        self._token = token
        self._base = base_url
        self._slack = float(timeout_slack_seconds)
        # no proxies from the environment (a proxy would see the token),
        # no redirects (a redirect could carry the path elsewhere)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        self.last_error_detail: str | None = None

    def __repr__(self) -> str:
        return "BotApiClient(<redacted>)"

    def _call(self, method: str, payload: dict, *, timeout: float) -> object:
        url = f"{self._base}/bot{self._token.decode('ascii')}/{method}"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        status: int | None = None
        raw = b""
        failure: str | None = None
        cause: BaseException | None = None
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                raw = b""
            finally:
                exc.close()
        except Exception as exc:
            failure, cause = "transport", exc
        if failure is None:
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            error_code = (
                decoded.get("error_code") if isinstance(decoded, dict) else None
            )
            if status in (401, 403) or error_code in (401, 403):
                failure = "auth"
            elif status is None or not 200 <= status < 300:
                failure = "http_status"
            elif len(raw) > MAX_RESPONSE_BYTES or not isinstance(decoded, dict):
                failure = "malformed"
            elif decoded.get("ok") is not True or "result" not in decoded:
                failure = "not_ok"
        if failure is not None:
            # ONE redaction point for EVERY failure path (gate 21)
            self.last_error_detail = _error_detail(method, url, cause)
            del url, request, cause
            raise BotApiError(failure) from None
        del url, request
        return decoded["result"]

    # -- the five methods ---------------------------------------------

    def get_me(self) -> dict:
        result = self._call("getMe", {}, timeout=self._slack)
        if not isinstance(result, dict):
            raise BotApiError("malformed")
        return result

    def get_updates(self, *, offset: int, timeout: int, limit: int = 100) -> list:
        payload = {
            "offset": int(offset),
            "timeout": int(timeout),
            "limit": max(1, min(100, int(limit))),
            "allowed_updates": [
                "message",
                "edited_message",
                "channel_post",
                "callback_query",
            ],
        }
        result = self._call(
            "getUpdates", payload, timeout=int(timeout) + self._slack
        )
        if not isinstance(result, list):
            raise BotApiError("malformed")
        return result

    def send_message(self, *, chat_id: int, text: str, reply_markup: dict) -> dict:
        result = self._call(
            "sendMessage",
            {"chat_id": int(chat_id), "text": text, "reply_markup": reply_markup},
            timeout=self._slack,
        )
        if not isinstance(result, dict):
            raise BotApiError("malformed")
        return result

    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict,
    ) -> object:
        return self._call(
            "editMessageText",
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": text,
                "reply_markup": reply_markup,
            },
            timeout=self._slack,
        )

    def answer_callback_query(self, *, callback_query_id: str, text: str) -> object:
        return self._call(
            "answerCallbackQuery",
            {"callback_query_id": str(callback_query_id), "text": text[:200]},
            timeout=self._slack,
        )
