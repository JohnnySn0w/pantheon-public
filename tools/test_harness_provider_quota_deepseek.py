"""T-5005 (T-4924 H1): DeepSeek prepaid-balance client, observer and banding.

Uses DeepSeek's documented GET /user/balance example body (spec-derived; a
redacted live body joins these tests at H7). No test touches the network.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.request
import urllib.response
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tools.harness_adapter.provider_quota import (
    DeepSeekBalanceObserver,
    DeepSeekBalanceReadError,
    _read_env_key,
    build_deepseek_opener,
    parse_deepseek_balance_observation,
    read_deepseek_balance,
)
from tools.harness_adapter.quota import (
    BalanceReading,
    BalanceThresholds,
    QuotaSnapshot,
    classify_balance,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
# DeepSeek API docs, "Get User Balance" example response (spec-derived).
DOCUMENTED_BODY = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "110.00",
            "granted_balance": "10.00",
            "topped_up_balance": "100.00",
        }
    ],
}
FAKE_KEY = "fixture-key-not-real-0000"


def _thresholds(low: str = "50", floor: str = "7", currency: str = "CNY") -> BalanceThresholds:
    return BalanceThresholds(currency=currency, low_water=Decimal(low), hard_floor=Decimal(floor))


def _body(total: str, *, available: bool = True, currency: str = "CNY") -> dict:
    return {
        "is_available": available,
        "balance_infos": [
            {"currency": currency, "total_balance": total, "granted_balance": "0.00", "topped_up_balance": total}
        ],
    }


def _parse(body, thresholds=None):
    return parse_deepseek_balance_observation(
        body, profile="hermes/deepseek-direct", thresholds=thresholds or _thresholds(), observed_at=NOW
    )


def test_documented_body_parses_as_decimal_and_is_ok():
    snapshot = _parse(DOCUMENTED_BODY)
    assert snapshot.windows == ()
    assert snapshot.freshness == "fresh"
    assert snapshot.balance == BalanceReading(
        is_available=True, currency="CNY", total=Decimal("110.00"),
        granted=Decimal("10.00"), topped_up=Decimal("100.00"), observed_at=NOW,
    )
    assert snapshot.evidence["balance_band"] == "ok"
    assert snapshot.evidence["provider"] == "deepseek"
    assert snapshot.evidence["source"] == "provider-api"


@pytest.mark.parametrize(
    "total,available,band",
    [
        ("110.00", True, "ok"),
        ("50.00", True, "ok"),
        ("49.99", True, "low"),
        ("7.01", True, "low"),
        ("7.00", True, "exhausted"),
        ("0.00", True, "exhausted"),
        ("110.00", False, "exhausted"),
    ],
)
def test_bands(total, available, band):
    assert _parse(_body(total, available=available)).evidence["balance_band"] == band


@pytest.mark.parametrize(
    "body,reason",
    [
        ({}, "malformed-body"),
        ({"is_available": "yes", "balance_infos": []}, "malformed-body"),
        ({"is_available": True, "balance_infos": ["x"]}, "malformed-body"),
        (_body("abc"), "malformed-body"),
        (_body("NaN"), "malformed-body"),
        (_body("10.00", currency="EUR"), "currency-absent"),
        (_body("10.00", currency="USD"), "currency-absent"),
    ],
)
def test_malformed_or_unknown_currency_is_unknown(body, reason):
    snapshot = _parse(body)
    assert snapshot.evidence["balance_band"] == "unknown"
    assert snapshot.evidence["reason"] == reason
    assert snapshot.balance is None
    assert snapshot.freshness == "unavailable"


def test_no_thresholds_is_unknown():
    snapshot = parse_deepseek_balance_observation(
        DOCUMENTED_BODY, profile="p", thresholds=None, observed_at=NOW
    )
    assert snapshot.evidence["balance_band"] == "unknown"


def test_classify_balance_edge_cases():
    reading = BalanceReading(True, "USD", Decimal("5"), Decimal("0"), Decimal("5"), NOW)
    assert classify_balance(None, _thresholds()) == "unknown"
    assert classify_balance(reading, None) == "unknown"
    assert classify_balance(reading, _thresholds(currency="CNY")) == "unknown"
    assert classify_balance(reading, _thresholds(low="1", floor="2", currency="USD")) == "unknown"
    assert classify_balance(reading, _thresholds(low="10", floor="1", currency="USD")) == "low"


def test_snapshot_balance_defaults_to_none_for_windowed_providers():
    snapshot = QuotaSnapshot(profile="codex/x", windows=(), observed_at=NOW, freshness="fresh")
    assert snapshot.balance is None
    assert snapshot == QuotaSnapshot("codex/x", (), NOW, "fresh", {})


def test_observer_wraps_read_failures_without_leaking():
    def boom():
        raise DeepSeekBalanceReadError("balance request failed: URLError")

    snapshot = DeepSeekBalanceObserver(boom, profile="p", thresholds=_thresholds(), now=lambda: NOW).read()
    assert snapshot.freshness == "unavailable"
    assert snapshot.evidence["reason"] == "read-failed"
    assert snapshot.evidence["balance_band"] == "unknown"
    missing = DeepSeekBalanceObserver(lambda: None, profile="p", thresholds=_thresholds(), now=lambda: NOW).read()
    assert missing.evidence["reason"] == "missing-observation"


def test_observer_reads_documented_body():
    snapshot = DeepSeekBalanceObserver(
        lambda: DOCUMENTED_BODY, profile="p", thresholds=_thresholds(), now=lambda: NOW
    ).read()
    assert snapshot.evidence["balance_band"] == "ok"


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_sends_bearer_and_parses_body(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"OTHER=1\nDEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps(DOCUMENTED_BODY).encode("utf-8"))

    body = read_deepseek_balance(env, opener=opener)
    assert body == DOCUMENTED_BODY
    assert seen["url"] == "https://api.deepseek.com/user/balance"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"


def test_client_errors_never_carry_the_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    def failing(request, timeout):
        raise OSError(f"connection refused while sending {FAKE_KEY}")

    with pytest.raises(DeepSeekBalanceReadError) as info:
        read_deepseek_balance(env, opener=failing)
    assert FAKE_KEY not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None

    with pytest.raises(DeepSeekBalanceReadError):
        read_deepseek_balance(env, opener=lambda r, timeout: _FakeResponse(b"not json"))
    with pytest.raises(DeepSeekBalanceReadError):
        read_deepseek_balance(env, opener=lambda r, timeout: _FakeResponse(b"[]"))
    with pytest.raises(DeepSeekBalanceReadError):
        read_deepseek_balance(env, opener=lambda r, timeout: _FakeResponse(b" " * (64 * 1024 + 10)))


def test_client_without_key_does_not_call_out(tmp_path):
    called = []
    with pytest.raises(DeepSeekBalanceReadError):
        read_deepseek_balance(tmp_path / "missing.env", opener=lambda *a, **k: called.append(1))
    assert called == []


class _RedirectingTransport(urllib.request.BaseHandler):
    """A fake https transport: api.deepseek.com answers 302 to another host;
    any other host would answer 200. Records every request it is handed.
    handler_order 100 puts it ahead of the real HTTPSHandler (500), so no test
    request reaches the network."""

    handler_order = 100

    def __init__(self):
        self.seen = []

    def https_open(self, request):
        self.seen.append((request.full_url, request.get_header("Authorization")))
        if request.host == "api.deepseek.com":
            headers = email.message.Message()
            headers["Location"] = "https://collector.example/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, code=302)
            response.msg = "Found"
            return response
        body = json.dumps(DOCUMENTED_BODY).encode("utf-8")
        response = urllib.response.addinfourl(io.BytesIO(body), email.message.Message(), request.full_url, code=200)
        response.msg = "OK"
        return response


def test_redirect_is_refused_and_key_never_reaches_a_second_host(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    transport = _RedirectingTransport()
    opener = build_deepseek_opener(transport)
    with pytest.raises(DeepSeekBalanceReadError) as info:
        read_deepseek_balance(env, opener=opener.open)
    assert "HTTPError" in str(info.value)
    assert transport.seen == [("https://api.deepseek.com/user/balance", f"Bearer {FAKE_KEY}")]

    # Negative control: the stock opener follows the 302 and re-sends the key.
    stock_transport = _RedirectingTransport()
    stock = urllib.request.build_opener(stock_transport)
    read_deepseek_balance(env, opener=stock.open)
    assert stock_transport.seen[1] == ("https://collector.example/steal", f"Bearer {FAKE_KEY}")


def test_default_opener_refuses_redirects():
    from tools.harness_adapter.provider_quota import _RefuseRedirects

    opener = build_deepseek_opener()
    assert any(isinstance(handler, _RefuseRedirects) for handler in opener.handlers)
    assert not any(
        type(handler) is urllib.request.HTTPRedirectHandler for handler in opener.handlers
    )


@pytest.mark.parametrize("base_url", ["http://api.deepseek.com", "ftp://x", "api.deepseek.com"])
def test_non_https_base_url_is_refused_before_any_call(tmp_path, base_url):
    env = tmp_path / ".env"
    env.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    called = []
    with pytest.raises(DeepSeekBalanceReadError) as info:
        read_deepseek_balance(env, base_url=base_url, opener=lambda *a, **k: called.append(1))
    assert "https" in str(info.value)
    assert called == []


@pytest.mark.parametrize(
    "line,expected",
    [
        ("DEEPSEEK_API_KEY=abc # rotated 10-14", "abc"),
        ("DEEPSEEK_API_KEY=abc#notacomment", "abc#notacomment"),
        ('DEEPSEEK_API_KEY="abc # kept" # tail', "abc # kept"),
        ("export DEEPSEEK_API_KEY='abc'", "abc"),
        ("DEEPSEEK_API_KEY=", None),
    ],
)
def test_env_key_parsing(tmp_path, line, expected):
    env = tmp_path / ".env"
    env.write_text(line + "\n", encoding="utf-8")
    assert _read_env_key(env, "DEEPSEEK_API_KEY") == expected
