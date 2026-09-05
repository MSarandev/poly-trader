"""place_market_order, place_limit_order (clean-maker rounding), cancel_order."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from polytrader import ValidationError
from polytrader import markets

UP_TOKEN = "1111111111"


def _body_for(fake_venue, path):
    for req in reversed(fake_venue.requests):
        if req.url.path == path:
            return json.loads(req.content or b"{}")
    raise AssertionError(f"no POST recorded for {path}")


# --- market orders --------------------------------------------------------- #

async def test_place_market_order_buy_success(polytrader, fake_venue):
    res = await polytrader.place_market_order(
        token_id=UP_TOKEN, side="BUY", amount_usd=10
    )
    assert res.ok is True
    assert res.status == "matched"
    assert res.order_id == "order-abc-123"
    assert res.spent_usd == Decimal("10.000000")
    assert res.client_order_id  # auto-generated coi present
    body = _body_for(fake_venue, "/place-market-order")
    assert body["amount_usd"] == 10.0
    assert body["order_type"] == "FAK"  # default_order_type


async def test_place_market_order_sell_parses_without_crash(polytrader):
    res = await polytrader.place_market_order(
        token_id=UP_TOKEN, side="SELL", amount_usd=10
    )
    assert res.ok is True
    # SELL parsing path: making=shares, taking=usd; assert no crash + ok.
    assert res.status == "matched"


async def test_place_market_order_respects_explicit_client_order_id(polytrader, fake_venue):
    res = await polytrader.place_market_order(
        token_id=UP_TOKEN, side="BUY", amount_usd=5, client_order_id="my-coi-1"
    )
    assert res.client_order_id == "my-coi-1"
    body = _body_for(fake_venue, "/place-market-order")
    assert body["client_order_id"] == "my-coi-1"


async def test_place_market_order_invalid_side_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.place_market_order(token_id=UP_TOKEN, side="HOLD", amount_usd=5)


async def test_place_market_order_bad_amount_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=0)
    with pytest.raises(ValidationError):
        await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd="nan")


async def test_place_market_order_venue_rejection_structured(polytrader, fake_venue):
    fake_venue.order_error = "book closed"
    res = await polytrader.place_market_order(token_id=UP_TOKEN, side="BUY", amount_usd=5)
    assert res.ok is False
    assert res.status == "rejected"
    assert "book closed" in res.error_msg


# --- limit orders: clean-maker rounding ------------------------------------ #

async def test_place_limit_buy_snaps_to_clean_2dp_maker(polytrader, fake_venue):
    # price 0.72, size 13.875 -> budget 9.99. SDK would roundDown size to 2dp and
    # produce a >2dp maker. shares_for_clean_maker must snap size so maker is
    # exactly 2dp.
    res = await polytrader.place_limit_order(
        token_id=UP_TOKEN, side="BUY", price="0.72", size="13.875", order_type="FAK"
    )
    assert res.ok is True
    body = _body_for(fake_venue, "/place-order")
    price = Decimal(str(body["price"]))
    size = Decimal(str(body["size"]))
    # Size snapped to 13.75 (largest clean-maker size under the 9.99 budget).
    assert size == Decimal("13.75")
    maker = (price * size)
    # maker lands cleanly on the cent grid (2 dp): no fractional cent remainder.
    assert (maker * 100) == (maker * 100).to_integral_value()
    assert maker == Decimal("9.9000")


async def test_place_limit_buy_tiny_budget_rejected_not_submitted(polytrader, fake_venue):
    # budget 0.00099 is too small to buy one clean-maker increment, so
    # shares_for_clean_maker returns None. Must be rejected, not silently
    # submitted with an un-snapped (>2dp) size (clean-maker None regression).
    res = await polytrader.place_limit_order(
        token_id=UP_TOKEN, side="BUY", price="0.99", size="0.001", order_type="FAK"
    )
    assert res.ok is False
    assert res.status == "error"
    assert "clean maker" in res.error_msg.lower()
    # nothing reached the bridge
    assert not any(r.url.path == "/place-order" for r in fake_venue.requests)


async def test_place_limit_sell_passes_size_through_unchanged(polytrader, fake_venue):
    res = await polytrader.place_limit_order(
        token_id=UP_TOKEN, side="SELL", price="0.60", size="10", order_type="GTC"
    )
    assert res.ok is True
    body = _body_for(fake_venue, "/place-order")
    assert Decimal(str(body["size"])) == Decimal("10")
    assert body["order_type"] == "GTC"


async def test_place_limit_rejects_nonpositive_price_size(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.place_limit_order(token_id=UP_TOKEN, side="BUY", price="0", size="5")
    with pytest.raises(ValidationError):
        await polytrader.place_limit_order(token_id=UP_TOKEN, side="BUY", price="0.5", size="0")


async def test_place_limit_bad_numeric_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.place_limit_order(
            token_id=UP_TOKEN, side="BUY", price="abc", size="5"
        )


# --- shares_for_clean_maker unit tests ------------------------------------- #

def test_shares_for_clean_maker_returns_clean_maker():
    out = markets.shares_for_clean_maker(Decimal("9.99"), Decimal("0.72"))
    assert out is not None
    size, maker = out
    assert size == Decimal("13.75")
    assert maker == Decimal("9.90")
    # maker exactly 2dp; size * price reproduces maker.
    assert (Decimal("0.72") * size) == Decimal("9.9000")


def test_shares_for_clean_maker_size_at_most_2dp():
    out = markets.shares_for_clean_maker(Decimal("100"), Decimal("0.37"))
    assert out is not None
    size, maker = out
    # size has at most 2 decimals
    assert size == size.quantize(Decimal("0.01"))
    # maker has at most 2 decimals
    assert maker == maker.quantize(Decimal("0.01"))
    assert (Decimal("0.37") * size).quantize(Decimal("0.01")) == maker


def test_shares_for_clean_maker_none_when_budget_too_small():
    # Not enough budget to buy a single clean-maker increment.
    assert markets.shares_for_clean_maker(Decimal("0.001"), Decimal("0.99")) is None
    assert markets.shares_for_clean_maker(Decimal("0"), Decimal("0.5")) is None
    assert markets.shares_for_clean_maker(Decimal("10"), Decimal("0")) is None


# --- cancel ---------------------------------------------------------------- #

async def test_cancel_order_success(polytrader, fake_venue):
    res = await polytrader.cancel_order("order-abc-123")
    assert res.ok is True
    assert res.status == "cancelled"
    body = _body_for(fake_venue, "/cancel")
    assert body["order_id"] == "order-abc-123"


async def test_cancel_order_venue_rejection_structured(polytrader, fake_venue):
    fake_venue.order_error = "unknown order"
    res = await polytrader.cancel_order("nope")
    assert res.ok is False
    assert res.status == "rejected"
    assert "unknown order" in res.error_msg


async def test_cancel_order_empty_id_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.cancel_order("")
