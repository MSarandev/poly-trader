# polytrader

A standalone, async Python client for **Polymarket connectivity** — market data,
order placement, balance, treasury, and health checks — with **zero strategy**.
No prediction model, no database, no gates. Just the venue plumbing, so a new
service can `pip install polytrader` and trade.

Order placement goes through a small **Node CLOB bridge** (shipped in `bridge/`).
Python — and every Python CLOB SDK — can't construct the ERC-1271 / POLY_1271 L1
auth signatures Polymarket requires for deposit-wallet API keys. The TS SDK can,
so the bridge owns that leg and everything else stays in Python.

```
your service ──> polytrader (Python) ──HTTP──> bridge (Node) ──> Polymarket CLOB
                     │
                     └──HTTP──> Polymarket Gamma API (market data)
                     └──HTTP──> Polymarket CLOB /book (order book)
```

## Install

```bash
pip install -e .
# optional extras:
pip install -e ".[http2]"     # HTTP/2 keep-alive (falls back to HTTP/1.1)
pip install -e ".[withdraw]"  # web3/eth-account for optional local chain tooling
pip install -e ".[test]"      # pytest + pytest-asyncio
```

The **core** client needs only `httpx`. `web3`/`eth-account` are an optional
extra and are lazy-imported — `import polytrader` works without them. The actual
on-chain USDC transfer for withdrawals happens inside the Node bridge, not in
Python.

## Quickstart

```python
import asyncio
from decimal import Decimal
from polytrader import PolyTrader, PolyTraderConfig

async def main():
    config = PolyTraderConfig(bridge_url="http://localhost:3000")
    async with PolyTrader(config) as pt:
        health = await pt.verify_conn_health()          # bridge + CLOB reachable?
        balance = await pt.verify_balance()             # USDC collateral
        market = await pt.get_market("btc-updown-15m-1735689600")
        book = await pt.get_order_book(market.outcomes[0].token_id)
        result = await pt.bet_on(market, "UP", Decimal("5"))   # $5 market BUY
        print(result.ok, result.status, result.avg_price, result.error_msg)

asyncio.run(main())
```

Config can also come from the environment:
`PolyTraderConfig.from_env(prefix="POLYTRADER_")` (see `.env.example`).

## Running the bridge (Docker)

The bridge is the only component that ever sees your private key. Configure it
via env (never commit real values — copy `.env.example` to `.env`):

```bash
cd bridge
docker build -t polytrader-bridge .
docker run --rm -p 3000:3000 \
  -e POLYMARKET_PRIVATE_KEY=0x...      \
  -e POLYMARKET_FUNDER_ADDRESS=0x...   \
  -v "$PWD/creds:/app/creds"           \
  polytrader-bridge
```

On first boot the bridge derives its CLOB API creds and persists them to
`/app/creds/api-creds.json` (a gitignored volume). Subsequent boots reload them;
a persistent 401 triggers an automatic re-derive. Point the Python client at it
with `bridge_url="http://localhost:3000"`.

### Bridge endpoints

| Method | Path                  | Purpose                                   |
|--------|-----------------------|-------------------------------------------|
| GET    | `/healthz`            | liveness + which wallet is loaded         |
| GET    | `/balance`           | USDC collateral (guards a null payload)   |
| GET    | `/recent-events`      | ring buffer of recent bridge activity     |
| POST   | `/place-market-order` | market order (BUY spends a USDC amount)   |
| POST   | `/place-order`        | limit order                               |
| POST   | `/cancel`             | cancel a resting order                    |
| POST   | `/withdraw`           | on-chain USDC transfer (guarded)          |

## Public API

`PolyTrader` (all methods `async`):

- `verify_conn_health() -> HealthStatus` — bridge `/healthz` + a live CLOB probe.
- `verify_balance() -> Balance` — USDC collateral; `ok=False` on a null/bad
  payload (never crashes).
- `bet_on(market, outcome, amount_usd) -> OrderResult` — resolve an outcome
  (`"UP"/"DOWN"/"YES"/"NO"`, a label, an index, or a token id) and place a
  market BUY.
- `place_market_order(*, token_id, side, amount_usd, ...) -> OrderResult`
- `place_limit_order(*, token_id, side, price, size, ...) -> OrderResult` —
  snaps BUY size to a clean 2-dp maker so the CLOB doesn't reject it.
