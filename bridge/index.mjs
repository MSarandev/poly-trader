/**
 * polytrader CLOB bridge — Node.js sidecar that wraps
 * @polymarket/clob-client-v2 with POLY_1271 (deposit-wallet) signing.
 *
 * Python (and every Python CLOB SDK) can't construct ERC-1271-style L1 auth
 * signatures, which Polymarket requires for any API key bound to a deposit
 * wallet. The TS SDK does this correctly, so we run the order-placement +
 * treasury leg here and keep everything else in the Python `polytrader`
 * package.
 *
 * Endpoints:
 *   GET  /healthz            liveness check
 *   GET  /balance            trading wallet's collateral balance (USDC, 6 dp)
 *   GET  /recent-events      ring buffer of recent bridge activity
 *   POST /place-market-order market order (BUY spends USDC amount)
 *   POST /place-order        limit order
 *   POST /cancel             cancel a resting order  (NEW)
 *   POST /withdraw           on-chain USDC transfer   (NEW, guarded)
 *
 * Config is env-only; NO secrets are committed. See .env.example.
 * Creds storage: api-creds.json under /app/creds (volume-mounted, gitignored).
 * First boot: createOrDeriveApiKey, persist. Subsequent boots: load from disk.
 * If saved creds 401, re-derive.
 */

import { createWalletClient, http, createPublicClient, encodeFunctionData,
         getAddress, parseUnits, isAddress } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import express from "express";
import fs from "node:fs/promises";
import path from "node:path";

import {
  Chain,
  ClobClient,
  OrderType,
  Side,
  SignatureTypeV2,
} from "@polymarket/clob-client-v2";

const PORT = parseInt(process.env.BRIDGE_PORT || "3000", 10);
const HOST = process.env.CLOB_API_URL || "https://clob.polymarket.com";
const PRIVATE_KEY = process.env.POLYMARKET_PRIVATE_KEY;
const FUNDER = process.env.POLYMARKET_FUNDER_ADDRESS;
const CREDS_PATH = process.env.CREDS_PATH || "/app/creds/api-creds.json";
const RECENT_EVENTS_MAX = 50;

// --- withdraw config (only used by POST /withdraw) ---
// Withdrawals move real funds; keep them off by default.
const ALLOW_WITHDRAW = /^(1|true|yes|on)$/i.test(process.env.BRIDGE_ALLOW_WITHDRAW || "");
const RPC_URL = process.env.POLYGON_RPC_URL || "https://polygon-bor-rpc.publicnode.com";
// USDC (PoS) on Polygon mainnet — public contract address.
const USDC_ADDRESS = process.env.USDC_ADDRESS || "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const USDC_DECIMALS = parseInt(process.env.USDC_DECIMALS || "6", 10);
const ERC20_TRANSFER_ABI = [{
  name: "transfer",
  type: "function",
  stateMutability: "nonpayable",
  inputs: [
    { name: "to", type: "address" },
    { name: "amount", type: "uint256" },
  ],
  outputs: [{ name: "", type: "bool" }],
}];

// In-memory ring buffer of recent bridge events, surfaced by /recent-events.
const recentEvents = [];
function recordEvent(event) {
  recentEvents.push({ ts: Date.now(), ...event });
  while (recentEvents.length > RECENT_EVENTS_MAX) recentEvents.shift();
}

if (!PRIVATE_KEY) throw new Error("POLYMARKET_PRIVATE_KEY env var is required");
if (!FUNDER) throw new Error("POLYMARKET_FUNDER_ADDRESS env var is required");

// ---------------------------------------------------------------------------
// Client construction
// ---------------------------------------------------------------------------

const account = privateKeyToAccount(
  PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : `0x${PRIVATE_KEY}`,
);
const walletClient = createWalletClient({
  account,
  chain: polygon,
  transport: http(RPC_URL),
});
const EOA = account.address;

