"""Low-level async HTTP client for the Node CLOB bridge sidecar.

The bridge owns ``@polymarket/clob-client-v2`` + the ERC-1271 / POLY_1271 auth
path required for deposit-wallet API keys (which no Python SDK can construct), so
it is the only path to real orders. This client stays SDK-free: it POSTs plain
strings ("BUY"/"SELL", "FAK"/"FOK"/"GTC"/"GTD") and returns structured results.

Never raises on a venue rejection. Transport/5xx get bounded retries via
:func:`polytrader._http.request_with_retry`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import httpx

from ._http import request_with_retry


@dataclass(frozen=True)
class BridgeOrderResult:
    """Raw result of a bridge order/cancel/withdraw call."""

    ok: bool
    raw: dict
    error_msg: Optional[str]
    client_order_id: Optional[str] = None
    # Wall-clock ms spent inside the HTTP call (caller → bridge → CLOB → back).
    # The bridge's own CLOB round-trip is in raw["latency_ms"].
    call_ms: Optional[int] = None
    status_code: Optional[int] = None


class BridgeClient:
    """Thin async HTTP client for the bridge. Owns no pool by default — pass a
    shared ``httpx.AsyncClient`` so one keep-alive pool serves the whole app.
    If none is given, one is created and owned by this instance.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                timeout=timeout_s,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=60.0,
                ),
            )
            self._owns_client = True

    async def close(self) -> None:
        """Close the underlying client only if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    # --- health / balance ---------------------------------------------------

    async def healthz(self) -> dict:
        """Raw ``GET /healthz``. Raises on transport/HTTP error (used by the
        health probe, which catches and classifies)."""
        resp = await request_with_retry(
            lambda: self._client.get(f"{self.base_url}/healthz", timeout=self.timeout),
            max_retries=self.max_retries,
            backoff_s=self.retry_backoff_s,
            description="bridge healthz",
        )
        resp.raise_for_status()
        return resp.json()

    async def balance(self) -> dict:
        """Raw ``GET /balance`` payload. Raises on transport/HTTP error.

        Returns the dict ``{balance_raw, balance_usd, allowances}``. The
        null-``balance_usd`` guard lives in the higher-level
        ``PolyTrader.verify_balance``; this stays raw so both callers share it.
        """
        resp = await request_with_retry(
            lambda: self._client.get(f"{self.base_url}/balance", timeout=self.timeout),
            max_retries=self.max_retries,
            backoff_s=self.retry_backoff_s,
            description="bridge balance",
        )
        resp.raise_for_status()
        return resp.json()

    async def balance_usd(self) -> Optional[Decimal]:
        """Deposit wallet's collateral balance as a Decimal, or None on any
        error / non-finite payload.

        Guards the ``balance_usd: null`` case: the bridge computes
        ``Number(ba.balance) / 1e6``; a bad SDK field yields NaN which serialises
        to JSON ``null``. ``Decimal("nan")`` is not caught by ``except
        ValueError`` (it's an ``ArithmeticError``), so we catch that too and
        non-finite values explicitly.
        """
        try:
            data = await self.balance()
            raw = data["balance_usd"]
            if raw is None:
                return None
            val = Decimal(str(raw))
            if not val.is_finite():
                return None
            return val
        except (httpx.HTTPError, KeyError, ValueError, ArithmeticError):
            return None

    # --- orders -------------------------------------------------------------

    async def _post_order(self, path: str, body: dict, coi: Optional[str]) -> BridgeOrderResult:
        """Shared POST helper for order-like endpoints. Returns a structured
        result on any 4xx/5xx (no raise).

        IMPORTANT — order placement is NEVER auto-retried. The bridge/CLOB does
        not dedup on ``client_order_id``, so retrying after a lost response
        (e.g. a ReadTimeout where the order was actually accepted) would place a
        SECOND fill and double real exposure. A single attempt is made; on a
        transport failure or 5xx the caller gets a structured error and must
        reconcile (re-check balance/positions) before any manual resubmit. Only
        idempotent reads (balance/book/health) use the retry path."""
        t0 = time.monotonic()
        try:
            resp = await request_with_retry(
                lambda: self._client.post(
                    f"{self.base_url}{path}", json=body, timeout=self.timeout
                ),
                max_retries=0,  # never auto-retry a non-idempotent order write
                backoff_s=self.retry_backoff_s,
                description=f"bridge {path}",
            )
        except httpx.HTTPError as exc:
            return BridgeOrderResult(
                ok=False, raw={}, error_msg=f"transport: {exc}",
                client_order_id=coi,
                call_ms=int((time.monotonic() - t0) * 1000),
            )
        call_ms = int((time.monotonic() - t0) * 1000)
        try:
            data = resp.json()
        except ValueError:
            return BridgeOrderResult(
                ok=False, raw={}, error_msg=f"non-json response (HTTP {resp.status_code})",
                client_order_id=coi, call_ms=call_ms, status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            return BridgeOrderResult(
                ok=False, raw=data, error_msg=str(data.get("error", resp.text))[:300],
                client_order_id=coi, call_ms=call_ms, status_code=resp.status_code,
            )
        return BridgeOrderResult(
            ok=True, raw=data, error_msg=None,
            client_order_id=coi, call_ms=call_ms, status_code=resp.status_code,
        )

    async def place_market_order(
        self,
        *,
        client_order_id: str,
        token_id: str,
        side: str,             # "BUY" | "SELL"
        amount_usd: Decimal,   # USDC to spend (2-dp)
        order_type: str,       # "FOK" | "FAK"
        tick_size: str = "0.01",
    ) -> BridgeOrderResult:
        """Market order — Polymarket handles the share count. For BUY,
        ``amount_usd`` is the USDC to spend; whatever shares fill at the resting
        asks. Avoids the ">2dp maker" validation that hits limit orders."""
        body = {
            "client_order_id": client_order_id,
            "token_id": token_id,
            "side": side,
            "amount_usd": float(amount_usd),
            "order_type": order_type,
            "tick_size": tick_size,
        }
        return await self._post_order("/place-market-order", body, client_order_id)

    async def place_order(
        self,
        *,
        client_order_id: str,
        token_id: str,
        side: str,             # "BUY" | "SELL"
        price: Decimal,
        size: Decimal,         # shares
        order_type: str,       # "GTC" | "GTD" | "FOK" | "FAK"
        tick_size: str = "0.01",
    ) -> BridgeOrderResult:
        """Submit a limit order via the bridge. Structured result; does not raise
        on rejection (CLOB error becomes ok=False, error_msg=...)."""
        body = {
            "client_order_id": client_order_id,
            "token_id": token_id,
            "side": side,
            "price": float(price),
            "size": float(size),
            "order_type": order_type,
            "tick_size": tick_size,
        }
        return await self._post_order("/place-order", body, client_order_id)

    async def cancel_order(self, order_id: str) -> BridgeOrderResult:
        """Cancel a resting order via ``POST /cancel``."""
        return await self._post_order("/cancel", {"order_id": order_id}, None)

    async def withdraw(self, *, to: str, amount_usd: Decimal) -> BridgeOrderResult:
        """Request an on-chain USDC transfer via ``POST /withdraw``. Guarded on
        the bridge by ``BRIDGE_ALLOW_WITHDRAW`` (a disabled bridge returns 403,
        surfaced here as ok=False)."""
        body = {"to": to, "amount_usd": float(amount_usd)}
        return await self._post_order("/withdraw", body, None)
