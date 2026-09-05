"""get_market (slug + condition_id), get_order_book, get_price, and the pure
parsers."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from polytrader import ValidationError
from polytrader import markets
from conftest import DEFAULT_MARKET, DEFAULT_BOOK

CONDITION_ID = "0x" + "ab" * 32
UP_TOKEN = "1111111111"
DOWN_TOKEN = "2222222222"


# --- get_market ------------------------------------------------------------ #

async def test_get_market_by_slug(polytrader, fake_venue):
    m = await polytrader.get_market(DEFAULT_MARKET["slug"])
    assert m is not None
    assert m.slug == DEFAULT_MARKET["slug"]
    assert m.condition_id == CONDITION_ID
    assert len(m.outcomes) == 2
    assert m.outcomes[0].label == "Up"
    assert m.outcomes[0].token_id == UP_TOKEN
    assert m.outcomes[1].token_id == DOWN_TOKEN
    assert m.outcomes[0].price == Decimal("0.52")
    assert m.best_bid == Decimal("0.51")
    assert m.best_ask == Decimal("0.53")
    # Routed via slug param, not condition_ids.
    markets_reqs = [r for r in fake_venue.requests if r.url.path == "/markets"]
    assert markets_reqs
    assert "slug" in markets_reqs[-1].url.params


async def test_get_market_by_condition_id(polytrader, fake_venue):
    m = await polytrader.get_market(CONDITION_ID)
    assert m is not None
    assert m.condition_id == CONDITION_ID
    markets_reqs = [r for r in fake_venue.requests if r.url.path == "/markets"]
    assert "condition_ids" in markets_reqs[-1].url.params


async def test_get_market_not_found_returns_none(polytrader, fake_venue):
    fake_venue.market = None
    m = await polytrader.get_market("missing-slug")
    assert m is None


async def test_get_market_empty_arg_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.get_market("")


# --- get_order_book -------------------------------------------------------- #

async def test_get_order_book_parsing_and_sorting(polytrader):
    ob = await polytrader.get_order_book(UP_TOKEN)
    assert ob is not None
    assert ob.token_id == UP_TOKEN
    # asks cheapest-first, bids highest-first.
    assert [lv.price for lv in ob.asks] == [Decimal("0.53"), Decimal("0.55")]
    assert [lv.price for lv in ob.bids] == [Decimal("0.51"), Decimal("0.49")]
    assert ob.best_ask == Decimal("0.53")
    assert ob.best_bid == Decimal("0.51")
    assert ob.tick_size == Decimal("0.01")


async def test_get_order_book_missing_returns_none(polytrader, fake_venue):
    fake_venue.book = None  # /book -> 404
    ob = await polytrader.get_order_book(UP_TOKEN)
    assert ob is None


async def test_get_order_book_empty_id_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.get_order_book("")


# --- get_price ------------------------------------------------------------- #

async def test_get_price_buy_returns_best_ask(polytrader):
    p = await polytrader.get_price(UP_TOKEN, side="BUY")
    assert p == Decimal("0.53")


async def test_get_price_sell_returns_best_bid(polytrader):
    p = await polytrader.get_price(UP_TOKEN, side="SELL")
    assert p == Decimal("0.51")


async def test_get_price_no_book_returns_none(polytrader, fake_venue):
    fake_venue.book = None
    assert await polytrader.get_price(UP_TOKEN, "BUY") is None


async def test_get_price_empty_side_returns_none(polytrader, fake_venue):
    def handler(request):
        return httpx.Response(200, json={"asks": [], "bids": [{"price": "0.4", "size": "5"}],
                                         "tick_size": "0.01"})

    fake_venue.set_handler("GET", "/book", handler)
    assert await polytrader.get_price(UP_TOKEN, "BUY") is None   # no asks
    assert await polytrader.get_price(UP_TOKEN, "SELL") == Decimal("0.4")


async def test_get_price_invalid_side_raises(polytrader):
    with pytest.raises(ValidationError):
        await polytrader.get_price(UP_TOKEN, "HOLD")


# --- pure parsers ---------------------------------------------------------- #

def test_parse_market_zips_tokens_and_prices():
    m = markets.parse_market(DEFAULT_MARKET)
    assert m.outcomes[0].token_id == UP_TOKEN
    assert m.outcomes[1].label == "Down"
    assert m.outcomes[1].price == Decimal("0.48")
    assert m.volume == Decimal("12345.6")
    assert m.liquidity == Decimal("6789.0")


def test_parse_order_book_drops_malformed_and_nonpositive_levels():
    raw = {
        "asks": [
            {"price": "0.55", "size": "500"},
            {"price": "0.00", "size": "100"},    # non-positive price -> dropped
            {"price": "0.53", "size": "0"},      # non-positive size -> dropped
            {"price": "bad", "size": "10"},      # unparseable -> dropped
            {"size": "10"},                      # missing price -> dropped
            {"price": "0.54", "size": "10"},
        ],
        "bids": [{"price": "0.40", "size": "5"}],
        "tick_size": "0.01",
    }
    ob = markets.parse_order_book(UP_TOKEN, raw)
    assert [lv.price for lv in ob.asks] == [Decimal("0.54"), Decimal("0.55")]
    assert ob.best_bid == Decimal("0.40")


def test_parse_order_book_bad_tick_defaults_to_penny():
    ob = markets.parse_order_book(UP_TOKEN, {"asks": [], "bids": [], "tick_size": "junk"})
    assert ob.tick_size == Decimal("0.01")
    ob2 = markets.parse_order_book(UP_TOKEN, {"asks": [], "bids": [], "tick_size": "0"})
    assert ob2.tick_size == Decimal("0.01")