- `cancel_order(order_id) -> OrderResult`
- `get_market(slug_or_condition) -> Market | None`
- `get_order_book(token_id) -> OrderBook | None`
- `get_price(token_id, side="BUY") -> Decimal | None` — best ask / best bid.
- `deposit(amount_usd=None) -> DepositInfo` — see caveats below.
- `withdraw(amount_usd, to_address) -> WithdrawResult` — see caveats below.
- `close()` / `async with` — release the pooled HTTP client.

Order and treasury calls **never raise on a venue rejection** — they return a
structured result with `ok` / `error_msg`. Only bad arguments (`ValidationError`)
or unrecoverable config (`ConfigError`) raise. Transport errors and 5xx get
bounded retries with backoff first.

## Market-BUY slippage (read before sizing up)

`bet_on` and `place_market_order(side="BUY")` route to the bridge's market-order
endpoint, which takes a USDC **amount** and **takes whatever's at the top of
book** — there is no max-price parameter.

- **Small sizes (< ~$50): fine.** A $5 order eats only the first few resting
  asks; slippage is a fraction of a cent.
- **Large sizes (> ~$50): risky.** A single market BUY starts crossing the whole
  inside level and biting into deeper, worse levels. If the top is `$0.50 × $5`
  and the next is `$0.65 × $50`, a $50 market BUY pays mostly `$0.65` — well worse
  than the inside quote. That can be 5–10% slippage.

For larger sizes prefer a **limit FAK** at the inside ask via
`place_limit_order(..., order_type="FAK")` — it caps the fill at your snapshot
price. `place_limit_order` already snaps the BUY size so `price × size` lands on
a clean 2-dp maker (otherwise the CLOB rejects a marketable limit BUY once its
SDK rounds the size down to 2 dp). You can also split a large order into chunks,
re-reading the book between each.

## Reliability & order idempotency (read before you wire in recovery logic)

Idempotent **reads** (`verify_balance`, `verify_conn_health`, `get_order_book`,
`get_price`, `get_market`) are retried on transport errors and 5xx/429 with
bounded exponential backoff (`max_retries` / `retry_backoff_s`).

**Order placement is never auto-retried.** The bridge/CLOB does not dedup on
`client_order_id`, so retrying after a lost response — e.g. a `ReadTimeout` where
the order was actually accepted — would place a **second fill** and double your
exposure. So `bet_on` / `place_market_order` / `place_limit_order` make exactly
one attempt; on a transport failure or 5xx they return a structured
`OrderResult(ok=False, error_msg="transport: …")`. On such an ambiguous result,
**reconcile before resubmitting** — call `verify_balance()` / check positions to
learn whether the order landed, rather than blindly retrying. If you want at-most-
once retries, add `client_order_id` de-duplication in the bridge first.

## deposit / withdraw — honest scope

**`deposit(amount_usd=None)` cannot pull funds.** Polymarket deposits are you
sending USDC on-chain to the deposit wallet (or using the Polymarket UI on-ramp).
`deposit()` returns the deposit address, chain, USDC contract, and instructions
— the workflow is *external-send, then poll `verify_balance()`* until the
collateral reflects the deposit. It does not move money.

**`withdraw(amount_usd, to_address)` is implemented but move-real-funds.** It
signs a USDC ERC-20 `transfer` from the trading wallet via the bridge's
`/withdraw` endpoint. It is **hard-guarded**:

- disabled unless `config.allow_withdraw=True` (Python) **and**
  `BRIDGE_ALLOW_WITHDRAW=true` (bridge env);
- validates the destination address;
- refuses if `amount <= 0` or `amount > balance` (fails closed if balance can't
  be read).

It moves real money, so treat it as **implemented-but-verify-on-testnet-first**.
The test suite covers it only with a mocked bridge (no live-chain calls).

## Tests

```bash
pip install -e ".[test]"
pytest
```

The suite is fully offline — the bridge, Gamma, and CLOB endpoints are served by
an `httpx.MockTransport` (see `tests/conftest.py`). No network, no real bridge,
no creds.

## Configuration reference

See `.env.example`. Key `PolyTraderConfig` fields: `bridge_url`, `gamma_api_url`,
`clob_api_url`, `request_timeout_s`, `max_retries`, `retry_backoff_s`,
`default_order_type`, `default_tick_size`, `allow_withdraw`, and the withdraw
chain settings (`rpc_url`, `usdc_address`, `chain_id`). **No private key or API
secret lives in the Python config** — those belong to the bridge env only.
