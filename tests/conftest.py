"""Shared pytest fixtures for the polytrader suite.

Everything runs OFFLINE: outbound HTTP is served by an ``httpx.MockTransport``
that emulates the Node bridge, the Gamma API, and the CLOB book endpoint. No
network, no real bridge, no creds. The actual test cases are written by a
separate agent — this file just gives them a batteries-included fake venue to
build on.

Typical use::

    async def test_balance(polytrader):
        bal = await polytrader.verify_balance()
        assert bal.ok and bal.usd is not None

    async def test_bridge_down(make_polytrader, fake_venue):
        fake_venue.fail_healthz = True
        async with make_polytrader() as pt:
            h = await pt.verify_conn_health()
            assert not h.ok

Override behaviour by mutating ``fake_venue`` attributes or by registering a
custom handler with ``fake_venue.set_handler("POST", "/place-order", fn)``.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Callable, Optional

import httpx
import pytest
import pytest_asyncio

from polytrader import PolyTrader, PolyTraderConfig

# Base URLs the client talks to. Tests can rely on these matching the config
# built by `make_polytrader`.
BRIDGE_URL = "http://bridge.test"
GAMMA_URL = "https://gamma.test"
CLOB_URL = "https://clob.test"


def _json(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload)


# A realistic 15-minute BTC up/down market as Gamma returns it (fields are
# JSON-encoded strings, mirroring the real API).
DEFAULT_MARKET = {
    "slug": "btc-updown-15m-1735689600",
    "question": "Bitcoin Up or Down - 15 minute",
    "conditionId": "0x" + "ab" * 32,
    "endDate": "2025-01-01T00:15:00Z",
    "outcomes": json.dumps(["Up", "Down"]),
    "outcomePrices": json.dumps(["0.52", "0.48"]),
    "clobTokenIds": json.dumps(["1111111111", "2222222222"]),
    "lastTradePrice": "0.52",
    "bestBid": "0.51",
    "bestAsk": "0.53",
    "volumeNum": "12345.6",
    "liquidityNum": "6789.0",
}

# A live CLOB book for the "Up" token. Note Polymarket returns each side
# worst-price-first; the parser sorts them.
DEFAULT_BOOK = {
    "asks": [
        {"price": "0.55", "size": "500"},
        {"price": "0.53", "size": "100"},
    ],
    "bids": [
        {"price": "0.49", "size": "100"},
        {"price": "0.51", "size": "200"},
    ],
    "tick_size": "0.01",
}


class FakeVenue:
    """Configurable in-memory Polymarket venue for the MockTransport.

    Mutate the public attributes to steer default behaviour, or call
    :meth:`set_handler` to fully override a specific route. Every request is
    recorded in :attr:`requests` for assertions.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # Toggles for the default handlers.
        self.balance_usd: Optional[float] = 250.0     # set None to emit null
        self.balance_status: int = 200
        self.fail_healthz: bool = False               # healthz returns 503
        self.allow_withdraw: bool = True              # withdraw path enabled
        self.market: Optional[dict] = DEFAULT_MARKET  # None -> empty list (404-ish)
        self.book: Optional[dict] = DEFAULT_BOOK
        self.order_status: int = 200                  # status for order endpoints
        self.order_error: Optional[str] = None        # if set, order is rejected
        # How many times to fail with 503 before succeeding (for retry tests).
        self.transient_failures: int = 0
        self._transient_seen: dict[str, int] = {}
        # Custom per-route handlers: {(method, path): fn(request) -> Response}.
        self._handlers: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def set_handler(self, method: str, path: str, fn) -> None:
        self._handlers[(method.upper(), path)] = fn

    # -- routing -----------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path

        custom = self._handlers.get((method, path))
        if custom is not None:
            return custom(request)

        # Transient-failure injection keyed by path (for retry/backoff tests).
        if self.transient_failures:
            seen = self._transient_seen.get(path, 0)
            if seen < self.transient_failures:
                self._transient_seen[path] = seen + 1
                return _json(503, {"error": "transient"})

        if path == "/healthz":
            if self.fail_healthz:
                return _json(503, {"error": "unhealthy"})
            return _json(200, {"ok": True, "eoa": "0x" + "1" * 40,
                               "funder": "0x" + "2" * 40, "allow_withdraw": True})

        if path == "/balance":
            if self.balance_status != 200:
                return _json(self.balance_status, {"error": "bad balance"})
            usd = self.balance_usd
            return _json(200, {
                "balance_raw": None if usd is None else str(int(usd * 1e6)),
                "balance_usd": usd,   # may be None to exercise the null guard
                "allowances": {"exchange": "1000000000"},
            })

        if path == "/markets":  # Gamma
            if self.market is None:
                return _json(200, [])
            return _json(200, [self.market])

        if path == "/book":  # CLOB
            if self.book is None:
                return _json(404, {"error": "no book"})
            return _json(200, self.book)

        if path in ("/place-order", "/place-market-order"):
            return self._order_response(request)

        if path == "/cancel":
            if self.order_error:
                return _json(self.order_status if self.order_status >= 400 else 400,
                             {"error": self.order_error})
            return _json(200, {"order_id": "cancel-1", "latency_ms": 3,
                               "response": {"status": "cancelled"}})

        if path == "/withdraw":
            if not self.allow_withdraw:
                return _json(403, {"error": "withdrawals disabled"})
            return _json(200, {"tx_hash": "0x" + "de" * 32, "latency_ms": 42})

        return _json(404, {"error": f"unrouted {method} {path}"})

    def _order_response(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        coi = body.get("client_order_id")
        if self.order_error:
            code = self.order_status if self.order_status >= 400 else 400
            return _json(code, {"client_order_id": coi, "error": self.order_error})
        # Emulate the bridge success envelope for a filled BUY: makingAmount is
        # USDC spent, takingAmount is shares received (decimal strings).
        amount = body.get("amount_usd") or (
            float(body.get("price", 0)) * float(body.get("size", 0)))
        making = f"{float(amount):.6f}"
        taking = f"{float(amount) / 0.53:.6f}" if amount else "0"
        return _json(200, {
            "client_order_id": coi,
            "latency_ms": 12,
            "response": {
                "status": "matched",
                "orderID": "order-abc-123",
                "makingAmount": making,
                "takingAmount": taking,
                "success": True,
            },
        })


@pytest.fixture
def fake_venue() -> FakeVenue:
    """A fresh configurable fake venue per test."""
    return FakeVenue()


@pytest.fixture
def mock_transport(fake_venue: FakeVenue) -> httpx.MockTransport:
    """An httpx.MockTransport backed by the fake venue."""
    return httpx.MockTransport(fake_venue.handle)


@pytest.fixture
def test_config() -> PolyTraderConfig:
    """Config pointing at the fake hosts, with fast/no backoff for speed."""
    return PolyTraderConfig(
        bridge_url=BRIDGE_URL,
        gamma_api_url=GAMMA_URL,
        clob_api_url=CLOB_URL,
        request_timeout_s=5.0,
        max_retries=2,
        retry_backoff_s=0.0,   # keep retry tests instant
        allow_withdraw=True,
        deposit_address="0x" + "3" * 40,
    )


@pytest.fixture
def make_polytrader(mock_transport, test_config):
    """Factory: returns a callable building a PolyTrader wired to the mock.

    Accepts config overrides, e.g. ``make_polytrader(allow_withdraw=False)``.
    The returned client owns its (mock-backed) httpx client, so use it as an
    async context manager or call ``await pt.close()``.
    """
    def _make(**overrides) -> PolyTrader:
        cfg = test_config.with_overrides(**overrides) if overrides else test_config
        client = httpx.AsyncClient(transport=mock_transport)
        return PolyTrader(cfg, http_client=client)

    return _make


@pytest_asyncio.fixture
async def polytrader(make_polytrader):
    """A ready-to-use PolyTrader against the default fake venue.

    NOTE: it borrows an injected client, so closing it does not close the mock
    transport (harmless in tests). Yielded already-open.
    """
    pt = make_polytrader()
    # The injected client is owned by the fixture, close it here.
    pt._owns_http = True  # ensure the mock-backed client is cleaned up
    try:
        yield pt
    finally:
        await pt.close()


@pytest.fixture
def usdc() -> Callable[[str], Decimal]:
    """Tiny helper to build USDC Decimals in assertions."""
    return lambda v: Decimal(str(v))
