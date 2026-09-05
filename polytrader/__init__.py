"""polytrader — a standalone client for Polymarket connectivity.

Market data + order placement + balance + treasury + health, wrapping the Node
CLOB bridge. No model, DB, gates, or strategy — just the venue plumbing.

Quickstart::

    import asyncio
    from polytrader import PolyTrader, PolyTraderConfig

    async def main():
        async with PolyTrader(PolyTraderConfig(bridge_url="http://localhost:3000")) as pt:
            health = await pt.verify_conn_health()
            bal = await pt.verify_balance()
            market = await pt.get_market("btc-updown-15m-1735689600")
            result = await pt.bet_on(market, "UP", 5)
            print(result.ok, result.status, result.error_msg)

    asyncio.run(main())
"""
from __future__ import annotations

from .client import PolyTrader
from .config import (
    POLYGON_CHAIN_ID,
    POLYGON_USDC_ADDRESS,
    PolyTraderConfig,
)
from .errors import (
    ConfigError,
    PolyTraderError,
    TransportError,
    ValidationError,
)
from .models import (
    Balance,
    BookLevel,
    DepositInfo,
    HealthStatus,
    Market,
    OrderBook,
    OrderResult,
    Outcome,
    WithdrawResult,
)

__version__ = "0.1.0"

__all__ = [
    "PolyTrader",
    "PolyTraderConfig",
    "POLYGON_CHAIN_ID",
    "POLYGON_USDC_ADDRESS",
    # errors
    "PolyTraderError",
    "ConfigError",
    "TransportError",
    "ValidationError",
    # models
    "Market",
    "Outcome",
    "OrderBook",
    "BookLevel",
    "OrderResult",
    "Balance",
    "HealthStatus",
    "DepositInfo",
    "WithdrawResult",
    "__version__",
]
