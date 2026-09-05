"""Public data types returned by :class:`polytrader.client.PolyTrader`.

Frozen dataclasses (no pydantic). Order/treasury results carry ``ok`` /
``error_msg`` instead of raising on a venue rejection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class Outcome:
    """One side of a binary (or multi-outcome) market."""

    label: str            # e.g. "Up" / "Down" / "Yes" / "No"
    token_id: str         # CLOB v2 ERC-1155 token id used when placing orders
    price: Optional[Decimal] = None   # last outcomePrice snapshot from Gamma
    index: int = 0        # position within the market's outcome list


@dataclass(frozen=True)
class Market:
    """A Polymarket market and its outcomes (from the Gamma API)."""

    slug: str
    question: str
    condition_id: Optional[str]
    outcomes: list[Outcome]
    end_date: Optional[str] = None          # ISO-8601 string
    last_trade_price: Optional[Decimal] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    liquidity: Optional[Decimal] = None
    raw: dict = field(default_factory=dict)

    def outcome_by_label(self, label: str) -> Optional[Outcome]:
        want = label.strip().lower()
        for o in self.outcomes:
            if o.label.strip().lower() == want:
                return o
        return None

    def outcome_by_token(self, token_id: str) -> Optional[Outcome]:
        for o in self.outcomes:
            if o.token_id == token_id:
                return o
        return None


@dataclass(frozen=True)
class BookLevel:
    """A single price level in an order book."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    """A parsed live CLOB order book for one outcome token.

    ``asks`` are sorted cheapest-first, ``bids`` highest-first.
    """

    token_id: str
    asks: list[BookLevel]
    bids: list[BookLevel]
    tick_size: Decimal = Decimal("0.01")

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None


@dataclass(frozen=True)
class OrderResult:
    """Structured result of any order / cancel call. Never raises on rejection."""

    ok: bool
    status: Optional[str] = None            # "matched" / "rejected" / "ok" / ...
    order_id: Optional[str] = None
    filled_shares: Optional[Decimal] = None
    avg_price: Optional[Decimal] = None
    spent_usd: Optional[Decimal] = None
    client_order_id: Optional[str] = None
    error_msg: Optional[str] = None
    call_ms: Optional[int] = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Balance:
    """Collateral (USDC) balance of the trading wallet."""

    ok: bool
    usd: Optional[Decimal] = None
    raw: Optional[str] = None               # raw 1e6-scaled string from the SDK
    allowances: Optional[Any] = None
    error_msg: Optional[str] = None


@dataclass(frozen=True)
class HealthStatus:
    """Result of a connectivity/auth probe. Never raises."""

    ok: bool
    bridge_ok: bool
    clob_ok: bool
    latency_ms: Optional[int] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class DepositInfo:
    """Instructions for funding the trading wallet.

    Deposits cannot be pulled programmatically: this is the address to send USDC
    to. Poll ``verify_balance()`` afterwards.
    """

    deposit_address: Optional[str]
    chain: str
    usdc_contract: str
    amount_usd: Optional[Decimal] = None
    instructions: str = ""


@dataclass(frozen=True)
class WithdrawResult:
    """Structured result of an on-chain USDC withdrawal. Never raises."""

    ok: bool
    tx_hash: Optional[str] = None
    error_msg: Optional[str] = None
    raw: dict = field(default_factory=dict)
