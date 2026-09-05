"""verify_balance (incl. the null / non-finite payload guard) and
verify_conn_health (bridge up / bridge down / CLOB down)."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from conftest import BRIDGE_URL


# --- verify_balance -------------------------------------------------------- #

async def test_verify_balance_happy_path(polytrader):
    bal = await polytrader.verify_balance()
    assert bal.ok is True
    assert bal.usd == Decimal("250.0")
    assert bal.error_msg is None
    assert bal.allowances is not None


async def test_verify_balance_null_payload_no_crash(polytrader, fake_venue):
    """Real prod bug: a null balance_usd used to crash. Must return ok=False."""
    fake_venue.balance_usd = None  # bridge emits balance_usd: null
    bal = await polytrader.verify_balance()  # must not raise
    assert bal.ok is False
    assert bal.usd is None
    assert "null balance" in bal.error_msg


async def test_verify_balance_non_finite_no_crash(polytrader, fake_venue):
    """A NaN/Infinity string in balance_usd must be rejected, not crash."""
    def handler(request):
        return httpx.Response(200, json={
            "balance_raw": "0", "balance_usd": "NaN", "allowances": {},
        })

    fake_venue.set_handler("GET", "/balance", handler)
    bal = await polytrader.verify_balance()
    assert bal.ok is False
    assert bal.usd is None
    assert "non-finite" in bal.error_msg


async def test_verify_balance_unparseable_no_crash(polytrader, fake_venue):
    def handler(request):
        return httpx.Response(200, json={
            "balance_raw": "0", "balance_usd": "not-a-number", "allowances": {},
        })

    fake_venue.set_handler("GET", "/balance", handler)
    bal = await polytrader.verify_balance()
    assert bal.ok is False
    assert bal.usd is None
    assert "unparseable" in bal.error_msg


async def test_verify_balance_transport_failure_no_crash(polytrader, fake_venue):
    def handler(request):
        raise httpx.ConnectError("bridge down")

    fake_venue.set_handler("GET", "/balance", handler)
    bal = await polytrader.verify_balance()
    assert bal.ok is False
    assert bal.usd is None
    assert "balance fetch failed" in bal.error_msg


# --- verify_conn_health ---------------------------------------------------- #

async def test_health_bridge_up_clob_up(polytrader):
    h = await polytrader.verify_conn_health()
    assert h.ok is True
    assert h.bridge_ok is True
    assert h.clob_ok is True
    assert h.latency_ms is not None


async def test_health_bridge_down_transport_error(polytrader, fake_venue):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    fake_venue.set_handler("GET", "/healthz", handler)
    h = await polytrader.verify_conn_health()  # never raises
    assert h.ok is False
    assert h.bridge_ok is False
    assert h.clob_ok is False
    assert "bridge unreachable" in h.detail


async def test_health_bridge_down_503(polytrader, fake_venue):
    fake_venue.fail_healthz = True  # /healthz -> 503 (raise_for_status)
    h = await polytrader.verify_conn_health()
    assert h.ok is False
    assert h.bridge_ok is False


async def test_health_bridge_up_clob_down_null_balance(polytrader, fake_venue):
    fake_venue.balance_usd = None  # bridge reachable, CLOB balance null
    h = await polytrader.verify_conn_health()
    assert h.ok is False
    assert h.bridge_ok is True
    assert h.clob_ok is False
    assert "CLOB balance unavailable" in h.detail


async def test_health_bridge_up_clob_down_balance_5xx(polytrader, fake_venue):
    fake_venue.balance_status = 500  # CLOB probe errors out
    h = await polytrader.verify_conn_health()
    assert h.ok is False
    assert h.bridge_ok is True
    assert h.clob_ok is False
