"""Retry/backoff on transient failures, no-retry on 4xx, timeouts, and
context-manager lifecycle."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from polytrader import PolyTrader
from polytrader._http import request_with_retry

UP_TOKEN = "1111111111"


def _count(fake_venue, path):
    return len([r for r in fake_venue.requests if r.url.path == path])


# --- retry via the real client stack --------------------------------------- #

async def test_balance_retries_transient_5xx_then_succeeds(polytrader, fake_venue):
    fake_venue.transient_failures = 2  # 2x 503 then 200; max_retries=2 => 3 attempts
    bal = await polytrader.verify_balance()
    assert bal.ok is True
    assert bal.usd == Decimal("250.0")
    assert _count(fake_venue, "/balance") == 3


async def test_balance_gives_up_after_budget_exhausted(polytrader, fake_venue):
    fake_venue.transient_failures = 3  # exceeds the 3-attempt budget
    bal = await polytrader.verify_balance()  # must not raise
    assert bal.ok is False
    assert _count(fake_venue, "/balance") == 3  # 3 attempts, all 503


async def test_order_5xx_is_not_retried_no_double_submit(polytrader, fake_venue):
    # Orders are non-idempotent (the bridge doesn't dedup on client_order_id),
    # so a transient 5xx must NOT be auto-retried — retrying after a possibly-
    # accepted order would double-fill. Exactly one attempt, structured error.
    fake_venue.transient_failures = 2  # would-be-transient, but orders don't retry
    res = await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=5)
    assert res.ok is False
    assert _count(fake_venue, "/place-market-order") == 1  # single attempt, no retry


async def test_order_4xx_rejection_is_not_retried(polytrader, fake_venue):
    fake_venue.order_error = "invalid amount"  # 400 rejection
    res = await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=5)
    assert res.ok is False
    assert res.status == "rejected"
    assert "invalid amount" in res.error_msg
    assert _count(fake_venue, "/place-market-order") == 1  # NOT retried


async def test_order_transport_error_surfaces_structured(polytrader, fake_venue):
    def handler(request):
        raise httpx.ConnectError("boom")

    fake_venue.set_handler("POST", "/place-market-order", handler)
    res = await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=5)
    assert res.ok is False
    assert "transport" in res.error_msg


async def test_market_data_retries_transient(polytrader, fake_venue):
    fake_venue.transient_failures = 2
    ob = await polytrader.get_order_book(UP_TOKEN)
    assert ob is not None
    assert _count(fake_venue, "/book") == 3


# --- timeouts -------------------------------------------------------------- #

async def test_balance_timeout_no_crash(polytrader, fake_venue):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    fake_venue.set_handler("GET", "/balance", handler)
    bal = await polytrader.verify_balance()
    assert bal.ok is False
    assert bal.usd is None


async def test_order_timeout_surfaces_structured(polytrader, fake_venue):
    def handler(request):
        raise httpx.ReadTimeout("slow")

    fake_venue.set_handler("POST", "/place-market-order", handler)
    res = await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=5)
    assert res.ok is False
    assert "transport" in res.error_msg


# --- request_with_retry unit tests ----------------------------------------- #

async def test_request_with_retry_retries_transport_then_succeeds():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("nope")
        return httpx.Response(200, json={"ok": True})

    resp = await request_with_retry(send, max_retries=2, backoff_s=0.0)
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_request_with_retry_retries_5xx_then_succeeds():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, json={"error": "x"})
        return httpx.Response(200, json={"ok": True})

    resp = await request_with_retry(send, max_retries=2, backoff_s=0.0)
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_request_with_retry_retries_429():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"ok": True})

    resp = await request_with_retry(send, max_retries=2, backoff_s=0.0)
    assert resp.status_code == 200
    assert calls["n"] == 2


async def test_request_with_retry_does_not_retry_4xx():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    resp = await request_with_retry(send, max_retries=3, backoff_s=0.0)
    assert resp.status_code == 400
    assert calls["n"] == 1  # returned immediately, not retried


async def test_request_with_retry_raises_when_budget_exhausted():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        raise httpx.ConnectError("always down")

    with pytest.raises(httpx.ConnectError):
        await request_with_retry(send, max_retries=2, backoff_s=0.0)
    assert calls["n"] == 3  # max_retries + 1 attempts


# --- lifecycle / context manager ------------------------------------------- #

async def test_injected_client_not_closed_by_close(make_polytrader, mock_transport):
    pt = make_polytrader()  # borrows the mock-backed client (_owns_http=False)
    assert pt._owns_http is False
    await pt.close()
    assert pt._http.is_closed is False  # injected client left open for its owner


async def test_context_manager_does_not_close_injected_client(make_polytrader):
    async with make_polytrader() as pt:
        client = pt._http
        assert (await pt.verify_balance()).ok is True
    assert client.is_closed is False


async def test_owned_client_is_closed_on_close(test_config):
    pt = PolyTrader(test_config)  # no injected client -> owns it
    assert pt._owns_http is True
    assert pt._http.is_closed is False
    await pt.close()
    assert pt._http.is_closed is True