console.log(`[bridge] EOA       : ${EOA}`);
console.log(`[bridge] funder    : ${FUNDER}`);
console.log(`[bridge] host      : ${HOST}`);
console.log(`[bridge] creds at  : ${CREDS_PATH}`);
console.log(`[bridge] withdraw  : ${ALLOW_WITHDRAW ? "ENABLED" : "disabled"}`);

async function loadCreds() {
  try {
    const buf = await fs.readFile(CREDS_PATH, "utf-8");
    const creds = JSON.parse(buf);
    if (!creds.key || !creds.secret || !creds.passphrase) {
      throw new Error("malformed");
    }
    console.log(`[bridge] creds loaded from disk (api_key=${creds.key.slice(0, 8)}...)`);
    return creds;
  } catch (e) {
    if (e.code === "ENOENT" || e.message === "malformed") return null;
    throw e;
  }
}

async function saveCreds(creds) {
  await fs.mkdir(path.dirname(CREDS_PATH), { recursive: true });
  await fs.writeFile(CREDS_PATH, JSON.stringify(creds, null, 2), { mode: 0o600 });
  console.log(`[bridge] creds persisted to ${CREDS_PATH}`);
}

// L1-only client used to create or derive the API key. POLY_1271 + funder makes
// the TS SDK put the funder in POLY_ADDRESS and sign via ERC-1271.
async function ensureCreds() {
  let creds = await loadCreds();
  if (creds) return creds;
  console.log(`[bridge] no saved creds; calling createOrDeriveApiKey...`);
  const l1Client = new ClobClient({
    host: HOST,
    chain: Chain.POLYGON,
    signer: walletClient,
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: FUNDER,
  });
  const raw = await l1Client.createOrDeriveApiKey();
  creds = { key: raw.key, secret: raw.secret, passphrase: raw.passphrase };
  await saveCreds(creds);
  return creds;
}

let creds = await ensureCreds();
let client = new ClobClient({
  host: HOST,
  chain: Chain.POLYGON,
  signer: walletClient,
  creds,
  signatureType: SignatureTypeV2.POLY_1271,
  funderAddress: FUNDER,
});

// Re-derive the API key and rebuild the authed client. createOrDeriveApiKey
// returns the *existing* registered key when there is one, so this is safe to
// call on a 401 — it recovers a rotated/invalidated key without minting a
// colliding nonce-0 key.
async function refreshCreds() {
  const l1Client = new ClobClient({
    host: HOST,
    chain: Chain.POLYGON,
    signer: walletClient,
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: FUNDER,
  });
  const raw = await l1Client.createOrDeriveApiKey();
  creds = { key: raw.key, secret: raw.secret, passphrase: raw.passphrase };
  await saveCreds(creds);
  client = new ClobClient({
    host: HOST,
    chain: Chain.POLYGON,
    signer: walletClient,
    creds,
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: FUNDER,
  });
  console.log(`[bridge] creds refreshed after 401 (api_key=${creds.key.slice(0, 8)}...)`);
  recordEvent({
    type: "creds-refresh",
    level: "warn",
    message: `CLOB creds refreshed after 401 (api_key=${creds.key.slice(0, 8)}…)`,
  });
}

// Place an order with auth resilience. A lone 401 amid a run of successes is
// usually a transient signing/timestamp blip, so we first retry with the same
// creds; only if that still 401s do we re-derive the key and retry once more.
const AUTH_STATUS = 401;
async function submitWithAuthRetry(submitFn, coi) {
  let lastResp;
  for (let attempt = 0; attempt < 3; attempt++) {
    let resp;
    try {
      resp = await submitFn();
    } catch (e) {
      const status = e?.response?.status ?? null;
      if (status !== AUTH_STATUS) throw e; // non-auth error: let caller handle
      resp = { status: AUTH_STATUS, errorMsg: e?.response?.data?.error || e?.message };
    }
    lastResp = resp;
    const is401 = typeof resp?.status === "number" && resp.status === AUTH_STATUS;
    if (!is401) return resp;

    if (attempt === 0) {
      console.warn(`[bridge] 401 on ${coi || "order"}; retrying with same creds`);
    } else if (attempt === 1) {
      console.warn(`[bridge] 401 persists on ${coi || "order"}; refreshing creds and retrying`);
      try {
        await refreshCreds();
      } catch (e) {
        console.error(`[bridge] creds refresh failed: ${e?.message || e}`);
        return resp; // surface the original 401
      }
    }
  }
  return lastResp; // still 401 after refresh + retry
}

