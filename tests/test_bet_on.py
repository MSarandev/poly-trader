"""bet_on: outcome resolution, happy path, and structured error handling.

All offline against the FakeVenue in conftest.py.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from conftest import DEFAULT_MARKET

UP_TOKEN = "1111111111"
DOWN_TOKEN = "2222222222"


def _last_order_body(fake_venue):
    """Return the JSON body of the most recent order POST."""
    for req in reversed(fake_venue.requests):
        if req.url.path in ("/place-order", "/place-market-order"):
            return json.loads(req.content or b"{}")
    raise AssertionError("no order POST recorded")


async def test_bet_on_happy_path_market_object(polytrader, fake_venue):
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, "UP", 5)
    assert res.ok is True
    assert res.status == "matched"
    assert res.order_id == "order-abc-123"
    assert res.spent_usd == Decimal("5.000000")
    assert res.filled_shares is not None and res.filled_shares > 0
    # avg_price = making/taking = 5 / (5/0.53) == 0.53
    assert res.avg_price == pytest.approx(Decimal("0.53"))
    body = _last_order_body(fake_venue)
    assert body["token_id"] == UP_TOKEN
    assert body["side"] == "BUY"


async def test_bet_on_by_slug_string_looks_up_market(polytrader, fake_venue):
    res = await polytrader.bet_on(DEFAULT_MARKET["slug"], "UP", 5)
    assert res.ok is True
    # Gamma lookup happened before the order.
    paths = [r.url.path for r in fake_venue.requests]
    assert "/markets" in paths
    assert "/place-market-order" in paths


@pytest.mark.parametrize(
    "outcome,expected_token",
    [
        ("UP", UP_TOKEN),
        ("DOWN", DOWN_TOKEN),
        ("up", UP_TOKEN),
        ("down", DOWN_TOKEN),
        ("YES", UP_TOKEN),     # alias: yes -> up
        ("NO", DOWN_TOKEN),    # alias: no -> down
        ("Up", UP_TOKEN),      # exact label
        ("Down", DOWN_TOKEN),  # exact label
        (0, UP_TOKEN),         # integer index
        (1, DOWN_TOKEN),       # integer index
        (UP_TOKEN, UP_TOKEN),  # explicit token id
        (DOWN_TOKEN, DOWN_TOKEN),
    ],
)
async def test_bet_on_outcome_resolution(polytrader, fake_venue, outcome, expected_token):
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, outcome, 3)
    assert res.ok is True, f"outcome {outcome!r} should resolve"
    body = _last_order_body(fake_venue)
    assert body["token_id"] == expected_token


async def test_bet_on_bad_outcome_returns_structured_error(polytrader):
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, "PURPLE", 5)
    assert res.ok is False
    assert res.status == "error"
    assert "could not resolve" in res.error_msg
    assert "PURPLE" in res.error_msg


async def test_bet_on_out_of_range_index_returns_error(polytrader):
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, 9, 5)
    assert res.ok is False
    assert res.status == "error"


async def test_bet_on_market_not_found_returns_error(polytrader, fake_venue):
    fake_venue.market = None  # Gamma returns []
    res = await polytrader.bet_on("no-such-slug", "UP", 5)
    assert res.ok is False
    assert res.status == "error"
    assert "market not found" in res.error_msg


async def test_bet_on_rejects_nonpositive_amount(polytrader):
    from polytrader import ValidationError

    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    with pytest.raises(ValidationError):
        await polytrader.bet_on(market, "UP", 0)
    with pytest.raises(ValidationError):
        await polytrader.bet_on(market, "UP", -1)


async def test_bet_on_sub_dollar_amount_rejected_client_side(polytrader, fake_venue):
    """The $1 marketable-BUY minimum is enforced client-side: a sub-$1 BUY
    fails fast with a structured error and never reaches the bridge (config
    min_marketable_buy_usd defaults to 1.0; 0 would disable it)."""
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, "UP", "0.50")
    assert res.ok is False
    assert res.status == "error"
    assert "minimum" in res.error_msg.lower()


async def test_bet_on_sub_dollar_minimum_disabled_passes_through(fake_venue, make_polytrader):
    """With min_marketable_buy_usd=0 the local floor is disabled and the sub-$1
    amount is forwarded to the bridge (venue then decides)."""
    pt = make_polytrader(min_marketable_buy_usd=0)
    market = await pt.get_market(DEFAULT_MARKET["slug"])
    res = await pt.bet_on(market, "UP", "0.50")
    assert res.ok is True
    body = _last_order_body(fake_venue)
    assert body["amount_usd"] == 0.5


async def test_bet_on_venue_rejection_is_structured(polytrader, fake_venue):
    fake_venue.order_error = "insufficient balance"
    market = await polytrader.get_market(DEFAULT_MARKET["slug"])
    res = await polytrader.bet_on(market, "UP", 5)
    assert res.ok is False
    assert res.status == "rejected"
    assert "insufficient balance" in res.error_msg
