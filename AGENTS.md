# AGENTS.md

Guide for AI agents and humans using the `polytrader` package. This is the full
usage reference. `CLAUDE.md` is a condensed operating playbook that points here.

## What this is

`polytrader` is an async Python client for Polymarket. It does market data,
order placement, balance, treasury, and health. It does NOT contain any trading
strategy, model, or database. You bring the strategy; this handles the plumbing.

## Architecture (read this first)

```
your service  ->  polytrader (Python, this package)  ->  Node bridge  ->  Polymarket CLOB + Gamma
```

The **Node bridge is required**. Polymarket needs ERC-1271 / POLY_1271
deposit-wallet auth that Python cannot build, so all CLOB auth and order signing
happen in a small Node sidecar (`bridge/`). The Python package talks to that
bridge over HTTP. Market data (Gamma) and the order book (CLOB) are read
directly over HTTP.

Consequence: **all secrets live in the bridge's environment, never in Python.**
The Python config holds URLs and knobs only.

## Setup

### 1. Run the bridge

```bash
cd bridge
cp .env.example .env      # fill in POLYMARKET_PRIVATE_KEY + CLOB key/secret/passphrase
docker build -t polytrader-bridge .
docker run -p 3000:3000 --env-file .env polytrader-bridge
```

The bridge derives and persists CLOB API creds on first boot. To enable
withdrawals set `BRIDGE_ALLOW_WITHDRAW=true` in the bridge env.

### 2. Install the package

```bash
pip install -e .              # core (httpx only)
pip install -e ".[withdraw]"  # optional on-chain deps (lazy-imported)
pip install -e ".[http2]"     # optional HTTP/2 keep-alive
pip install -e ".[test]"      # test deps
```

`import polytrader` works without the optional extras.

## Configuration

`PolyTraderConfig` (all injectable, all have defaults except none required):

| Field | Default | Notes |
|--|--|--|
| `bridge_url` | `http://localhost:3000` | The Node bridge |
| `gamma_api_url` | `https://gamma-api.polymarket.com` | Market lookup |
| `clob_api_url` | `https://clob.polymarket.com` | Order book |
| `request_timeout_s` | `10.0` | Per-request timeout |
| `max_retries` | `2` | Retries on transport/5xx, reads only |
| `retry_backoff_s` | `0.25` | Exponential backoff base |
| `default_order_type` | `FAK` | FAK, FOK, GTC, GTD |
| `default_tick_size` | `0.01` | |
| `min_marketable_buy_usd` | `1.0` | Client-side BUY floor. 0 disables |
| `allow_withdraw` | `False` | Must be True to withdraw |
| `rpc_url` | `None` | Only for advanced local on-chain use |
| `usdc_address` | Polygon USDC | Public contract |
| `chain_id` | `137` | Polygon |

Build from env with `PolyTraderConfig.from_env(prefix="POLYTRADER_")`. Each field
maps to `POLYTRADER_<FIELD_UPPER>`. See `.env.example`.

## Quickstart

```python
import asyncio
from polytrader import PolyTrader, PolyTraderConfig

async def main():
    cfg = PolyTraderConfig(bridge_url="http://localhost:3000")
    async with PolyTrader(cfg) as pt:
        health = await pt.verify_conn_health()
        if not health.ok:
            raise SystemExit(f"not ready: {health.detail}")

        bal = await pt.verify_balance()
        print("USDC:", bal.usd)

        res = await pt.bet_on("btc-updown-15m-1735689600", "UP", amount_usd=5)
        print(res.ok, res.filled_shares, res.avg_price)

asyncio.run(main())
```

Always use `async with` (or call `await pt.close()`) so the pooled HTTP client
is cleaned up. If you inject your own `http_client`, the package will not close
it.

## API reference

All methods are `async`.

### bet_on(market, outcome, amount_usd, \*, order_type="FAK") -> OrderResult
Primary entry point. Resolves `outcome` to a token id on `market`, then places a
market BUY spending `amount_usd` USDC.
- `market`: a `Market` object, or a slug / condition id string (looked up if str).
- `outcome`: `"UP"`/`"DOWN"`, `"YES"`/`"NO"`, an outcome label, an index (0/1), or
  an explicit token id.
- `amount_usd`: number or Decimal. Must be >= `min_marketable_buy_usd` (default 1).

```python
res = await pt.bet_on("btc-updown-15m-1735689600", "DOWN", 10)
if res.ok:
    print(res.filled_shares, "shares @", res.avg_price)
else:
    print("rejected:", res.error_msg)
```

### place_market_order(\*, token_id, side, amount_usd, order_type="FAK", client_order_id=None) -> OrderResult
Low-level market order. `side` is `"BUY"` or `"SELL"`. For BUY, `amount_usd` is
USDC to spend.

