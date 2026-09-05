"""Integration tests over a real loopback HTTP server.

Unlike the unit suite (httpx.MockTransport), these hit a real TCP server via
pytest-httpserver, exercising the actual httpx stack: real sockets, real
retries, real timeouts. Run with `pytest -m integration`.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from werkzeug.wrappers import Response

from polytrader import PolyTrader, PolyTraderConfig

pytestmark = pytest.mark.integration

MARKET = {
    "slug": "btc-updown-15m-1735689600",
    "question": "Bitcoin Up or Down - 15 minute",
    "conditionId": "0x" + "ab" * 32,
    "endDate": "2025-01-01T00:15:00Z",
    "outcomes": json.dumps(["Up", "Down"]),
    "outcomePrices": json.dumps(["0.52", "0.48"]),
    "clobTokenIds": json.dumps(["1111111111", "2222222222"]),
    "bestBid": "0.51",
    "bestAsk": "0.53",
}
BOOK = {
    "asks": [{"price": "0.55", "size": "500"}, {"price": "0.53", "size": "100"}],
    "bids": [{"price": "0.49", "size": "100"}, {"price": "0.51", "size": "200"}],
    "tick_size": "0.01",
}
ORDER_OK = {
    "client_order_id": "coi",
    "response": {"status": "matched", "orderID": "order-1",
                 "makingAmount": "5.000000", "takingAmount": "9.433962",
                 "success": True},
}


def _cfg(httpserver, **over) -> PolyTraderConfig:
    base = httpserver.url_for("/").rstrip("/")
    kw = dict(bridge_url=base, gamma_api_url=base, clob_api_url=base,
              retry_backoff_s=0.0)
    kw.update(over)
    return PolyTraderConfig(**kw)


def _json_resp(payload, status=200) -> Response:
    return Response(json.dumps(payload), status=status,
                    content_type="application/json")


async def test_health_and_balance_over_real_http(httpserver):
    httpserver.expect_request("/healthz").respond_with_json({"ok": True})
    httpserver.expect_request("/balance").respond_with_json(
        {"balance_raw": "250000000", "balance_usd": 250.0, "allowances": {}})
    async with PolyTrader(_cfg(httpserver)) as pt:
        assert (await pt.verify_conn_health()).ok is True
        bal = await pt.verify_balance()
        assert bal.ok and bal.usd == Decimal("250.0")


async def test_bet_on_over_real_http(httpserver):
    httpserver.expect_request("/markets").respond_with_json([MARKET])
    httpserver.expect_request("/place-market-order").respond_with_json(ORDER_OK)
    async with PolyTrader(_cfg(httpserver)) as pt:
        res = await pt.bet_on("btc-updown-15m-1735689600", "UP", 5)
        assert res.ok is True
        assert res.filled_shares and res.filled_shares > 0


async def test_order_book_and_price_over_real_http(httpserver):
    httpserver.expect_request("/book").respond_with_json(BOOK)
    async with PolyTrader(_cfg(httpserver)) as pt:
        ob = await pt.get_order_book("1111111111")
        assert ob.asks[0].price == Decimal("0.53")   # cheapest ask first
        assert await pt.get_price("1111111111", "BUY") == Decimal("0.53")


async def test_balance_retries_over_real_sockets(httpserver):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _json_resp({"error": "transient"}, 503)
        return _json_resp({"balance_usd": 100.0, "allowances": {}})

    httpserver.expect_request("/balance").respond_with_handler(handler)
    async with PolyTrader(_cfg(httpserver, max_retries=2)) as pt:
        bal = await pt.verify_balance()
    assert bal.ok is True
    assert calls["n"] == 3  # 2x 503 then 200, over real TCP


async def test_order_not_retried_over_real_sockets(httpserver):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _json_resp({"error": "transient"}, 503)

    httpserver.expect_request("/place-market-order").respond_with_handler(handler)
    async with PolyTrader(_cfg(httpserver, max_retries=2)) as pt:
        res = await pt.place_market_order(token_id="1111111111", side="BUY", amount_usd=5)
    assert res.ok is False
    assert calls["n"] == 1  # non-idempotent: exactly one attempt, no double-submit


async def test_withdraw_over_real_http(httpserver):
    httpserver.expect_request("/balance").respond_with_json(
        {"balance_usd": 250.0, "allowances": {}})
    httpserver.expect_request("/withdraw").respond_with_json(
        {"tx_hash": "0x" + "de" * 32})
    async with PolyTrader(_cfg(httpserver, allow_withdraw=True)) as pt:
        res = await pt.withdraw(10, "0x" + "1" * 40)
    assert res.ok is True
    assert res.tx_hash.startswith("0x")


async def test_withdraw_disabled_never_hits_server(httpserver):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _json_resp({"tx_hash": "0x0"})

    httpserver.expect_request("/withdraw").respond_with_handler(handler)
    async with PolyTrader(_cfg(httpserver, allow_withdraw=False)) as pt:
        res = await pt.withdraw(10, "0x" + "1" * 40)
    assert res.ok is False
    assert calls["n"] == 0  # refused client-side, no network call
