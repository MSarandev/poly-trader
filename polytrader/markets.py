"""Polymarket market-data access: active market lookup, order book, prices,
and the tick/rounding helper.

This module is intentionally market-data only — no prediction, model, or
strategy logic lives here. Everything is stateless and takes an injected
``httpx.AsyncClient`` + base URLs so nothing is hardcoded.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from math import floor
from typing import Optional

import httpx

from ._http import request_with_retry
from .models import BookLevel, Market, OrderBook, Outcome

logger = logging.getLogger(__name__)

# 15-minute "BTC up/down" market cadence, used by the slug/slot helpers. These
# are Polymarket conventions, not strategy.
SLOT_SECONDS = 15 * 60
SLOT_MS = SLOT_SECONDS * 1000
SLUG_PREFIX = "btc-updown-15m-"


# --------------------------------------------------------------------------- #
# Slug / slot helpers
# --------------------------------------------------------------------------- #

def slot_start_ms(now_ms: int) -> int:
    """Floor a wall-clock ms timestamp to its 15-minute slot boundary."""
    return (now_ms // SLOT_MS) * SLOT_MS


def slug_for_slot(slot_start_seconds: int) -> str:
    """Build the Gamma slug for a 15-minute slot start (in seconds)."""
    return f"{SLUG_PREFIX}{slot_start_seconds}"


def _opt_decimal(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Gamma market parsing / fetch
# --------------------------------------------------------------------------- #

def parse_market(m: dict) -> Market:
    """Parse a Gamma ``/markets`` element into a :class:`Market`.

    Outcomes are zipped with their CLOB v2 token ids and last outcomePrices so
    each :class:`Outcome` carries the ``token_id`` needed to place an order.
    """
    outcomes = json.loads(m.get("outcomes") or "[]")
    raw_prices = json.loads(m.get("outcomePrices") or "[]")
    clob_token_ids = json.loads(m.get("clobTokenIds") or "[]")

    parsed_outcomes: list[Outcome] = []
    for i, label in enumerate(outcomes):
        token_id = str(clob_token_ids[i]) if i < len(clob_token_ids) else ""
        price = _opt_decimal(raw_prices[i]) if i < len(raw_prices) else None
        parsed_outcomes.append(
            Outcome(label=str(label), token_id=token_id, price=price, index=i)
        )

    return Market(
        slug=m["slug"],
        question=m.get("question", ""),
        condition_id=m.get("conditionId") or m.get("condition_id"),
        outcomes=parsed_outcomes,
        end_date=m.get("endDate"),
        last_trade_price=_opt_decimal(m.get("lastTradePrice")),
        best_bid=_opt_decimal(m.get("bestBid")),
        best_ask=_opt_decimal(m.get("bestAsk")),
        volume=_opt_decimal(m.get("volumeNum")),
        liquidity=_opt_decimal(m.get("liquidityNum")),
        raw=m,
    )


async def fetch_market(
    client: httpx.AsyncClient,
    slug: str,
    *,
    gamma_api_url: str,
    timeout: float = 10.0,
    max_retries: int = 2,
    backoff_s: float = 0.25,
) -> Optional[Market]:
    """Fetch a market by its Gamma slug. Returns None if not found or on a
    transport/parse failure (caller decides how to react)."""
    try:
        resp = await request_with_retry(
            lambda: client.get(
                f"{gamma_api_url}/markets",
                params={"slug": slug},
                timeout=timeout,
            ),
            max_retries=max_retries,
            backoff_s=backoff_s,
            description=f"gamma markets {slug}",
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("gamma fetch failed for %s: %s", slug, exc)
        return None
    if not data:
        return None
    try:
        return parse_market(data[0])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("gamma parse failed for %s: %s", slug, exc)
        return None


async def fetch_market_by_condition(
    client: httpx.AsyncClient,
    condition_id: str,
    *,
    gamma_api_url: str,
    timeout: float = 10.0,
    max_retries: int = 2,
    backoff_s: float = 0.25,
) -> Optional[Market]:
    """Fetch a market by its condition id via Gamma's ``condition_ids`` filter."""
    try:
        resp = await request_with_retry(
            lambda: client.get(
                f"{gamma_api_url}/markets",
                params={"condition_ids": condition_id},
                timeout=timeout,
            ),
            max_retries=max_retries,
            backoff_s=backoff_s,
            description=f"gamma condition {condition_id[:12]}",
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("gamma condition fetch failed for %s: %s", condition_id, exc)
        return None
    if not data:
        return None
    try:
        return parse_market(data[0])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("gamma condition parse failed for %s: %s", condition_id, exc)
        return None


async def fetch_active_market(
    client: httpx.AsyncClient,
    slot_start_seconds: int,
    *,
    gamma_api_url: str,
    timeout: float = 10.0,
    max_retries: int = 2,
    backoff_s: float = 0.25,
) -> Optional[Market]:
    """Fetch the 15-minute market for a given slot start (convenience wrapper)."""
    return await fetch_market(
        client,
        slug_for_slot(slot_start_seconds),
        gamma_api_url=gamma_api_url,
        timeout=timeout,
        max_retries=max_retries,
        backoff_s=backoff_s,
    )


def market_outcome_price(market: Market, outcome_label: str) -> Optional[float]:
    """Gamma's snapshot implied price for an outcome label, as a float, or None."""
    o = market.outcome_by_label(outcome_label)
    if o is None or o.price is None:
        return None
    return float(o.price)


# --------------------------------------------------------------------------- #
# CLOB order book
# --------------------------------------------------------------------------- #

def parse_order_book(token_id: str, raw: dict) -> OrderBook:
    """Parse a CLOB ``GET /book`` payload into a sorted :class:`OrderBook`.

    Pure — does no I/O. Drops malformed or non-positive levels rather than
    raising. Asks are sorted cheapest-first, bids highest-first (Polymarket
    returns each side worst-price-first).
    """

    def _levels(items) -> list[BookLevel]:
        out: list[BookLevel] = []
        for it in items or []:
            try:
                price = Decimal(str(it["price"]))
                size = Decimal(str(it["size"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if price <= 0 or size <= 0:
                continue
            out.append(BookLevel(price=price, size=size))
        return out

    asks = sorted(_levels(raw.get("asks")), key=lambda lv: lv.price)
    bids = sorted(_levels(raw.get("bids")), key=lambda lv: lv.price, reverse=True)
    try:
        tick = Decimal(str(raw.get("tick_size")))
        if tick <= 0:
            tick = Decimal("0.01")
    except (TypeError, ValueError, InvalidOperation):
        tick = Decimal("0.01")
    return OrderBook(token_id=str(token_id), asks=asks, bids=bids, tick_size=tick)


async def fetch_order_book(
    client: httpx.AsyncClient,
    token_id: str,
    *,
    clob_api_url: str,
    timeout: float = 10.0,
    max_retries: int = 2,
    backoff_s: float = 0.25,
) -> Optional[OrderBook]:
    """Fetch and parse the live CLOB order book for ``token_id``. Returns None on
    any transport/JSON error so callers can skip rather than trade on a stale or
    missing quote."""
    try:
        resp = await request_with_retry(
            lambda: client.get(
                f"{clob_api_url}/book",
                params={"token_id": token_id},
                timeout=timeout,
            ),
            max_retries=max_retries,
            backoff_s=backoff_s,
            description=f"clob book {str(token_id)[:12]}",
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("clob book fetch failed for %s: %s", str(token_id)[:12], exc)
        return None
    try:
        return parse_order_book(token_id, data)
    except Exception as exc:  # parse is defensive, but never let it bubble
        logger.warning("clob book parse failed for %s: %s", str(token_id)[:12], exc)
        return None


# --------------------------------------------------------------------------- #
# Order sizing / rounding
# --------------------------------------------------------------------------- #

def shares_for_clean_maker(
    amount_usd: Decimal,
    price: Decimal,
    *,
    taker_dp: int = 2,
    maker_dp: int = 2,
) -> Optional[tuple[Decimal, Decimal]]:
    """Largest share ``size`` buyable for up to ``amount_usd`` at limit ``price``
    such that the maker USDC amount (``price × size``) lands on a clean
    ``maker_dp``-decimal value and ``size`` has at most ``taker_dp`` decimals.

    ``taker_dp`` MUST match what ``@polymarket/clob-client-v2`` rounds ``size`` to
    before it recomputes ``maker = size × price``. Its ``ROUNDING_CONFIG`` uses
    ``size: 2`` for *every* tick size, i.e. it ``roundDown``s our size to 2 dp. If
    we hand it a finer size (e.g. 13.875 at price 0.72) it truncates to 13.87 and
    the recomputed maker (9.9864) is no longer 2 dp — which the CLOB rejects on a
    *marketable* BUY with ``400 invalid amounts, max 2 decimals for maker``.
    Rounding ``size`` to 2 dp here keeps ``maker = price × size`` on the cent grid
    through the SDK. Returns ``(size, maker_usd)``, or None when no positive clean
    size fits the budget (caller should skip / fall back).

    Exact — uses ``Fraction``, no float error. ``size × price == maker_usd`` holds
    to the returned precision by construction.
    """
    P = Fraction(str(price))
    A = Fraction(str(amount_usd))
    if P <= 0 or A <= 0:
        return None
    taker_unit = Fraction(1, 10 ** taker_dp)   # smallest share increment
    maker_unit = Fraction(1, 10 ** maker_dp)   # smallest USDC increment (cent)
    step = ((P * taker_unit) / maker_unit).denominator
    kmax = floor(A / (P * taker_unit))
    k = (kmax // step) * step
    if k <= 0:
        return None
    size = Fraction(k) * taker_unit
    maker = P * size
    size_d = (Decimal(size.numerator) / Decimal(size.denominator)).quantize(
        Decimal(1).scaleb(-taker_dp)
    )
    maker_d = (Decimal(maker.numerator) / Decimal(maker.denominator)).quantize(
        Decimal(1).scaleb(-maker_dp)
    )
    return size_d, maker_d