### place_limit_order(\*, token_id, side, price, size, order_type="GTC", client_order_id=None) -> OrderResult
Low-level limit order. For a marketable BUY, `size` is snapped so `price x size`
lands on a clean 2-dp maker (the CLOB rejects a finer maker). If no clean size
fits the budget the order is rejected, not submitted.

### cancel_order(order_id) -> OrderResult
Cancels a resting order.

### verify_balance() -> Balance
USDC collateral balance. Returns `Balance(ok=False, usd=None, ...)` on failure,
never raises, and never crashes on a null or non-finite payload.

### verify_conn_health() -> HealthStatus
Checks the bridge (`/healthz`) and a live CLOB reachability probe. Returns
`HealthStatus(ok, bridge_ok, clob_ok, latency_ms, detail)`. Never raises.

### get_market(slug_or_condition) -> Market | None
Looks a market up by slug or condition id.

### get_order_book(token_id) -> OrderBook | None
Live book. Asks sorted cheapest-first, bids highest-first.

### get_price(token_id, side="BUY") -> Decimal | None
Best ask for BUY, best bid for SELL.

### deposit(amount_usd=None) -> DepositInfo
Returns the deposit address, chain, USDC contract, and instructions. Deposits
are external: you send USDC to the wallet, then poll `verify_balance()`. This
cannot pull funds.

### withdraw(amount_usd, to_address) -> WithdrawResult
Signs an on-chain USDC transfer via the bridge. Guarded: needs
`config.allow_withdraw=True` and the bridge's `BRIDGE_ALLOW_WITHDRAW=true`.
Validates the address, checks the balance, and fails closed if the balance is
unreadable. Test on testnet before production.

### close() / async context manager
Closes the pooled HTTP client (only if the package owns it).

## Data models

- `OrderResult`: `ok, status, order_id, filled_shares, avg_price, spent_usd, client_order_id, error_msg, call_ms, raw`
- `Balance`: `ok, usd, raw, allowances, error_msg`
- `HealthStatus`: `ok, bridge_ok, clob_ok, latency_ms, detail`
- `Market`: `slug, question, condition_id, outcomes, end_date, last_trade_price, best_bid, best_ask, volume, liquidity, raw`
- `Outcome`: `label, token_id, price, index`
- `OrderBook`: `token_id, asks, bids, tick_size`
- `BookLevel`: `price, size`
- `DepositInfo`: `deposit_address, chain, usdc_contract, amount_usd, instructions`
- `WithdrawResult`: `ok, tx_hash, error_msg, raw`

## Critical behaviors (do not skip)

1. **Orders never auto-retry.** The bridge does not dedup on `client_order_id`,
   so a retry after a lost response could double-fill. `bet_on` /
   `place_market_order` / `place_limit_order` make exactly one attempt. On a
   transport failure or 5xx you get `OrderResult(ok=False, ...)`. Reconcile with
   `verify_balance()` or position checks before resubmitting. Reads (balance,
   book, health, market) do retry, with bounded backoff.
2. **$1 marketable-BUY floor.** Polymarket rejects sub-$1 BUYs. The client
   rejects them first with a clear error. Set `min_marketable_buy_usd=0` to
   defer to the venue.
3. **Market-BUY slippage.** Market BUYs take top-of-book with no price cap. Fine
   under ~$50, risky above it. For larger size use
   `place_limit_order(order_type="FAK")` at the inside ask.
4. **Secrets belong to the bridge only.** Never put a private key or CLOB creds
   in Python config or code.
5. **Health-check before trading.** Call `verify_conn_health()` at startup.
6. **Withdraw is real money.** Off by default, guarded, testnet-first.

## Recipes

### Safe bet with reconciliation
```python
res = await pt.bet_on(market, "UP", 5)
if not res.ok and res.error_msg and "transport" in res.error_msg:
    # ambiguous: the order may or may not have landed. Do NOT blindly retry.
    bal = await pt.verify_balance()
    # compare against the pre-bet balance / positions before any resubmit
```

### Price-checked limit entry for larger size
```python
ask = await pt.get_price(token_id, "BUY")
res = await pt.place_limit_order(token_id=token_id, side="BUY",
                                 price=ask, size=200, order_type="FAK")
```

### Balance-gated sizing
```python
bal = await pt.verify_balance()
if bal.ok and bal.usd and bal.usd >= 5:
    await pt.bet_on(market, "DOWN", 5)
```

## Error handling

- Programmer errors (bad side, non-numeric amount, non-positive price) raise
  `ValidationError`.
- Venue rejections and transport failures return structured results
  (`ok=False`), they do not raise.
- Config errors raise `ConfigError`.

## Testing

```bash
pytest                       # unit + integration
pytest -m "not integration"  # unit only (offline, mocked bridge)
pytest -m integration        # real loopback HTTP server
```

## Contributing

Issues and pull requests are welcome. Run the tests before opening a PR. The
package is MIT licensed (see `LICENSE`); use it freely.
