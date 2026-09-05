"""withdraw (guarded) and deposit; mocked at the bridge-HTTP boundary so no eth
libraries are imported."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

VALID_ADDR = "0x" + "1" * 40


# --- withdraw -------------------------------------------------------------- #

async def test_withdraw_disabled_by_default_refuses(make_polytrader):
    async with make_polytrader(allow_withdraw=False) as pt:
        res = await pt.withdraw(10, VALID_ADDR)
    assert res.ok is False
    assert res.tx_hash is None
    assert "disabled" in res.error_msg


async def test_withdraw_enabled_success_returns_tx_hash(polytrader, fake_venue):
    # default config allow_withdraw=True, balance 250.
    res = await polytrader.withdraw(100, VALID_ADDR)
    assert res.ok is True
    assert res.tx_hash == "0x" + "de" * 32
    assert res.error_msg is None
    # ensure it went through the bridge /withdraw endpoint
    wd = [r for r in fake_venue.requests if r.url.path == "/withdraw"]
    assert wd
    body = json.loads(wd[-1].content or b"{}")
    assert body["to"] == VALID_ADDR
    assert body["amount_usd"] == 100.0


async def test_withdraw_invalid_address_refuses(polytrader, fake_venue):
    res = await polytrader.withdraw(10, "0xnothex")
    assert res.ok is False
    assert "invalid to_address" in res.error_msg
    # never reached the bridge
    assert not [r for r in fake_venue.requests if r.url.path == "/withdraw"]


async def test_withdraw_amount_exceeds_balance_refuses(polytrader, fake_venue):
    res = await polytrader.withdraw(1000, VALID_ADDR)  # balance is 250
    assert res.ok is False
    assert "exceeds balance" in res.error_msg
    assert not [r for r in fake_venue.requests if r.url.path == "/withdraw"]


async def test_withdraw_nonpositive_amount_refuses(polytrader):
    res = await polytrader.withdraw(0, VALID_ADDR)
    assert res.ok is False
    assert res.error_msg is not None


async def test_withdraw_fails_closed_when_balance_unreadable(polytrader, fake_venue):
    fake_venue.balance_usd = None  # cannot verify balance -> fail closed
    res = await polytrader.withdraw(10, VALID_ADDR)
    assert res.ok is False
    assert "cannot verify balance" in res.error_msg
    assert not [r for r in fake_venue.requests if r.url.path == "/withdraw"]


async def test_withdraw_bridge_disabled_403_is_structured(polytrader, fake_venue):
    # config allows it but the bridge itself refuses (BRIDGE_ALLOW_WITHDRAW off).
    fake_venue.allow_withdraw = False  # bridge /withdraw -> 403
    res = await polytrader.withdraw(10, VALID_ADDR)
    assert res.ok is False
    assert res.error_msg is not None  # surfaced, not raised


async def test_withdraw_no_tx_hash_in_response_is_not_ok(polytrader, fake_venue):
    def handler(request):
        return httpx.Response(200, json={"latency_ms": 1})  # no tx_hash

    fake_venue.set_handler("POST", "/withdraw", handler)
    res = await polytrader.withdraw(10, VALID_ADDR)
    assert res.ok is False
    assert "no tx_hash" in res.error_msg


# --- deposit --------------------------------------------------------------- #

async def test_deposit_returns_instructions_with_address(polytrader):
    info = await polytrader.deposit(50)
    assert info.deposit_address == "0x" + "3" * 40  # from test_config
    assert info.amount_usd == Decimal("50")
    assert "Polygon" in info.chain
    assert "verify_balance" in info.instructions


async def test_deposit_without_address(make_polytrader):
    async with make_polytrader(deposit_address=None) as pt:
        info = await pt.deposit()
    assert info.deposit_address is None
    assert "No deposit_address configured" in info.instructions
