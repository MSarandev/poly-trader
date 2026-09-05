"""The high-level ``PolyTrader`` API.

One pooled ``httpx.AsyncClient`` serves market data (Gamma + CLOB) and the Node
bridge (orders, balance, health, withdraw). All I/O is async; use
``async with PolyTrader(...) as pt:`` or call ``await pt.close()`` explicitly.

Design contract: never raises on a venue rejection — order and treasury calls
return structured ``ok`` / ``error_msg`` results. Only bad arguments
(ValidationError) or unrecoverable config (ConfigError) raise.
"""
from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

import httpx

from . import markets
from ._http import make_http_client
from .bridge_client import BridgeClient, BridgeOrderResult
from .config import PolyTraderConfig
from .errors import ValidationError
from .models import (
    Balance,
    DepositInfo,
    HealthStatus,
    Market,
    OrderBook,
    OrderResult,
    WithdrawResult,
)

logger = logging.getLogger(__name__)

# Common aliases so bet_on("...", "yes"/"up") resolves regardless of the
# market's exact outcome labels.
_LABEL_ALIASES = {
    "up": ("up", "yes"),
    "down": ("down", "no"),
    "yes": ("yes", "up"),
    "no": ("no", "down"),
}


class PolyTrader:
    """High-level Polymarket trading client. See the module docstring."""

    def __init__(
        self,
        config: Optional[PolyTraderConfig] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        **overrides,
    ):
        base = config or PolyTraderConfig()
        self.config = base.with_overrides(**overrides) if overrides else base

        # One pooled client for everything. If the caller injects one we borrow
        # it (and don't close it); otherwise we own its lifecycle.
        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = make_http_client(timeout=self.config.request_timeout_s)
            self._owns_http = True

        self._bridge = BridgeClient(
            self.config.bridge_url,
            client=self._http,
            timeout_s=self.config.request_timeout_s,
            max_retries=self.config.max_retries,
            retry_backoff_s=self.config.retry_backoff_s,
        )

    # --- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "PolyTrader":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        """Release resources. Closes the HTTP pool only if this instance created
        it (an injected client is left for its owner to close)."""
        await self._bridge.close()  # no-op on the shared client
        if self._owns_http:
            await self._http.aclose()

    # --- health / balance ---------------------------------------------------

    async def verify_conn_health(self) -> HealthStatus:
        """Probe the bridge (``/healthz``) and CLOB reachability (``/balance``).

        Never raises — returns ``ok=False`` with a ``detail`` string on failure.
        ``clob_ok`` reflects whether the bridge could reach the CLOB (a live
        balance read exercises the authed CLOB path end-to-end).
        """
        t0 = time.monotonic()
        bridge_ok = False
        clob_ok = False
        detail: Optional[str] = None
        try:
            await self._bridge.healthz()
            bridge_ok = True
        except Exception as exc:  # transport / HTTP / json
            latency = int((time.monotonic() - t0) * 1000)
            return HealthStatus(
                ok=False, bridge_ok=False, clob_ok=False,
                latency_ms=latency, detail=f"bridge unreachable: {exc}",
            )
        # Bridge is up — now check it can actually reach the CLOB.
        try:
            bal = await self._bridge.balance_usd()
            clob_ok = bal is not None
            if not clob_ok:
                detail = "bridge up but CLOB balance unavailable"
        except Exception as exc:
            detail = f"bridge up but CLOB probe failed: {exc}"
        latency = int((time.monotonic() - t0) * 1000)
        return HealthStatus(
            ok=bridge_ok and clob_ok,
            bridge_ok=bridge_ok,
            clob_ok=clob_ok,
            latency_ms=latency,
            detail=detail,
        )

    async def verify_balance(self) -> Balance:
        """Collateral (USDC) balance of the trading wallet.

        Returns ``Balance(ok=False, usd=None, ...)`` on failure — never crashes
        on a null/non-finite payload.
        """
        try:
            data = await self._bridge.balance()
        except Exception as exc:
            return Balance(ok=False, usd=None, error_msg=f"balance fetch failed: {exc}")
        raw = data.get("balance_usd")
        if raw is None:
            return Balance(
                ok=False, usd=None,
                raw=str(data.get("balance_raw")),
                error_msg="bridge returned null balance_usd",
            )
        try:
            val = Decimal(str(raw))
        except (InvalidOperation, ValueError, ArithmeticError):
            return Balance(ok=False, usd=None, error_msg=f"unparseable balance: {raw!r}")
        if not val.is_finite():
            return Balance(ok=False, usd=None, error_msg=f"non-finite balance: {raw!r}")
        return Balance(
            ok=True,
            usd=val,
            raw=str(data.get("balance_raw")),
            allowances=data.get("allowances"),
        )

    # --- betting / orders ---------------------------------------------------

    async def bet_on(
        self,
        market: Union[Market, str],
        outcome: Union[str, int],
        amount_usd: Union[Decimal, float, int, str],
        *,
        order_type: Optional[str] = None,
    ) -> OrderResult:
        """Primary entry point: resolve ``outcome`` to a token id against
        ``market``, then place a MARKET BUY spending ``amount_usd`` USDC.

        ``market``: a :class:`Market`, or a slug / condition-id string (looked up).
        ``outcome``: "UP"/"DOWN"/"YES"/"NO", the outcome label, an integer index,
        or an explicit token id string.
        ``amount_usd``: USDC to spend (>= $1 marketable-BUY minimum).

        NOTE (slippage): market BUYs take whatever's at the top of book — there
        is no max-price parameter. At small sizes (<~$50) this eats only the
        inside asks and is fine; above ~$50 a single market BUY can bite into
        deeper, worse levels. For large sizes prefer a limit FAK via
        :meth:`place_limit_order` at the inside ask. See README.
        """
        amt = self._coerce_amount(amount_usd)
        resolved = market if isinstance(market, Market) else await self.get_market(market)
        if resolved is None:
            return OrderResult(
                ok=False, status="error",
                error_msg=f"market not found: {market!r}",
            )
        token_id = self._resolve_token_id(resolved, outcome)
        if token_id is None:
            labels = [o.label for o in resolved.outcomes]
            return OrderResult(
                ok=False, status="error",
                error_msg=f"could not resolve outcome {outcome!r} in outcomes {labels}",
            )
        return await self.place_market_order(
            token_id=token_id,
            side="BUY",
            amount_usd=amt,
            order_type=order_type or self.config.default_order_type,
        )

    async def place_market_order(
        self,
        *,
        token_id: str,
        side: str,
        amount_usd: Union[Decimal, float, int, str],
        order_type: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Low-level market order. ``side`` in {BUY, SELL}; ``amount_usd`` is USDC
        to spend (BUY) / shares' notional (SELL, as the bridge/SDK interprets)."""
        side = self._validate_side(side)
        amt = self._coerce_amount(amount_usd)
        coi = client_order_id or self._new_coi()
        # Polymarket rejects marketable BUYs below $1; fail fast client-side with
        # a clear error (config.min_marketable_buy_usd=0 disables this check).
        min_buy = Decimal(str(self.config.min_marketable_buy_usd))
        if side == "BUY" and min_buy > 0 and amt < min_buy:
            return OrderResult(
                ok=False, status="error", client_order_id=coi,
                error_msg=(
                    f"marketable BUY amount ${amt} below Polymarket minimum "
                    f"${min_buy}"
                ),
            )
        res = await self._bridge.place_market_order(
            client_order_id=coi,
            token_id=str(token_id),
            side=side,
            amount_usd=amt,
            order_type=order_type or self.config.default_order_type,
            tick_size=self.config.default_tick_size,
        )
        return self._to_order_result(res, side=side)

    async def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: Union[Decimal, float, int, str],
        size: Union[Decimal, float, int, str],
        order_type: str = "GTC",
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Low-level limit order.

        For a marketable BUY, ``size`` is snapped so ``price × size`` lands on a
        clean 2-dp maker (``shares_for_clean_maker``) — otherwise the CLOB rejects
        it once the SDK ``roundDown``s the size to 2 dp. SELL/GTC resting orders
        pass the requested size through unchanged.
        """
        side = self._validate_side(side)
        price_d = self._coerce_decimal(price, "price")
        size_d = self._coerce_decimal(size, "size")
        if price_d <= 0:
            raise ValidationError("price must be > 0")
        if size_d <= 0:
            raise ValidationError("size must be > 0")
        coi = client_order_id or self._new_coi()

        if side == "BUY":
            # Snap to a clean maker so a marketable BUY isn't rejected for >2dp.
            # shares_for_clean_maker returns None when the budget is too small to
            # buy even one clean-maker increment — treat that as "no fit" rather
            # than silently submitting the un-snapped (>2dp maker) size.
            budget = price_d * size_d
            snapped = markets.shares_for_clean_maker(budget, price_d)
            if snapped is None or snapped[0] <= 0:
                return OrderResult(
                    ok=False, status="error", client_order_id=coi,
                    error_msg="no clean maker size fits the requested price × size",
                )
            size_d = snapped[0]

        res = await self._bridge.place_order(
            client_order_id=coi,
            token_id=str(token_id),
            side=side,
            price=price_d,
            size=size_d,
            order_type=order_type,
            tick_size=self.config.default_tick_size,
        )
        return self._to_order_result(res, side=side)

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a resting order (bridge ``POST /cancel``)."""
        if not order_id:
            raise ValidationError("order_id is required")
        res = await self._bridge.cancel_order(str(order_id))
        return self._to_order_result(res, side=None)

    # --- market data --------------------------------------------------------

    async def get_market(self, slug_or_condition: str) -> Optional[Market]:
        """Look up a market by slug (preferred) or condition id. Returns None if
        not found."""
        if not slug_or_condition:
            raise ValidationError("slug_or_condition is required")
        s = str(slug_or_condition)
        # Condition ids are 0x-prefixed 32-byte hex; anything else is a slug.
        if s.startswith("0x") and len(s) >= 42:
            return await markets.fetch_market_by_condition(
                self._http, s,
                gamma_api_url=self.config.gamma_api_url,
                timeout=self.config.request_timeout_s,
                max_retries=self.config.max_retries,
                backoff_s=self.config.retry_backoff_s,
            )
        return await markets.fetch_market(
            self._http, s,
            gamma_api_url=self.config.gamma_api_url,
            timeout=self.config.request_timeout_s,
            max_retries=self.config.max_retries,
            backoff_s=self.config.retry_backoff_s,
        )

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch the live CLOB order book for ``token_id``. None on error."""
        if not token_id:
            raise ValidationError("token_id is required")
        return await markets.fetch_order_book(
            self._http, str(token_id),
            clob_api_url=self.config.clob_api_url,
            timeout=self.config.request_timeout_s,
            max_retries=self.config.max_retries,
            backoff_s=self.config.retry_backoff_s,
        )

    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[Decimal]:
        """Best ask (BUY) / best bid (SELL) from the live book. None if the
        relevant side is empty or the book is unavailable."""
        side = self._validate_side(side)
        book = await self.get_order_book(token_id)
        if book is None:
            return None
        return book.best_ask if side == "BUY" else book.best_bid

    # --- treasury -----------------------------------------------------------

    async def deposit(
        self, amount_usd: Optional[Union[Decimal, float, int, str]] = None
    ) -> DepositInfo:
        """Deposits are EXTERNAL — funds cannot be pulled programmatically.
        Returns the address to send USDC to plus instructions; poll
        :meth:`verify_balance` afterwards to confirm arrival. ``amount_usd`` is
        informational only."""
        amt = self._coerce_amount(amount_usd) if amount_usd is not None else None
        chain = f"Polygon (chain_id={self.config.chain_id})"
        addr = self.config.deposit_address
        if addr:
            instr = (
                f"Send USDC on Polygon to {addr}. Deposits cannot be initiated by "
                "this client; after sending, poll verify_balance() until the "
                "collateral balance reflects the deposit."
            )
        else:
            instr = (
                "No deposit_address configured. Set config.deposit_address to the "
                "Polymarket deposit wallet, or use the Polymarket UI on-ramp. "
                "Deposits cannot be initiated by this client; after sending USDC "
                "on Polygon, poll verify_balance() to confirm arrival."
            )
        return DepositInfo(
            deposit_address=addr,
            chain=chain,
            usdc_contract=self.config.usdc_address,
            amount_usd=amt,
            instructions=instr,
        )

    async def withdraw(
        self,
        amount_usd: Union[Decimal, float, int, str],
        to_address: str,
    ) -> WithdrawResult:
        """On-chain USDC transfer from the trading wallet to ``to_address`` via
        the bridge's ``/withdraw`` endpoint.

        Hard-guarded: disabled unless ``config.allow_withdraw`` is True; validates
        the destination address and that ``0 < amount <= balance`` before calling
        the bridge. Returns a structured result — never raises on a chain/venue
        error. Verify on testnet before production use.
        """
        if not self.config.allow_withdraw:
            return WithdrawResult(
                ok=False,
                error_msg="withdrawals disabled: set config.allow_withdraw=True to enable",
            )
        if not self._is_valid_address(to_address):
            return WithdrawResult(
                ok=False, error_msg=f"invalid to_address: {to_address!r}",
            )
        try:
            amt = self._coerce_amount(amount_usd)
        except ValidationError as exc:
            return WithdrawResult(ok=False, error_msg=str(exc))
        if amt <= 0:
            return WithdrawResult(ok=False, error_msg="amount_usd must be > 0")

        # Balance check — refuse to over-withdraw. A failed balance read blocks
        # the withdrawal (fail closed on a treasury op).
        bal = await self.verify_balance()
        if not bal.ok or bal.usd is None:
            return WithdrawResult(
                ok=False,
                error_msg=f"cannot verify balance before withdraw: {bal.error_msg}",
            )
        if amt > bal.usd:
            return WithdrawResult(
                ok=False,
                error_msg=f"amount {amt} exceeds balance {bal.usd}",
            )

        res = await self._bridge.withdraw(to=to_address, amount_usd=amt)
        tx_hash = None
        if res.ok:
            tx_hash = res.raw.get("tx_hash") or res.raw.get("txHash")
        return WithdrawResult(
            ok=res.ok and bool(tx_hash),
            tx_hash=tx_hash,
            error_msg=res.error_msg if not res.ok else (
                None if tx_hash else "bridge returned no tx_hash"
            ),
            raw=res.raw,
        )

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _new_coi() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _validate_side(side: str) -> str:
        s = str(side).strip().upper()
        if s not in ("BUY", "SELL"):
            raise ValidationError(f"side must be BUY or SELL, got {side!r}")
        return s

    @staticmethod
    def _coerce_decimal(v, name: str) -> Decimal:
        try:
            return Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError(f"{name} must be numeric, got {v!r}")

    def _coerce_amount(self, v) -> Decimal:
        amt = self._coerce_decimal(v, "amount_usd")
        if not amt.is_finite() or amt <= 0:
            raise ValidationError(f"amount_usd must be a positive finite number, got {v!r}")
        return amt

    @staticmethod
    def _is_valid_address(addr) -> bool:
        if not isinstance(addr, str) or not addr.startswith("0x"):
            return False
        hexpart = addr[2:]
        if len(hexpart) != 40:
            return False
        try:
            int(hexpart, 16)
        except ValueError:
            return False
        return True

    def _resolve_token_id(
        self, market: Market, outcome: Union[str, int]
    ) -> Optional[str]:
        """Map an outcome selector to a token id. Accepts an int index, an
        explicit token id, an outcome label, or a UP/DOWN/YES/NO alias."""
        # Integer index.
        if isinstance(outcome, int) and not isinstance(outcome, bool):
            if 0 <= outcome < len(market.outcomes):
                return market.outcomes[outcome].token_id
            return None
        s = str(outcome).strip()
        # Explicit token id (long numeric string that matches an outcome token).
        by_token = market.outcome_by_token(s)
        if by_token is not None:
            return by_token.token_id
        # Exact label match.
        by_label = market.outcome_by_label(s)
        if by_label is not None:
            return by_label.token_id
        # Alias (up/down/yes/no) → try each candidate label.
        for candidate in _LABEL_ALIASES.get(s.lower(), ()):  # type: ignore[arg-type]
            hit = market.outcome_by_label(candidate)
            if hit is not None:
                return hit.token_id
        return None

    @staticmethod
    def _to_order_result(res: BridgeOrderResult, *, side: Optional[str]) -> OrderResult:
        """Convert a raw bridge result into the public :class:`OrderResult`,
        best-effort parsing fill amounts from the CLOB response body."""
        if not res.ok:
            return OrderResult(
                ok=False,
                status="rejected",
                client_order_id=res.client_order_id,
                error_msg=res.error_msg,
                call_ms=res.call_ms,
                raw=res.raw,
            )
        # Success envelope: {client_order_id, latency_ms, response: <clob body>}.
        body = res.raw.get("response") if isinstance(res.raw, dict) else None
        body = body if isinstance(body, dict) else {}
        status = body.get("status") or ("ok" if body.get("success") is not False else "rejected")
        order_id = body.get("orderID") or body.get("orderId") or body.get("id")

        making = _to_dec(body.get("makingAmount") or body.get("making_amount"))
        taking = _to_dec(body.get("takingAmount") or body.get("taking_amount"))

        filled_shares: Optional[Decimal] = None
        spent_usd: Optional[Decimal] = None
        avg_price: Optional[Decimal] = None
        # For a BUY: making = USDC spent, taking = shares received.
        # For a SELL: making = shares given, taking = USDC received.
        if side == "SELL":
            filled_shares, spent_usd = making, taking
            if making and taking and making != 0:
                avg_price = taking / making
        else:  # BUY or unknown (cancel): interpret as BUY-style if present.
            spent_usd, filled_shares = making, taking
            if making and taking and taking != 0:
                avg_price = making / taking

        return OrderResult(
            ok=True,
            status=status,
            order_id=order_id,
            filled_shares=filled_shares,
            avg_price=avg_price,
            spent_usd=spent_usd,
            client_order_id=res.client_order_id,
            error_msg=None,
            call_ms=res.call_ms,
            raw=res.raw,
        )


def _to_dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None
