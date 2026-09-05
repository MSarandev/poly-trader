# polytrader

[![CI](https://github.com/MSarandev/poly-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/MSarandev/poly-trader/actions/workflows/ci.yml)
[![coverage](https://raw.githubusercontent.com/MSarandev/poly-trader/master/coverage.svg)](https://github.com/MSarandev/poly-trader/actions/workflows/ci.yml)

Async Python client for Polymarket: market data, orders, balance, treasury, and
health. All CLOB auth and order signing happen in a small Node bridge (Polymarket
needs ERC-1271 deposit-wallet auth that Python can't build); this package talks to
that bridge over HTTP.

## Install

```bash
pip install -e .              # core
pip install -e ".[withdraw]"  # + on-chain withdraw deps (optional)
pip install -e ".[test]"      # + test deps
```

## Quickstart

```python
import asyncio
from polytrader import PolyTrader, PolyTraderConfig

async def main():
    cfg = PolyTraderConfig(bridge_url="http://localhost:3000")
    async with PolyTrader(cfg) as pt:
        if not (await pt.verify_conn_health()).ok:
            raise SystemExit("bridge/CLOB unreachable")
        print(await pt.verify_balance())
        res = await pt.bet_on("btc-updown-15m-1735689600", "UP", amount_usd=5)
        print(res.ok, res.filled_shares, res.avg_price)

asyncio.run(main())
```

## API

| Method | Does |
|--|--|
| `bet_on(market, outcome, amount_usd)` | Resolve outcome to token, place a market BUY |
| `place_market_order(...)` / `place_limit_order(...)` | Low-level orders |
| `cancel_order(order_id)` | Cancel a resting order |
| `verify_balance()` | USDC collateral balance |
| `verify_conn_health()` | Bridge + CLOB reachability |
| `get_market(slug_or_condition)` | Look up a market |
| `get_order_book(token_id)` / `get_price(token_id, side)` | Book / best price |
| `deposit()` | Deposit address + instructions (see below) |
| `withdraw(amount_usd, to_address)` | On-chain USDC transfer (guarded) |

`market` accepts a `Market` or a slug/condition-id; `outcome` accepts
`UP`/`DOWN`, `YES`/`NO`, a label, an index, or a token id.

## Bridge

```bash
cd bridge && docker build -t polytrader-bridge . && \
  docker run -p 3000:3000 --env-file .env polytrader-bridge
```
Creds (`POLYMARKET_PRIVATE_KEY`, CLOB key/secret/passphrase) live only in the
bridge env. See `.env.example`. Withdraw requires `BRIDGE_ALLOW_WITHDRAW=true`.

## Deposit / withdraw

- **deposit** is external: you send USDC to the deposit wallet, then poll
  `verify_balance()`. `deposit()` returns the address and instructions; it cannot
  pull funds.
- **withdraw** signs an on-chain USDC transfer. Off by default
  (`config.allow_withdraw` + bridge `BRIDGE_ALLOW_WITHDRAW`), validates the
  address, checks the balance, and fails closed. Test on testnet before real use.

## Reliability

- Reads (balance, book, health, market) retry on transport errors and 5xx/429
  with bounded backoff.
- **Orders never auto-retry.** The bridge doesn't dedup on `client_order_id`, so
  a retry after a lost response could double-fill. On an ambiguous failure,
  reconcile with `verify_balance()` before resubmitting.
- Market BUYs take top-of-book with no price cap; fine at small size, risky above
  ~$50. For larger size use `place_limit_order(order_type="FAK")`.

## Config

`PolyTraderConfig(bridge_url=..., request_timeout_s=..., max_retries=...,
allow_withdraw=..., min_marketable_buy_usd=...)`, or
`PolyTraderConfig.from_env(prefix="POLYTRADER_")`. See `.env.example`.

## Tests

```bash
pytest                    # unit + integration
pytest -m "not integration"   # unit only
```
Unit tests mock the bridge (offline). Integration tests run against a real
loopback HTTP server.

## Contributing

Issues and pull requests are welcome. Run `pytest` before opening a PR. See
`AGENTS.md` and `CLAUDE.md` for usage and conventions.

## License

MIT (see `LICENSE`). Use it freely.
