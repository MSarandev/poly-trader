"""Configuration for :class:`polytrader.client.PolyTrader`.

All knobs are injectable via the constructor or overridable from the
environment (``PolyTraderConfig.from_env``). No secrets live here — the wallet
private key and CLOB API creds belong to the Node bridge's own environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import Optional

from .errors import ConfigError

# Public, non-secret shared defaults. USDC on Polygon PoS mainnet is a public
# contract address (safe to ship as a documented default).
POLYGON_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_CHAIN_ID = 137
DEFAULT_GAMMA_API_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_API_URL = "https://clob.polymarket.com"


def _as_bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class PolyTraderConfig:
    """Immutable configuration bundle.

    Construct directly, via :meth:`from_env`, or clone with :meth:`with_overrides`.
    """

    # --- endpoints ---
    bridge_url: str = "http://localhost:3000"
    gamma_api_url: str = DEFAULT_GAMMA_API_URL
    clob_api_url: str = DEFAULT_CLOB_API_URL

    # --- transport ---
    request_timeout_s: float = 10.0
    max_retries: int = 2
    retry_backoff_s: float = 0.25

    # --- order defaults ---
    default_order_type: str = "FAK"
    default_tick_size: str = "0.01"
    # Polymarket rejects marketable BUYs below $1 ("min size: $1"). Enforced
    # client-side for a clear pre-flight error instead of a cryptic venue 400.
    # Set to 0 to disable the local check and defer entirely to the venue.
    min_marketable_buy_usd: float = 1.0

    # --- treasury / withdraw (only used when allow_withdraw) ---
    allow_withdraw: bool = False
    rpc_url: Optional[str] = None
    usdc_address: str = POLYGON_USDC_ADDRESS
    chain_id: int = POLYGON_CHAIN_ID
    # Informational only — the deposit address is surfaced by deposit(); it is
    # NOT a secret (funds are sent TO it). Left None by default so the package
    # ships no wallet addresses.
    deposit_address: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.bridge_url:
            raise ConfigError("bridge_url is required")
        if self.request_timeout_s <= 0:
            raise ConfigError("request_timeout_s must be > 0")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")
        if self.retry_backoff_s < 0:
            raise ConfigError("retry_backoff_s must be >= 0")
        # Normalise: strip trailing slashes so callers can join paths cleanly.
        object.__setattr__(self, "bridge_url", self.bridge_url.rstrip("/"))
        object.__setattr__(self, "gamma_api_url", self.gamma_api_url.rstrip("/"))
        object.__setattr__(self, "clob_api_url", self.clob_api_url.rstrip("/"))

    def with_overrides(self, **overrides) -> "PolyTraderConfig":
        """Return a copy with the given fields replaced."""
        known = {f.name for f in fields(self)}
        bad = set(overrides) - known
        if bad:
            raise ConfigError(f"unknown config field(s): {sorted(bad)}")
        return replace(self, **overrides)

    @classmethod
    def from_env(
        cls, prefix: str = "POLYTRADER_", env: Optional[dict] = None
    ) -> "PolyTraderConfig":
        """Build a config from environment variables.

        Every field maps to ``{prefix}{FIELD_NAME_UPPER}`` — e.g.
        ``POLYTRADER_BRIDGE_URL``, ``POLYTRADER_ALLOW_WITHDRAW``. Unset vars fall
        back to the dataclass defaults.
        """
        src = os.environ if env is None else env

        def _get(name: str):
            return src.get(f"{prefix}{name.upper()}")

        kwargs: dict = {}
        for f in fields(cls):
            raw = _get(f.name)
            if raw is None:
                continue
            if f.type == "bool" or f.name == "allow_withdraw":
                kwargs[f.name] = _as_bool(raw)
            elif f.name in ("request_timeout_s", "retry_backoff_s",
                            "min_marketable_buy_usd"):
                kwargs[f.name] = float(raw)
            elif f.name in ("max_retries", "chain_id"):
                kwargs[f.name] = int(raw)
            else:
                kwargs[f.name] = raw
        return cls(**kwargs)
