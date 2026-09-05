# CLAUDE.md

Operating playbook for Claude Code (and any agent) writing code that uses
`polytrader`, or working on this repo. `AGENTS.md` is the full reference; read it
for the complete API. This file is the short list of rules you must follow.

## What you are working with

`polytrader` is an async Python client for Polymarket (market data, orders,
balance, treasury, health). It has no strategy or model of its own. It talks to
a required Node bridge that owns CLOB auth and signing:

```
service -> polytrader (Python) -> Node bridge -> Polymarket CLOB + Gamma
```

## Non-negotiable rules

1. **Never retry an order.** `bet_on` / `place_market_order` / `place_limit_order`
   make one attempt on purpose. The bridge does not dedup on `client_order_id`,
   so a retry after a lost response can double-fill. On an ambiguous failure,
   reconcile with `verify_balance()` before any resubmit. Do not wrap orders in
   a retry loop.
2. **Never put secrets in Python.** No private keys, no CLOB creds in config or
   code. They live only in the bridge's environment.
3. **Always health-check first.** Call `verify_conn_health()` before trading;
   bail if `ok` is False.
4. **Respect the $1 BUY floor.** Marketable BUYs under $1 are rejected. Do not
   work around it unless you set `min_marketable_buy_usd=0` deliberately.
5. **Use limit FAK for size above ~$50.** Market BUYs have no price cap and eat
   deeper book levels. Use `place_limit_order(order_type="FAK")` at the ask.
6. **Withdraw is real money.** Off by default, guarded, test on testnet first.
7. **Always close the client.** Use `async with PolyTrader(cfg) as pt:` or call
   `await pt.close()`.
8. **Results are structured, not exceptions.** Check `res.ok` / `bal.ok`. Only
   bad arguments raise (`ValidationError`); config errors raise `ConfigError`.

## Minimal correct usage

```python
from polytrader import PolyTrader, PolyTraderConfig

async with PolyTrader(PolyTraderConfig(bridge_url="http://localhost:3000")) as pt:
    if not (await pt.verify_conn_health()).ok:
        return
    res = await pt.bet_on("btc-updown-15m-1735689600", "UP", amount_usd=5)
    if not res.ok:
        # inspect res.error_msg; if it looks like a transport failure, reconcile
        # with verify_balance() before resubmitting. Do not blindly retry.
        ...
```

## API cheatsheet

| Method | Returns |
|--|--|
| `bet_on(market, outcome, amount_usd)` | `OrderResult` |
| `place_market_order(token_id, side, amount_usd)` | `OrderResult` |
| `place_limit_order(token_id, side, price, size)` | `OrderResult` |
| `cancel_order(order_id)` | `OrderResult` |
| `verify_balance()` | `Balance` |
| `verify_conn_health()` | `HealthStatus` |
| `get_market(slug_or_condition)` | `Market \| None` |
| `get_order_book(token_id)` / `get_price(token_id, side)` | `OrderBook` / `Decimal` |
| `deposit()` | `DepositInfo` (address + instructions) |
| `withdraw(amount_usd, to_address)` | `WithdrawResult` |

`outcome` accepts UP/DOWN, YES/NO, a label, an index, or a token id. `market`
accepts a `Market` or a slug/condition-id string.

## Anti-patterns (do not do these)

- Retrying a failed order automatically.
- Putting the private key or CLOB secret in `PolyTraderConfig` or `.env` read by
  Python.
- Sizing a market BUY over ~$50 without switching to a limit FAK.
- Assuming methods raise on rejection; they return `ok=False`.
- Calling `deposit()` and expecting funds to move; deposits are external.
- Leaving the client open (leaks the HTTP pool).

## Working on this repo

- Setup: `pip install -e ".[test]"`. The bridge lives in `bridge/` (Node, run via
  Docker).
- Tests: `pytest` (unit + integration). Unit tests are offline (mocked bridge);
  integration tests use a real loopback HTTP server (`pytest-httpserver`).
  `pytest -m "not integration"` for unit only.
- CI runs on every push (`.github/workflows/ci.yml`) and publishes the coverage
  badge.
- Style: short comments, no novels, no em dashes. Structured results over
  exceptions. Keep the package free of strategy/model/DB code.

## License

MIT (see `LICENSE`). Free to use. Issues and pull requests are welcome.
