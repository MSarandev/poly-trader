"""Low-level async HTTP client for the Node CLOB bridge sidecar.

The bridge owns clob-client-v2 + the POLY_1271 auth no Python SDK can build, so
it is the only path to real orders. SDK-free: POSTs plain strings, returns
structured results. Never raises on a venue rejection.
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
    """Thin async HTTP client for the bridge. Pass a shared ``httpx.AsyncClient``
    so one keep-alive pool serves the whole app; else one is created and owned
    here.
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
        """Raw ``GET /balance`` (``{balance_raw, balance_usd, allowances}``).
        Raises on transport/HTTP error; the null-``balance_usd`` guard lives in
        ``PolyTrader.verify_balance`` so this stays raw for both callers.
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
        """Collateral balance as a Decimal, or None on any error/non-finite.

        Guards ``balance_usd: null`` (bridge NaN serialises to JSON null).
        ``Decimal("nan")`` raises ``ArithmeticError``, not ``ValueError``, so
        catch both and reject non-finite explicitly.
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
        """Shared POST helper for order-like endpoints. Structured result on any
        4xx/5xx (no raise).

        Order placement is NEVER auto-retried: the bridge/CLOB doesn't dedup on
        ``client_order_id``, so a retry after a lost-but-accepted response would
        double-fill. Single attempt; the caller reconciles before any resubmit."""
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
        """Market order; Polymarket handles the share count. BUY spends
        ``amount_usd`` USDC. Avoids the ">2dp maker" validation that hits limit
        orders."""
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