// Quick smoke check — fail loudly if auth is broken so "starting"→"healthy"
// reflects real state.
try {
  const ba = await client.getBalanceAllowance({ asset_type: "COLLATERAL" });
  const balanceUsd = Number(ba.balance) / 1e6;
  console.log(`[bridge] startup balance: $${balanceUsd.toFixed(2)}`);
  recordEvent({
    type: "startup",
    level: "info",
    message: `bridge ready · funder=${FUNDER.slice(0, 10)}… · balance=$${balanceUsd.toFixed(2)}`,
  });
} catch (e) {
  console.error(`[bridge] startup balance check FAILED: ${e?.message || e}`);
  recordEvent({
    type: "startup",
    level: "error",
    message: `startup balance check failed: ${e?.message || e}`,
  });
  // Don't exit — the order endpoint returns the error to the caller, and
  // /healthz stays up so ops can read the logs.
}

// ---------------------------------------------------------------------------
// HTTP API
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json({ limit: "16kb" }));

app.get("/healthz", (_, res) => {
  res.json({ ok: true, eoa: EOA, funder: FUNDER, allow_withdraw: ALLOW_WITHDRAW });
});

app.get("/recent-events", (_, res) => {
  res.json({ events: [...recentEvents].reverse() });
});

app.get("/balance", async (_, res) => {
  try {
    const ba = await client.getBalanceAllowance({ asset_type: "COLLATERAL" });
    const balanceUsd = Number(ba.balance) / 1e6;
    if (!Number.isFinite(balanceUsd)) {
      // SDK returned an object without a usable `balance` — surface as an error
      // (not a null balance_usd, which the Python side can't parse).
      res.status(502).json({ error: `bad balance payload: ${JSON.stringify(ba?.balance)}` });
      return;
    }
    res.json({
      balance_raw: ba.balance,
      balance_usd: balanceUsd,
      allowances: ba.allowances,
    });
  } catch (e) {
    res.status(502).json({ error: e?.message || String(e) });
  }
});

// Side mapping: Python sends "BUY"/"SELL"; TS SDK expects Side.BUY / Side.SELL
function parseSide(s) {
  if (s === "BUY") return Side.BUY;
  if (s === "SELL") return Side.SELL;
  throw new Error(`invalid side: ${s}`);
}

function parseOrderType(t) {
  // GTC = good-til-cancel, GTD = good-til-date, FOK = fill-or-kill, FAK = IOC
  const map = {
    GTC: OrderType.GTC,
    GTD: OrderType.GTD,
    FOK: OrderType.FOK,
    FAK: OrderType.FAK,
  };
  const ot = map[t];
  if (!ot) throw new Error(`invalid order_type: ${t}`);
  return ot;
}

// Market BUY — pass amount in USDC (2-decimal precision native), let Polymarket
// figure out shares. Limit orders for marketable buys hit "invalid amounts"
// errors at non-trivial sizes because makerAmount = price × shares can exceed
// 2-dp precision; market orders avoid that.
app.post("/place-market-order", async (req, res) => {
  const { token_id, side, amount_usd, order_type, tick_size, client_order_id } =
    req.body || {};

  if (!token_id || side == null || amount_usd == null || !order_type) {
    res.status(400).json({ error: "missing required field(s)" });
    return;
  }

  recordEvent({
    type: "place-market-order",
    level: "info",
    side,
    amount_usd: Number(amount_usd),
    order_type,
    token_short: String(token_id).slice(0, 12) + "…",
    client_order_id: client_order_id || null,
    message: `submit MARKET ${order_type} ${side} $${Number(amount_usd).toFixed(2)}`,
  });

  try {
    const args = {
      tokenID: String(token_id),
      side: parseSide(side),
      amount: Number(amount_usd),
      orderType: parseOrderType(order_type),
    };
    const options = { tickSize: String(tick_size || "0.01") };
    const t0 = Date.now();
    const resp = await submitWithAuthRetry(
      () => client.createAndPostMarketOrder(args, options, parseOrderType(order_type)),
      client_order_id,
    );
    const t1 = Date.now();

    const errorMsg = resp?.errorMsg || resp?.error || null;
    const httpErr = (typeof resp?.status === "number" && resp.status >= 400)
      ? resp.status : null;
    if (httpErr) {
      const emsg = errorMsg || `CLOB returned ${httpErr}`;
      console.error(
        `[bridge] market-order rejected (${httpErr}): ${emsg}` +
          (client_order_id ? ` (coi=${client_order_id})` : ""),
      );
      recordEvent({
        type: "market-order-result",
        level: "warn",
        status: `http_${httpErr}`,
        error: emsg,
        client_order_id: client_order_id || null,
        message: `rejected (${httpErr}): ${emsg}`,
      });
      res.status(httpErr >= 400 && httpErr < 600 ? httpErr : 502).json({
        client_order_id,
        error: emsg,
      });
      return;
    }

    const status = resp?.status || (resp?.success === false ? "rejected" : "ok");
    const orderId = resp?.orderID || resp?.orderId || resp?.id || null;
    const makingAmount = resp?.makingAmount || resp?.making_amount || null;
    const takingAmount = resp?.takingAmount || resp?.taking_amount || null;

    console.log(
      `[bridge] market-order ${order_type} ${side} $${Number(amount_usd).toFixed(2)} -> ` +
        `${status} in ${t1 - t0}ms` +
        (client_order_id ? ` (coi=${client_order_id})` : ""),
    );
    recordEvent({
      type: "market-order-result",
      level: errorMsg || status === "rejected" ? "warn" : "info",
      status,
      order_id: orderId ? `${orderId.slice(0, 12)}…` : null,
      // makingAmount / takingAmount come back as decimal strings — do not /1e6.
      filled_usd: makingAmount ? Number(makingAmount) : null,
      latency_ms: t1 - t0,
      error: errorMsg || null,
      client_order_id: client_order_id || null,
      message:
        errorMsg
          ? `${status}: ${errorMsg}`
          : `${status}` + (makingAmount ? ` · $${Number(makingAmount).toFixed(4)} spent` : "")
                       + (takingAmount ? ` for ${Number(takingAmount).toFixed(2)} shares` : ""),
    });
    res.json({ client_order_id, latency_ms: t1 - t0, response: resp });
  } catch (e) {
    const msg = e?.response?.data?.error || e?.message || String(e);
    const status = e?.response?.status || 502;
    console.error(`[bridge] market-order error (${status}): ${msg}`);
    recordEvent({
      type: "market-order-result",
      level: "error",
      status: `http_${status}`,
      error: msg,
      client_order_id: client_order_id || null,
      message: `error (${status}): ${msg}`,
    });
    res.status(status >= 400 && status < 600 ? status : 502).json({
      client_order_id,
      error: msg,
    });
  }
});

app.post("/place-order", async (req, res) => {
  const { token_id, side, price, size, order_type, tick_size, client_order_id } =
    req.body || {};

  if (!token_id || side == null || price == null || size == null || !order_type) {
    res.status(400).json({ error: "missing required field(s)" });
    return;
  }

  recordEvent({
    type: "place-order",
    level: "info",
    side,
    price: Number(price),
    size: Number(size),
    order_type,
    token_short: String(token_id).slice(0, 12) + "…",
    client_order_id: client_order_id || null,
    message: `submit ${order_type} ${side} ${size}@${price}`,
  });

  try {
    const orderArgs = {
      tokenID: String(token_id),
      side: parseSide(side),
      price: Number(price),
      size: Number(size),
    };
    const options = { tickSize: String(tick_size || "0.01") };
    const t0 = Date.now();
    const resp = await submitWithAuthRetry(
      () => client.createAndPostOrder(orderArgs, options, parseOrderType(order_type)),
      client_order_id,
    );
    const t1 = Date.now();

    const errorMsg = resp?.errorMsg || resp?.error || null;
    const httpErr = (typeof resp?.status === "number" && resp.status >= 400)
      ? resp.status : null;
    if (httpErr) {
      const emsg = errorMsg || `CLOB returned ${httpErr}`;
      console.error(
        `[bridge] place-order rejected (${httpErr}): ${emsg}` +
          (client_order_id ? ` (coi=${client_order_id})` : ""),
      );
      recordEvent({
        type: "place-order-result",
        level: "warn",
        status: `http_${httpErr}`,
        error: emsg,
        client_order_id: client_order_id || null,
        message: `rejected (${httpErr}): ${emsg}`,
      });
      res.status(httpErr >= 400 && httpErr < 600 ? httpErr : 502).json({
        client_order_id,
        error: emsg,
      });
      return;
    }

    const status = resp?.status || (resp?.success === false ? "rejected" : "ok");
    const orderId = resp?.orderID || resp?.orderId || resp?.id || null;
    const makingAmount = resp?.makingAmount || resp?.making_amount || null;

    console.log(
      `[bridge] place-order ${order_type} ${side} ${size}@${price} -> ` +
        `${status} in ${t1 - t0}ms` +
        (client_order_id ? ` (coi=${client_order_id})` : ""),
    );
    recordEvent({
      type: "place-order-result",
      level: errorMsg || status === "rejected" ? "warn" : "info",
      status,
      order_id: orderId ? `${orderId.slice(0, 12)}…` : null,
      filled_usd: makingAmount ? Number(makingAmount) : null,
      latency_ms: t1 - t0,
      error: errorMsg || null,
      client_order_id: client_order_id || null,
      message: errorMsg ? `${status}: ${errorMsg}` : `${status}` +
        (makingAmount ? ` · $${Number(makingAmount).toFixed(4)} filled` : ""),
    });
    res.json({
      client_order_id,
      latency_ms: t1 - t0,
      response: resp,
    });
  } catch (e) {
    const msg = e?.response?.data?.error || e?.message || String(e);
    const status = e?.response?.status || 502;
    console.error(`[bridge] place-order error (${status}): ${msg}`);
    recordEvent({
      type: "place-order-result",
      level: "error",
      status: `http_${status}`,
      error: msg,
      client_order_id: client_order_id || null,
      message: `error (${status}): ${msg}`,
    });
    res.status(status >= 400 && status < 600 ? status : 502).json({
      client_order_id,
      error: msg,
    });
  }
});

// Cancel a resting order. Body: { order_id }.
app.post("/cancel", async (req, res) => {
  const { order_id } = req.body || {};
  if (!order_id) {
    res.status(400).json({ error: "missing required field: order_id" });
    return;
  }
  recordEvent({
    type: "cancel",
    level: "info",
    order_id: String(order_id).slice(0, 12) + "…",
    message: `cancel ${String(order_id).slice(0, 12)}…`,
  });
  try {
    const t0 = Date.now();
    // The v2 client exposes cancelOrder({ orderID }); fall back to cancel(id)
    // for SDK builds that expose the older shape.
    const resp = await submitWithAuthRetry(
      () => (typeof client.cancelOrder === "function"
        ? client.cancelOrder({ orderID: String(order_id) })
        : client.cancel(String(order_id))),
      order_id,
    );
    const t1 = Date.now();

    const errorMsg = resp?.errorMsg || resp?.error || null;
    const httpErr = (typeof resp?.status === "number" && resp.status >= 400)
      ? resp.status : null;
    if (httpErr || errorMsg) {
      const emsg = errorMsg || `CLOB returned ${httpErr}`;
      recordEvent({
        type: "cancel-result", level: "warn",
        status: httpErr ? `http_${httpErr}` : "rejected",
        error: emsg, message: `cancel rejected: ${emsg}`,
      });
      res.status(httpErr && httpErr >= 400 && httpErr < 600 ? httpErr : 502)
         .json({ order_id, error: emsg });
      return;
    }
    recordEvent({
      type: "cancel-result", level: "info",
      latency_ms: t1 - t0, message: `cancelled ${String(order_id).slice(0, 12)}…`,
    });
    res.json({ order_id, latency_ms: t1 - t0, response: resp });
  } catch (e) {
    const msg = e?.response?.data?.error || e?.message || String(e);
    const status = e?.response?.status || 502;
    console.error(`[bridge] cancel error (${status}): ${msg}`);
    recordEvent({
      type: "cancel-result", level: "error",
      status: `http_${status}`, error: msg, message: `cancel error (${status}): ${msg}`,
    });
    res.status(status >= 400 && status < 600 ? status : 502).json({ order_id, error: msg });
  }
});

// On-chain USDC transfer from the trading wallet (EOA) to `to`. Guarded by
// BRIDGE_ALLOW_WITHDRAW. Moves REAL funds — verify on testnet before production.
// Body: { to, amount_usd }.
const publicClient = createPublicClient({ chain: polygon, transport: http(RPC_URL) });

app.post("/withdraw", async (req, res) => {
  if (!ALLOW_WITHDRAW) {
    res.status(403).json({ error: "withdrawals disabled: set BRIDGE_ALLOW_WITHDRAW=true" });
    return;
  }
  const { to, amount_usd } = req.body || {};
  if (!to || amount_usd == null) {
    res.status(400).json({ error: "missing required field(s): to, amount_usd" });
    return;
  }
  if (!isAddress(to)) {
    res.status(400).json({ error: `invalid to address: ${to}` });
    return;
  }
  const amtNum = Number(amount_usd);
  if (!Number.isFinite(amtNum) || amtNum <= 0) {
    res.status(400).json({ error: `invalid amount_usd: ${amount_usd}` });
    return;
  }

  recordEvent({
    type: "withdraw", level: "warn",
    to: getAddress(to).slice(0, 10) + "…",
    amount_usd: amtNum,
    message: `withdraw $${amtNum.toFixed(2)} -> ${getAddress(to).slice(0, 10)}…`,
  });

  try {
    // parseUnits keeps full 6-dp precision without float error.
    const amount = parseUnits(String(amount_usd), USDC_DECIMALS);
    const data = encodeFunctionData({
      abi: ERC20_TRANSFER_ABI,
      functionName: "transfer",
      args: [getAddress(to), amount],
    });
    const t0 = Date.now();
    const txHash = await walletClient.sendTransaction({
      to: getAddress(USDC_ADDRESS),
      data,
      value: 0n,
    });
    const t1 = Date.now();
    console.log(`[bridge] withdraw submitted tx=${txHash} in ${t1 - t0}ms`);
    recordEvent({
      type: "withdraw-result", level: "info",
      tx_hash: txHash, latency_ms: t1 - t0,
      message: `withdraw tx ${txHash.slice(0, 12)}…`,
    });
    res.json({ tx_hash: txHash, latency_ms: t1 - t0 });
  } catch (e) {
    const msg = e?.shortMessage || e?.message || String(e);
    console.error(`[bridge] withdraw error: ${msg}`);
    recordEvent({
      type: "withdraw-result", level: "error",
      error: msg, message: `withdraw error: ${msg}`,
    });
    res.status(502).json({ error: msg });
  }
});

app.listen(PORT, () => {
  console.log(`[bridge] listening on :${PORT}`);
});
